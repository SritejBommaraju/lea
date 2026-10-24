"""CLS slot interface (task M7): is there a visible defect in this crop, and of which kind.

Implementations live in repairos.slots.cls_<impl>/ and register with `@register("CLS", "<impl>")`; heavy imports
(torch, weights) happen on first use. Input crops are the runtime crops from `vision.crops.cut` (full resolution
around a detector box). Output is a calibrated probability per crop plus the most likely kind from
DEFECT_CLASSES (vision/generic_classes.py, GEN1).

How the pipeline uses it (vision/inspect.py): the classifier sees every crop; crops it flags or is unsure about go
to the VLM, which settles them and writes the explanation; a crop it is confident about without a defect is
reported `ok` on the classifier's word (a model did look at it, I3), with the confidence in words. The two
thresholds come with the trained head and are chosen on its validation split, never on held-out products. A
classifier answer is a suspect, never proof (I1).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from repairos.vision.generic_classes import DEFECT_CLASSES

OK, UNSURE, FLAGGED, ABSTAIN = "ok", "unsure", "flagged", "abstain"
UNFAMILIAR = "unfamiliar part or image type"


@dataclass(frozen=True)
class DefectScore:
    p_defect: float               # calibrated probability that a defect is visible in the crop
    defect_class: str | None      # most likely kind from DEFECT_CLASSES, given a defect
    p_class: float                # p(defect_class | defect)
    model: str                    # backbone and head that produced the score
    abstain_reason: str | None = None  # set when the classifier gives no answer (e.g. UNFAMILIAR); p_* are void

    @property
    def abstained(self) -> bool:
        return self.abstain_reason is not None

    def __post_init__(self):
        if not 0.0 <= self.p_defect <= 1.0:
            raise ValueError(f"p_defect {self.p_defect} outside [0, 1]")
        if self.defect_class is not None and self.defect_class not in DEFECT_CLASSES:
            raise ValueError(f"defect class {self.defect_class!r} not in DEFECT_CLASSES")


class DefectClassifier(Protocol):
    model_id: str
    ok_below: float   # p_defect below this: no visible defect, confident enough to skip the VLM
    flag_at: float    # p_defect at or above this: flagged as a likely defect

    def classify(self, crops: Sequence[Any]) -> list[DefectScore]:
        """One score per crop (PIL images from `vision.crops.cut`), same order."""
        ...


def route(score: DefectScore, ok_below: float, flag_at: float) -> str:
    """OK (classifier answers, VLM skipped), UNSURE (VLM settles it), FLAGGED (VLM explains it) or ABSTAIN (the
    crop is unlike the training data: no classifier answer, the VLM decides)."""
    if score.abstained:
        return ABSTAIN
    if score.p_defect >= flag_at:
        return FLAGGED
    return OK if score.p_defect < ok_below else UNSURE


def likelihood_word(p: float) -> str:
    """A probability in words (S5: numbers stay out of evidence and model prompts)."""
    for bound, word in ((0.95, "very likely"), (0.75, "likely"), (0.5, "possibly"), (0.25, "unlikely")):
        if p >= bound:
            return word
    return "very unlikely"


def confidence_word(p_defect: float) -> str:
    """How sure the classifier is of its yes/no answer, as one of CONFIDENCE_WORDS."""
    m = max(p_defect, 1.0 - p_defect)
    return "high" if m >= 0.95 else "medium" if m >= 0.8 else "low"


def evidence_words(score: DefectScore) -> str:
    """The classifier's answer as one evidence sentence, words only."""
    if score.abstained:
        return f"defect classifier: no answer, {score.abstain_reason}"
    head = f"defect classifier: a visible defect is {likelihood_word(score.p_defect)}"
    if score.defect_class and score.p_defect >= 0.5:
        kind = score.defect_class if score.p_class >= 0.5 else f"perhaps {score.defect_class}"
        head += f", kind {kind}"
    return head
