"""Phase 1 perception for one still: detect -> full-resolution crops -> VLM state per crop -> Findings.

Product-agnostic: prompts come from the generic classes only (no reference in Phase 1, design_trust low, I7).
- Every finding is a suspect: source repairos.vision, is_proof false (I1).
- A detected region the VLM did not look at (over the crop budget, or a box covering most of the still) is
  `not_inspected`, never `ok` (I3).
- VLM `cannot_tell` becomes `insufficient_evidence` with the reason in the evidence (I4); so does a still with no
  detected parts.
Numbers stay in `measurements`/`evidence_refs`; evidence sentences use words (S5).

Budget and prompt (eval/defects/QUICKWINS.md):
- regions whose crops would show nearly the same pixels are merged first (vision/ranking.py); a merged region whose
  detector box shows the same part as the kept one (inspect.same_part) gets the kept answer, else it is
  `not_inspected` with that reason;
- each crop gets the failure checklist of its part family (vision/checklists.py); a weak detector label or a crop the
  classifier abstains on gets the general checklist, and a region nobody identified is named `unknown_part`.

With a defect classifier (CLS slot selected, or `classifier=` passed; task M7) the classifier scores every part crop
first (up to `max_classified`) and decides which crops the VLM sees:
- flagged (likely defect) and unsure crops go to the VLM, flagged first, most likely defect first, up to `max_crops`;
  the VLM's state is the finding's state and its sentence the explanation; the classifier's answer is added in
  words, and a disagreement lowers the confidence;
- a crop the classifier is confident has no visible defect is `ok` on the classifier's word (a model looked at it,
  I3), with its confidence in words and no VLM call;
- a crop unlike the classifier's training data (unfamiliar part or image type) gets no classifier answer and goes
  to the VLM after the flagged ones;
- over the VLM budget, a flagged crop is a suspect on the classifier's word alone, and an unsure or unfamiliar crop
  is `insufficient_evidence` with that reason (I4).

    python -m repairos.vision.inspect still.jpg --det <impl> [--vlm <impl>] [--cls <impl>|none] [--scan-id ID]
"""
from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repairos.contracts import validate
from repairos.slots.classifier import (ABSTAIN, FLAGGED, OK, UNSURE, DefectScore, confidence_word, evidence_words,
                                      route)
from repairos.vision.checklists import UNKNOWN_PART, WEAK_LABEL_BELOW, crop_family
from repairos.vision.crops import context_view, crop_region, cut, load_still, outline
from repairos.vision.generic_classes import DEFECT_TO_CROP_STATE, WIRE_CLASSES
from repairos.vision.ranking import MERGE_IOU, RANK_BY, rank_regions
from repairos.vision.tiling import iou

VLM_MAX_SIDE = 1568      # crops are downsized only past this (the VLM's own input limit), never upscaled
MAX_BOX_FRACTION = 0.5   # a box over half the still is scene-level, not a part: not inspected at crop level
# Region list caps (eval/defects/QUICKWINS.md §5). A 12-24 MP phone photo of a whole device gives the tiled detector
# about ninety boxes; most are specks and texture. Chosen on the non-held-out dev stills (VisA pcb1-3 + webcam):
# every floor from 0 to 0.015 loses no dev defect (0.0175 loses one), and MAX_REGIONS is the per-still cap past which
# no dev defect is gained (11 is the smallest lossless value; 12 matches the VLM crop budget). The floor is the low
# end of that range: the dev pick 0.015 cost one flagged held-out defect crop, 0.01 costs none (disclosed there).
MIN_BOX_SIDE = 0.01      # sqrt(box area / still area): a box smaller than this is dropped before cropping
MAX_REGIONS = 12         # regions kept per still after merging, by detector score (a classifier's budget if larger)
SMALL_REGIONS, MORE_REGIONS = "small_regions", "more_regions"   # refs of the per-still not-inspected roll-ups

STATE_TO_FINDING = {
    "ok": "ok", "damaged": "damaged", "burnt": "damaged", "cracked": "broken", "corroded": "damaged",
    "loose": "loose", "unplugged": "unseated", "missing": "missing", "cannot_tell": "insufficient_evidence",
}
CONFIDENCE = {"low": 0.35, "medium": 0.6, "high": 0.8}


