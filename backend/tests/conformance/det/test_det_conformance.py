"""DET conformance (task M1) on recorded REAL stills only. Same assertions for every implementation:
    pytest -m vision backend/tests/conformance/det [--impl gdino|owlv2]
Recall at expected locations needs human box labels (samples/sessions/*/stills/<id>.boxes.json:
[{"box": [x0, y0, x1, y1], "class": <generic class>}]); until they exist that test skips with HUMAN ACTION NEEDED."""
from __future__ import annotations

import json
import time

import pytest

from repairos.vision.crops import load_still
from repairos.vision.prompts import build_prompts
from repairos.vision.tiling import iou

pytestmark = pytest.mark.vision


def test_boxes_are_full_resolution_in_bounds_and_from_the_prompts(det_impl, stills):
    prompts = build_prompts()
    for path in stills:
        im = load_still(path)
        t = time.perf_counter()
        dets = det_impl.detect(im, prompts)
        dt = time.perf_counter() - t
        assert dt < 120, f"{path.name}: {dt:.0f} s"
        for d in dets:
            x0, y0, x1, y1 = d.box
            assert 0 <= x0 < x1 <= im.width and 0 <= y0 < y1 <= im.height, d
            assert d.label in prompts and 0 <= d.score <= 1
        assert det_impl.model_id


def test_deterministic(det_impl, stills):
    im = load_still(stills[0])
    assert det_impl.detect(im) == det_impl.detect(im)


def test_min_score_respected(det_impl, stills):
    im = load_still(stills[0])
    assert all(d.score >= 0.5 for d in det_impl.detect(im, min_score=0.5))


def test_recall_at_labelled_locations(det_impl, stills):
    labelled = [(p, p.with_suffix(".boxes.json")) for p in stills if p.with_suffix(".boxes.json").exists()]
    if not labelled:
        pytest.skip("HUMAN ACTION NEEDED: label part boxes on real stills (samples/sessions/*/stills/<id>.boxes.json)")
    hit = total = 0
    for path, lab in labelled:
        dets = det_impl.detect(load_still(path))
        for row in json.loads(lab.read_text()):
            total += 1
            hit += any(iou(tuple(row["box"]), d.box) >= 0.3 for d in dets)
    assert total and hit / total >= 0.6, f"recall {hit}/{total}"
