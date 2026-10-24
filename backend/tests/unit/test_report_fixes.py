"""Report fixes after the blind tests (2026-10-24): severity from the VLM, cosmetic issues apart, one row per physical
issue (same photo or same place on the scan), the most-likely-problems ranking, plain words in the next step, and the
wall-power safety note. Rows come from the real vision.inspect builders; no model is stood in for."""
from __future__ import annotations

import json
from pathlib import Path

from repairos.contracts import validate
from repairos.fusion.next_action import decide
from repairos.fusion.state import build_state, subject_words
from repairos.fusion.suspects import Judgment, rank_suspects
from repairos.fusion import questions as Q
from repairos.pipeline.run import still_words
from repairos.report import build_report, honesty_violations, to_html, to_markdown, words_for
from repairos.report.build import collapse_rows, part_family, rank_key, same_issue
from repairos.report.safety import NOT_MERGED_ACROSS_PHOTOS, runs_on_mains, safety_notes
from repairos.report.summary import number_words, photo_words
from repairos.slots.detector import Detection
from repairos.slots.vlm import CropState
from repairos.slots.vlm_anthropic import CROP_SCHEMA
from repairos.vision import inspect as insp
from repairos.vision.crops import Crop

EXAMPLES = Path(__file__).resolve().parents[3] / "spec" / "contracts" / "examples"
NOT_COMPUTED = {"status": "not_computed", "reason": "no camera pose for this photo"}
TEST_SUMMARY = {"text": "Test text, not written by a model.", "written_by": "unit test", "from_words_only": True}
MODELS = {"detector": "d", "classifier": "not selected (CLS=none)", "vlm": "v", "ocr": "o", "summary_writer": "s",
          "jev": "j", "locate": "not available"}