@dataclass
class Inspection:
    still_id: str
    findings: list[Any] = field(default_factory=list)      # repairos.contracts Finding models
    detections: list[Any] = field(default_factory=list)    # slots.detector.Detection
    timing: dict[str, float] = field(default_factory=dict)
    defect_scores: dict[int, DefectScore] = field(default_factory=dict)  # detection index -> CLS score (M7)
    routes: dict[int, str] = field(default_factory=dict)   # detection index -> ok | unsure | flagged (CLS routing)
    merged: dict[int, int] = field(default_factory=dict)   # detection index -> the index whose crop covers it
    part_classes: dict[int, str] = field(default_factory=dict)  # index -> detector label, or unknown_part (weak)
    dropped: dict[str, list[int]] = field(default_factory=dict)  # small_regions | more_regions -> indices not kept
    rollups: dict[str, dict] = field(default_factory=dict)  # finding_id -> {part_class, regions} for a roll-up


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fid(*parts: object) -> str:
    return "f_" + hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:12]


def _strength(score: float) -> str:
    return "strong" if score >= 0.5 else "moderate" if score >= 0.35 else "weak"


def _subject(still_id: str, index: int, label: str) -> dict:
    return {"kind": "wire" if label in WIRE_CLASSES else "part", "ref": f"{still_id}/det{index}"}


def part_name(vlm_part_class: str | None) -> str:
    """The VLM's own generic part name; `unknown_part` when it could not identify the part."""
    p = (vlm_part_class or "").strip()
    return UNKNOWN_PART if p.lower().replace("_", " ") in ("", "unknown", "unknown part", "unidentified part") else p


def vlm_answer_for(state: str) -> str:
    return {"missing": "absent", "unplugged": "unplugged", "cannot_tell": "cannot_tell"}.get(state, "present")


def finding_from_crop(*, scan_id: str, still_id: str, index: int, det, crop, crop_state, crop_file: str | None = None,
                      sharpness: float | None = None, defect_score: DefectScore | None = None) -> dict:
    """The Finding (as a dict) for one VLM-inspected crop; `defect_score` adds the classifier's answer in words.
    The VLM's answer passes the honesty gate first (honest_crop_state): code, not the model, decides what a view
    that is not clear, or a device that is only open, may claim."""
    crop_state, gate = honest_crop_state(crop_state)
    state = STATE_TO_FINDING[crop_state.state]
    words = crop_state.state.replace("_", " ")
    part = part_name(crop_state.part_class)
    lead = (f"could not judge the {part} in still {still_id}" if crop_state.state == "cannot_tell"
            else f"{part} in still {still_id} looks {words}")
    evidence = [
        lead,
        f"vision model ({crop_state.model}{', after escalation' if crop_state.escalated else ''}): "
        f"{crop_state.evidence_text}",
        f"detector region labelled '{det.label}' ({_strength(det.score)} match)",
        "vision suspect, not proof",
    ]
    if gate:
        evidence.insert(2, gate)
    confidence = CONFIDENCE.get(crop_state.confidence_word, 0.35)
    if defect_score is not None:
        evidence.insert(2, evidence_words(defect_score))
        vlm_defect = crop_state.state not in ("ok", "cannot_tell")
        if (not defect_score.abstained and crop_state.state != "cannot_tell"
                and vlm_defect != (defect_score.p_defect >= 0.5)):
            evidence.insert(3, "the defect classifier and the vision model disagree")
            confidence = CONFIDENCE["low"]
    refs: dict[str, Any] = {"still_ids": [still_id], "crop_box_px": [int(v) for v in crop.region_px]}
    if crop_file:
        refs["crop_file"] = crop_file
    meas: dict[str, Any] = {"views": 1}
    if sharpness is not None:
        meas["sharpness"] = sharpness
    out = {
        "finding_id": _fid(scan_id, still_id, index, det.box, det.label),
        "scan_id": scan_id,
        "subject": _subject(still_id, index, det.label),
        "state": state,
        "source": "repairos.vision",
        "confidence": confidence,
        "is_proof": False,
        "evidence": evidence,
        "evidence_refs": refs,
        "measurements": meas,
        "vlm_answer": vlm_answer_for(crop_state.state),
        "design_trust": "low",
        "created_at": _now(),
    }
    if (sev := severity_of(state, getattr(crop_state, "severity", "none"))):
        out["severity"] = sev
    if (pw := " ".join((getattr(crop_state, "part_words", "") or "").split())[:200]):
        out["part_words"] = pw
    return out


