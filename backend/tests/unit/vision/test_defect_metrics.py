"""Benchmark metrics (eval/defects/metrics.py) and the committed CLS=dinov2 head file (task M7)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "eval" / "defects"))
import metrics as M  # noqa: E402

sys.path.pop(0)


def test_auroc_known_values():
    pytest.importorskip("numpy")
    assert M.auroc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0
    assert M.auroc([0, 0, 1, 1], [0.9, 0.8, 0.2, 0.1]) == 0.0
    assert M.auroc([0, 1], [0.5, 0.5]) == 0.5
    assert M.auroc([1, 1], [0.1, 0.2]) is None
    assert M.auroc([0, 0, 1, 1], [0.1, 0.4, 0.35, 0.8]) == 0.75


def test_ece_perfect_and_off():
    pytest.importorskip("numpy")
    assert M.ece([0, 1], [0.0, 1.0]) == 0.0
    assert abs(M.ece([0, 0, 0, 0], [0.9, 0.9, 0.9, 0.9]) - 0.9) < 1e-9


def test_prf_counts():
    r = M.prf([1, 1, 0, 0, 1], [1, 0, 1, 0, 1])
    assert (r["tp"], r["fp"], r["fn"], r["tn"]) == (2, 1, 1, 1)
    assert r["precision"] == r["recall"] == pytest.approx(2 / 3)
    assert M.prf([0, 0], [0, 0])["precision"] is None


def test_committed_head_matches_the_slot():
    head_dir = ROOT / "training" / "models" / "cls_dinov2"
    if not (head_dir / "head.json").exists():
        pytest.skip("no trained head: backend/.venv/bin/python training/defects/train.py all")
    np = pytest.importorskip("numpy")
    from repairos.slots.cls_dinov2 import MODEL_REVISION, Head
    from repairos.vision.generic_classes import DEFECT_CLASSES

    meta = json.loads((head_dir / "head.json").read_text())
    assert meta["backbone"]["revision"] == MODEL_REVISION and meta["classes"] == list(DEFECT_CLASSES)
    assert 0 < meta["thresholds"]["ok_below"] < meta["thresholds"]["flag_at"] <= 1
    assert meta["data"]["splits_used"] == ["train", "val"]  # test and held-out never fitted
    head = Head(head_dir)
    x = np.zeros((2, head.w["mean"].shape[0]), dtype=np.float32) + head.w["mean"]
    p_def, p_kind = head.predict(x)
    assert p_def.shape == (2,) and p_kind.shape == (2, len(DEFECT_CLASSES))
    assert ((0 <= p_def) & (p_def <= 1)).all() and np.allclose(p_kind.sum(1), 1, atol=1e-5)


def test_committed_head_abstains_by_a_val_cutoff():
    head_dir = ROOT / "training" / "models" / "cls_dinov2"
    if not (head_dir / "head.json").exists():
        pytest.skip("no trained head: backend/.venv/bin/python training/defects/train.py all")
    np = pytest.importorskip("numpy")
    from repairos.slots.cls_dinov2 import Head

    head = Head(head_dir)
    ood = head.meta["ood"]
    assert ood["cutoff"] > 0 and 0 < ood["val_abstain_share"] <= 0.05 and "val" in ood["rule"]
    centre = (head.w["ood_centre"] * head.w["std"] + head.w["mean"])[None, :]  # the train centre: familiar
    far = centre + 50 * head.w["std"][None, :]                                # far from every train crop
    assert not head.unfamiliar(centre)[0] and head.unfamiliar(far)[0]
    assert np.isfinite(head.ood_distance(centre)).all()
