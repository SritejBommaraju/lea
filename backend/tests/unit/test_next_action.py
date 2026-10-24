"""F4.4: suspect ranking, the meter-reading override (I2) and the next-action truth table. No network: the
Judgment inputs are plain data handed to code, not a stand-in engine."""
from __future__ import annotations

import itertools
import re

import pytest

from repairos.contracts import validate
from repairos.fusion import questions as Q
from repairos.fusion.next_action import TABLE, ar_label, decide
from repairos.fusion.override import reading_subjects, reading_verdict
from repairos.fusion.state import build_state
from repairos.fusion.suspects import Judgment, rank_suspects
from repairos.slots.fusion import Answers, FusionBadAnswer, Question, check_answers

P = {"yes": 0.9, "unsettled": 0.5, "no": 0.1}


def finding(fid, kind, ref, state="unseated", pins=None, path=True):
    d = {"finding_id": fid, "scan_id": "scan_t", "subject": {"kind": kind, "ref": ref}, "state": state,
         "source": "repairos.vision", "confidence": 0.8, "is_proof": False, "evidence": [],
         "evidence_refs": {"still_ids": ["s1"]}, "on_symptom_power_path": path, "created_at": "2026-10-24T10:00:00Z"}
    if pins:
        d["subject"]["pins"] = pins
    return d


def reading(rid, continuity, pins=("J1.1", "J2.1"), net="NET_X", expected=True, at="2026-10-24T10:05:00Z"):
    r = {"reading_id": rid, "scan_id": "scan_t", "session_id": "sess", "kind": "continuity", "net": net,
         "pins": list(pins), "value": {"continuity": continuity}, "power_state": "off", "source": "typed",
         "recorded_at": at}
    if expected is not None:
        r["expected"] = {"continuity": expected, "source": "repairos_derived"}
    return r


def judgment(problem="yes", clear="yes", explains="yes", kind=None):
    kind = kind or {"unplugged": 0.7, "broken_or_missing": 0.1, "wrong_pin_or_colour_order": 0.1,
                    "damaged_insulation": 0.1}
    return Judgment(P[problem], P[clear], P[explains], kind, 42, {k: 0.2 for k in Q.LIMITS})


def run(findings, readings=(), j=None, photos=0, expectations=None):
    fs = build_state("scan_t", findings, readings)
    js = {k: (j(k) if callable(j) else j or judgment()) for k, s in fs.subjects.items() if s.finding_ids}
    sus = rank_suspects(fs, js, "jev-test")
    return fs, sus, decide(fs, sus, js, photos, expectations)


# ---------------------------------------------------------------- meter verdicts in code (S5)

@pytest.mark.parametrize("r,want", [
    (reading("r", True), "agrees"), (reading("r", False), "disagrees"), (reading("r", False, expected=None), "unknown"),
    ({**reading("r", True), "verdict": "disagrees"}, "disagrees"),
    ({"kind": "voltage_dc", "value": {"volts": 4.8}, "expected": {"volts_min": 4.9, "volts_max": 5.1}}, "agrees"),
    ({"kind": "voltage_dc", "value": {"volts": 3.1}, "expected": {"volts_min": 4.9, "volts_max": 5.1}}, "disagrees"),
    ({"kind": "voltage_dc", "value": {"volts": 3.1}, "expected": {}}, "unknown"),
    ({"kind": "resistance", "value": {"ohms": 0.4}, "expected": {"ohms_max": 1.0}}, "agrees"),
    ({"kind": "resistance", "value": {"ohms": 1e6}, "expected": {"continuity": True}}, "disagrees"),
])
def test_reading_verdict(r, want):
    assert reading_verdict(r) == want


def test_reading_subjects_by_net_pins_and_action():
    subjects = {("wire", "W1"): {"pins": ["J1.1", "J2.1"]}, ("part", "J1"): {"pins": ["J1.1", "J1.2"]},
                ("wire", "W2"): {"pins": []}}
    actions = {"act_1": {"target": {"harness_row": "W2"}}}
    got = reading_subjects({**reading("r", False), "action_id": "act_1"}, subjects, actions)
    assert got == {("net", "NET_X"), ("wire", "W1"), ("wire", "W2")}       # never the part J1 on its own