ISSUE_STATES = frozenset(s for s in STATE_TO_FINDING if s not in ("ok", "cannot_tell"))
GET_CLOSER = "take a closer, sharper photo of this spot"


def honest_crop_state(cs: Any) -> tuple[Any, str | None]:
    """(the crop state the finding may claim, the reason in words when code changed the VLM's state).

    Code decides, from the VLM's own typed answers (visibility, open_for_access), never from words in its text:
    - an issue on a view that is not clear (partial or poor) is cannot_tell: a guess on a soft, small or distant
      view is not a finding (it becomes insufficient_evidence with the reason, I4);
    - ok on a poor view is cannot_tell too (ok needs a view sharp enough to show a fault, I3);
    - an issue that is only the device being open for inspection (cover off, panel open, wiring exposed) is ok:
      an opened device is the normal state of an inspected device, not a fault;
    - severity safety needs a clear view; on any other view it is lowered to functional.
    An answer without visibility (older recordings) passes unchanged."""
    from dataclasses import replace

    vis = getattr(cs, "visibility", "") or ""
    if cs.state in ISSUE_STATES and getattr(cs, "open_for_access", False):
        if vis == "poor":
            return replace(cs, state="cannot_tell", severity="none"), \
                f"the view is too unclear to judge; {GET_CLOSER}"
        return replace(cs, state="ok", severity="none"), \
            "the device is open for inspection, which is expected and not a fault"
    if cs.state in ISSUE_STATES and vis in ("partial", "poor"):
        return replace(cs, state="cannot_tell", severity="none"), \
            (f"the vision model saw a possible {cs.state.replace('_', ' ')} part, but the view is "
             f"{'soft, small or partly hidden' if vis == 'partial' else 'blurred, too small or hidden'}, so it is "
             f"not a finding; {GET_CLOSER}")
    if cs.state == "ok" and vis == "poor":
        return replace(cs, state="cannot_tell"), f"the view is too unclear to call it intact; {GET_CLOSER}"
    if getattr(cs, "severity", "none") == "safety" and vis and vis != "clear":
        return replace(cs, severity="functional"), "a safety fault needs a clear view; rated functional"
    return cs, None


def severity_of(finding_state: str, vlm_severity: str) -> str | None:
    """Finding.severity for a VLM-judged crop: only an issue has one; an issue the VLM gave no severity (`none`, or
    an older answer without the field) is treated as functional, never waved through as cosmetic."""
    if finding_state in ("ok", "insufficient_evidence"):
        return None
    return vlm_severity if vlm_severity in ("cosmetic", "functional", "safety") else "functional"


def finding_from_classifier(*, scan_id: str, still_id: str, index: int, det, crop, score: DefectScore,
                            reason: str, crop_file: str | None = None, sharpness: float | None = None) -> dict:
    """The Finding for a crop only the classifier judged (no VLM call): `ok` when it is confident there is no
    visible defect, a defect suspect when it flags one. Never proof (I1); confidence in words (S5)."""
    if score.p_defect >= 0.5:
        state = STATE_TO_FINDING[DEFECT_TO_CROP_STATE[score.defect_class]] if score.defect_class else "damaged"
        lead = f"region labelled '{det.label}' in still {still_id} looks defective to the defect classifier"
    else:
        state = "ok"
        lead = f"region labelled '{det.label}' in still {still_id} shows no visible defect to the defect classifier"
    word = confidence_word(score.p_defect)
    refs: dict[str, Any] = {"still_ids": [still_id], "crop_box_px": [int(v) for v in crop.region_px]}
    if crop_file:
        refs["crop_file"] = crop_file
    meas: dict[str, Any] = {"views": 1}
    if sharpness is not None:
        meas["sharpness"] = sharpness
    return {
        "finding_id": _fid(scan_id, still_id, index, det.box, det.label),
        "scan_id": scan_id,
        "subject": _subject(still_id, index, det.label),
        "state": state,
        "source": "repairos.vision",
        "confidence": CONFIDENCE[word],
        "is_proof": False,
        "evidence": [lead, f"{evidence_words(score)} ({word} confidence)",
                     f"detector region labelled '{det.label}' ({_strength(det.score)} match)",
                     f"not checked by the vision model: {reason}", "vision suspect, not proof"],
        "evidence_refs": refs,
        "measurements": meas,
        "design_trust": "low",
        "created_at": _now(),
    }


