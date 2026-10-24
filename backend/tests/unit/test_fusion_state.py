"""F4.1: evidence -> words. No network."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from repairos.fusion import words
from repairos.fusion.questions import build_questions
from repairos.fusion.state import build_state, scrub
from repairos.slots.fusion import Question, S5Violation, assert_words_only, numeric_tokens

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "spec" / "contracts" / "examples"
SCHEMA = json.loads((ROOT / "spec" / "contracts" / "finding.schema.json").read_text())


def example(name):
    return json.loads((EXAMPLES / name).read_text())


def finding(fid, kind, ref, state, *, source="repairos.vision", pins=None, evidence=(), meas=None, path=None):
    d = {"finding_id": fid, "scan_id": "scan_t", "subject": {"kind": kind, "ref": ref}, "state": state,
         "source": source, "confidence": 0.8, "is_proof": source == "repairos.meter", "evidence": list(evidence),
         "created_at": "2026-10-24T10:00:00Z"}
    if pins:
        d["subject"]["pins"] = pins
    if meas:
        d["measurements"] = meas
    if path is not None:
        d["on_symptom_power_path"] = path
    return d


def all_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from all_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from all_strings(v)


def test_contract_examples_become_words_without_numbers():
    fs = build_state("scan_7f3a", [example("finding.json"), example("finding.partly.json")], [example("meter-reading.json")],
                     symptom="Stopped working after 2 days on the 5V adapter")
    assert_words_only(fs.state)
    text = json.dumps(fs.state)
    for leaked in ("2.1", "0.93", "0.71", "603", "0.72", "0.8", "5V", "2 days"):
        assert leaked not in text
    assert "J1.1" in text and "HOUSING1:lid" in text           # refs with digits survive
    j1 = fs.state["subjects"]["part J1"]
    assert j1["findings"][0]["connector_gap"] == "a clear gap"
    assert j1["findings"][0]["photo_check"] == words.VLM_ANSWER["gap_visible"]
    assert fs.state["subjects"]["net NET_A"]["meter"] == words.METER["disagrees"]   # reading-only subject
    assert fs.design_trust == "low"


def test_every_finding_enum_maps_to_words():
    props = SCHEMA["properties"]
    assert set(props["state"]["enum"]) == set(words.FINDING_STATE)
    assert set(props["source"]["enum"]) == set(words.FINDING_SOURCE)
    assert set(props["vlm_answer"]["enum"]) == set(words.VLM_ANSWER)
    for st in props["state"]["enum"]:
        fs = build_state("s", [finding("f1", "part", "P1", st)])
        assert fs.state["subjects"]["part P1"]["findings"][0]["says"] == words.FINDING_STATE[st]


def test_every_finding_is_represented():
    fds = [finding(f"f{i}", "wire", f"W{i}", "ok", pins=[f"A{i}.1", f"B{i}.2"]) for i in range(5)]
    fds.append(finding("f9", "wire", "W0", "unseated"))
    fs = build_state("s", fds)
    assert sum(len(s["findings"]) for s in fs.state["subjects"].values()) == len(fds)
    assert fs.subjects[("wire", "W0")].finding_ids == ["f0", "f9"]


@pytest.mark.parametrize("text,leak", [("housing gap ~2 mm (still s7)", "2 mm"), ("coverage 0.72", "0.72"),
                                       ("a 5V adapter", "5V"), ("1.5V cell", "1.5"), ("10% low", "10"),
                                       ("22AWG wire", "22AWG")])
def test_scrub_removes_numbers(text, leak):
    out = scrub(text)
    assert leak not in out and words.MEASURED in out and not numeric_tokens(out)


def test_scrub_keeps_identifiers():
    out = scrub("plug J1.1 to LOAD1.1 on net 3V3, still s7, keyframe kf_000, ref 12", allowed=("12",))
    for name in ("J1.1", "LOAD1.1", "3V3", "s7", "kf_000", "ref 12"):
        assert name in out


def test_s5_guard_raises():
    with pytest.raises(S5Violation):
        assert_words_only({"gap": 2.1})
    with pytest.raises(S5Violation):
        assert_words_only({"note": "gap 2.1 mm"})
    with pytest.raises(S5Violation):
        assert_words_only({"ok": "x"}, [Question("q", "noul", "Is the gap above 2 mm?")])
    assert_words_only({"subjects": {"wire W12": {"ref": "W12", "pins": ["J1.1", "J2.1"], "seen": True}}})


def test_vision_finding_cannot_be_proof():
    f = finding("f1", "part", "P1", "broken")
    f["is_proof"] = True
    with pytest.raises(ValueError, match="I1"):
        build_state("s", [f])


def test_not_inspected_subject():
    fs = build_state("s", [finding("f1", "part", "P1", "not_inspected"), finding("f2", "part", "P2", "ok")])
    assert fs.subjects[("part", "P1")].not_inspected_only
    assert not fs.subjects[("part", "P2")].not_inspected_only


def test_shuffle_seed_reproducible_and_recorded():
    fs = build_state("s", [finding("f1", "wire", "W1", "unseated", pins=["J1.1", "J2.1"]),
                           finding("f2", "part", "P1", "damaged")])
    a, seed_a = build_questions(fs, seed=11)
    b, _ = build_questions(fs, seed=11)
    c, _ = build_questions(fs, seed=12)
    assert seed_a == 11
    assert [list(q.options) for q in a] == [list(q.options) for q in b]
    assert [list(q.options) for q in a] != [list(q.options) for q in c]
    for qa, qc in zip(a, c):
        assert set(qa.options) == set(qc.options)
        if qa.type == "choice":
            assert qa.seed is not None
    default_1, s1 = build_questions(fs)
    default_2, s2 = build_questions(fs)
    assert s1 == s2 and [list(q.options) for q in default_1] == [list(q.options) for q in default_2]


def test_questions_are_words_and_never_carry_user_text():
    marker = "Zorblax"
    f = finding("f1", "part", "P1", "damaged", evidence=[f"{marker} label torn"])
    f["measurements"] = {"ocr_text": f"{marker} 12V"}
    fs = build_state("s", [f], symptom=f"{marker} will not start")
    qs, _ = build_questions(fs)
    assert_words_only(fs.state, qs)
    assert marker in json.dumps(fs.state)                               # A9: user/OCR text is state
    assert not any(marker in t for q in qs for t in [q.instructions, *all_strings(q.options)])


def test_state_budget_for_a_forty_row_harness():
    fds = []
    for i in range(40):
        f = finding(f"f{i:02d}", "wire", f"WIRE_{i:02d}", "unseated" if i == 7 else "ok",
                    pins=[f"J{i}.1", f"LOAD{i}.1"], evidence=["wire present between both ends, colour matches"],
                    meas={"colour_match_p": 0.9, "views": 2, "sharpness": 500.0}, path=i < 10)
        f["vlm_answer"] = "seated"
        fds.append(f)
    fs = build_state("s", fds, symptom="does not power on")
    approx_tokens = len(json.dumps(fs.state, separators=(",", ":"))) / 4   # ~4 chars per token
    assert approx_tokens <= 4000, approx_tokens
