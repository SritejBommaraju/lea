"""CLS conformance (task M7) on real held-out images (VisA pcb4, never trained on). Same assertions for every
implementation: scores in range, kinds from the generic vocabulary, deterministic, order-independent, and defect
crops scored above the same place on normal boards. inspect.py with the classifier keeps I1/I3/I4."""
from __future__ import annotations

from pathlib import Path

import pytest

from repairos.contracts import validate
from repairos.slots.classifier import DefectScore
from repairos.vision.crops import cut, load_still
from repairos.vision.generic_classes import DEFECT_CLASSES

pytestmark = pytest.mark.vision
VISA = Path(__file__).resolve().parents[4] / "data" / "datasets" / "visa" / "raw"


def _defect_and_normal_crops(rows, n: int = 12):
    import numpy as np
    from PIL import Image

    anomalies = [r for r in rows if r["label"] != "normal" and r["mask"]][:n]
    normals = [r for r in rows if r["label"] == "normal"][:n]
    defect, normal = [], []
    for a, b in zip(anomalies, normals):
        mask = np.asarray(Image.open(VISA / a["mask"])) > 0
        ys, xs = np.nonzero(mask)
        box = (float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1))
        defect.append(cut(load_still(VISA / a["image"]), box).image)
        normal.append(cut(load_still(VISA / b["image"]), box).image)  # the same place on a normal board
    return defect, normal


def test_scores_kinds_and_determinism(cls_impl, heldout_rows):
    defect, normal = _defect_and_normal_crops(heldout_rows, 4)
    crops = defect + normal
    a = cls_impl.classify(crops)
    assert len(a) == len(crops) and all(isinstance(s, DefectScore) for s in a)
    assert all(0 <= s.p_defect <= 1 and s.defect_class in DEFECT_CLASSES and 0 <= s.p_class <= 1 for s in a)
    b = cls_impl.classify(list(reversed(crops)))
    assert [round(s.p_defect, 3) for s in a] == [round(s.p_defect, 3) for s in reversed(b)]
    assert cls_impl.classify([]) == []
    assert 0 < cls_impl.ok_below < cls_impl.flag_at <= 1 and cls_impl.model_id


def test_held_out_defects_rank_above_normal_boards(cls_impl, heldout_rows):
    defect, normal = _defect_and_normal_crops(heldout_rows)
    pd = [s.p_defect for s in cls_impl.classify(defect)]
    pn = [s.p_defect for s in cls_impl.classify(normal)]
    pairs = sum((x > y) + 0.5 * (x == y) for x in pd for y in pn) / (len(pd) * len(pn))
    assert pairs >= 0.7, f"pairwise ranking {pairs:.2f} on held-out pcb4"


def test_inspect_with_classifier_keeps_honesty(cls_impl, heldout_rows):
    from repairos.registry import Registry
    from repairos.vision.inspect import inspect_still

    reg = Registry()
    det = reg.load("DET", reg.selected("DET"))
    judge = reg.load("VLM", reg.selected("VLM"))
    row = next(r for r in heldout_rows if r["label"] != "normal")
    res = inspect_still(VISA / row["image"], still_id="pcb4_conformance", scan_id="conformance", detector=det,
                        judge=judge, classifier=cls_impl, max_crops=2, max_classified=16)
    assert res.findings and res.defect_scores and set(res.routes) == set(res.defect_scores)
    for f in res.findings:
        f = validate("finding", f.model_dump(mode="json", exclude_none=True))
        assert f.is_proof is False
        if f.state.value == "insufficient_evidence":
            assert f.evidence and f.evidence[0]
    vlm_seen = sum(1 for f in res.findings if any(e.startswith("vision model (") for e in f.evidence))
    assert vlm_seen <= 2
