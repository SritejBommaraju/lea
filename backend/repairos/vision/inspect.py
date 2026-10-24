"""Phase 1 perception for one still: detect -> full-resolution crops -> VLM state per crop -> Findings.

Product-agnostic: prompts come from the generic classes only (no reference in Phase 1, design_trust low, I7).
- Every finding is a suspect: source repairos.vision, is_proof false (I1).
- A detected region the VLM did not look at (over the crop budget, or a box covering most of the still) is
  `not_inspected`, never `ok` (I3).
- VLM `cannot_tell` becomes `insufficient_evidence` with the reason in the evidence (I4); so does a still with no
  detected parts.
Numbers stay in `measurements`/`evidence_refs`; evidence sentences use words (S5).

    python -m repairos.vision.inspect still.jpg --det <impl> [--vlm <impl>] [--scan-id ID] [--still-id ID]
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
from repairos.vision.crops import context_view, cut, load_still
from repairos.vision.generic_classes import WIRE_CLASSES

VLM_MAX_SIDE = 1568      # crops are downsized only past this (the VLM's own input limit), never upscaled
MAX_BOX_FRACTION = 0.5   # a box over half the still is scene-level, not a part: not inspected at crop level

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


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fid(*parts: object) -> str:
    return "f_" + hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:12]


def _strength(score: float) -> str:
    return "strong" if score >= 0.5 else "moderate" if score >= 0.35 else "weak"


def _subject(still_id: str, index: int, label: str) -> dict:
    return {"kind": "wire" if label in WIRE_CLASSES else "part", "ref": f"{still_id}/det{index}"}


def vlm_answer_for(state: str) -> str:
    return {"missing": "absent", "unplugged": "unplugged", "cannot_tell": "cannot_tell"}.get(state, "present")


def finding_from_crop(*, scan_id: str, still_id: str, index: int, det, crop, crop_state, crop_file: str | None = None,
                      sharpness: float | None = None) -> dict:
    """The Finding (as a dict) for one VLM-inspected crop."""
    state = STATE_TO_FINDING[crop_state.state]
    words = crop_state.state.replace("_", " ")
    lead = (f"could not judge the {crop_state.part_class} in still {still_id}" if crop_state.state == "cannot_tell"
            else f"{crop_state.part_class} in still {still_id} looks {words}")
    evidence = [
        lead,
        f"vision model ({crop_state.model}{', after escalation' if crop_state.escalated else ''}): "
        f"{crop_state.evidence_text}",
        f"detector region labelled '{det.label}' ({_strength(det.score)} match)",
        "vision suspect, not proof",
    ]
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
        "confidence": CONFIDENCE.get(crop_state.confidence_word, 0.35),
        "is_proof": False,
        "evidence": evidence,
        "evidence_refs": refs,
        "measurements": meas,
        "vlm_answer": vlm_answer_for(crop_state.state),
        "design_trust": "low",
        "created_at": _now(),
    }


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
    """Indices in inspection order: by detector score. Damage labels get no priority: measured on a real photo,
    zero-shot damage labels ('melted plastic', 'missing screw') fired mostly on blur and intact parts, so ranking
    them first spent the crop budget on noise. The VLM judges damage on every crop it sees."""
    return sorted(range(len(detections)), key=lambda i: -detections[i].score)


def inspect_still(image: Any, *, still_id: str, scan_id: str, detector: Any = None, judge: Any = None,
                  prompts: list[str] | None = None, min_score: float | None = None, max_crops: int = 12,
                  with_context: bool = True, crop_dir: str | Path | None = None, workers: int = 4,
                  sharpness: float | None = None) -> Inspection:
    """Run detect -> crops -> VLM on one full-resolution still (PIL image, array or path)."""
    from repairos.registry import slot

    detector = detector or slot("DET")
    judge = judge or slot("VLM")
    im = load_still(image) if isinstance(image, (str, Path)) else image
    from repairos.vision.crops import as_pil

    im = as_pil(im)
    out = Inspection(still_id=still_id)
    t0 = time.perf_counter()
    dets = detector.detect(im, prompts, min_score)
    out.detections = dets
    out.timing["detect_s"] = time.perf_counter() - t0
    if not dets:
        out.findings.append(validate("finding", finding_nothing_detected(scan_id=scan_id, still_id=still_id)))
        return out

    area = im.width * im.height
    chosen: list[int] = []
    raw: dict[int, dict] = {}
    for i in order_for_inspection(dets):
        b = dets[i].box
        if (b[2] - b[0]) * (b[3] - b[1]) > MAX_BOX_FRACTION * area:
            raw[i] = finding_not_inspected(scan_id=scan_id, still_id=still_id, index=i, det=dets[i],
                                           reason="the region covers most of the still; take a closer still")
        elif len(chosen) >= max_crops:
            raw[i] = finding_not_inspected(scan_id=scan_id, still_id=still_id, index=i, det=dets[i],
                                           reason="over this still's crop budget")
        else:
            chosen.append(i)

    crop_dir = Path(crop_dir) if crop_dir else None
    if crop_dir:
        crop_dir.mkdir(parents=True, exist_ok=True)

    def look(i: int) -> tuple[int, dict]:
        d = dets[i]
        crop = cut(im, d.box, still_id=still_id, max_side=VLM_MAX_SIDE)
        ctx = context_view(im, d.box) if with_context else None
        state = judge.inspect_crop(crop.image, ctx, part_hint=d.label)
        crop_file = None
        if crop_dir:
            p = crop_dir / f"{still_id}_det{i}.jpg"
            crop.image.save(p, quality=92)
            crop_file = str(p)
        return i, finding_from_crop(scan_id=scan_id, still_id=still_id, index=i, det=d, crop=crop,
                                    crop_state=state, crop_file=crop_file, sharpness=sharpness)

    t1 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for i, f in pool.map(look, chosen):
            raw[i] = f
    out.timing["vlm_s"] = time.perf_counter() - t1
    out.findings = [validate("finding", raw[i]) for i in sorted(raw)]
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    from repairos.registry import Registry

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("image")
    ap.add_argument("--det", required=True, help="DET implementation id")
    ap.add_argument("--vlm", default=None, help="VLM implementation id (default: the configured one)")
    ap.add_argument("--scan-id", default="local")
    ap.add_argument("--still-id", default=None)
    ap.add_argument("--max-crops", type=int, default=12)
    ap.add_argument("--crop-dir", default=None)
    a = ap.parse_args(argv)
    reg = Registry()
    res = inspect_still(a.image, still_id=a.still_id or Path(a.image).stem, scan_id=a.scan_id,
                        detector=reg.load("DET", a.det), judge=reg.load("VLM", a.vlm or reg.selected("VLM")),
                        max_crops=a.max_crops, crop_dir=a.crop_dir)
    print(json.dumps({"timing": res.timing, "detections": len(res.detections),
                      "findings": [f.model_dump(mode="json", exclude_none=True) for f in res.findings]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
