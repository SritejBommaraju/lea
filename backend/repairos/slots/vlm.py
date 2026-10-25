"""VLM slot interface (task M3). Implementations live in repairos.slots.vlm_<impl>.

Questions, all product-agnostic:
- `inspect_crop(crop, context=None, part_hint=None, family=None) -> CropState`: what part is this and what state is it
  in, one of CROP_STATES (vision/generic_classes.py), with a short evidence sentence. Phase 1 (no reference).
- `assembly_check(images, object_words) -> AssemblyCheck` and `inspect_state(crop, ...) -> CropState` (optional): is
  the object complete and correctly assembled, over a few photos, then one place's state on a full-resolution crop
  (vision/assembly.py).
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
    fix: str = ""            # inspect_state only: the plain fix for a setup state ("Put the batteries in, then test
                             # again."); "" otherwise


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


ASSEMBLY_STATUS = ("as_expected", "problem", "not_visible")


@dataclass(frozen=True)
class AssemblyItem:
    """One thing a complete, correctly assembled unit of this kind has (the overview's own checklist), and what the
    photos show of it. A `problem` is a claim to confirm on a full-resolution crop (vision/assembly.py), never a
    finding by itself; `not_visible` is a gap (a photo is needed), never a finding."""
    part: str                # generic name of the part or place, without its state ("battery bay", "display ribbon")
    kind: str                # part | wire
    expect: str              # how it is on a complete, correctly assembled unit, in a few words
    status: str              # one of ASSEMBLY_STATUS
    problem: str = "none"    # one of generic_classes.ASSEMBLY_PROBLEMS when status is problem, else none
    photo: int | None = None  # 0-based index of the image that shows it (problem / as_expected), None when not shown
    box: tuple[float, float, float, float] | None = None  # where, in that image's pixels (as given to the call)
    evidence: str = ""       # what in the photo supports the status
    visibility: str = ""     # one of VISIBILITY
    photo_needed: str = ""   # not_visible: what one more photo should show, in plain words


@dataclass(frozen=True)
class AssemblyCheck:
    """`assembly_check`: what the object is and whether it looks complete and correctly assembled, over a few
    photos of it."""
    object_words: str        # short generic kind of product, as the model sees it
    opened: bool             # the device is opened for inspection in the photos (cover off, panel open)
    items: tuple[AssemblyItem, ...]
    model: str
    latency_ms: int
    attempts: tuple[dict, ...] = field(default_factory=tuple)
    refused: bool = False    # the model declined: no answer at all (never "nothing is wrong"); the check is redone


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
                     family: str | None = None, hints: Sequence[str] | None = None, view: str = "part",
                     px_per_mm: float | None = None) -> CropState:
        """`crop`: PIL image cut from the full-resolution still. `context`: optional downsized whole still with the
        crop's box outlined. `part_hint`: the detector's label (data, not instruction). `family`: the part family
        whose failure checklist to apply (vision/checklists.py; `unknown_part` = the general checks only), None =
        the family the label suggests. `hints`: optional public-reference notes (untrusted data, never evidence).
        `view`: "part" (a crop around one detected region) or "tile" (one close tile of
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

    # Optional (vision/assembly.py checks hasattr): the whole-object assembly check.
    def assembly_check(self, images: Sequence[Any], object_words: str | None = None) -> AssemblyCheck:
        """`images`: a few photos of one object (PIL, any size). Lists what a complete, correctly assembled unit of
        this kind has and what the photos show of each; boxes are in the pixels of the image as given."""
        ...

    def inspect_state(self, crop: Any, context: Any | None, part: str | None,
                      px_per_mm: float | None = None) -> CropState:
        """One full-resolution crop of a place where something should be fitted or connected: is it there, fully
        seated, connected and the right way round (generic_classes.ASSEMBLY_STATES), or damaged. `part` is the
        overview's name for the place (quoted data, never its claim)."""
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
