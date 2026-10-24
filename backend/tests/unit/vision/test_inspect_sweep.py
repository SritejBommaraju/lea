"""Close tiles, look-alike strips, whole-crop judging and physical sizes (vision/sweep.py, vision/scale.py,
vision/inspect.py). Geometry and decisions in code; the live VLM path is covered by backend/tests/conformance/vlm."""
from __future__ import annotations

import numpy as np
import pytest

from repairos.contracts import validate
from repairos.pipeline.run import regions_for
from repairos.slots.detector import Detection
from repairos.slots.vlm import Comparison, CropState
from repairos.slots.vlm_anthropic import (COMPARE_SCHEMA, CROP_SCHEMA, SYSTEM, AnthropicVisionJudge,
                                          resolution_words)
from repairos.vision import inspect as insp
from repairos.vision import sweep as sw
from repairos.vision.crops import Crop
from repairos.vision.scale import PixelScale

W, H = 1400, 1000


def det(box, label="resistor", score=0.5):
    return Detection(box=tuple(float(v) for v in box), label=label, score=score)


def cs(state="ok", visibility="clear", where="", part="circuit board"):
    return CropState(part_class=part, state=state, evidence_text="seen", confidence_word="medium", model="m",
                     latency_ms=1, severity="functional" if state not in ("ok", "cannot_tell") else "none",
                     visibility=visibility, where=where)


# ------------------------------------------------------------------------------------------------ scale from depth
def test_px_per_mm_is_focal_length_over_depth():
    s = PixelScale.constant(15.2, 4032, 3024)        # 12 MP at 20 cm
    assert s.px_per_mm((100, 100, 200, 200)) == pytest.approx(15.2)
    assert s.mm(152, (0, 0, 10, 10)) == pytest.approx(10.0)


def test_scale_comes_from_the_stills_own_depth_grid_and_intrinsics():
    depth = np.full((192, 256), 0.25)
    depth[:, :128] = np.nan                          # no depth on the left half
    still = {"width_px": 4032, "height_px": 3024, "intrinsics": {"fx": 3000.0, "fy": 3000.0, "cx": 2016, "cy": 1512}}
    s = PixelScale.from_still(still, depth)
    assert s.px_per_mm((3000, 1000, 3200, 1200)) == pytest.approx(3000 / 250)
    assert s.px_per_mm((100, 1000, 300, 1200)) is None
    assert PixelScale.from_still(still, None) is None
    assert PixelScale.from_still({**still, "intrinsics": None}, depth) is None
    assert PixelScale.from_still({**still, "pose_reliable": False}, depth) is None   # macro_detail: no scale


def test_small_box_floor_is_in_millimetres_with_a_scale_else_relative():
    tiny = (0, 0, 12, 12)                            # 12 px
    assert insp.too_small(tiny, 4032, 3024, insp.MIN_BOX_SIDE)                       # 1 % of the still is 35 px
    assert not insp.too_small(tiny, 4032, 3024, insp.MIN_BOX_SIDE, lambda b: 10.0)  # 1.2 mm at 10 px/mm
    assert insp.too_small(tiny, 4032, 3024, insp.MIN_BOX_SIDE, lambda b: 15.0)      # 0.8 mm


# ------------------------------------------------------------------------------------------------------- tiles
def test_a_large_box_is_cut_into_a_few_overlapping_square_tiles_that_cover_it():
    box = (200, 300, 1150, 750)                      # a board, about two to one
    assert sw.is_large(box, W, H, None)
    tiles = sw.tile_boxes(box, W, H, None)
    assert 2 <= len(tiles) <= sw.MAX_TILES
    for t in tiles:
        assert t[2] - t[0] == pytest.approx(t[3] - t[1]) and t[0] >= 0 and t[1] >= 0 and t[2] <= W and t[3] <= H
    xs = sorted(tiles)
    assert xs[0][0] <= box[0] and xs[-1][2] >= box[2]
    for a, b in zip(xs, xs[1:]):
        assert b[0] < a[2]                           # neighbours overlap


def test_tiles_are_about_two_centimetres_with_a_scale_and_never_more_than_the_cap():
    box = (0, 0, 2000, 1000)
    tiles = sw.tile_boxes(box, 4032, 3024, 40.0)     # 50 x 25 mm at 40 px/mm
    assert len(tiles) <= sw.MAX_TILES
    assert tiles[0][2] - tiles[0][0] >= sw.TILE_MM * 40.0
    assert not sw.is_large((0, 0, 1000, 600), 4032, 3024, 40.0)      # 25 mm: a part, not swept
    assert sw.is_large((0, 0, 1300, 600), 4032, 3024, 40.0)          # 32 mm


def test_regions_inside_a_tile_are_covered_and_the_large_box_is_swept():
    dets = [det((200, 300, 1150, 750), "circuit board", 0.6), det((300, 400, 360, 440)), det((1300, 900, 1390, 990))]
    sel = insp.select_regions(dets, W, H, budget=6)
    big, tiles, covered = insp.plan_tiles(dets, sel, W, H, 6)
    assert big == 0 and tiles and covered.get(1) is not None and 2 not in covered
    small_budget = insp.plan_tiles(dets, sel, W, H, 2)[1]
    assert len(small_budget) <= 2


