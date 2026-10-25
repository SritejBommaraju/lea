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


def test_coverage_counts_only_poses_in_the_original_frame():
    centre = [0.0, 0.0, 0.0]
    half = [{"camera": {"position_m": [0.4 * math.cos(math.radians(a)), 0.1, 0.4 * math.sin(math.radians(a))]}}
            for a in range(15, 180, 30)]
    other = [{"segment": s, "camera": {"position_m": [0.4 * math.cos(math.radians(a)), 0.1, 0.4 * math.sin(math.radians(a))]}}
             for s in (1, 2, 3) for a in range(195, 360, 30)]
    base = {"crop_box": {"center_m": centre}, "gravity_arkit_world": [0, -1, 0], "keyframes": half + other}
    segments = [{"segment": 1, "reason": "continued", "aligned": False}, {"segment": 2, "reason": "flipped"},
                {"segment": 3, "reason": "continued"}]
    # 1 (other world origin) and 2 (object moved) add nothing; 3 (relocalised continuation) adds its views
    assert view_coverage({**base, "segments": segments[:2]}, [])["sectors"] == 6
    assert view_coverage({**base, "segments": segments}, [])["sectors"] == 12
    stills = [{"segment": 1, "camera": {"position_m": [0.01, 0.5, 0.0]}}]
    assert view_coverage({**base, "segments": segments[:2]}, stills)["cells"] == 6
    # hand-held: a flip the app tracked relative to the object counts; an unaligned continuation never does
    tracked = [{**k, "pose_source": "object_tracked"} if k.get("segment") == 2 else k for k in half + other]
    handheld = {**base, "keyframes": tracked, "pose_source": "object_tracked", "segments": segments[:2]}
    assert view_coverage(handheld, [])["sectors"] == 12
    # --turn-it: hand-held first capture, then a continuation with fresh ARKit poses (aligned false, no world map)
    turn_it = {**base, "keyframes": half + [k for k in other if k["segment"] == 1], "pose_source": "object_tracked",
               "segments": [{"segment": 1, "reason": "continued", "aligned": False}]}
    assert view_coverage(turn_it, [])["sectors"] == 6


def test_the_caveat_counts_every_region_a_grouped_row_stands_for():
    from repairos.live.spacetime_bridge import verdict_line
    from repairos.report.verdict import caveat

    rep = {"findings": [], "insufficient_evidence": [{}], "not_inspected": [
        {"part_class": "connector", "regions": 11}, {"part_class": "switch"}, {"part_class": "whole photo", "regions": 4}]}
    assert caveat(rep) == "13 areas need a closer photo."   # 1 + 11 + 1; a left-out whole photo is not an area
    line = verdict_line({"verdict_reason": {"text": "Nothing looks wrong.", "caveat": caveat(rep)}})
    assert line == "Nothing looks wrong. 13 areas need a closer photo."
    assert verdict_line({"verdict_reason": {"text": "Nothing looks wrong."}}) == "Nothing looks wrong."


def test_one_frame_rule_for_mesh_and_coverage():
    from repairos.pipeline.mesh import keyframes_in_frame
    from repairos.pipeline.segments import same_frame_segments

    pkg = {"segments": [{"segment": 1, "reason": "continued"}, {"segment": 2, "reason": "continued", "aligned": False},
                        {"segment": 3, "reason": "flipped"}, {"segment": 4, "reason": "continued", "object_moved": True}],
           "keyframes": [{"keyframe_id": str(i), "segment": i} for i in range(5)]}
    assert same_frame_segments(pkg) == {0, 1}
    assert [k["keyframe_id"] for k in keyframes_in_frame(pkg)] == ["0", "1"]


def test_texture_views_follow_the_same_frame_rule(tmp_path):
    import json
    from pathlib import Path

    from PIL import Image

    from repairos.recreation.texture import scan_views

    example = json.loads((Path(__file__).resolve().parents[3] / "spec/contracts/examples/still.json").read_text())
    stills = []
    for i, seg in enumerate([0, 0, 1, 4]):
        img = tmp_path / f"s{i}.jpg"
        Image.new("RGB", (64, 48), (40 * i, 90, 140)).save(img)
        stills.append({**example, "still_id": f"s{i}", "segment": seg, "image_file": str(img), "depth_file": None,
                       "width_px": 64, "height_px": 48, "pose_reliable": True})
    pkg = {"segments": [{"segment": 1, "reason": "continued"},
                        {"segment": 4, "reason": "continued", "object_moved": True}], "keyframes": []}
    assert [v.name for v in scan_views(pkg, stills)] == ["s0", "s1", "s2"]   # the moved segment's photo is out


def test_the_new_verdict_has_words_everywhere_and_validates():
    assert export.TITLE["no_problems_seen"] == "No problems seen"
    assert "no_problems_seen" in summary.VERDICT_WORDS
    rep = {**ex("report"), "summary": TEST_SUMMARY}
    rep["findings"] = [{**f, "issue": "none"} for f in rep["findings"]][:1] or rep["findings"]
    rep.update(verdict="no_problems_seen", verdict_reason={"code": "nothing_seen_wrong", "rule": "V8",
                                                           "text": "Nothing looked wrong.", "caveat": "3 areas need a closer photo."})
    if rep["findings"]:
        validate("report", rep)
