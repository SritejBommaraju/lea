"""VLM slot interface (task M3). Implementations live in repairos.slots.vlm_<impl>.

Two questions, both product-agnostic:
- `inspect_crop(crop, context=None, part_hint=None, family=None) -> CropState`: what part is this and what state is it
  in, one of CROP_STATES (vision/generic_classes.py), with a short evidence sentence. Phase 1 (no reference).
- `judge_crop(crop, render_crop, expectation, options) -> Judgement`: pick exactly one of `options` (Phase 2 crop pair,
  Phase 2 with a render; `render_crop` may be None). `cannot_tell` is always added (I4).

Escalation policy (here, the slot wrapper): the configured models in order (config/vlm.yaml), the next only on
`cannot_tell` (Q3).
Prompt-injection boundary (A9): hints and expectation words (BOM, OCR, detector labels) go into a quoted data block,
never into instructions. Never ask counts or pin numbers. Answers are suspects, never proof (I1).
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from repairos.vision.generic_classes import CONFIDENCE_WORDS, CROP_STATES

CANNOT_TELL = "cannot_tell"
assert CANNOT_TELL in CROP_STATES
SEVERITIES = ("none", "cosmetic", "functional", "safety")
# How clearly the crop shows the place the state is about (the VLM's own word; code decides what it allows):
# clear = sharp and large enough to see the detail the state depends on; partial = visible but soft, small or partly
# hidden; poor = blurred, too small, too dark or hidden. "" = not reported (an older recorded answer).
VISIBILITY = ("clear", "partial", "poor")


@dataclass(frozen=True)
class CropState:
    part_class: str          # the VLM's own generic name for the main part in the crop
    state: str               # one of CROP_STATES
    evidence_text: str       # one or two short sentences: what in the image supports the state
    confidence_word: str     # low | medium | high (words, I6)
    model: str               # model that gave the final answer
    latency_ms: int          # total, including escalation
    escalated: bool = False  # True when the small model said cannot_tell and the larger model answered
    attempts: tuple[dict, ...] = field(default_factory=tuple)  # every model's raw answer, for the record
    severity: str = "none"   # one of SEVERITIES: how much a fault would matter; none = ok or cannot_tell
    part_words: str = ""     # a few plain words to find the part in the photo (colour, what it connects to, where)
    visibility: str = ""     # one of VISIBILITY, or "" when the answer did not report it
    open_for_access: bool = False  # what the state describes is only the device being open (cover off, panel open,
                                   # wiring exposed), with nothing broken: the normal state of an inspected device
    where: str = ""          # plain words: where in the crop the fault is ("" for ok, cannot_tell or older answers)
    too_small: bool = False  # cannot_tell only because the detail is too small for this view: a closer photo settles it


@dataclass(frozen=True)
class Comparison:
    """`compare_parts`: which of several look-alike parts shown side by side in one numbered strip differ (a fault)."""
    odd: tuple[int, ...]     # 1-based cell numbers that differ from the rest (all of them when all share one fault)
    state: str               # one of CROP_STATES: the fault of the odd cells; ok = alike and fine; cannot_tell
    part_class: str          # the VLM's generic name for the look-alike parts
    evidence_text: str       # the difference it sees, in words
    confidence_word: str
    model: str
    latency_ms: int
    severity: str = "none"
    visibility: str = ""
    attempts: tuple[dict, ...] = field(default_factory=tuple)
    escalated: bool = False
    open_for_access: bool = False


@dataclass(frozen=True)
class Judgement:
    choice: str              # one of the options given, or cannot_tell
    confidence_word: str
    evidence_text: str
    model: str
    latency_ms: int
    escalated: bool = False
    attempts: tuple[dict, ...] = field(default_factory=tuple)


class VisionJudge(Protocol):
    models: tuple[str, ...]  # escalation ladder, small first

    def inspect_crop(self, crop: Any, context: Any | None = None, part_hint: str | None = None,
                     family: str | None = None, view: str = "part", px_per_mm: float | None = None) -> CropState:
        """`crop`: PIL image cut from the full-resolution still. `context`: optional downsized whole still with the
        crop's box outlined. `part_hint`: the detector's label (data, not instruction). `family`: the part family
        whose failure checklist to apply (vision/checklists.py; `unknown_part` = the general checks only), None =
        the family the label suggests. `view`: "part" (a crop around one detected region) or "tile" (one close tile of
        a larger object). Either way the whole crop is judged: a fault anywhere in it is the answer, with `where`.
        `px_per_mm`: how finely the crop resolves the surface (from depth), stated in the request so claims about
        details too small for the view are cannot_tell (`too_small`); None without a scale."""
        ...

    def compare_parts(self, strip: Any, context: Any | None, count: int, part_hint: str | None = None,
                      px_per_mm: float | None = None) -> Comparison:
        """`strip`: `count` look-alike parts side by side in numbered cells (vision/sweep.strip_image). Which differ?"""
        ...

    def judge_crop(self, crop: Any, render_crop: Any | None, expectation: str, options: Sequence[str]) -> Judgement:
        ...


def with_cannot_tell(options: Sequence[str]) -> list[str]:
    out = [o for o in dict.fromkeys(options) if o]
    if CANNOT_TELL not in out:
        out.append(CANNOT_TELL)
    return out


T = TypeVar("T")


def escalate(models: Sequence[str], ask: Callable[[str], T], answer_of: Callable[[T], str]) -> tuple[T, int]:
    """Ask each model in turn; stop at the first answer that is not cannot_tell. Returns (last result, index)."""
    if not models:
        raise ValueError("no models to ask")
    result = None
    for i, m in enumerate(models):
        result = ask(m)
        if answer_of(result) != CANNOT_TELL:
            return result, i
    return result, len(models) - 1


def check_confidence_word(word: str) -> str:
    if word not in CONFIDENCE_WORDS:
        raise ValueError(f"confidence word {word!r} not in {CONFIDENCE_WORDS}")
    return word
