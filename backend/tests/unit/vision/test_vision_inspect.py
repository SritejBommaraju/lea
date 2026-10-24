"""Findings from vision are suspects (I1), unseen is not_inspected (I3), cannot_tell is insufficient_evidence (I4).
These test the mapping to the contract; the live VLM path is covered by backend/tests/conformance/vlm."""
from __future__ import annotations

import pytest

from repairos.contracts import validate
from repairos.slots.detector import Detection
from repairos.slots.vlm import CropState
from repairos.vision import inspect as insp
from repairos.vision.crops import Crop
from repairos.vision.generic_classes import CROP_STATES


def crop() -> Crop:
    return Crop(image=None, region_px=(100, 200, 612, 712), scale=1.0, box_px=(300, 400, 400, 500), still_id="s1")


@pytest.mark.parametrize("state", CROP_STATES)
def test_every_crop_state_maps_to_a_valid_suspect(state):
    det = Detection(box=(300, 400, 400, 500), label="connector", score=0.42)
    cs = CropState(part_class="connector", state=state, evidence_text="seen", confidence_word="medium",
                   model="m", latency_ms=1)
    f = validate("finding", insp.finding_from_crop(scan_id="scan_x", still_id="s1", index=3, det=det, crop=crop(),
                                                   crop_state=cs, sharpness=12.0))
    assert f.is_proof is False and f.source.value == "repairos.vision"
    assert f.state.value == insp.STATE_TO_FINDING[state]
    assert [getattr(v, "root", v) for v in f.evidence_refs.crop_box_px] == [100, 200, 612, 712]
    if state == "cannot_tell":
        assert f.state.value == "insufficient_evidence" and f.vlm_answer.value == "cannot_tell"
    assert not any(ch.isdigit() for ch in f.evidence[2])  # detector score in words (S5)


def test_wire_classes_become_wire_subjects():
    det = Detection(box=(1, 1, 5, 5), label="ribbon cable", score=0.3)
    f = insp.finding_not_inspected(scan_id="scan_x", still_id="s2", index=0, det=det, reason="budget")
    assert validate("finding", f).subject.kind.value == "wire"


def test_not_inspected_is_never_ok():
    det = Detection(box=(-3, 1, 5, 5), label="fuse", score=0.9)
    f = validate("finding", insp.finding_not_inspected(scan_id="scan_x", still_id="s1", index=0, det=det, reason="r"))
    assert f.state.value == "not_inspected" and f.confidence == 0 and not f.is_proof


def test_nothing_detected_is_insufficient_evidence():
    f = validate("finding", insp.finding_nothing_detected(scan_id="scan_x", still_id="s1"))
    assert f.state.value == "insufficient_evidence"


def test_inspection_order_is_by_score():
    dets = [Detection((0, 0, 1, 1), "a", 0.2), Detection((0, 0, 1, 1), "b", 0.9), Detection((0, 0, 1, 1), "c", 0.5)]
    assert insp.order_for_inspection(dets) == [1, 2, 0]