def finding_classifier_unsure(*, scan_id: str, still_id: str, index: int, det, crop, score: DefectScore,
                              reason: str) -> dict:
    """The classifier could not decide (unsure, or abstained on an unfamiliar crop) and the VLM did not look:
    insufficient evidence, with the reason (I4)."""
    why = f"the defect classifier gave no answer ({score.abstain_reason})" if score.abstained else \
        "the defect classifier was unsure"
    return {
        "finding_id": _fid(scan_id, still_id, index, det.box, det.label),
        "scan_id": scan_id,
        "subject": _subject(still_id, index, det.label),
        "state": "insufficient_evidence",
        "source": "repairos.vision",
        "confidence": 0.0,
        "is_proof": False,
        "evidence": [f"could not judge the region labelled '{det.label}' in still {still_id}: {why} and {reason}",
                     evidence_words(score)],
        "evidence_refs": {"still_ids": [still_id], "crop_box_px": [int(v) for v in crop.region_px]},
        "design_trust": "low",
        "created_at": _now(),
    }


def vlm_queue(scores: dict[int, DefectScore], ok_below: float, flag_at: float) -> tuple[list[int], dict[int, str]]:
    """(indices for the VLM in order, route per index): flagged first, then abstained (unfamiliar), then unsure,
    most likely defect first within each. Confident-ok crops are not queued."""
    routes = {i: route(s, ok_below, flag_at) for i, s in scores.items()}
    rank = {FLAGGED: 0, ABSTAIN: 1, UNSURE: 2}
    queue = sorted((i for i, r in routes.items() if r != OK),
                   key=lambda i: (rank[routes[i]], -scores[i].p_defect if routes[i] != ABSTAIN else 0.0, i))
    return queue, routes


def resolve_classifier(classifier: Any) -> Any:
    """None = the configured CLS slot (nothing when the profile says `none` or has no CLS line); False = none."""
    if classifier is False:
        return None
    if classifier is not None:
        return classifier
    from repairos.registry import RegistryError, default_registry

    reg = default_registry()
    try:
        impl = reg.selected("CLS")
    except RegistryError:
        return None
    return None if impl == "none" else reg.slot("CLS")


def finding_not_inspected(*, scan_id: str, still_id: str, index: int, det, reason: str) -> dict:
    return {
        "finding_id": _fid(scan_id, still_id, index, det.box, det.label),
        "scan_id": scan_id,
        "subject": _subject(still_id, index, det.label),
        "state": "not_inspected",
        "source": "repairos.vision",
        "confidence": 0.0,
        "is_proof": False,
        "evidence": [f"region labelled '{det.label}' in still {still_id} was detected but not inspected: {reason}"],
        "evidence_refs": {"still_ids": [still_id], "crop_box_px": [max(0, int(v)) for v in det.box]},
        "design_trust": "low",
        "created_at": _now(),
    }


ROLLUP_WORDS = {
    SMALL_REGIONS: ("small regions", "too small in this photo to judge; take a closer photo of the area"),
    MORE_REGIONS: ("more regions", "past this photo's limit of the strongest detector regions; take closer photos "
                                   "to inspect them"),
}


def box_side(box: Sequence[float], width: int, height: int) -> float:
    """A box's size relative to the still: sqrt(box area / still area) (a thin long part keeps its area)."""
    area = max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])
    return (area / (width * height)) ** 0.5 if width * height > 0 else 0.0


def finding_rollup(*, scan_id: str, still_id: str, what: str, indices: Sequence[int], dets: Sequence[Any]) -> dict:
    """One not_inspected finding for the detector regions of a still that were not kept as regions (too small, or
    past the per-still region cap): nobody looked at them (I3), so they are counted, never called ok. The exact
    count is in the evidence; the report shows it as one row per still."""
    part, why = ROLLUP_WORDS[what]
    labels = sorted({dets[i].label for i in indices})
    shown = ", ".join(f"'{x}'" for x in labels[:6]) + (", and others" if len(labels) > 6 else "")
    return {
        "finding_id": _fid(scan_id, still_id, what, tuple(sorted(indices))),
        "scan_id": scan_id,
        "subject": {"kind": "part", "ref": f"{still_id}/{what}"},
        "state": "not_inspected",
        "source": "repairos.vision",
        "confidence": 0.0,
        "is_proof": False,
        "evidence": [(f"{len(indices)} {part} in still {still_id} were detected but not inspected: {why}"
                      if len(indices) > 1 else
                      f"{len(indices)} {part[:-1]} in still {still_id} was detected but not inspected: {why}"),
                     f"detector labels: {shown}"],
        "evidence_refs": {"still_ids": [still_id]},
        "design_trust": "low",
        "created_at": _now(),
    }


