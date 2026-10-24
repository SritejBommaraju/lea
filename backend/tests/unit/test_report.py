"""B11: the verdict truth table, the report builder, the words-only summary payload (S5) and the exports.
Rows are the public contract examples and the real vision.inspect builders; nothing stands in for a model: the
summary text below is labelled as test text, and the live writer is tested in tests/integration (-m live)."""
from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path

import pytest

from repairos.contracts import validate
from repairos.report import build_report, decide, facts_of, honesty_violations, to_html, to_markdown, words_for
from repairos.report.build import ReportError, confidence_word
from repairos.report.verdict import TABLE, Facts
from repairos.slots.detector import Detection
from repairos.slots.fusion import assert_words_only
from repairos.slots.vlm import CropState
from repairos.vision import inspect as insp
from repairos.vision.crops import Crop

EXAMPLES = Path(__file__).resolve().parents[3] / "spec" / "contracts" / "examples"
NOT_COMPUTED = {"status": "not_computed", "reason": "the 3D localization module is not installed yet"}
TEST_SUMMARY = {"text": "Test text, not written by a model.", "written_by": "unit test", "from_words_only": True}


def ex(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------------ truth table
EXPECTED = {
    (False, False, False): ("insufficient_evidence", "no_regions"),
    (False, False, True): ("insufficient_evidence", "nothing_inspected"),
    (False, True, False): ("potential_issues", "issues_found"),
    (False, True, True): ("potential_issues", "issues_found"),
    (True, False, False): ("looks_good", "all_inspected_ok"),
    (True, True, False): ("potential_issues", "issues_found"),
    (True, True, True): ("potential_issues", "issues_found"),
    (True, False, True): ("insufficient_evidence", "some_not_inspected"),
}


@pytest.mark.parametrize("judged,issue,unsettled", list(itertools.product([False, True], repeat=3)))
def test_truth_table_every_row(judged, issue, unsettled):
    v = decide(Facts(judged, issue, unsettled))
    assert (v.verdict, v.code) == EXPECTED[(judged, issue, unsettled)]
    assert v.text and not any(ch.isdigit() for ch in v.text)   # the reason goes to the summary writer (S5)


def test_truth_table_is_total_and_rules_are_unique():
    assert set(TABLE) == set(itertools.product([False, True], repeat=3))
    assert len({row[0] for row in TABLE.values()}) == len(TABLE)


def test_looks_good_only_when_everything_was_judged_and_fine():
    good = [k for k, (_, verdict, _) in TABLE.items() if verdict == "looks_good"]
    assert good == [(True, False, False)]


def test_facts_meter_overrides_photo():
    entry = {"issue": "loose", "proof": {"status": "ruled_out_by_meter", "reading_id": "r1"}}
    assert facts_of({"findings": [entry]}) == Facts(True, False, False)
    entry = {"issue": "none", "proof": {"status": "confirmed_by_meter", "reading_id": "r1"}}
    assert facts_of({"findings": [entry]}).issue


def test_confidence_words_match_what_inspect_stores():
    assert {w: confidence_word(c) for w, c in insp.CONFIDENCE.items()} == {"low": "low", "medium": "medium",
                                                                          "high": "high"}


# --------------------------------------------------------------------------------------------------- builder
def _findings(scan_id: str, still_id: str, states: list[str]) -> tuple[list[dict], dict]:
    """Findings exactly as vision.inspect writes them, one per crop state, plus one not inspected region."""
    out, regions = [], {}
    for i, state in enumerate(states):
        det = Detection(box=(100 + i, 200, 400 + i, 500), label="connector", score=0.42)
        crop = Crop(image=None, region_px=(50 + i, 150, 450 + i, 550), scale=1.0, box_px=det.box, still_id=still_id)
        cs = CropState(part_class="connector", state=state, evidence_text="a gap of 2 mm is visible",
                       confidence_word="medium", model="vlm", latency_ms=1)
        f = insp.finding_from_crop(scan_id=scan_id, still_id=still_id, index=i, det=det, crop=crop, crop_state=cs)
        out.append(f)
        regions[f["finding_id"]] = {"part_class": "connector", "inspected_by": ["detector", "vlm"]}
    det = Detection(box=(0, 0, 3000, 2900), label="circuit board", score=0.9)
    f = insp.finding_not_inspected(scan_id=scan_id, still_id=still_id, index=len(states), det=det,
                                   reason="the region covers most of the still; take a closer still")
    out.append(f)
    regions[f["finding_id"]] = {"part_class": "circuit board", "inspected_by": ["detector"]}
    return out, regions


def _build(states: list[str], with_ni: bool = True, **kw) -> tuple[dict, list[dict]]:
    scan, still = ex("scan-package"), ex("still")
    findings, regions = _findings(scan["scan_id"], still["still_id"], states)
    if not with_ni:
        findings = findings[:-1]
    rep = build_report(scan=scan, stills=[still], findings=findings, suspects=kw.get("suspects", []),
                       next_action=kw.get("next_action"), regions=regions,
                       locations={f["finding_id"]: NOT_COMPUTED for f in findings},
                       models={"detector": "d", "classifier": "not selected (CLS=none)", "vlm": "v", "ocr": "o",
                               "summary_writer": "s", "jev": "j", "locate": "not available"})
    return rep, findings


def test_every_finding_lands_in_exactly_one_bucket_and_validates():
    rep, findings = _build(["loose", "ok", "cannot_tell"])
    assert [e["issue"] for e in rep["findings"]] == ["loose", "none"]          # issues first
    assert len(rep["insufficient_evidence"]) == 1 and len(rep["not_inspected"]) == 1
    assert honesty_violations(rep, findings) == []
    assert rep["verdict"] == "potential_issues" and rep["verdict_reason"]["rule"] == "V6"
    assert rep["findings"][0]["location"] == NOT_COMPUTED
    assert rep["findings"][0]["proof"] == {"status": "not_measured"}
    assert {k: v for k, v in rep["capture"].items() if k != "views"} == {
        "depth_source": "lidar", "stills": 1, "stills_inspected": 1, "keyframes": 2,
        "scan_points": ex("scan-package")["point_count"], "coverage_words": "well covered"}
    assert rep["capture"]["views"]["cells_total"] == 36     # the camera positions on the view dome (coverage.py)
    validate("report", {**rep, "summary": TEST_SUMMARY})


def test_looks_good_needs_every_region_judged():
    rep, _ = _build(["ok", "ok"], with_ni=False)
    assert rep["verdict"] == "looks_good"
    validate("report", {**rep, "summary": TEST_SUMMARY})
    rep, _ = _build(["ok", "ok"])                   # one region was not inspected
    assert rep["verdict"] == "insufficient_evidence" and rep["verdict_reason"]["code"] == "some_not_inspected"


def test_nothing_judged_is_insufficient_evidence_with_a_reason():
    rep, _ = _build(["cannot_tell"])
    assert rep["verdict"] == "insufficient_evidence" and rep["verdict_reason"]["code"] == "nothing_inspected"
    assert rep["insufficient_evidence"][0]["reason"].startswith("could not judge")


def test_suspect_words_and_meter_proof_join_the_finding():
    scan = ex("scan-package")
    rep, findings = _build(["loose"], with_ni=False)
    sus = {**ex("suspect"), "finding_ids": [findings[0]["finding_id"]], "verdict": "cleared",
           "proof_reading_id": "r_1", "rank": 1, "p_fault_word": "very_unlikely"}
    rep, _ = _build(["loose"], with_ni=False, suspects=[sus])
    e = rep["findings"][0]
    assert e["proof"] == {"status": "ruled_out_by_meter", "reading_id": "r_1"}
    assert e["judgment"] == {"rank": 1, "likelihood_word": "very_unlikely", "verdict": "cleared"}
    assert rep["verdict"] == "looks_good" and scan["scan_id"] == rep["scan_id"]


def test_a_judged_region_without_a_model_is_refused():
    scan, still = ex("scan-package"), ex("still")
    findings, regions = _findings(scan["scan_id"], still["still_id"], ["ok"])
    regions[findings[0]["finding_id"]]["inspected_by"] = ["detector"]
    with pytest.raises(ReportError, match="no model judged"):
        build_report(scan=scan, stills=[still], findings=findings[:1], suspects=[], next_action=None,
                     regions=regions, locations={findings[0]["finding_id"]: NOT_COMPUTED}, models={})


def test_a_finding_without_its_region_record_is_refused():
    scan, still = ex("scan-package"), ex("still")
    findings, _ = _findings(scan["scan_id"], still["still_id"], ["ok"])
    with pytest.raises(ReportError, match="no region"):
        build_report(scan=scan, stills=[still], findings=findings, suspects=[], next_action=None, regions={},
                     locations={}, models={})


def test_honesty_check_catches_a_dropped_or_doubled_region():
    rep, findings = _build(["loose", "ok"])
    bad = copy.deepcopy(rep)
    bad["not_inspected"] = []
    assert any("no bucket" in p for p in honesty_violations(bad, findings))
    bad = copy.deepcopy(rep)
    bad["insufficient_evidence"].append({**rep["not_inspected"][0], "inspected_by": ["detector"]})
    assert any("in ['not_inspected', 'insufficient_evidence']" in p for p in honesty_violations(bad, findings))


def test_no_stills_no_findings():
    rep = build_report(scan=ex("scan-package"), stills=[], findings=[], suspects=[], next_action=None, regions={},
                       locations={}, models={"detector": "d", "classifier": "c", "vlm": "v", "summary_writer": "s",
                                             "jev": "not called", "locate": "l"})
    assert rep["verdict"] == "insufficient_evidence" and rep["verdict_reason"]["code"] == "no_regions"
    validate("report", {**rep, "summary": TEST_SUMMARY})


# ---------------------------------------------------------------------------------------- words only (S5)
def test_summary_payload_is_words_only():
    action = {**ex("next-action")}
    rep, _ = _build(["loose", "ok", "cannot_tell"], next_action=action)
    payload = words_for(rep, symptom="stopped after 3 seconds at 5V")
    assert_words_only(payload)
    text = json.dumps(payload)
    assert "2 mm" not in text and "3 seconds" not in text and "[measured]" in text
    assert payload["verdict"] == "potential issues" and payload["looked_fine"] == ["connector"]
    assert "box_px" not in text and "confidence\": 0" not in text


def test_example_reports_payloads_are_words_only():
    for name in ("report", "report.looks_good", "report.insufficient"):
        assert_words_only(words_for(ex(name)))


# -------------------------------------------------------------------------------------------------- exports
def test_exports_show_words_never_a_probability():
    rep = ex("report")
    md, page = to_markdown(rep), to_html(rep)
    for text in (md, page):
        assert "Potential issues" in text and "Not inspected" in text and "Could not be judged" in text
        assert "location not computed" in text and "only a meter reading proves" in text
        assert "p_fault" not in text and "0.6" not in text
    assert page.startswith("<!doctype html>") and "prefers-color-scheme: dark" in page


def test_html_embeds_crops_when_given():
    page = to_html(ex("report"), lambda ref: b"\xff\xd8jpeg")
    assert "data:image/jpeg;base64," in page


# ------------------------------------------------------------------------- many not-inspected regions per still
def _flooded_report(extra_small: int, budget_rows: int):
    """One still with real findings plus many regions nobody looked at: a roll-up of `extra_small` tiny boxes and
    `budget_rows` regions over the crop budget (the shape vision.inspect writes)."""
    scan, still = ex("scan-package"), ex("still")
    sid = still["still_id"]
    findings, regions = _findings(scan["scan_id"], sid, ["loose", "ok"])
    tiny = [Detection((10 * i, 10, 10 * i + 8, 18), "screw", 0.2) for i in range(extra_small)]
    roll = insp.finding_rollup(scan_id=scan["scan_id"], still_id=sid, what=insp.SMALL_REGIONS,
                               indices=list(range(extra_small)), dets=tiny)
    findings.append(roll)
    regions[roll["finding_id"]] = {"part_class": "small regions", "inspected_by": ["detector"],
                                   "regions": extra_small}
    for i in range(budget_rows):
        det = Detection((500 + i, 500, 700 + i, 700), "screw", 0.3)
        f = insp.finding_not_inspected(scan_id=scan["scan_id"], still_id=sid, index=100 + i, det=det,
                                       reason="over this still's crop budget")
        findings.append(f)
        regions[f["finding_id"]] = {"part_class": "screw", "inspected_by": ["detector"]}
    rep = build_report(scan=scan, stills=[still], findings=findings, suspects=[], next_action=None, regions=regions,
                       locations={f["finding_id"]: NOT_COMPUTED for f in findings},
                       models={"detector": "d", "classifier": "c", "vlm": "v", "ocr": "o", "summary_writer": "s",
                               "jev": "j", "locate": "l"})
    return rep, findings


def test_many_not_inspected_regions_of_a_still_become_one_row_with_the_exact_count():
    rep, findings = _flooded_report(extra_small=41, budget_rows=3)
    assert len(rep["not_inspected"]) == 1
    row = rep["not_inspected"][0]
    # 41 tiny boxes in the roll-up, 3 over the budget, 1 scene-sized box (from _findings)
    assert row["regions"] == 45 and len(row["finding_ids"]) == 5 and row["finding_id"] == row["finding_ids"][0]
    assert row["reason"].startswith("45 regions in this photo were not inspected: 41 regions: too small")
    assert "3 regions: over this still's crop budget" in row["reason"]
    assert honesty_violations(rep, findings) == []                  # every finding in exactly one row (MS1)
    assert [e["issue"] for e in rep["findings"]] == ["loose", "none"]   # real findings are not buried
    assert rep["verdict"] == "potential_issues"
    validate("report", {**rep, "summary": TEST_SUMMARY})
    assert "45 regions in this photo were not inspected" in to_markdown({**rep, "summary": TEST_SUMMARY})
    assert_words_only(words_for(rep))                               # the count stays out of the summary writer


def test_a_few_not_inspected_regions_keep_their_own_rows():
    rep, findings = _build(["ok"])
    assert len(rep["not_inspected"]) == 1 and "finding_ids" not in rep["not_inspected"][0]
    assert honesty_violations(rep, findings) == []


def test_a_grouped_row_that_drops_or_doubles_a_finding_is_caught():
    rep, findings = _flooded_report(extra_small=5, budget_rows=2)
    bad = copy.deepcopy(rep)
    bad["not_inspected"][0]["finding_ids"] = bad["not_inspected"][0]["finding_ids"][:-1]
    assert any("no bucket" in p for p in honesty_violations(bad, findings))
    bad = copy.deepcopy(rep)
    bad["not_inspected"][0]["regions"] = 1
    assert any("fewer regions" in p for p in honesty_violations(bad, findings))