# ---------------------------------------------------------------- the override (I2)

def test_disagreeing_reading_beats_a_clean_judgment():
    fs, sus, act = run([finding("f1", "wire", "W1", "ok", pins=["J1.1", "J2.1"])], [reading("rd_1", False)],
                       j=judgment(problem="no"))
    w1 = next(s for s in sus if s["subject"]["ref"] == "W1")
    assert (w1["verdict"], w1["p_fault"], w1["proof_reading_id"]) == ("confirmed", 1.0, "rd_1")
    assert w1["rank"] == 1
    assert act["kind"] == "report_finding" and act["target"]["harness_row"] == "W1"


def test_agreeing_reading_clears_a_damning_judgment():
    fs, sus, act = run([finding("f1", "wire", "W1", pins=["J1.1", "J2.1"])], [reading("rd_1", True)])
    w1 = next(s for s in sus if s["subject"]["ref"] == "W1")
    assert (w1["verdict"], w1["p_fault"], w1["fault_kind"]["choice"]) == ("cleared", 0.0, "ok")
    assert act["kind"] == "report_insufficient_evidence" and act["insufficient_evidence_reason"]


def test_latest_reading_wins_and_unknown_proves_nothing():
    rs = [reading("rd_1", False, at="2026-10-24T10:01:00Z"), reading("rd_2", True, at="2026-10-24T10:09:00Z")]
    _, sus, _ = run([finding("f1", "wire", "W1", pins=["J1.1", "J2.1"])], rs)
    assert next(s for s in sus if s["subject"]["ref"] == "W1")["proof_reading_id"] == "rd_2"
    _, sus, _ = run([finding("f1", "wire", "W1", pins=["J1.1", "J2.1"])], [reading("rd_3", False, expected=None)])
    w1 = next(s for s in sus if s["subject"]["ref"] == "W1")
    assert w1["verdict"] == "suspect" and w1["proof_reading_id"] is None


def test_no_confirmed_without_proof():
    for combo in itertools.product(P, repeat=3):
        _, sus, _ = run([finding("f1", "wire", "W1", pins=["J1.1", "J2.1"]), finding("f2", "part", "P1", "damaged")],
                        j=judgment(*combo))
        assert all(s["verdict"] != "confirmed" and s["proof_reading_id"] is None for s in sus)
        assert all(s["calibration_validated"] is False for s in sus)


def test_ranking_and_verdicts():
    def j(key):
        return {("part", "A"): judgment("yes"), ("part", "B"): judgment("no"),
                ("part", "C"): judgment(clear="no"), ("part", "D"): judgment()}[key]
    fds = [finding("f1", "part", "A", "damaged"), finding("f2", "part", "B", "ok"),
           finding("f3", "part", "C", "unsure"), finding("f4", "part", "D", "not_inspected")]
    _, sus, _ = run(fds, j=j)
    assert [(s["subject"]["ref"], s["verdict"]) for s in sus] == [
        ("A", "suspect"), ("B", "suspect"), ("C", "insufficient_evidence"), ("D", "not_inspected")]
    assert [s["rank"] for s in sus] == [1, 2, 3, 4]
    assert sus[0]["p_fault_word"] == "likely" and sus[1]["p_fault_word"] == "very_unlikely"


# ---------------------------------------------------------------- the truth table

def test_table_covers_every_combination():
    assert set(TABLE) == set(itertools.product(("yes", "unsettled", "no"), repeat=2))


@pytest.mark.parametrize("problem,clear,explains,kind,pins,photos", list(itertools.product(
    P, P, P, ("wire", "part", "net"), (None, ["J1.1", "J2.1"]), (0, Q.MAX_EXTRA_PHOTOS))))