def finding_nothing_detected(*, scan_id: str, still_id: str) -> dict:
    return {
        "finding_id": _fid(scan_id, still_id, "nothing"),
        "scan_id": scan_id,
        "subject": {"kind": "part", "ref": f"{still_id}/whole"},
        "state": "insufficient_evidence",
        "source": "repairos.vision",
        "confidence": 0.0,
        "is_proof": False,
        "evidence": [f"no parts detected in still {still_id}: take a closer, sharper still"],
        "evidence_refs": {"still_ids": [still_id]},
        "design_trust": "low",
        "created_at": _now(),
    }


def order_for_inspection(detections: Sequence[Any]) -> list[int]:
    """Indices by detector score (the order before merging). Damage labels get no priority: measured on a real
    photo, zero-shot damage labels ('melted plastic', 'missing screw') fired mostly on blur and intact parts; ranking
    by the detector's damage evidence did not raise the defects inside the budget on held-out stills either
    (eval/defects/QUICKWINS.md). The VLM judges damage on every crop it sees."""
    return sorted(range(len(detections)), key=lambda i: -detections[i].score)


def inside_share(box: Sequence[float], region: Sequence[float]) -> float:
    """How much of `box` lies inside `region` (0..1)."""
    w = max(0.0, min(box[2], region[2]) - max(box[0], region[0]))
    h = max(0.0, min(box[3], region[3]) - max(box[1], region[1]))
    area = (box[2] - box[0]) * (box[3] - box[1])
    return w * h / area if area > 0 else 0.0


COVERED_INSIDE = 0.9  # a merged detector box at least this much inside the kept detector box is part of that part


def same_part(box: Sequence[float], kept_box: Sequence[float]) -> bool:
    """Whether a merged region's detector box shows the part the kept region's VLM look judged: the two DETECTOR
    boxes overlap at IoU >= MERGE_IOU, or the merged box lies inside the kept box (a pin of a judged connector). The
    padded crops are not used: crop_region pads small boxes to at least 256 px, so a neighbouring part can sit inside
    the kept crop without being the part the model judged."""
    return iou(box, kept_box) >= MERGE_IOU or inside_share(box, kept_box) >= COVERED_INSIDE


def finding_from_merged(kept: dict, *, scan_id: str, still_id: str, index: int, det, into: int,
                        into_box: Sequence[float]) -> dict:
    """A region merged into detection `into` (detector box `into_box`): the kept answer when the detector boxes show
    the same part (same_part), else not inspected with that reason (I3)."""
    looked = kept["state"] not in ("not_inspected",) and same_part(det.box, into_box)
    if not looked:
        why = "its crop overlaps the crop of another region, which was inspected in its place" \
            if kept["state"] != "not_inspected" else "it overlaps a region that was not inspected either"
        return finding_not_inspected(scan_id=scan_id, still_id=still_id, index=index, det=det, reason=why)
    f = dict(kept)
    f["finding_id"] = _fid(scan_id, still_id, index, det.box, det.label)
    f["subject"] = _subject(still_id, index, det.label)
    f["evidence"] = [kept["evidence"][0], f"judged in the crop of {still_id}/det{into}, which covers this region "
                     f"(detector label '{det.label}')"] + list(kept["evidence"][1:])
    return f


SHARE_WAIT_S = 300.0   # how long a still waits for another still's answer on a shared region


