"""CLS step in vision/inspect.py (task M7): routing, findings in words, honesty states (I1, I3, I4, S5).
The live classifier is covered by backend/tests/conformance/cls (real images, -m vision)."""
from __future__ import annotations

import re

import pytest

from repairos.contracts import validate
from repairos.slots import classifier as C
from repairos.slots.detector import Detection
from repairos.slots.vlm import CropState
from repairos.vision import generic_classes as gc
from repairos.vision import inspect as insp
from repairos.vision.crops import Crop

DET = Detection(box=(300, 400, 400, 500), label="electrolytic capacitor", score=0.41)
CROP = Crop(image=None, region_px=(100, 200, 612, 712), scale=1.0, box_px=DET.box, still_id="s1")


def score(p: float, kind: str = "burnt or melted", pk: float = 0.8) -> C.DefectScore:
    return C.DefectScore(p_defect=p, defect_class=kind, p_class=pk, model="m")


def test_defect_classes_are_generic_and_map_to_crop_states():
    assert len(set(gc.DEFECT_CLASSES)) == len(gc.DEFECT_CLASSES)
    for name in gc.DEFECT_CLASSES:
        assert name == name.lower().strip() and not re.search(r"\d", name), name
    assert set(gc.DEFECT_TO_CROP_STATE) == set(gc.DEFECT_CLASSES)
    assert set(gc.DEFECT_TO_CROP_STATE.values()) <= set(gc.CROP_STATES) - {"ok", "cannot_tell"}


def test_score_is_validated():
    with pytest.raises(ValueError):
        C.DefectScore(p_defect=1.2, defect_class=None, p_class=0.0, model="m")
    with pytest.raises(ValueError):
        C.DefectScore(p_defect=0.5, defect_class="exploded toaster", p_class=0.5, model="m")


@pytest.mark.parametrize("p,want", [(0.01, C.OK), (0.2, C.UNSURE), (0.5, C.FLAGGED), (0.99, C.FLAGGED)])
def test_route(p, want):
    assert C.route(score(p), ok_below=0.05, flag_at=0.5) == want


def test_vlm_queue_flagged_first_then_unsure_most_likely_first_ok_never():
    scores = {0: score(0.01), 1: score(0.3), 2: score(0.9), 3: score(0.6), 4: score(0.45)}
    queue, routes = insp.vlm_queue(scores, ok_below=0.05, flag_at=0.5)
    assert queue == [2, 3, 4, 1] and routes[0] == C.OK


@pytest.mark.parametrize("p", [0.0, 0.03, 0.3, 0.5, 0.77, 0.97, 1.0])
def test_classifier_words_have_no_numbers(p):
    s = score(p)
    assert not re.search(r"\d", C.evidence_words(s)) and C.confidence_word(p) in gc.CONFIDENCE_WORDS


def test_confident_ok_is_ok_on_the_classifiers_word_never_proof():
    f = validate("finding", insp.finding_from_classifier(scan_id="sc", still_id="s1", index=0, det=DET, crop=CROP,
                                                          score=score(0.01), reason="the classifier was confident"))
    assert f.state.value == "ok" and f.is_proof is False and f.source.value == "repairos.vision"
    assert any("defect classifier" in e for e in f.evidence) and any("not checked by the vision model" in e
                                                                      for e in f.evidence)
    assert not any(re.search(r"\d", e) for e in f.evidence[1:])


@pytest.mark.parametrize("kind", gc.DEFECT_CLASSES)
def test_flagged_over_budget_is_a_suspect_of_its_kind(kind):
    f = validate("finding", insp.finding_from_classifier(scan_id="sc", still_id="s1", index=0, det=DET, crop=CROP,
                                                          score=score(0.9, kind), reason="over budget"))
    assert f.state.value == insp.STATE_TO_FINDING[gc.DEFECT_TO_CROP_STATE[kind]] and not f.is_proof


def test_unsure_without_vlm_is_insufficient_evidence_with_a_reason():
    f = validate("finding", insp.finding_classifier_unsure(scan_id="sc", still_id="s1", index=0, det=DET, crop=CROP,
                                                            score=score(0.3), reason="the budget was used up"))
    assert f.state.value == "insufficient_evidence" and "unsure" in f.evidence[0] and f.confidence == 0


def test_vlm_settles_and_disagreement_lowers_confidence():
    cs = CropState(part_class="capacitor", state="ok", evidence_text="looks intact", confidence_word="high",
                   model="m", latency_ms=1)
    f = validate("finding", insp.finding_from_crop(scan_id="sc", still_id="s1", index=0, det=DET, crop=CROP,
                                                   crop_state=cs, defect_score=score(0.9)))
    assert f.state.value == "ok" and f.confidence == insp.CONFIDENCE["low"]
    assert "disagree" in " ".join(f.evidence) and "defect classifier" in " ".join(f.evidence)
    agree = validate("finding", insp.finding_from_crop(scan_id="sc", still_id="s1", index=0, det=DET, crop=CROP,
                                                       crop_state=cs, defect_score=score(0.02)))
    assert agree.confidence == insp.CONFIDENCE["high"] and "disagree" not in " ".join(agree.evidence)


def test_classifier_off_and_configured_none():
    assert insp.resolve_classifier(False) is None
    from repairos.registry import default_registry

    if default_registry().slots.get("CLS", "none") == "none":
        assert insp.resolve_classifier(None) is None


def test_inspect_signature_is_backward_compatible():
    import inspect as pyinspect

    params = pyinspect.signature(insp.inspect_still).parameters
    old = ["image", "still_id", "scan_id", "detector", "judge", "prompts", "min_score", "max_crops", "with_context",
           "crop_dir", "workers", "sharpness"]
    assert list(params)[: len(old)] == old
    assert params["classifier"].default is None and params["max_classified"].default == 48


def test_unfamiliar_crop_abstains_goes_to_the_vlm_and_never_counts_as_ok():
    odd = C.DefectScore(p_defect=0.01, defect_class="solder fault", p_class=0.9, model="m",
                        abstain_reason=C.UNFAMILIAR)
    assert C.route(odd, ok_below=0.05, flag_at=0.5) == C.ABSTAIN
    assert C.evidence_words(odd) == f"defect classifier: no answer, {C.UNFAMILIAR}"
    queue, routes = insp.vlm_queue({0: score(0.3), 1: odd, 2: score(0.9), 3: score(0.01)}, ok_below=0.05,
                                   flag_at=0.5)
    assert queue == [2, 1, 0] and routes[1] == C.ABSTAIN and 3 not in queue
    f = validate("finding", insp.finding_classifier_unsure(scan_id="sc", still_id="s1", index=1, det=DET, crop=CROP,
                                                            score=odd, reason="the budget was used up"))
    assert f.state.value == "insufficient_evidence" and C.UNFAMILIAR in f.evidence[0]
    cs = CropState(part_class="capacitor", state="damaged", evidence_text="vent bulged", confidence_word="high",
                   model="m", latency_ms=1)
    g = validate("finding", insp.finding_from_crop(scan_id="sc", still_id="s1", index=1, det=DET, crop=CROP,
                                                   crop_state=cs, defect_score=odd))
    assert g.state.value == "damaged" and g.confidence == insp.CONFIDENCE["high"]
    assert "disagree" not in " ".join(g.evidence) and C.UNFAMILIAR in " ".join(g.evidence)