def ex(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def crop_finding(still: str, i: int, box, state="damaged", part="connector", severity="functional", words="",
                 confidence="medium", evidence="seen"):
    det = Detection(box=box, label="connector", score=0.5)
    crop = Crop(image=None, region_px=box, scale=1.0, box_px=box, still_id=still)
    cs = CropState(part_class=part, state=state, evidence_text=evidence, confidence_word=confidence, model="vlm",
                   latency_ms=1, severity=severity, part_words=words)
    return insp.finding_from_crop(scan_id="scan_t", still_id=still, index=i, det=det, crop=crop, crop_state=cs)


def report(findings, locations=None, suspects=(), explains=None, stills=None):
    scan = {**ex("scan-package"), "scan_id": "scan_t"}
    still = ex("still")
    ids = sorted({f["evidence_refs"]["still_ids"][0] for f in findings})
    stills = stills or [{**still, "still_id": s} for s in ids]
    regions = {f["finding_id"]: {"part_class": insp.part_name(f["evidence"][0].split(" in still ")[0]),
                                 "inspected_by": ["detector", "vlm"]} for f in findings}
    locations = locations or {f["finding_id"]: NOT_COMPUTED for f in findings}
    rep = build_report(scan=scan, stills=stills, findings=findings, suspects=list(suspects), next_action=None,
                       regions=regions, locations=locations, models=MODELS, explains=explains)
    assert honesty_violations(rep, findings) == []
    validate("report", {**rep, "summary": TEST_SUMMARY})
    return rep


# ------------------------------------------------------------------------------------------- 1. severity (VLM)
def test_crop_schema_asks_for_severity_and_part_words():
    assert {"severity", "part_words"} <= set(CROP_SCHEMA["required"])
    assert CROP_SCHEMA["properties"]["severity"]["enum"] == ["none", "cosmetic", "functional", "safety"]


def test_finding_severity_only_on_issues_and_none_means_functional():
    assert crop_finding("s1", 0, (0, 0, 10, 10), severity="cosmetic")["severity"] == "cosmetic"
    assert crop_finding("s1", 0, (0, 0, 10, 10), severity="none")["severity"] == "functional"
    assert "severity" not in crop_finding("s1", 0, (0, 0, 10, 10), state="ok", severity="none")
    assert "severity" not in crop_finding("s1", 0, (0, 0, 10, 10), state="cannot_tell", severity="none")
    f = crop_finding("s1", 0, (0, 0, 10, 10), words="  black  wire to the motor ")
    assert f["part_words"] == "black wire to the motor"
    validate("finding", f)


def test_severity_reaches_the_fusion_state_in_words():
    fs = build_state("scan_t", [crop_finding("s1", 0, (0, 0, 10, 10), severity="cosmetic")])
    (entry,) = fs.state["subjects"].values()
    assert entry["findings"][0]["matters"].startswith("cosmetic")


# ---------------------------------------------------------------------------------- 2. cosmetic and dedupe
def test_same_photo_duplicates_collapse_neighbours_do_not():
    a = crop_finding("s1", 0, (100, 100, 300, 300))
    dup = crop_finding("s1", 1, (100, 100, 300, 300))
    near = crop_finding("s1", 2, (230, 100, 430, 300))       # IoU 0.48 with a: a distinct neighbour
    inside = crop_finding("s1", 3, (0, 0, 1000, 1000))       # contains a (containment 1.0, IoU 0.04)
    rep = report([a, dup, near, inside])
    rows = rep["findings"]
    assert len(rows) == 3
    merged = next(e for e in rows if e.get("finding_ids"))
    assert set(merged["finding_ids"]) == {a["finding_id"], dup["finding_id"]}
    assert merged["regions"] == 2 and merged["merged_by"] == "same_photo_overlap" and len(merged["crops"]) == 1


def test_different_issues_never_collapse():
    a = crop_finding("s1", 0, (100, 100, 300, 300), state="loose")
    b = crop_finding("s1", 1, (100, 100, 300, 300), state="burnt")
    assert len(report([a, b])["findings"]) == 2


def loc(lo, hi):
    return {"status": "computed", "frame": "arkit_world", "point_m": [(x + y) / 2 for x, y in zip(lo, hi)],
            "region_min_m": lo, "region_max_m": hi, "confidence_word": "medium", "method": "triangulation"}


def test_cross_photo_rows_collapse_when_located_regions_meet_and_family_matches():
    a = crop_finding("s1", 0, (0, 0, 50, 50), part="black insulated wire")
    b = crop_finding("s2", 0, (0, 0, 50, 50), part="wire")
    c = crop_finding("s3", 0, (0, 0, 50, 50), part="connector")
    locs = {a["finding_id"]: loc([0, 0, 0], [0.02, 0.02, 0.02]), b["finding_id"]: loc([0.01, 0.01, 0.01],
                                                                                     [0.03, 0.03, 0.03]),
            c["finding_id"]: loc([0, 0, 0], [0.02, 0.02, 0.02])}
    rep = report([a, b, c], locations=locs)
    merged = next(e for e in rep["findings"] if e.get("finding_ids"))
    assert set(merged["finding_ids"]) == {a["finding_id"], b["finding_id"]}
    assert merged["merged_by"] == "same_place_on_scan"
    assert len(rep["findings"]) == 2 and NOT_MERGED_ACROSS_PHOTOS not in rep["limits"]


def test_cross_photo_rows_apart_on_the_scan_stay_apart():
    a = crop_finding("s1", 0, (0, 0, 50, 50), part="wire")
    b = crop_finding("s2", 0, (0, 0, 50, 50), part="wire")
    locs = {a["finding_id"]: loc([0, 0, 0], [0.01, 0.01, 0.01]), b["finding_id"]: loc([0.5, 0, 0], [0.6, 0.1, 0.1])}
    assert same_issue_rows(a, b, locs) is None


def same_issue_rows(a, b, locs):
    rep = report([a, b], locations=locs)
    r1, r2 = rep["findings"]
    return same_issue(r1, r2)


def test_without_scan_locations_photos_are_not_merged_and_the_limit_says_so():
    a = crop_finding("s1", 0, (0, 0, 50, 50), part="wire")
    b = crop_finding("s2", 0, (0, 0, 50, 50), part="wire")
    rep = report([a, b])
    assert len(rep["findings"]) == 2 and NOT_MERGED_ACROSS_PHOTOS in rep["limits"]
    assert NOT_MERGED_ACROSS_PHOTOS not in report([a])["limits"]


def test_part_family_is_the_longest_generic_class_inside_the_name():
    assert part_family("Black insulated wire") == "wire"
    assert part_family("ribbon cable connector") == "ribbon cable"   # the longest generic name inside wins
    assert part_family("electrolytic capacitor can") == "electrolytic capacitor"
    assert part_family("thermal fuse") == "fuse"
    assert part_family("gizmo") == "gizmo"


def test_cosmetic_issues_are_listed_apart_and_never_make_the_verdict():
    scuff = crop_finding("s1", 0, (0, 0, 50, 50), part="housing", severity="cosmetic")
    ok = crop_finding("s1", 1, (500, 500, 600, 600), state="ok", severity="none")
    rep = report([scuff, ok])
    assert [e["finding_id"] for e in rep["cosmetic"]] == [scuff["finding_id"]]
    assert all(e["issue"] == "none" for e in rep["findings"])
    assert rep["verdict"] == "looks_good" and "functional or safety" in rep["verdict_reason"]["text"]
    assert rep["most_likely"] == []


def test_only_cosmetic_rows_is_still_judged():
    rep = report([crop_finding("s1", 0, (0, 0, 50, 50), part="housing", severity="cosmetic")])
    assert rep["findings"] == [] and rep["verdict"] == "looks_good"


def test_mixed_severity_group_takes_the_worst_and_stays_a_finding():
    a = crop_finding("s1", 0, (100, 100, 300, 300), severity="cosmetic")
    b = crop_finding("s1", 1, (100, 100, 300, 300), severity="safety")
    rep = report([a, b])
    assert rep["cosmetic"] == [] and rep["findings"][0]["severity"] == "safety"


def test_honesty_catches_a_cosmetic_row_among_the_findings():
    rep = report([crop_finding("s1", 0, (0, 0, 50, 50), severity="cosmetic")])
    bad = {**rep, "findings": rep["cosmetic"], "cosmetic": []}
    assert any("cosmetic issue listed among the findings" in p for p in honesty_violations(bad, []))


# ------------------------------------------------------------------------------------------------ ranking
def row(fid, issue="damaged", severity=None, explains=None, conf="medium", photos=1, rank=5, proof="not_measured"):
    e = {"finding_id": fid, "issue": issue, "confidence_word": conf, "proof": {"status": proof},
         "crop": {"still_id": "s1"}, "crops": [{"still_id": f"s{i}"} for i in range(2, photos + 1)],
         "judgment": {"rank": rank, **({"explains_symptom": explains} if explains else {})}}
    if severity:
        e["severity"] = severity
    return e


def test_rank_safety_then_functional_then_explains_then_confidence_then_photos():
    rows = [row("ok", issue="none", conf="high"), row("cosmetic", severity="cosmetic", conf="high"),
            row("low", conf="low", explains="yes"), row("photos", photos=3), row("high", conf="high"),
            row("explains", explains="yes"), row("not_explains", explains="no", conf="high"),
            row("safety", severity="safety", conf="low"), row("proved", proof="confirmed_by_meter", conf="low")]
    got = [e["finding_id"] for e in sorted(rows, key=rank_key)]
    assert got == ["proved", "safety", "explains", "low", "high", "photos", "not_explains", "cosmetic", "ok"]


def test_most_likely_is_the_top_three_issue_rows():
    fs = [crop_finding("s1", i, (i * 200, 0, i * 200 + 100, 100), confidence=c, severity=s)
          for i, (c, s) in enumerate([("low", "functional"), ("high", "functional"), ("medium", "safety"),
                                      ("high", "cosmetic"), ("medium", "functional")])]
    rep = report(fs)
    top = rep["most_likely"]
    assert top == [e["finding_id"] for e in rep["findings"][:3]]
    assert top == [fs[2]["finding_id"], fs[1]["finding_id"], fs[4]["finding_id"]]
    md = to_markdown({**rep, "summary": TEST_SUMMARY})
    assert md.index("## Most likely problems") < md.index("## Potential issues") < md.index("## Cosmetic only")
    assert "Most likely problems" in to_html({**rep, "summary": TEST_SUMMARY})


def test_jev_explains_judgment_joins_the_row():
    f = crop_finding("s1", 0, (0, 0, 50, 50))
    sus = {**ex("suspect"), "scan_id": "scan_t", "subject": f["subject"], "finding_ids": [f["finding_id"]],
           "rank": 1, "verdict": "suspect", "p_fault_word": "likely", "proof_reading_id": None}
    rep = report([f], suspects=[sus], explains={(f["subject"]["kind"], f["subject"]["ref"]): "unsettled"})
    assert rep["findings"][0]["judgment"]["explains_symptom"] == "unsure"


def test_collapse_keeps_the_best_ranked_row_first():
    rows = [row("weak", conf="low"), row("strong", conf="high")]
    for e in rows:
        e["crop"] = {"still_id": "s1", "box_px": [0, 0, 10, 10]}
        e["crops"] = []
        e["evidence"] = [e["finding_id"]]
    (out,) = collapse_rows(rows)
    assert out["finding_ids"] == ["strong", "weak"] and out["evidence"] == ["strong", "weak"]


# ------------------------------------------------------------------------------ 3. plain-words next step
def test_still_words_number_photos_by_capture_time():
    stills = [{"still_id": "b", "captured_at": "2026-10-24T10:00:02Z"},
              {"still_id": "a", "captured_at": "2026-10-24T10:00:05Z"},
              {"still_id": "c", "captured_at": "2026-10-24T10:00:01Z"}]
    assert still_words(stills) == {"c": "photo 1", "b": "photo 2", "a": "photo 3"}


def test_subject_words_prefer_part_words_then_the_vlm_name_then_the_rollup():
    f = crop_finding("s9", 5, (0, 0, 10, 10), part="spade terminal", words="the red wire on the motor")
    assert subject_words([f], "s9/det5", ["s9"], {"s9": "photo 9"}) == "red wire on the motor (photo 9)"
    g = crop_finding("s9", 5, (0, 0, 10, 10), part="spade terminal")
    assert subject_words([g], "s9/det5", ["s9"], {"s9": "photo 9"}) == "spade terminal (photo 9)"
    assert subject_words([], "s2/small_regions", ["s2"], {"s2": "photo 2"}) == "small regions (photo 2)"
    assert subject_words([g], "s9/det5", ["s9"], {}) is None


def test_next_step_names_the_part_in_words_and_keeps_refs_in_the_target():
    f = crop_finding("s9", 5, (0, 0, 10, 10), state="loose", part="spade terminal", words="red wire on the motor")
    fs = build_state("scan_t", [f], still_words={"s9": "photo 9"})
    j = Judgment(0.9, 0.5, 0.9, {k: 0.2 for k in Q.FAULT_KINDS["part"]}, 1, {k: 0.2 for k in Q.LIMITS})
    js = {k: j for k in fs.subjects}
    act = decide(fs, rank_suspects(fs, js, "jev-test"), js)
    assert "s9/det5" not in act["instruction"] and "s9/det5" not in act["reason"]
    assert "red wire on the motor (photo 9)" in act["instruction"]
    assert act["target"]["part_refs"] == ["s9/det5"]
    assert "s9/det5" in json.dumps(fs.state) and "red wire" not in json.dumps(fs.state)   # Jev sees refs only


def test_photo_numbers_survive_the_no_numbers_rule_as_words():
    assert number_words(9) == "nine" and number_words(21) == "twenty-one" and number_words(40) == "forty"
    assert photo_words("look at the wire (photo 9, photo 12)") == "look at the wire (photo nine, photo twelve)"
    f = crop_finding("s1", 0, (0, 0, 50, 50))
    rep = report([f])
    rep["next_step"] = {"action_id": "a", "kind": "request_closeup",
                        "instruction": "Take a close, sharp photo of the connector (photo 3)."}
    assert "photo three" in words_for(rep)["next_step"]


# ------------------------------------------------------------------------------------- 4. wall-power safety
def r(part, issue="none", evidence=(), words=None, reason=None):
    e = {"part_class": part, "issue": issue, "evidence": list(evidence), "proof": {"status": "not_measured"}}
    if words:
        e["part_words"] = words
    if reason:
        e["reason"] = reason
    return e


def test_mains_words_in_any_looked_at_row_add_the_wall_power_note():
    assert runs_on_mains([r("plug prong", "damaged")])
    assert runs_on_mains([r("connector", words="left blade of the wall plug")])
    assert runs_on_mains([r("wire", evidence=["the power cord insulation is intact"])])
    assert not runs_on_mains([r("usb port"), r("plug")])            # "plug" alone is not wall power
    assert not runs_on_mains([r("transformers toy")])               # whole words only


def test_spade_terminal_with_a_motor_means_wall_power():
    assert runs_on_mains([r("spade terminal"), r("motor")])
    assert not runs_on_mains([r("spade terminal")])


def test_mains_note_reads_ok_and_undecided_rows_and_comes_before_unplug():
    notes = safety_notes([r("connector", "loose")], [], [r("thermostat", reason="could not judge the thermostat")])
    assert [n["code"] for n in notes] == ["mains_power", "unplug_first"]
    assert [n["code"] for n in safety_notes([r("heating element")])] == ["mains_power"]


def test_wall_power_note_on_a_built_report():
    f = crop_finding("s1", 0, (0, 0, 50, 50), part="wall charger plug prong", state="ok", severity="none")
    assert [n["code"] for n in report([f])["safety"]] == ["mains_power"]


def test_a_meter_confirmed_fault_stays_in_findings_even_if_it_looks_cosmetic():
    # Greptile #6.2: a proven fault must never move to the cosmetic section or drop out of most_likely
    f = crop_finding("s1", 0, (0, 0, 50, 50), part="housing", severity="cosmetic")
    sus = {**ex("suspect"), "scan_id": "scan_t", "subject": f["subject"], "finding_ids": [f["finding_id"]],
           "rank": 1, "verdict": "confirmed", "p_fault_word": "very_likely", "proof_reading_id": "r_1"}
    rep = report([f], suspects=[sus])
    assert rep["cosmetic"] == [] and rep["most_likely"] == [f["finding_id"]]
    assert rep["findings"][0]["proof"]["status"] == "confirmed_by_meter" and rep["verdict"] == "potential_issues"