def tile_finding(state, where=""):
    crop = Crop(image=None, region_px=(200, 300, 677, 777), scale=1.0, box_px=(200.0, 300.0, 677.0, 777.0))
    return insp.finding_from_tile(scan_id="sc", still_id="s1", k=0, n_tiles=3, crop=crop,
                                  crop_state=cs(state, where=where))


def test_a_tile_finding_names_the_tile_and_says_where_the_fault_is():
    f = validate("finding", tile_finding("burnt", "lower left, on the pad beside the chip"))
    d = f.model_dump(mode="json", exclude_none=True)
    assert d["subject"]["ref"] == "s1/tile0" and d["state"] == "damaged"
    assert any(e.startswith("where in the crop: lower left") for e in d["evidence"])
    assert any("close tile 1 of 3" in e for e in d["evidence"])
    ok = tile_finding("ok", "ignored for ok")
    assert not any(e.startswith("where in the crop") for e in ok["evidence"])


def test_a_covered_region_takes_the_tiles_ok_but_never_a_fault_found_elsewhere_in_it():
    d1 = det((300, 400, 360, 440))
    ok = validate("finding", insp.finding_covered(tile_finding("ok"), scan_id="sc", still_id="s1", index=1, det=d1, k=0))
    assert ok.state.value == "ok" and ok.subject.ref == "s1/det1"
    bad = validate("finding", insp.finding_covered(tile_finding("burnt"), scan_id="sc", still_id="s1", index=1, det=d1,
                                                   k=0))
    assert bad.state.value == "not_inspected" and "fault is reported for the tile" in bad.evidence[0]
    unclear = insp.finding_covered(tile_finding("cannot_tell"), scan_id="sc", still_id="s1", index=1, det=d1, k=0)
    assert unclear["state"] == "insufficient_evidence"


def test_the_swept_box_is_ok_only_when_every_tile_is():
    big = det((200, 300, 1150, 750), "circuit board")
    kw = dict(scan_id="sc", still_id="s1", index=0, det=big)
    assert insp.finding_swept([tile_finding("ok")] * 3, **kw)["state"] == "ok"
    assert insp.finding_swept([tile_finding("ok"), tile_finding("cannot_tell")], **kw)["state"] == \
        "insufficient_evidence"
    assert insp.finding_swept([tile_finding("ok"), tile_finding("burnt")], **kw)["state"] == "not_inspected"


def test_a_tile_region_is_judged_by_the_vlm_in_the_report_regions():
    f = tile_finding("burnt", "middle")
    regions = regions_for([f], [], "whole photo")
    assert regions[f["finding_id"]] == {"part_class": "circuit board", "inspected_by": ["vlm"]}


# ----------------------------------------------------------------------------------------------- look-alikes
def test_lookalikes_are_same_label_same_size_separate_boxes_numbered_in_reading_order():
    dets = [det((100, 100, 120, 160), "pin"), det((140, 100, 160, 158), "pin"), det((180, 101, 200, 161), "pin"),
            det((101, 100, 121, 160), "pin"),                     # a second box on the first pin
            det((220, 100, 260, 200), "pin"),                     # another size
            det((300, 300, 320, 360), "resistor")]
    groups = sw.lookalike_groups(dets, range(len(dets)))
    assert groups[0].label == "pin" and groups[0].members == (0, 1, 2)
    assert all(g.label != "resistor" for g in groups)


def test_the_strip_shows_every_member_at_one_scale():
    from PIL import Image

    still = Image.new("RGB", (400, 300), (40, 90, 160))
    img, scale = sw.strip_image(still, [(100, 100, 120, 160), (140, 100, 160, 158), (180, 101, 200, 161)])
    assert img.width > img.height and 0 < scale


def test_a_lookalike_that_differs_gets_the_fault_and_the_others_are_ok():
    comp = Comparison(odd=(2,), state="damaged", part_class="header pin", evidence_text="cell 2 is bent sideways",
                      confidence_word="high", model="m", latency_ms=1, severity="functional", visibility="clear")
    states = insp.comparison_states(comp, (7, 8, 9))
    assert states[8].state == "damaged" and "differs" in states[8].evidence_text
    assert states[7].state == "ok" and states[9].state == "ok"
    every = insp.comparison_states(Comparison((), "burnt", "pin", "all scorched", "low", "m", 1), (1, 2))
    assert all(v.state == "burnt" for v in every.values())
    unclear = insp.comparison_states(Comparison((), "cannot_tell", "pin", "too blurred", "low", "m", 1), (1, 2))
    assert all(v.state == "cannot_tell" for v in unclear.values())