def test_every_combination_gives_a_valid_action(problem, clear, explains, kind, pins, photos):
    _, _, act = run([finding("f1", kind, "X1", pins=pins)], j=judgment(problem, clear, explains), photos=photos)
    validate("next-action", act)
    assert act["chosen_by"] == "code_on_jev"
    if act["kind"].startswith("run_"):
        assert re.match(r"^Power (OFF|ON)\b", act["instruction"])                  # S4: power state first
        assert act["instruction"].index("Power") < act["instruction"].index("Probe")
        assert any("colour" in n for n in act["safety_notes"])                     # S1 line always present
        assert all(lab not in act["target"]["pins"] for lab in act["target"]["ar_labels"])
    if act["kind"] == "report_insufficient_evidence":
        assert act["insufficient_evidence_reason"]
    if act["kind"] == "report_finding":
        assert "Unproven" in act["instruction"]                                     # I2: no proof yet
    if photos >= Q.MAX_EXTRA_PHOTOS:
        assert act["kind"] not in ("request_closeup", "request_other_angle")


@pytest.mark.parametrize("clear,problem,explains,want", [
    ("yes", "yes", "yes", "run_continuity_test"), ("yes", "unsettled", "yes", "run_continuity_test"),
    ("unsettled", "yes", "yes", "request_closeup"), ("unsettled", "no", "yes", "request_other_angle"),
    ("no", "yes", "yes", "request_closeup"),
])
def test_table_rows_on_a_testable_wire(clear, problem, explains, want):
    _, _, act = run([finding("f1", "wire", "W1", pins=["J1.1", "J2.1"])], j=judgment(problem, clear, explains))
    assert act["kind"] == want


def test_unproven_visible_problem_on_a_part_is_reported_as_unproven():
    _, _, act = run([finding("f1", "part", "P1", "broken")])
    assert act["kind"] == "report_finding" and "Unproven" in act["instruction"]


def test_clean_photos_on_the_power_path_lead_to_the_meter():
    _, _, act = run([finding("f1", "wire", "W1", "ok", pins=["J1.1", "J2.1"])], j=judgment(problem="no"))
    assert act["kind"] == "run_continuity_test" and act["power_state"] == "off"
    assert act["expected"] == {"continuity": True, "source": "repairos_derived"}


def test_voltage_needs_an_expectation():
    exp = {("net", "VBUS"): {"volts_min": 4.75, "volts_max": 5.25, "source": "datasheet"}}
    f = finding("f1", "net", "VBUS", "unsure", pins=["J1.1", "J1.4"])
    _, _, act = run([f], j=judgment(), expectations=exp)
    assert act["kind"] == "run_voltage_test" and act["power_state"] == "on"
    assert act["expected"]["volts_min"] == 4.75
    _, _, act = run([f], j=judgment())
    assert act["kind"] == "run_continuity_test"


def test_nothing_to_judge_is_insufficient_evidence():
    fs = build_state("scan_t", [])
    act = decide(fs, rank_suspects(fs, {}, "none"), {})
    assert act["kind"] == "report_insufficient_evidence" and act["insufficient_evidence_reason"] == "other"


def test_ar_labels_never_bare_pins():
    assert ar_label("J1.1") == "J1 pin 1"
    assert ar_label("TP3") != "TP3"


# ---------------------------------------------------------------- engine answer contract

def test_check_answers_rejects_bad_answers():
    qs = [Question("a", "noul", "Is it?"), Question("b", "choice", "Which?", {"x": "x", "y": "y"})]
    check_answers(qs, Answers({"a": 0.2, "b": {"x": 0.4, "y": 0.6}}, "m", 1))
    for bad in ({"a": 0.2}, {"a": 1.2, "b": {"x": 0.4, "y": 0.6}}, {"a": 0.2, "b": {"x": 0.4}},
                {"a": 0.2, "b": {"x": 0.9, "y": 0.9}}):
        with pytest.raises(FusionBadAnswer):
            check_answers(qs, Answers(bad, "m", 1))