def finding_from_other_still(entry: Any, *, scan_id: str, still_id: str, index: int, det) -> dict:
    """A region another still of the scan judges (same place on the scan, similar size: pipeline/regions.py): that
    answer, named for this region, with where it was judged in the evidence; its crop and box stay the other still's
    (the pixels the model saw). If that check failed, this region is not_inspected with the reason (I3)."""
    try:
        kept = entry.result(SHARE_WAIT_S)
    except Exception as e:  # noqa: BLE001 - the other still's failure is reported there; here the region was not judged
        return finding_not_inspected(scan_id=scan_id, still_id=still_id, index=index, det=det,
                                     reason=f"it was to be judged in still {entry.still_id}, which could not judge "
                                            f"it ({type(e).__name__})")
    if kept["state"] == "not_inspected":
        return finding_not_inspected(scan_id=scan_id, still_id=still_id, index=index, det=det,
                                     reason=f"the same place in still {entry.still_id} was not inspected either")
    f = dict(kept)
    f["finding_id"] = _fid(scan_id, still_id, index, det.box, det.label)
    f["subject"] = _subject(still_id, index, det.label)
    f["evidence"] = [kept["evidence"][0], f"judged once, in the crop of {entry.still_id}/det{entry.index}, which "
                     f"shows the same place on the scan (this photo's detector label '{det.label}')"] + \
        list(kept["evidence"][1:])
    return f


def region_part_class(det, classifier_abstained: bool = False) -> str:
    """The part name for a region before any model names it: the detector label, or `unknown_part` when that label
    is a weak match or the classifier did not recognise the crop (vision/checklists.crop_family)."""
    return UNKNOWN_PART if crop_family(det.label, det.score, classifier_abstained) == UNKNOWN_PART \
        and (classifier_abstained or det.score < WEAK_LABEL_BELOW) else det.label


@dataclass
class RegionSelection:
    scene: list[int]                          # boxes over MAX_BOX_FRACTION of the still (not inspected, own rows)
    ordered: list[int]                        # kept regions in inspection order (at most the region cap)
    merged: dict[int, int]                    # kept merged index -> the kept index whose crop covers it
    dropped: dict[str, list[int]]             # small_regions / more_regions -> indices not kept
    regions: dict[int, tuple[int, int, int, int]]  # crop region of every candidate (still pixels)


def region_cap(budget: int, max_regions: int | None = None) -> int:
    """Regions kept per still: MAX_REGIONS (or `max_regions`), never fewer than the inspection budget."""
    return max(MAX_REGIONS if max_regions is None else max_regions, budget)


def select_regions(dets: Sequence[Any], width: int, height: int, *, budget: int, min_box_side: float = MIN_BOX_SIDE,
                   max_regions: int | None = None, by: str = RANK_BY,
                   merge_iou: float | None = MERGE_IOU) -> RegionSelection:
    """Which detections become regions, before any crop is cut. Scene-sized boxes are set aside; boxes under
    `min_box_side` are dropped; the rest are ranked and merged (vision/ranking.py) and only the first
    region_cap(budget, max_regions) are kept. A region merged into a dropped one is dropped with it."""
    area = width * height
    scene, cand, tiny = [], [], []
    for i in order_for_inspection(dets):
        b = dets[i].box
        if (b[2] - b[0]) * (b[3] - b[1]) > MAX_BOX_FRACTION * area:
            scene.append(i)
        elif box_side(b, width, height) < min_box_side:
            tiny.append(i)
        else:
            cand.append(i)
    regions = {i: crop_region(dets[i].box, width, height) for i in cand}
    ordered, merged = rank_regions(dets, cand, regions, by=by, merge_iou=merge_iou)
    cap = region_cap(budget, max_regions)
    beyond = set(ordered[cap:])
    beyond |= {i for i, into in merged.items() if into in beyond}
    dropped = {k: sorted(v) for k, v in ((SMALL_REGIONS, tiny), (MORE_REGIONS, beyond)) if v}
    return RegionSelection(scene, ordered[:cap], {i: k for i, k in merged.items() if i not in beyond}, dropped,
                           regions)


