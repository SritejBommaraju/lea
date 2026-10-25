"""Is the object complete and correctly assembled? An object-level check beside the per-crop defect check (2026-10-25).

A crop check sees one region and asks "is this damaged"; it cannot see that a socket is empty, that a connector's
partner is dangling, that a battery bay holds nothing or that a cell is in backwards, because those are statements about
what SHOULD be there. This check asks that question once per scan, product-agnostic (GEN1: no product lists in code):

  1. overview   one vision-model call over a few photos of the object (pick_stills) with what the object is (identity,
                research brief; quoted data, A9). The model lists what a complete, correctly assembled unit of this
                kind has that can be seen in the state shown, and for each: as_expected, problem (missing, unplugged,
                loose, reversed; with a photo and a box) or not_visible. Its answer is a list of CLAIMS, never findings.
  2. confirm    each problem claim (at most `max_confirm`) is looked at again on a full-resolution crop of its box with
                the state question (slot `inspect_state`: present, seated, connected, the right way round, or damaged).
                The claim itself is not sent, only the place's name. The answer passes the same honesty gate as every
                crop (vision.inspect.honest_crop_state): an issue on a view that is not clear is cannot_tell, an opened
                cover is ok. Only then is it a finding, located like any other (still id + box -> a 3D marker).
  3. gaps       an item the photos do not show (not_visible, at most `max_gaps`) is insufficient_evidence with what
                photo to take (I4): never a finding.

A claim the confirm step calls ok leaves no row (the overview was wrong; it is kept in the run record). A later check
over other photos (more photos added to the scan) replaces the earlier check's rows (pipeline.run.run_assembly).
Every finding is a suspect (I1); a setup state (missing, unplugged, not fully in, in backwards) carries category
`setup` and its plain fix (setup_fields). A spend cap that trips leaves the check not_inspected with the pause reason
(I3); the report still builds.
"""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repairos.spend import PAUSED, SpendLimit

PREVIEW_SIDE = 1568      # the overview photos as sent (the vision model's own input limit)
CONFIRM_PAD_FRAC = 0.5   # the confirm crop: the claimed box padded by half its side each way (the overview's boxes are
CONFIRM_MIN_SIDE = 640   # rough), at least this many still pixels, so the place and what it fits into are both in view
ASSEMBLY_REF = "assembly"

# Setup and assembly states (owner, 2026-10-25): a part missing, unplugged, not fully in or in backwards is usually not
# a broken product but how it was put together. Code gives such a finding category `setup` and a plain `fix`; the report
# lists it apart and it never makes the verdict "potential issues" on its own, unless it is a hazard (severity safety:
# a cell in backwards, a loose mains wire), which stays among the findings. Damage is always category `defect`.
SETUP_STATES = frozenset({"missing", "unseated", "loose", "reversed"})   # Finding.state values
CROP_SETUP_STATES = frozenset({"unseated", "loose"})   # from the per-crop check (an empty footprint stays a defect)
FIX = {   # Finding.state -> the plain fix, any product ({part} is the model's generic name for it)
    "missing": "Fit what is missing at the {part} (or put it back, if it was taken out on purpose), then test again.",
    "unseated": "Plug the {part} back in fully, then test again.",
    "loose": "Push the {part} fully home or clip it shut, then test again.",
    "reversed": "Turn the {part} round so its marks match the marks where it fits, then test again.",
}


def setup_fields(finding_state: str, part: str, *, from_crop_check: bool = False, model_fix: str = "") -> dict:
    """{category, fix} for a finding: setup with its plain fix for a setup state, else category defect (an issue) or
    nothing (ok, could not judge). The fix is the close look's own sentence when it wrote one (it names what goes
    there), else the generic one for the state. The per-crop check's `missing` (an empty footprint on a board, a part
    knocked off) stays a defect."""
    states = CROP_SETUP_STATES if from_crop_check else SETUP_STATES
    if finding_state in states:
        name = " ".join((part or "part").split()) or "part"
        fix = " ".join((model_fix or "").split())
        return {"category": "setup", "fix": (fix if len(fix.split()) >= 3 else FIX[finding_state].format(part=name))
                [:300]}
    if finding_state in ("ok", "insufficient_evidence", "not_inspected", "unsure", "not_seen_in_photo"):
        return {}
    return {"category": "defect"}


# Plain words per state: what the report and Lea say (generic, any product).
LOOKS = {"missing": "looks missing", "unplugged": "looks unplugged", "loose": "looks not fully in place",
         "reversed": "looks fitted the wrong way round", "damaged": "looks damaged"}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fid(*parts: object) -> str:
    return "f_" + hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:12]


