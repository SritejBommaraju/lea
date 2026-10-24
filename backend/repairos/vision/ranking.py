"""Which detected regions the VLM looks at first, and which regions one look covers.

The crop budget (12 VLM crops per still) goes to regions in rank order. Two things are here:
- **Merging (on):** regions whose crops would show nearly the same pixels (crop IoU >= MERGE_IOU) are merged, the
  best-ranked one kept, so the budget is not spent on the same part twice. Measured: 25-30 % fewer VLM crops at the
  same share of defects inside the inspected crops.
- **Defect-likelihood order (built, off):** the detector's own damage evidence for each box (its best score over the
  DAMAGE_CLASSES prompts, `Detection.damage_score`) plus a small failure prior per part family
  (vision/checklists.py). It was the research hypothesis for "the budget is spent on confident parts, not likely
  defects", but on held-out stills it did not put more defects inside the budget than the detector-score order
  (at 12 crops, 94 % of Wikimedia defects and every pcb4 defect the detector boxes at all are already inside), so the
  default stays `RANK_BY = "score"`. With a defect classifier, its p(defect) decides the VLM order
  (`vision.inspect.vlm_queue`).
Weights were chosen on non-held-out stills and measured once on held-out ones (eval/defects/QUICKWINS.md). Scores
here are ordering keys only; no number reaches a finding (S5).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from repairos.vision.checklists import family_for, prior_of
from repairos.vision.generic_classes import DAMAGE_CLASSES
from repairos.vision.tiling import iou

RANK_BY = "score"
MERGE_IOU = 0.5  # crop regions overlapping this much show the same part: one VLM look covers both
PRIOR_BONUS: dict[str, float] = {"high": 0.1, "medium": 0.0, "low": -0.1}

_DAMAGE = frozenset(DAMAGE_CLASSES)


def damage_evidence(det: Any) -> float:
    """The detector's damage evidence for one box: its damage-prompt score, else its score when its label is a
    damage word, else 0."""
    d = getattr(det, "damage_score", None)
    own = det.score if det.label in _DAMAGE else 0.0
    return max(own, d) if d is not None else own


def defect_likelihood(det: Any, prior_bonus: dict[str, float] | None = None) -> float:
    bonus = PRIOR_BONUS if prior_bonus is None else prior_bonus
    return damage_evidence(det) + bonus.get(prior_of(family_for(det.label)), 0.0)


def rank_regions(dets: Sequence[Any], indices: Sequence[int], regions: dict[int, tuple[int, int, int, int]], *,
                 by: str = RANK_BY, merge_iou: float | None = MERGE_IOU,
                 prior_bonus: dict[str, float] | None = None) -> tuple[list[int], dict[int, int]]:
    """(`indices` in inspection order, merged index -> the kept index whose crop covers it).

    `regions[i]` is the crop region of detection i (still pixels). `by`: "likelihood" (default) or "score" (the old
    detector-confidence order). Merging is greedy in rank order: a region whose crop overlaps an already kept crop by
    `merge_iou` or more is merged into it (None = no merging). Ties keep the detector-score order."""
    if by == "score":
        key = {i: (-dets[i].score, i) for i in indices}
    elif by == "likelihood":
        key = {i: (-defect_likelihood(dets[i], prior_bonus), -dets[i].score, i) for i in indices}
    else:
        raise ValueError(f"unknown ranking {by!r}")
    ordered = sorted(indices, key=key.__getitem__)
    if merge_iou is None:
        return ordered, {}
    kept: list[int] = []
    merged: dict[int, int] = {}
    for i in ordered:
        into = next((k for k in kept if iou(regions[k], regions[i]) >= merge_iou), None)
        if into is None:
            kept.append(i)
        else:
            merged[i] = into
    return kept, merged