def inspect_still(image: Any, *, still_id: str, scan_id: str, detector: Any = None, judge: Any = None,
                  prompts: list[str] | None = None, min_score: float | None = None, max_crops: int = 12,
                  with_context: bool = True, crop_dir: str | Path | None = None, workers: int = 4,
                  sharpness: float | None = None, classifier: Any = None, max_classified: int = 48,
                  min_box_side: float = MIN_BOX_SIDE, max_regions: int | None = None,
                  detections: Sequence[Any] | None = None, pool: Any = None, share: Any = None,
                  stop: Any = None) -> Inspection:
    """Run detect -> crops -> [defect classifier] -> VLM on one full-resolution still (PIL image, array or path).

    `classifier`: None = the configured CLS slot (off when the profile says `none`), False = off, or a
    DefectClassifier. `max_crops` is the VLM budget; `max_classified` caps the crops the classifier scores.
    Boxes under `min_box_side` (box_side) are dropped before cropping, and only the `max_regions` best regions after
    merging are kept (default MAX_REGIONS, or the crop budget when that is larger); what is dropped is counted in
    one not_inspected roll-up finding per reason (finding_rollup).

    Pipeline hooks (all optional): `detections` = the detector's answer for this still when it was already run (no
    second pass); `pool` = a shared executor for the VLM calls (bounds Claude calls across stills; `workers` is used
    only without it); `share` = the scan's cross-still region register (pipeline/regions.py): a region another still
    already sends to the VLM is not sent again, its answer is reused (finding_from_other_still), and reused regions do
    not use this still's crop budget. `share` applies without a classifier only. `stop` (a threading.Event): when
    set, no further VLM call starts (the worker is shutting down) and the inspection raises."""
    from repairos.registry import slot

    detector = detector or slot("DET")
    judge = judge or slot("VLM")
    classifier = resolve_classifier(classifier)
    im = load_still(image) if isinstance(image, (str, Path)) else image
    from repairos.vision.crops import as_pil

    im = as_pil(im)
    out = Inspection(still_id=still_id)
    t0 = time.perf_counter()
    dets = list(detections) if detections is not None else detector.detect(im, prompts, min_score)
    out.detections = dets
    out.timing["detect_s"] = time.perf_counter() - t0
    if not dets:
        out.findings.append(validate("finding", finding_nothing_detected(scan_id=scan_id, still_id=still_id)))
        return out

    budget = max_classified if classifier is not None else max_crops
    sel = select_regions(dets, im.width, im.height, budget=budget, min_box_side=min_box_side,
                         max_regions=max_regions)
    raw: dict[int, dict] = {}
    for i in sel.scene:
        raw[i] = finding_not_inspected(scan_id=scan_id, still_id=still_id, index=i, det=dets[i],
                                       reason="the region covers most of the still; take a closer still")
    regions, ordered, out.merged, out.dropped = sel.regions, sel.ordered, sel.merged, sel.dropped
    rollups = [finding_rollup(scan_id=scan_id, still_id=still_id, what=k, indices=v, dets=dets)
               for k, v in out.dropped.items()]
    out.rollups = {f["finding_id"]: {"part_class": ROLLUP_WORDS[k][0], "regions": len(v)}
                   for f, (k, v) in zip(rollups, out.dropped.items())}
    reused: dict[int, Any] = {}     # index -> the share entry of another still that judges the same place
    owned: dict[int, Any] = {}      # index -> this still's share entry (its answer is set when it arrives)
    if share is not None and classifier is None:
        candidates = []
        for i in ordered:
            entry, mine = share.claim(still_id, i, dets[i].box, register=len(candidates) < budget)
            if entry is not None and not mine:
                reused[i] = entry
            elif len(candidates) < budget:
                candidates.append(i)
                if entry is not None:
                    owned[i] = entry
            else:
                raw[i] = finding_not_inspected(scan_id=scan_id, still_id=still_id, index=i, det=dets[i],
                                               reason="over this still's crop budget")
    else:
        candidates = ordered[:budget]
        for i in ordered[budget:]:
            raw[i] = finding_not_inspected(scan_id=scan_id, still_id=still_id, index=i, det=dets[i],
                                           reason="over this still's crop budget")

    crop_dir = Path(crop_dir) if crop_dir else None
    if crop_dir:
        crop_dir.mkdir(parents=True, exist_ok=True)
    crops = {i: cut(im, dets[i].box, still_id=still_id, max_side=VLM_MAX_SIDE) for i in candidates}

    def save(i: int) -> str | None:
        if not crop_dir:
            return None
        p = crop_dir / f"{still_id}_det{i}.jpg"
        crops[i].image.save(p, quality=92)
        return str(p)

    chosen = candidates
    if classifier is not None:
        t2 = time.perf_counter()
        scores = classifier.classify([crops[i].image for i in candidates])
        if len(scores) != len(candidates):
            raise RuntimeError(f"CLS returned {len(scores)} scores for {len(candidates)} crops")
        out.defect_scores = dict(zip(candidates, scores))
        out.timing["classify_s"] = time.perf_counter() - t2
        queue, out.routes = vlm_queue(out.defect_scores, classifier.ok_below, classifier.flag_at)
        chosen = queue[:max_crops]
        for i in candidates:
            s, r = out.defect_scores[i], out.routes[i]
            kw = dict(scan_id=scan_id, still_id=still_id, index=i, det=dets[i], crop=crops[i], score=s)
            if r == OK:
                raw[i] = finding_from_classifier(**kw, reason="the classifier was confident", crop_file=save(i),
                                                 sharpness=sharpness)
            elif i not in chosen and r == FLAGGED:
                raw[i] = finding_from_classifier(**kw, reason="over this still's vision-model budget",
                                                 crop_file=save(i), sharpness=sharpness)
            elif i not in chosen and r in (UNSURE, ABSTAIN):
                raw[i] = finding_classifier_unsure(**kw, reason="the vision-model budget was used up")

    def abstained(i: int) -> bool:
        sc = out.defect_scores.get(i)
        return bool(sc is not None and sc.abstained)

    context_base = context_view(im) if with_context and chosen else None

    def look(i: int) -> tuple[int, dict]:
        d = dets[i]
        try:
            if stop is not None and stop.is_set():
                from repairos.pipeline.stopping import WorkerStopping

                raise WorkerStopping("the worker is stopping: no further vision-model call")
            ctx = outline(context_base, d.box, context_base.width / im.width) if context_base is not None else None
            state = judge.inspect_crop(crops[i].image, ctx, part_hint=d.label,
                                       family=crop_family(d.label, d.score, abstained(i)))
            f = finding_from_crop(scan_id=scan_id, still_id=still_id, index=i, det=d, crop=crops[i],
                                  crop_state=state, crop_file=save(i), sharpness=sharpness,
                                  defect_score=out.defect_scores.get(i))
        except BaseException as e:
            if i in owned:
                owned[i].fail(e)
            raise
        if i in owned:
            owned[i].resolve(f)
        return i, f

    t1 = time.perf_counter()
    try:
        if chosen:
            if pool is not None:
                for fut in [pool.submit(look, i) for i in chosen]:
                    i, f = fut.result()
                    raw[i] = f
            else:
                with ThreadPoolExecutor(max_workers=max(1, workers)) as own_pool:
                    for i, f in own_pool.map(look, chosen):
                        raw[i] = f
        for i, entry in reused.items():
            raw[i] = finding_from_other_still(entry, scan_id=scan_id, still_id=still_id, index=i, det=dets[i])
    finally:
        for i, entry in owned.items():   # never leave another still waiting on an answer that will not come
            if i not in chosen or i not in raw:
                entry.fail(RuntimeError(f"{still_id}/det{i} was not judged"))
    out.timing["vlm_s"] = time.perf_counter() - t1
    out.timing["vlm_calls"] = len(chosen)
    out.timing["reused_regions"] = len(reused)
    for i, into in out.merged.items():
        raw[i] = finding_from_merged(raw[into], scan_id=scan_id, still_id=still_id, index=i, det=dets[i],
                                     into=into, into_box=dets[into].box)
    out.part_classes = {i: region_part_class(d, abstained(i)) for i, d in enumerate(dets)}
    out.findings = [validate("finding", raw[i]) for i in sorted(raw)] + [validate("finding", f) for f in rollups]
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    from repairos.registry import Registry

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("image")
    ap.add_argument("--det", required=True, help="DET implementation id")
    ap.add_argument("--vlm", default=None, help="VLM implementation id (default: the configured one)")
    ap.add_argument("--cls", default=None, help="CLS implementation id, or none (default: the configured one)")
    ap.add_argument("--scan-id", default="local")
    ap.add_argument("--still-id", default=None)
    ap.add_argument("--max-crops", type=int, default=12)
    ap.add_argument("--crop-dir", default=None)
    a = ap.parse_args(argv)
    reg = Registry()
    cls = None if a.cls is None else False if a.cls == "none" else reg.load("CLS", a.cls)
    res = inspect_still(a.image, still_id=a.still_id or Path(a.image).stem, scan_id=a.scan_id,
                        detector=reg.load("DET", a.det), judge=reg.load("VLM", a.vlm or reg.selected("VLM")),
                        max_crops=a.max_crops, crop_dir=a.crop_dir, classifier=cls)
    print(json.dumps({"timing": res.timing, "detections": len(res.detections), "routes": res.routes,
                      "findings": [f.model_dump(mode="json", exclude_none=True) for f in res.findings]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
