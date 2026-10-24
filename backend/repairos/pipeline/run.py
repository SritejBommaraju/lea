"""The Phase 1 pipeline for one scan (task B10). No Grain calls.

    inspect   per still: detector -> full-res crops -> (classifier, when vision.inspect has one selected) -> Claude
              VLM -> Findings (repairos.vision.inspect). Label stills need OCR (M8): until it exists they are
              not_inspected with that reason; a skipped still is not_inspected (I3).
    fuse      Jev judgments + code decisions -> Suspects and one NextAction (repairos.fusion.fuse).
    locate    each finding's crop box -> its still's pose -> a place on the scan (repairos.locate, when installed;
              else every location says "not computed" with the reason).
    report    verdict by truth table, plain words by Claude from words only, validated against `report` (B11).

Stage results go through STATE transitions, so the row rules (I1-I4) check them: findings (`finding.create`),
suspects, the next action, the report (`report.publish`) and the run row (`pipeline-run`: per-stage status, per-still
results, the error). A stage that fails marks the run `failed` with the stage and the error and raises
PipelineFailed; nothing is defaulted in its place (GEN3).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repairos.config import ROOT
from repairos.contracts import validate
from repairos.pipeline import locate_iface
from repairos.report import build_report, honesty_violations, report_id_for, write_summary
from repairos.sessions.layout import EXT_BY_TYPE, ext_for, map_files
from repairos.slots.blob import REF_PREFIX
from repairos.slots.state import AlreadyClaimed, RevConflict

log = logging.getLogger(__name__)

STILL_WAIT_S = 900.0     # how long a run waits for a still another worker is inspecting
POLL_S = 0.5
OCR_NOT_BUILT = "reading label text needs OCR, which is not built yet (task M8)"
SKIPPED = "the user skipped this close-up"


class PipelineError(RuntimeError):
    """A pipeline precondition is not met (configuration, missing object). Raised inside a stage."""


class PipelineFailed(RuntimeError):
    """A stage failed. The run row says `failed` with `stage` and `message`."""

    def __init__(self, scan_id: str, stage: str, message: str, still_id: str | None = None):
        self.scan_id, self.stage, self.still_id = scan_id, stage, still_id
        super().__init__(f"scan {scan_id}: stage {stage}{f' (still {still_id})' if still_id else ''} failed: "
                         f"{message}")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_id_for(scan_id: str) -> str:
    return f"run_{scan_id}"


def data_dir() -> Path:
    return Path(os.environ.get("REPAIROS_DATA_DIR") or ROOT / "data")


@dataclass
class Context:
    """What one pipeline run needs. Slots load on first use, so a run that needs no detector never loads one."""

    state: Any
    blobs: Any
    registry: Any = None
    worker: str = "inline"
    root: Path | None = None
    _slots: dict[str, Any] = field(default_factory=dict)
    _locator: Any = None
    _locator_loaded: bool = False

    def __post_init__(self):
        if self.registry is None:
            from repairos.registry import Registry
            self.registry = Registry()
        self.root = Path(self.root) if self.root else data_dir() / "pipeline"

    def slot(self, name: str) -> Any:
        if name not in self._slots:
            self._slots[name] = self.registry.slot(name)
        return self._slots[name]

    def selected(self, name: str) -> str:
        return self.registry.selected(name)

    @property
    def locator(self) -> locate_iface.Locator | None:
        if not self._locator_loaded:
            self._locator, self._locator_loaded = locate_iface.load_locator(), True
        return self._locator

    def workdir(self, scan_id: str) -> Path:
        p = self.root / scan_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def local_file(self, scan_id: str, ref: str) -> Path:
        """A blob as a local file (the vision code and the locator read files)."""
        if not ref.startswith(REF_PREFIX):
            raise PipelineError(f"{ref!r} is not a blob ref")
        sha = ref[len(REF_PREFIX):]
        files = self.workdir(scan_id) / "files"
        files.mkdir(exist_ok=True)
        hit = next(iter(files.glob(f"{sha}.*")), None)
        if hit:
            return hit
        data = self.blobs.get(ref)
        p = files / f"{sha}{EXT_BY_TYPE.get(self.blobs.content_type(ref)) or ext_for(data)}"
        p.write_bytes(data)
        return p


# ------------------------------------------------------------------------------------------------- run row helpers
async def _update(ctx: Context, scan: dict, patch: dict) -> dict:
    row = await ctx.state.transition("pipeline-run", run_id_for(scan["scan_id"]), "pipeline.update",
                                     {"scan_id": scan["scan_id"], "session_id": scan["session_id"], **patch})
    return row.data


async def _run_row(ctx: Context, scan_id: str) -> dict | None:
    row = await ctx.state.get("pipeline-run", run_id_for(scan_id))
    return row.data if row else None


async def _fail(ctx: Context, scan: dict, stage: str, e: BaseException, still_id: str | None = None,
                retrying: bool = False) -> PipelineFailed:
    msg = f"{type(e).__name__}: {e}".strip() or type(e).__name__
    err = {"stage": stage, "message": msg[:2000], "at": _now(), "retrying": retrying, "still_id": still_id}
    await _update(ctx, scan, {"status": "failed", "error": err, "stages": {stage: {"status": "failed",
                                                                                   "finished_at": _now()}}})
    return PipelineFailed(scan["scan_id"], stage, msg, still_id)


async def _scan(ctx: Context, scan_id: str) -> dict:
    row = await ctx.state.get("scan-package", scan_id)
    if row is None:
        raise PipelineError(f"no scan-package {scan_id}")
    return row.data


# ---------------------------------------------------------------------------------------------- stage: capture
def capture_facts(scan: dict) -> dict:
    kfs = scan.get("keyframes") or []
    return {"status": "done", "finished_at": _now(), "keyframes": len(kfs), "points": scan.get("point_count"),
            "depth_source": locate_iface.depth_source(scan), "mesh": bool(scan.get("mesh_file"))}


async def record_capture(ctx: Context, scan_id: str) -> dict:
    """The scan package arrived (gateway `detect` job on a scan): record what it holds. Stills are inspected on
    their own jobs; the scan package has no detection stage in Phase 1."""
    scan = await _scan(ctx, scan_id)
    await _update(ctx, scan, {"stages": {"capture": capture_facts(scan)}})
    return {"stage": "capture", "scan_id": scan_id}


# ---------------------------------------------------------------------------------------------- stage: inspect
def _fid(*parts: object) -> str:
    return "f_" + hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:12]


def not_inspected_finding(scan_id: str, still_id: str, what: str, reason: str, source: str) -> dict:
    return {"finding_id": _fid(scan_id, still_id, what), "scan_id": scan_id,
            "subject": {"kind": "part", "ref": f"{still_id}/{what}"}, "state": "not_inspected", "source": source,
            "confidence": 0.0, "is_proof": False, "evidence": [f"still {still_id} was not inspected: {reason}"],
            "evidence_refs": {"still_ids": [still_id]}, "design_trust": "low", "created_at": _now()}


def _det_index(ref: str) -> int | None:
    tail = ref.rpartition("/")[2]
    return int(tail[3:]) if tail.startswith("det") and tail[3:].isdigit() else None


# The VLM's own generic part name, from the lead sentence vision.inspect.finding_from_crop writes.
def vlm_part_class(f: dict) -> str | None:
    lead = (f.get("evidence") or [""])[0].strip()
    m = re.match(r"^could not judge the (.+?) in still \S+$", lead) or re.match(r"^(.+?) in still \S+ looks .+$", lead)
    return m.group(1).strip() if m else None


def regions_for(findings: list[dict], detections: list[dict], whole: str,
                rollups: dict[str, dict] | None = None) -> dict[str, dict]:
    """finding_id -> {part_class, inspected_by} for one still's findings from vision.inspect. The part class is the
    VLM's generic name for what it looked at (detector labels are often wrong, e.g. a damage prompt on an intact
    part); a region no VLM looked at keeps the detector's label from the generic vocabulary, or `unknown_part` when
    that label was a weak match (vision.inspect.region_part_class)."""
    labels = {d["index"]: d.get("part_class") or d["label"] for d in detections}
    out = {}
    rollups = rollups or {}
    for f in findings:
        if f["finding_id"] in rollups:   # detector regions not kept (too small / past the cap): counted, not looked at
            r = rollups[f["finding_id"]]
            out[f["finding_id"]] = {"part_class": r["part_class"], "inspected_by": ["detector"],
                                    "regions": int(r["regions"])}
            continue
        idx = _det_index(f["subject"]["ref"])
        if idx is not None and idx not in labels:
            raise PipelineError(f"finding {f['finding_id']} names detection {idx}, which the detector did not return")
        looked = f["state"] != "not_inspected" and idx is not None
        part = (vlm_part_class(f) if looked else None) or (labels[idx] if idx is not None else whole)
        out[f["finding_id"]] = {"part_class": part, "inspected_by": ["detector", "vlm"] if looked else ["detector"]}
    return out


async def _store_findings(ctx: Context, findings: list[dict]) -> tuple[list[str], list[str]]:
    """finding.create each (rules I1, I3, I4). A finding already stored by an earlier, interrupted attempt keeps
    its first answer; its id is reported in `kept`."""
    ids, kept = [], []
    for f in findings:
        try:
            await ctx.state.transition("finding", f["finding_id"], "finding.create", f)
        except RevConflict:
            kept.append(f["finding_id"])
        ids.append(f["finding_id"])
    return ids, kept


def _inspect_sync(ctx: Context, scan_id: str, still: dict) -> tuple[list[dict], list[dict], str, str, dict]:
    from repairos.vision.inspect import inspect_still

    detector, judge = ctx.slot("DET"), ctx.slot("VLM")
    path = ctx.local_file(scan_id, still["image_file"])
    crop_dir = ctx.workdir(scan_id) / "crops" / still["still_id"]
    res = inspect_still(path, still_id=still["still_id"], scan_id=scan_id, detector=detector, judge=judge,
                        crop_dir=crop_dir, sharpness=still.get("sharpness"))
    findings = []
    for model in res.findings:
        f = model.model_dump(mode="json", exclude_none=True)
        refs = f.get("evidence_refs") or {}
        crop = refs.get("crop_file")
        if crop and not crop.startswith(REF_PREFIX):
            refs["crop_file"] = ctx.blobs.put(Path(crop).read_bytes(), "image/jpeg")
        findings.append(f)
    dets = [{"index": i, "label": d.label, "box_px": [max(0, int(round(v))) for v in d.box],
             "part_class": res.part_classes.get(i, d.label)} for i, d in enumerate(res.detections)]
    vlm = " then ".join(getattr(judge, "models", ())) or type(judge).__name__
    dropped = {i: why for why, idx in res.dropped.items() for i in idx}
    for d in dets:
        if d["index"] in dropped:
            d["not_kept"] = dropped[d["index"]]
    return findings, dets, getattr(detector, "model_id", type(detector).__name__), vlm, res.rollups


async def _inspect_one(ctx: Context, scan: dict, still: dict) -> dict:
    sid, scan_id = still["still_id"], scan["scan_id"]
    t0 = time.monotonic()
    stage: dict[str, Any] = {}
    if still.get("user_skipped"):
        findings = [not_inspected_finding(scan_id, sid, "whole", SKIPPED, "repairos.vision")]
        regions = {findings[0]["finding_id"]: {"part_class": "whole photo", "inspected_by": []}}
    elif still["kind"] == "label":
        ocr = ctx.selected("OCRB")
        if ocr != "none":
            raise PipelineError(f"OCRB={ocr} is selected, but the pipeline has no OCR stage until task M8")
        findings = [not_inspected_finding(scan_id, sid, "label", OCR_NOT_BUILT, "repairos.ocr")]
        regions = {findings[0]["finding_id"]: {"part_class": "label", "inspected_by": []}}
    else:
        findings, dets, det_id, vlm, rollups = await asyncio.to_thread(_inspect_sync, ctx, scan_id, still)
        regions = regions_for(findings, dets, "whole photo", rollups)
        stage.update(detections=dets, detector=det_id, vlm=vlm)
    ids, kept = await _store_findings(ctx, findings)
    stage.update(status="done", finding_ids=ids, regions=regions, finished_at=_now(),
                 seconds=round(time.monotonic() - t0, 3))
    if kept:
        stage["kept_earlier_answers"] = kept
    return stage


async def inspect_still_stage(ctx: Context, still_id: str) -> dict:
    """Inspect one still once (atomic claim on the run row). Returns the still's stage record. A still already
    inspected is not inspected again; one another worker is inspecting is waited for."""
    row = await ctx.state.get("still", still_id)
    if row is None:
        raise PipelineError(f"no still {still_id}")
    still = row.data
    scan = await _scan(ctx, still["scan_id"])
    rid = run_id_for(scan["scan_id"])
    try:
        await ctx.state.transition("pipeline-run", rid, "pipeline.claim_still",
                                   {"still_id": still_id, "worker": ctx.worker, "at": _now(),
                                    "scan_id": scan["scan_id"], "session_id": scan["session_id"]})
    except AlreadyClaimed as e:
        if e.status == "done":
            return (await _run_row(ctx, scan["scan_id"]))["stills"][still_id]
        return await _wait_for_still(ctx, scan, still_id)
    try:
        stage = await _inspect_one(ctx, scan, still)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        await _update(ctx, scan, {"stills": {still_id: {"status": "failed", "error": msg[:2000],
                                                        "finished_at": _now()}}})
        raise
    await _update(ctx, scan, {"stills": {still_id: stage}})
    return stage


async def _wait_for_still(ctx: Context, scan: dict, still_id: str) -> dict:
    deadline = time.monotonic() + STILL_WAIT_S
    while time.monotonic() < deadline:
        st = ((await _run_row(ctx, scan["scan_id"])) or {}).get("stills", {}).get(still_id, {})
        if st.get("status") == "done":
            return st
        if st.get("status") == "failed":
            raise PipelineError(f"still {still_id} failed on worker {st.get('worker')}: {st.get('error')}")
        await asyncio.sleep(POLL_S)
    raise PipelineError(f"still {still_id} is still being inspected by another worker after {STILL_WAIT_S:.0f} s")


# --------------------------------------------------------------------------------------------- the whole run
def _distinct(values: list[str | None], none: str) -> str:
    vals = sorted({v for v in values if v})
    return ", ".join(vals) if vals else none


def _classifier(ctx: Context) -> str:
    impl = ctx.selected("CLS")
    if impl == "none":
        return "not selected (CLS=none)"
    return f"{impl} ({getattr(ctx.slot('CLS'), 'model_id', 'model id not reported')})"


async def run_scan(ctx: Context, scan_id: str) -> dict:
    """Run every stage for one scan and publish its report. Returns the report. Raises PipelineFailed."""
    scan = await _scan(ctx, scan_id)
    prev = await _run_row(ctx, scan_id) or {}
    attempt = int(prev.get("attempt") or 0) + 1
    await _update(ctx, scan, {"status": "running", "attempt": attempt, "started_at": _now(), "error": None,
                              "report_id": None, "finished_at": None,
                              "stages": {"capture": capture_facts(scan),
                                         **{s: {"status": "pending"} for s in ("inspect", "fuse", "locate",
                                                                               "report")}}})

    # inspect
    t0 = time.monotonic()
    stills = [r.data for r in await ctx.state.list("still", scan_id=scan_id)]
    await _update(ctx, scan, {"stages": {"inspect": {"status": "running", "started_at": _now()}}})
    stages: dict[str, dict] = {}
    for st in sorted(stills, key=lambda s: s["still_id"]):
        try:
            stages[st["still_id"]] = await inspect_still_stage(ctx, st["still_id"])
        except Exception as e:
            raise await _fail(ctx, scan, "inspect", e, st["still_id"]) from e
    await _update(ctx, scan, {"stages": {"inspect": {"status": "done", "finished_at": _now(), "stills": len(stills),
                                                     "seconds": round(time.monotonic() - t0, 3)}}})

    # fuse
    t0 = time.monotonic()
    await _update(ctx, scan, {"stages": {"fuse": {"status": "running", "started_at": _now()}}})
    try:
        from repairos.fusion import fuse

        findings = [r.data for r in await ctx.state.list("finding", scan_id=scan_id)]
        readings = [r.data for r in await ctx.state.list("meter-reading", scan_id=scan_id)]
        engine = ctx.slot("FUSION") if findings or readings else None
        result = await asyncio.to_thread(fuse, scan_id, findings, readings, scan.get("symptom"), engine=engine)
        for s in result.suspects:
            await ctx.state.transition("suspect", s["suspect_id"], "suspect.update_from_fusion", s)
        await ctx.state.transition("next-action", result.next_action["action_id"], "next_action.issue",
                                   result.next_action)
        suspects = [r.data for r in await ctx.state.list("suspect", scan_id=scan_id)]
        jev = result.answers.model if result.answers else "not called (nothing to judge)"
        asked = {k for b in result.batches for k in b.subjects}
    except Exception as e:
        raise await _fail(ctx, scan, "fuse", e) from e
    await _update(ctx, scan, {"stages": {"fuse": {"status": "done", "finished_at": _now(), "jev_model": jev,
                                                  "jev_requests": len(result.batches),
                                                  "jev_questions": len(result.questions),
                                                  "subjects_judged": len(asked),
                                                  "subjects_decided_in_code": len(result.fusion_state.subjects)
                                                  - len(asked),
                                                  "outcome": result.outcome, "suspects": len(result.suspects),
                                                  "next_action_id": result.next_action["action_id"],
                                                  "seconds": round(time.monotonic() - t0, 3)}}})

    # locate
    t0 = time.monotonic()
    try:
        regions = {fid: info for st in stages.values() for fid, info in (st.get("regions") or {}).items()}
        locations, locate_words = await locate_findings(ctx, scan, stills, findings, regions)
    except Exception as e:
        raise await _fail(ctx, scan, "locate", e) from e
    computed = sum(1 for v in locations.values() if v["status"] == "computed")
    await _update(ctx, scan, {"stages": {"locate": {"status": "done", "finished_at": _now(), "locator": locate_words,
                                                    "computed": computed, "not_computed": len(locations) - computed,
                                                    "seconds": round(time.monotonic() - t0, 3)}}})

    # report
    t0 = time.monotonic()
    await _update(ctx, scan, {"stages": {"report": {"status": "running", "started_at": _now()}}})
    try:
        llm = ctx.slot("LLM")
        models = {
            "detector": _distinct([s.get("detector") for s in stages.values()], "not run (no still needed it)"),
            "classifier": _classifier(ctx),
            "vlm": _distinct([s.get("vlm") for s in stages.values()], "not run (no still needed it)"),
            "ocr": "not selected (OCRB=none)" if ctx.selected("OCRB") == "none" else ctx.selected("OCRB"),
            "summary_writer": llm.model,
            "jev": jev,
            "locate": locate_words,
        }
        draft = build_report(scan=scan, stills=stills, findings=findings, suspects=suspects,
                             next_action=result.next_action, regions=regions, locations=locations, models=models)
        draft["summary"] = await asyncio.to_thread(write_summary, draft, llm, symptom=scan.get("symptom"))
        validate("report", draft)
        problems = honesty_violations(draft, findings)
        if problems:
            raise PipelineError("honesty check failed: " + "; ".join(problems))
        await ctx.state.transition("report", draft["report_id"], "report.publish", draft)
    except Exception as e:
        raise await _fail(ctx, scan, "report", e) from e
    await _update(ctx, scan, {"status": "done", "report_id": draft["report_id"], "finished_at": _now(),
                              "stages": {"report": {"status": "done", "finished_at": _now(),
                                                    "verdict": draft["verdict"],
                                                    "seconds": round(time.monotonic() - t0, 3)}}})
    return draft


def _local_scan(ctx: Context, scan: dict) -> dict:
    """The scan package with every file field (points, mesh, keyframes) as an absolute local path."""
    sid = scan["scan_id"]
    out = map_files("scan-package", scan, lambda v, _b: str(ctx.local_file(sid, v)))
    if scan.get("mesh_file"):
        out["mesh_file"] = str(ctx.local_file(sid, scan["mesh_file"]))
    return out


async def locate_findings(ctx: Context, scan: dict, stills: list[dict], findings: list[dict],
                          regions: dict[str, dict]) -> tuple[dict[str, dict], str]:
    """finding_id -> report location for every finding, and the locator's provenance words. A LiDAR scan
    (depth_source lidar, also when absent) ray-casts each box with try_locate; a no-LiDAR scan triangulates across
    stills of the same target (locate_iface.triangulation_views); otherwise the reason is recorded."""
    locator = ctx.locator
    lidar = locate_iface.depth_source(scan) == "lidar"
    words = (f"{locator.version} ({'ray cast on the scan' if lidar else 'triangulation, no-LiDAR scan'})" if locator
             else f"not available ({locate_iface.NOT_INSTALLED})")
    by_still = {s["still_id"]: s for s in stills}
    out: dict[str, dict] = {}
    scan_local = None
    still_local: dict[str, dict] = {}
    part_class = {fid: r["part_class"] for fid, r in regions.items()}

    def local_still(sid: str) -> dict:
        if sid not in still_local:
            still_local[sid] = map_files("still", by_still[sid],
                                         lambda v, _b: str(ctx.local_file(scan["scan_id"], v)))
        return still_local[sid]

    for f in findings:
        refs = f.get("evidence_refs") or {}
        sid = (refs.get("still_ids") or [None])[0]
        box = refs.get("crop_box_px")
        if locator is None:
            out[f["finding_id"]] = locate_iface.not_computed(locate_iface.NOT_INSTALLED)
        elif not sid or sid not in by_still or not box:
            out[f["finding_id"]] = locate_iface.not_computed(locate_iface.NO_BOX)
        elif "camera" not in by_still[sid]:
            out[f["finding_id"]] = locate_iface.not_computed(locate_iface.NO_POSE)
        elif lidar:
            if scan_local is None:
                scan_local = await asyncio.to_thread(_local_scan, ctx, scan)
            out[f["finding_id"]] = await asyncio.to_thread(locator.locate, scan_local, local_still(sid), tuple(box))
        else:
            views = locate_iface.triangulation_views(f, findings, by_still, part_class)
            out[f["finding_id"]] = (await asyncio.to_thread(locator.triangulate, views) if views
                                    else locate_iface.not_computed(locate_iface.NO_SECOND_VIEW))
    return out, words


async def run_session(session_id: str, state: Any, blobs: Any, registry: Any = None, worker: str = "replay"
                      ) -> list[dict]:
    """Every scan of a session through the pipeline (tools/replay.py, `make replay`). Returns the reports."""
    ctx = Context(state=state, blobs=blobs, registry=registry, worker=worker)
    scans = sorted(r.id for r in await state.list("scan-package", session_id=session_id))
    if not scans:
        raise PipelineError(f"session {session_id} has no scan package")
    return [await run_scan(ctx, scan_id) for scan_id in scans]