SAME_PLACE = 0.6   # a claim whose box lies at least this much inside an earlier claim's box (or holds it) is that place


def same_place(a: Sequence[float], b: Sequence[float]) -> bool:
    """Two claimed boxes of one photo name the same place ("battery bay" and "battery contacts" in it): one is at
    least SAME_PLACE inside the other. Measured on the 2026-10-25 eval: the overview often names a place and a part
    of it as two items, which cost a second close look and showed one fault as two rows."""
    from repairos.vision.inspect import inside_share

    return inside_share(a, b) >= SAME_PLACE or inside_share(b, a) >= SAME_PLACE


def pick_stills(stills: Sequence[Mapping[str, Any]], max_n: int) -> list[Mapping[str, Any]]:
    """Up to `max_n` photo stills spread over the capture (labels and skipped stills are not photos of the object's
    state): the capture order cut into `max_n` runs, the sharpest of each run (the first when none has a sharpness)."""
    photos = sorted((s for s in stills if not s.get("user_skipped") and s.get("kind") != "label"),
                    key=lambda s: (s.get("captured_at") or "", s["still_id"]))
    if len(photos) <= max_n:
        return photos
    out = []
    for k in range(max_n):
        run = photos[k * len(photos) // max_n:(k + 1) * len(photos) // max_n]
        out.append(max(run, key=lambda s: float(s.get("sharpness") or 0.0)))
    return out


@dataclass
class AssemblyResult:
    findings: list[dict] = field(default_factory=list)
    regions: dict[str, dict] = field(default_factory=dict)   # finding_id -> {part_class, inspected_by}
    record: dict[str, Any] = field(default_factory=dict)     # for the run row: claims, confirms, calls, timing


def _subject(kind: str, still_id: str, k: int) -> dict:
    return {"kind": "wire" if kind == "wire" else "part", "ref": f"{still_id}/{ASSEMBLY_REF}{k}"}


def _base(scan_id: str, fid: str, subject: dict, state: str, evidence: list[str], refs: dict,
          confidence: float = 0.0) -> dict:
    out = {"finding_id": fid, "scan_id": scan_id, "subject": subject, "state": state, "source": "repairos.vision",
           "confidence": confidence, "is_proof": False, "evidence": evidence, "design_trust": "low",
           "created_at": _now()}
    if refs:
        out["evidence_refs"] = refs
    return out


def finding_from_state(*, scan_id: str, still_id: str, k: int, item: Any, crop: Any, state: Any,
                       crop_file: str | None = None, sharpness: float | None = None) -> dict:
    """The finding for one confirmed place: the state check's answer after the honesty gate. An issue is a suspect
    with the overview's and the close look's words and what to do; cannot_tell is insufficient_evidence with a retake
    request; ok returns the finding with state ok (the caller drops it: the claim was not confirmed)."""
    from repairos.vision.inspect import (CONFIDENCE, GET_CLOSER, ISSUE_STATES, STATE_TO_FINDING, honest_crop_state,
                                         severity_of, vlm_answer_for)

    cs, gate = honest_crop_state(state)
    words = item.part
    if cs.state == "cannot_tell":
        lead = f"could not judge the {words} in still {still_id}"   # the lead form fusion reads the part name from
    elif cs.state == "ok":
        lead = f"{words} in still {still_id} looks in place"
    else:
        lead = f"{words} in still {still_id} {LOOKS.get(cs.state, 'looks ' + cs.state)}"
    evidence = [lead, f"vision model ({cs.model}{', after escalation' if cs.escalated else ''}), close look: "
                      f"{cs.evidence_text}",
                f"whole-object assembly check: {item.evidence or 'the overview pointed at this place'}"]
    if gate:
        evidence.append(gate)
    if cs.state == "cannot_tell":
        evidence.insert(1, "could not confirm whether it is present, plugged in fully and the right way round")
        evidence.append(f"the detail is too small for this photo to show; {GET_CLOSER}" if cs.too_small
                        else GET_CLOSER)
    where = " ".join((cs.where or "").split())
    if where and cs.state in ISSUE_STATES:
        evidence.append(f"where in the crop: {where}")
    evidence.append("vision suspect, not proof")
    refs: dict[str, Any] = {"still_ids": [still_id], "crop_box_px": [max(0, int(round(v))) for v in item.box]}
    if crop_file:
        refs["crop_file"] = crop_file
    finding_state = STATE_TO_FINDING[cs.state]
    out = _base(scan_id, _fid(scan_id, ASSEMBLY_REF, still_id, item.part, item.problem),
                _subject(item.kind, still_id, k),
                finding_state, evidence, refs,
                0.0 if cs.state == "cannot_tell" else CONFIDENCE.get(cs.confidence_word, 0.35))
    meas: dict[str, Any] = {"views": 1}
    if sharpness is not None:
        meas["sharpness"] = sharpness
    out["measurements"] = meas
    out["vlm_answer"] = vlm_answer_for(cs.state)
    if (sev := severity_of(finding_state, cs.severity)):
        # a part in backwards, unplugged or missing stops the device working right: never cosmetic
        out["severity"] = "functional" if sev == "cosmetic" and cs.state in ("reversed", "unplugged") else sev
    if (pw := " ".join((cs.part_words or "").split())[:200]):
        out["part_words"] = pw
    out.update(setup_fields(finding_state, item.part, model_fix=getattr(cs, "fix", "")))
    return out


def finding_gap(*, scan_id: str, k: int, item: Any, still_ids: Sequence[str]) -> dict:
    """An item the photos do not show: insufficient_evidence with the photo to take (I4), never a finding. It names
    the photos that were looked at (no box: it is not in them)."""
    stills_checked = f"{len(still_ids)} photos" if len(still_ids) != 1 else "photo"
    need = item.photo_needed or f"take a photo that shows the {item.part}"
    keep = item.expect[1:2].isupper()   # "AA cells ...", "USB ...": an abbreviation keeps its capitals
    words = item.expect if keep else item.expect[:1].lower() + item.expect[1:]
    expect = f" (it should be: {words})" if item.expect else ""
    return _base(scan_id, _fid(scan_id, ASSEMBLY_REF, "gap", item.part), {"kind": "wire" if item.kind == "wire" else
                 "part", "ref": f"{scan_id}/{ASSEMBLY_REF}_gap{k}"}, "insufficient_evidence",
                 [f"could not check the {item.part}: it is not clearly visible in the {stills_checked} the assembly "
                  f"check looked at{expect}", f"to check it: {need}",
                  "whole-object assembly check: a gap, not a fault"], {"still_ids": list(still_ids)}) | \
        {"part_words": item.part[:200]}


def finding_not_run(*, scan_id: str, reason: str, still_ids: Sequence[str]) -> dict:
    """The assembly check did not run (a spend pause, a failed call): not_inspected with the reason (I3), naming the
    photos it was to look at."""
    return _base(scan_id, _fid(scan_id, ASSEMBLY_REF, "not_run", reason),
                 {"kind": "part", "ref": f"{scan_id}/{ASSEMBLY_REF}"}, "not_inspected",
                 [f"the whole-object assembly check (is everything there, plugged in and the right way round) was not "
                  f"done: {reason}"], {"still_ids": list(still_ids)})


def check_assembly(views: Sequence[tuple[Mapping[str, Any], Any]], *, scan_id: str, judge: Any,
                   object_words: str | None = None, max_confirm: int = 3, max_gaps: int = 2,
                   crop_dir: str | Path | None = None, px_per_mm_of: Mapping[str, Callable | None] | None = None,
                   workers: int = 3, stop: Any = None) -> AssemblyResult:
    """`views`: (still dict, full-resolution PIL image) for the stills to look at (pick_stills). Returns the findings,
    their report regions and a record for the run row. Raises only on a model error (the caller records it); a spend
    pause is the not_inspected finding."""
    from repairos.vision.crops import as_pil, context_view, cut, outline
    from repairos.vision.inspect import VLM_MAX_SIDE

    t0 = time.perf_counter()
    res = AssemblyResult(record={"stills": [s["still_id"] for s, _ in views], "calls": 0})
    if not views:
        res.record["skipped"] = "no photo of the object"
        return res
    images = [as_pil(im) for _s, im in views]
    previews = []
    for im in images:
        s = min(1.0, PREVIEW_SIDE / max(im.size))
        previews.append(im.resize((max(1, round(im.width * s)), max(1, round(im.height * s)))) if s < 1 else im)
    try:
        res.record["calls"] += 1
        check = judge.assembly_check(previews, object_words)
    except SpendLimit:
        f = finding_not_run(scan_id=scan_id, reason=PAUSED, still_ids=res.record["stills"])
        res.findings.append(f)
        res.regions[f["finding_id"]] = {"part_class": "assembly check", "inspected_by": []}
        res.record["paused"] = True
        return res
    res.record.update(object=check.object_words, opened=check.opened, model=check.model,
                      items=[{"part": i.part, "status": i.status, "problem": i.problem,
                              "still_id": views[i.photo][0]["still_id"] if i.photo is not None else None}
                             for i in check.items])

    # claims with a usable place, in the model's order (most important first)
    claims = []
    for i in check.items:
        if i.status != "problem":
            continue
        if i.photo is None or i.box is None:
            continue   # a claim nobody can point at is not evidence (recorded in `items`)
        pv, im = previews[i.photo], images[i.photo]
        sx, sy = im.width / pv.width, im.height / pv.height
        box = (i.box[0] * sx, i.box[1] * sy, i.box[2] * sx, i.box[3] * sy)
        if any(p == i.photo and same_place(box, b) for _c, p, b in claims):
            res.record.setdefault("claims_same_place", []).append(i.part)   # one place, one close look
            continue
        claims.append((i, i.photo, box))
    over = claims[max_confirm:]
    claims = claims[:max_confirm]
    res.record["claims_not_confirmed_budget"] = len(over)

    def confirm(c: tuple) -> tuple[tuple, Any, Any, Any]:
        item, p, box = c
        if stop is not None and stop.is_set():
            from repairos.pipeline.stopping import WorkerStopping

            raise WorkerStopping("the worker is stopping: no further vision-model call")
        im = images[p]
        crop = cut(im, box, still_id=views[p][0]["still_id"], max_side=VLM_MAX_SIDE, pad_frac=CONFIRM_PAD_FRAC,
                   min_side=CONFIRM_MIN_SIDE)
        base = context_view(im)
        ctx = outline(base, crop.region_px, base.width / im.width)
        of = (px_per_mm_of or {}).get(views[p][0]["still_id"])
        s = of(box) if of else None
        kw = {"px_per_mm": round(s * crop.scale, 1)} if s else {}
        try:
            return c, crop, judge.inspect_state(crop.image, ctx, item.part, **kw), None
        except SpendLimit as e:
            return c, crop, None, e

    from dataclasses import replace

    confirmed = []
    if claims:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(claims)))) as ex:
            confirmed = list(ex.map(confirm, claims))
    res.record["calls"] += len(claims)
    crop_dir = Path(crop_dir) if crop_dir else None
    outcomes = []
    for k, ((item, p, box), crop, state, paused) in enumerate(confirmed):
        sid = views[p][0]["still_id"]
        item = replace(item, box=box)
        if paused is not None:
            f = _base(scan_id, _fid(scan_id, ASSEMBLY_REF, sid, item.part, item.problem), _subject(item.kind, sid, k),
                      "not_inspected", [f"the {item.part} in still {sid} was to get a close look for whether it is in "
                                        f"place, but it was not inspected: {PAUSED}"],
                      {"still_ids": [sid], "crop_box_px": [max(0, int(round(v))) for v in box]})
            res.findings.append(f)
            res.regions[f["finding_id"]] = {"part_class": item.part, "inspected_by": []}
            outcomes.append({"part": item.part, "claim": item.problem, "confirm": "paused"})
            continue
        path = None
        if crop_dir:
            crop_dir.mkdir(parents=True, exist_ok=True)
            path = str(crop_dir / f"{sid}_{ASSEMBLY_REF}{k}.jpg")
            crop.image.save(path, quality=92)
        f = finding_from_state(scan_id=scan_id, still_id=sid, k=k, item=item, crop=crop, state=state, crop_file=path,
                               sharpness=views[p][0].get("sharpness"))
        outcomes.append({"part": item.part, "claim": item.problem, "confirm": state.state, "finding": f["state"]})
        if f["state"] == "ok":
            continue   # the close look does not confirm the claim: no row (the overview was wrong)
        res.findings.append(f)
        res.regions[f["finding_id"]] = {"part_class": state.part_class if state.part_class not in ("", "unknown")
                                        else item.part, "inspected_by": ["vlm"]}
    res.record["confirms"] = outcomes

    gaps = [i for i in check.items if i.status == "not_visible"][:max_gaps]
    for k, item in enumerate(gaps):
        f = finding_gap(scan_id=scan_id, k=k, item=item, still_ids=res.record["stills"])
        res.findings.append(f)
        res.regions[f["finding_id"]] = {"part_class": item.part, "inspected_by": ["vlm"]}
    res.record["gaps"] = len(gaps)
    res.record["seconds"] = round(time.perf_counter() - t0, 3)
    return res
