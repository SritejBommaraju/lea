"""Metrics for the defect benchmark (numpy only): AUROC, expected calibration error, precision/recall."""
from __future__ import annotations

from collections.abc import Sequence


def auroc(y: Sequence[int], score: Sequence[float]) -> float | None:
    """Area under the ROC curve (ties count one half). None when only one class is present."""
    import numpy as np

    y = np.asarray(y, dtype=int)
    s = np.asarray(score, dtype=float)
    pos, neg = s[y == 1], s[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    order = np.argsort(np.concatenate([pos, neg]), kind="mergesort")
    allv = np.concatenate([pos, neg])[order]
    ranks = np.empty(len(allv))
    i = 0
    while i < len(allv):  # average ranks over ties
        j = i
        while j + 1 < len(allv) and allv[j + 1] == allv[i]:
            j += 1
        ranks[i:j + 1] = (i + j) / 2 + 1
        i = j + 1
    r = np.empty(len(allv))
    r[order] = ranks
    return float((r[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def ece(y: Sequence[int], p: Sequence[float], bins: int = 10) -> float | None:
    """Expected calibration error of p(defect) over equal-width bins."""
    import numpy as np

    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    if len(y) == 0:
        return None
    idx = np.minimum((p * bins).astype(int), bins - 1)
    return float(sum(abs(y[idx == b].mean() - p[idx == b].mean()) * (idx == b).sum()
                     for b in range(bins) if (idx == b).any()) / len(y))


def prf(y_true: Sequence[int], y_pred: Sequence[int]) -> dict:
    tp = sum(1 for t, q in zip(y_true, y_pred) if t and q)
    fp = sum(1 for t, q in zip(y_true, y_pred) if not t and q)
    fn = sum(1 for t, q in zip(y_true, y_pred) if t and not q)
    tn = sum(1 for t, q in zip(y_true, y_pred) if not t and not q)
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else None
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": prec, "recall": rec, "f1": f1,
            "specificity": tn / (tn + fp) if tn + fp else None}


def per_class(y_true: Sequence[str], y_pred: Sequence[str], classes: Sequence[str]) -> dict:
    """Precision and recall per class for a single-label prediction."""
    out = {}
    for c in classes:
        out[c] = prf([t == c for t in y_true], [q == c for q in y_pred]) | {"support": sum(t == c for t in y_true)}
    acc = sum(t == q for t, q in zip(y_true, y_pred)) / len(y_true) if y_true else None
    return {"accuracy": acc, "classes": out}