def test_a_lookalike_fault_on_a_soft_view_goes_through_the_honesty_gate():
    comp = Comparison(odd=(1,), state="damaged", part_class="pin", evidence_text="bent", confidence_word="low",
                      model="m", latency_ms=1, visibility="partial")
    st = insp.comparison_states(comp, (4, 5))[4]
    crop = Crop(image=None, region_px=(0, 0, 50, 50), scale=1.0, box_px=(10.0, 10.0, 40.0, 40.0))
    f = insp.finding_from_crop(scan_id="sc", still_id="s1", index=4, det=det((10, 10, 40, 40), "pin"), crop=crop,
                               crop_state=st)
    assert f["state"] == "insufficient_evidence"


def test_a_strip_is_skipped_when_its_cells_are_too_coarse():
    dets = [det((100, 100, 120, 160), "pin"), det((140, 100, 160, 158), "pin")]
    assert insp.pick_strip(dets, [0, 1], W, H) == (0, 1)
    assert insp.pick_strip(dets, [0, 1], W, H, coarse=lambda region, box, s: True) == ()


# ------------------------------------------------------------------------------------------- pixels per millimetre
def test_too_few_pixels_per_millimetre_is_could_not_judge_with_a_retake_and_no_call():
    region = (0, 0, 400, 400)
    assert insp.crop_px_per_mm(region, region, lambda b: 15.2) == pytest.approx(15.2)
    assert insp.crop_px_per_mm((0, 0, 3136, 3136), region, lambda b: 30.0) == pytest.approx(15.0)  # downsized 2x
    assert insp.crop_px_per_mm(region, region, None) is None
    f = validate("finding", insp.finding_too_coarse(scan_id="sc", still_id="still_a",
                                                    subject={"kind": "part", "ref": "still_a/det3"},
                                                    what="region labelled 'chip'", fid_parts=(3,), region=region))
    assert f.state.value == "insufficient_evidence" and "closer, sharper photo" in f.evidence[0]
    assert not any(ch.isdigit() for ch in f.evidence[0])          # numbers stay out of the words (S5)


# ------------------------------------------------------------------------------------------------------ prompts
def test_the_crop_prompt_judges_the_whole_crop_and_states_the_physics_generally():
    assert "Judge everything Image 1 shows" in SYSTEM and "where says where in Image 1" in SYSTEM
    assert "solder fillets or cut lead stubs mean the part is there, not missing" in SYSTEM
    assert "foreshortened" in SYSTEM and "None of that alone is bent, loose or unplugged" in SYSTEM
    assert "kinks along its length or leans differently from its neighbours" in SYSTEM
    assert "residue around joints and pads" in SYSTEM and "not a fault by itself" in SYSTEM
    assert "evidence that a part belongs there" in SYSTEM and "An area that only looks empty is not missing" in SYSTEM
    assert {"where", "too_small"} <= set(CROP_SCHEMA["required"]) and "odd" in COMPARE_SCHEMA["required"]


def test_the_views_scale_is_stated_as_a_general_instruction_only_with_a_scale():
    assert resolution_words(None) == "" and resolution_words(0) == ""
    w = resolution_words(15.2)
    assert "about 15 pixels per millimetre" in w and "smaller than about 0.2 mm" in w and "too_small true" in w
    j = AnthropicVisionJudge(models=["m"], effort={"m": "low"}, checklists=True)
    assert "pixels per millimetre" in j.crop_request("resistor", None, px_per_mm=6.0)[1]
    assert "pixels per millimetre" not in j.crop_request("resistor", None)[1]


def test_could_not_judge_because_the_detail_is_too_small_asks_for_a_closer_photo_of_that_spot():
    crop = Crop(image=None, region_px=(0, 0, 300, 300), scale=1.0, box_px=(50.0, 50.0, 250.0, 250.0))
    small = CropState(part_class="solder joint", state="cannot_tell", evidence_text="joints too small to see",
                      confidence_word="low", model="m", latency_ms=1, visibility="partial", too_small=True)
    f = validate("finding", insp.finding_from_crop(scan_id="sc", still_id="still_a", index=0, det=det((50, 50, 250, 250)),
                                                   crop=crop, crop_state=small))
    assert f.state.value == "insufficient_evidence"
    assert any("too small for this photo to show" in e and "closer, sharper photo" in e for e in f.evidence)
    assert [getattr(v, "root", v) for v in f.evidence_refs.crop_box_px] == [0, 0, 300, 300]   # the spot to retake


def test_the_hard_floor_is_low_and_a_strip_answer_must_say_what_differs():
    assert insp.MIN_PX_PER_MM <= 6
    assert not insp.names_difference("x") and not insp.names_difference("cells 1, 2")
    assert insp.names_difference("cell 2 pin is bent sideways")


def test_a_tile_request_carries_no_detector_label_and_the_general_checks():
    j = AnthropicVisionJudge(models=["m"], effort={"m": "low"}, checklists=True)
    _, text = j.crop_request("resistor", "passive", view="tile")
    assert "Detector label" not in text and "close tile" in text and "identify the parts yourself" in text
    _, part = j.crop_request("resistor", None)
    assert "Detector label for this region: <data>resistor</data>" in part
