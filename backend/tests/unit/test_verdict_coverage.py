"""V8 "No problems seen" (2026-10-24): nothing judged shows an issue and the capture covered the object well, so the
verdict is no longer insufficient_evidence; what was not checked is the caveat. The trimmer case uses the counts and
view coverage of scan_51d2fccf9747fdcc (a real scan: 2 regions judged ok, 8 could not be judged, 5 not inspected;
17 of 36 dome cells, 10 of 12 sectors), not its photos."""
from __future__ import annotations

import math

from repairos.contracts import validate
from repairos.report import export, summary
from repairos.report.coverage import CELLS, view_coverage
from repairos.report.verdict import Facts, caveat, decide, facts_of, is_covered, load_config
from test_api_gateway import ex
from test_report_fixes import TEST_SUMMARY

TRIMMER_VIEWS = {"cells": 17, "cells_total": 36, "sectors": 10, "sectors_total": 12, "share": 0.472}


def _ok(i):
    return {"finding_id": f"f_ok{i}", "issue": "none", "proof": {"status": "not_measured"}, "part_class": "housing"}


def trimmer(views=TRIMMER_VIEWS, judged=2):
    return {"findings": [_ok(i) for i in range(judged)], "cosmetic": [],
            "insufficient_evidence": [{"finding_id": f"f_u{i}", "part_class": "unknown_part"} for i in range(8)],
            "not_inspected": [{"finding_id": "f_n0", "part_class": "small regions"},
                              {"finding_id": "f_n1", "part_class": "whole photo"},
                              {"finding_id": "f_n2", "part_class": "whole photo"},
                              {"finding_id": "f_n3", "part_class": "unknown_part"},
                              {"finding_id": "f_n4", "part_class": "small regions"}],
            "capture": {"views": views} if views else {}}


def test_the_trimmer_scan_reads_no_problems_seen_with_its_caveat():
    rep = trimmer()
    v = decide(facts_of(rep))
    assert (v.verdict, v.rule, v.code) == ("no_problems_seen", "V8", "nothing_seen_wrong")
    assert not any(ch.isdigit() for ch in v.text)             # the reason still goes to the summary writer (S5)
    assert caveat(rep) == "Nothing looked wrong in the 2 areas Lea could see clearly. 11 areas need a closer photo."


def test_poor_coverage_stays_insufficient_evidence():
    for rep in (trimmer(views={**TRIMMER_VIEWS, "cells": 6, "share": 0.167}),       # a small part of the dome
                trimmer(views={**TRIMMER_VIEWS, "sectors": 3}),                      # one side only
                trimmer(judged=1),                                                    # one place seen clearly
                trimmer(views=None)):                                                 # no poses (a loose photo)
        v = decide(facts_of(rep))
        assert (v.verdict, v.rule) == ("insufficient_evidence", "V7")


def test_an_issue_or_full_coverage_keep_their_rows():
    rep = trimmer()
    rep["findings"][0] = {**rep["findings"][0], "issue": "loose"}
    assert decide(facts_of(rep)).verdict == "potential_issues"
    clean = {**trimmer(), "insufficient_evidence": [], "not_inspected": []}
    assert decide(facts_of(clean)).verdict == "looks_good"
    assert decide(Facts(True, False, True, covered=True)).rule == "V8"
    assert decide(Facts(False, False, True, covered=True)).rule == "V1"          # nothing judged: never V8


def test_thresholds_come_from_config():
    cfg = load_config()
    assert set(cfg) == {"min_judged_regions", "min_view_share", "min_sectors"}
    assert not is_covered(trimmer(), {**cfg, "min_sectors": 11}) and is_covered(trimmer(), cfg)


def test_view_coverage_places_cameras_on_the_dome():
    centre = [0.0, 0.0, 0.0]
    ring = [{"camera": {"position_m": [0.4 * math.cos(math.radians(a)), 0.1, 0.4 * math.sin(math.radians(a))]}}
            for a in range(15, 360, 30)]
    top = [{"camera": {"position_m": [0.01, 0.5, 0.0]}}]
    v = view_coverage({"crop_box": {"center_m": centre}, "gravity_arkit_world": [0, -1, 0], "keyframes": ring}, top)
    assert v == {"cells": 13, "cells_total": CELLS, "sectors": 12, "sectors_total": 12, "share": round(13 / 36, 3)}
    assert view_coverage({"keyframes": ring}, []) is None                                 # no object box
    assert view_coverage({"crop_box": {"center_m": centre}}, [{"camera": {}}]) is None   # no pose


def test_the_new_verdict_has_words_everywhere_and_validates():
    assert export.TITLE["no_problems_seen"] == "No problems seen"
    assert "no_problems_seen" in summary.VERDICT_WORDS
    rep = {**ex("report"), "summary": TEST_SUMMARY}
    rep["findings"] = [{**f, "issue": "none"} for f in rep["findings"]][:1] or rep["findings"]
    rep.update(verdict="no_problems_seen", verdict_reason={"code": "nothing_seen_wrong", "rule": "V8",
                                                           "text": "Nothing looked wrong.", "caveat": "3 areas need a closer photo."})
    if rep["findings"]:
        validate("report", rep)
