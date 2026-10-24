"""More photos added to a scan from its report (2026-10-24): the re-made report keeps a finding seen again as one row
with the same id (collapse_rows `stable`), and a place that could not be judged before but is judged in a new photo
moves into that judged row (`superseded_ids`), never listed twice. Rows come from the real vision.inspect builders."""
from __future__ import annotations

from repairos.contracts import validate
from repairos.report import build_report, honesty_violations
from repairos.slots.state_sqlite import rules
from repairos.vision import inspect as insp
from test_report_fixes import MODELS, TEST_SUMMARY, crop_finding, ex, loc

PLACE = ([0, 0, 0], [0.02, 0.02, 0.02])
NEAR = ([0.01, 0.01, 0.01], [0.03, 0.03, 0.03])
ELSEWHERE = ([0.5, 0, 0], [0.6, 0.1, 0.1])


def build(findings, locations, previous=None):
    scan = {**ex("scan-package"), "scan_id": "scan_t"}
    ids = sorted({f["evidence_refs"]["still_ids"][0] for f in findings})
    stills = [{**ex("still"), "still_id": s} for s in ids]
    regions = {f["finding_id"]: {"part_class": insp.part_name(f["evidence"][0].split(" in still ")[0]
                                                             .replace("could not judge the ", "")),
                                 "inspected_by": ["detector", "vlm"]} for f in findings}
    rep = build_report(scan=scan, stills=stills, findings=findings, suspects=[], next_action=None, regions=regions,
                       locations=locations, models=MODELS, previous=previous)
    assert honesty_violations(rep, findings) == []
    validate("report", {**rep, "summary": TEST_SUMMARY})
    return rep


def test_a_finding_seen_again_keeps_its_row_and_id():
    old = crop_finding("s1", 0, (0, 0, 50, 50), part="wire", confidence="low")
    first = build([old], {old["finding_id"]: loc(*PLACE)})
    new = crop_finding("s9", 0, (0, 0, 50, 50), part="wire", confidence="high")   # a closer, sharper photo later
    locs = {old["finding_id"]: loc(*PLACE), new["finding_id"]: loc(*NEAR)}
    fresh = build([old, new], locs)
    second = build([old, new], locs, previous=first)
    assert fresh["findings"][0]["finding_id"] == new["finding_id"]          # without history: best-ranked first
    (row,) = second["findings"]
    assert row["finding_id"] == old["finding_id"] and row["finding_ids"] == [old["finding_id"], new["finding_id"]]
    assert row["merged_by"] == "same_place_on_scan" and second["most_likely"] == [old["finding_id"]]


def test_a_place_judged_in_a_new_photo_supersedes_could_not_judge():
    blurred = crop_finding("s1", 0, (0, 0, 50, 50), state="cannot_tell", part="wire")
    first = build([blurred], {blurred["finding_id"]: loc(*PLACE)})
    assert [e["finding_id"] for e in first["insufficient_evidence"]] == [blurred["finding_id"]]
    sharp = crop_finding("s9", 0, (0, 0, 50, 50), state="ok", part="wire")
    other = crop_finding("s2", 1, (0, 0, 50, 50), state="cannot_tell", part="wire")
    locs = {blurred["finding_id"]: loc(*PLACE), sharp["finding_id"]: loc(*NEAR), other["finding_id"]: loc(*ELSEWHERE)}
    second = build([blurred, sharp, other], locs, previous=first)
    (row,) = second["findings"]
    assert row["finding_id"] == sharp["finding_id"] and row["superseded_ids"] == [blurred["finding_id"]]
    assert [e["finding_id"] for e in second["insufficient_evidence"]] == [other["finding_id"]]   # elsewhere: kept


def test_could_not_judge_in_two_photos_of_one_place_is_one_row():
    a = crop_finding("s1", 0, (0, 0, 50, 50), state="cannot_tell", part="wire")
    b = crop_finding("s9", 0, (0, 0, 50, 50), state="cannot_tell", part="wire")
    first = build([b], {b["finding_id"]: loc(*NEAR)})
    rep = build([a, b], {a["finding_id"]: loc(*PLACE), b["finding_id"]: loc(*NEAR)}, previous=first)
    (row,) = rep["insufficient_evidence"]
    assert row["finding_id"] == b["finding_id"] and row["finding_ids"] == [b["finding_id"], a["finding_id"]]


def test_the_state_rules_refuse_a_judged_row_superseding_a_judged_finding():
    ok = crop_finding("s1", 0, (0, 0, 50, 50), state="ok", part="wire")
    sharp = crop_finding("s9", 0, (0, 0, 50, 50), state="ok", part="wire")
    rep = build([sharp], {sharp["finding_id"]: loc(*PLACE)})
    rep["findings"][0]["superseded_ids"] = [ok["finding_id"]]
    store = {("finding", f["finding_id"]): f for f in (ok, sharp)} | {("scan-package", "scan_t"):
                                                                       {**ex("scan-package"), "scan_id": "scan_t"}}
    try:
        rules.check("report", {**rep, "summary": TEST_SUMMARY}, None, lambda k, i: store.get((k, i)))
    except Exception as e:  # noqa: BLE001
        assert "supersedes" in str(e)
    else:
        raise AssertionError("a judged finding was accepted as superseded")
    assert any("supersedes" in p for p in honesty_violations(rep, [ok, sharp]))
