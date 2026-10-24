"""Which detector boxes become regions before any crop is cut (vision.inspect.select_regions), and how the ones not
kept are counted (roll-up findings, I3). Pure: no detector or VLM call."""
from __future__ import annotations

import random

from repairos.contracts import validate
from repairos.pipeline.run import regions_for
from repairos.slots.detector import Detection
from repairos.vision import inspect as insp

W, H = 4032, 3024


def flood(n: int = 90, seed: int = 0) -> list[Detection]:
    """A phone photo of a whole device: a few part-sized boxes and many specks (the shape of sess_blind1)."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        side = rng.choice([30, 40, 50, 300, 500]) if i >= 8 else 400 + 40 * i
        x, y = rng.uniform(0, W - side), rng.uniform(0, H - side)
        out.append(Detection((x, y, x + side, y + side * rng.uniform(0.6, 1.0)), rng.choice(["screw", "connector"]),
                             rng.uniform(0.1, 0.6)))
    return out


def test_box_side_is_relative_to_the_still():
    assert insp.box_side((0, 0, W, H), W, H) == 1.0
    assert abs(insp.box_side((0, 0, 0.1 * W, 0.1 * H), W, H) - 0.1) < 1e-9
    long_thin = (0, 0, 2000, 30)                      # a wire: thin, but its area keeps it
    assert insp.box_side(long_thin, W, H) > insp.MIN_BOX_SIDE


def test_small_boxes_are_dropped_and_the_region_list_is_capped():
    dets = flood()
    sel = insp.select_regions(dets, W, H, budget=12)
    assert set(sel.dropped) == {insp.SMALL_REGIONS, insp.MORE_REGIONS}
    kept = set(sel.ordered) | set(sel.merged)
    dropped = {i for v in sel.dropped.values() for i in v}
    assert kept.isdisjoint(dropped) and kept | dropped | set(sel.scene) == set(range(len(dets)))   # each box once
    assert len(sel.ordered) <= insp.MAX_REGIONS
    assert all(insp.box_side(dets[i].box, W, H) < insp.MIN_BOX_SIDE for i in sel.dropped[insp.SMALL_REGIONS])
    assert all(insp.box_side(dets[i].box, W, H) >= insp.MIN_BOX_SIDE for i in kept)
    scores = [dets[i].score for i in sel.ordered]
    beyond = sel.dropped.get(insp.MORE_REGIONS, [])
    if beyond:   # the kept regions are the best by detector score
        assert min(scores) >= max(dets[i].score for i in beyond if i not in sel.merged)
    assert all(into in sel.ordered for into in sel.merged.values())


def test_the_cap_never_cuts_below_the_inspection_budget():
    dets = flood()
    assert insp.region_cap(48) == 48 and insp.region_cap(4) == insp.MAX_REGIONS
    assert len(insp.select_regions(dets, W, H, budget=48).ordered) > insp.MAX_REGIONS


def test_without_the_floor_and_cap_nothing_is_dropped():
    dets = flood()
    sel = insp.select_regions(dets, W, H, budget=12, min_box_side=0.0, max_regions=10_000)
    assert sel.dropped == {}


def test_regions_not_kept_are_one_not_inspected_rollup_per_reason_with_the_exact_count():
    dets = flood()
    sel = insp.select_regions(dets, W, H, budget=12)
    for what, idx in sel.dropped.items():
        f = validate("finding", insp.finding_rollup(scan_id="scan_x", still_id="s1", what=what, indices=idx,
                                                    dets=dets)).model_dump(mode="json", exclude_none=True)
        assert f["state"] == "not_inspected" and f["subject"]["ref"] == f"s1/{what}"   # never ok (I3)
        assert f["evidence"][0].startswith(f"{len(idx)} ")
        out = regions_for([f], [], "whole photo",
                          {f["finding_id"]: {"part_class": insp.ROLLUP_WORDS[what][0], "regions": len(idx)}})
        assert out[f["finding_id"]] == {"part_class": insp.ROLLUP_WORDS[what][0], "inspected_by": ["detector"],
                                        "regions": len(idx)}


def test_a_single_dropped_region_reads_in_the_singular():
    dets = [Detection((0, 0, 10, 10), "screw", 0.3)]
    f = insp.finding_rollup(scan_id="scan_x", still_id="s1", what=insp.SMALL_REGIONS, indices=[0], dets=dets)
    assert f["evidence"][0].startswith("1 small region in still s1 was detected but not inspected: ")
