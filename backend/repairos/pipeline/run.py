"""The Phase 1 pipeline for one scan (task B10).

    select    which photo stills to inspect (repairos.pipeline.select): near-duplicate views by pose, depth and
              sharpness are covered by the kept one (not_inspected, naming the photo that covers it, I3); close-ups
              and new viewpoints are kept, at most `max_stills` (config/pipeline.yaml).
    inspect   per kept still, `stills_at_once` at a time: detector (one shared model, one still at a time on the GPU,
              often already run when the photo's bytes arrived: Context.detections_for) -> full-res crops ->
              (classifier, when selected) -> Claude VLM, at most `max_crops_per_still` calls per still and
              `vlm_calls_at_once` in flight in the whole worker -> Findings (repairos.vision.inspect). A place two
              stills show is judged once (repairos.pipeline.regions). Label stills need OCR (M8): until it exists
              they are not_inspected with that reason; a skipped still is not_inspected (I3).
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
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from repairos.config import ROOT
from repairos.contracts import validate
from repairos.fusion.state import vlm_part_class
from repairos.pipeline import locate_iface, segments
from repairos.pipeline.config import load_pipeline_config
from repairos.pipeline.stopping import WorkerStopping, is_stopping  # noqa: F401 (WorkerStopping re-exported)
from repairos.report import build_report, honesty_violations, report_id_for, write_summary
from repairos.sessions.layout import EXT_BY_TYPE, ext_for, map_files
from repairos.slots.blob import REF_PREFIX
from repairos.slots.state import AlreadyClaimed, RevConflict, StaleReport
from repairos.spend import PAUSED, SpendLimit

log = logging.getLogger(__name__)

STILL_WAIT_S = 900.0     # how long a run waits for a still another worker is inspecting
CLAIM_BEAT_S = 15.0      # a worker refreshes `heartbeat_at` on every still claim it holds this often
CLAIM_STALE_S = 90.0     # a `running` claim with no heartbeat for this long belongs to a worker that is gone
POLL_S = 0.5
OCR_NOT_BUILT = "reading label text needs OCR, which is not built yet (task M8)"
SKIPPED = "the user skipped this close-up"
PAUSED_LIMIT = ("Some areas were not checked: the vision model was paused because a usage limit was reached. "
                "Scan again later to check them.")
PAUSED_SUMMARY = {"text": "This report was built without its plain-words summary: a usage limit was reached, so the "
                          "writer was paused. The findings below are what was checked before the pause; areas marked "
                          "not inspected were not looked at.",
                  "written_by": "repairos (usage limit reached)", "from_words_only": True}
DETECTIONS_KEPT = 256    # detector answers kept in memory per worker (by blob), for stills that arrive later


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
    config: dict | None = None
    _slots: dict[str, Any] = field(default_factory=dict)
    _locator: Any = None
    _locator_loaded: bool = False

    def __post_init__(self):
        if self.registry is None:
            from repairos.registry import Registry
            self.registry = Registry()
        self.root = Path(self.root) if self.root else data_dir() / "pipeline"
        self.config = self.config or load_pipeline_config()
        self._lock = threading.Lock()          # slot loading, the detection cache, the VLM pool
        self.gpu = threading.Lock()            # one detector pass at a time (one model, one GPU)
        self._detections: OrderedDict[str, Future] = OrderedDict()
        self._vlm_pool: ThreadPoolExecutor | None = None
        self._shares: dict[str, Any] = {}
        self._scan_locks: dict[str, asyncio.Lock] = {}
        self._background: set[asyncio.Task] = set()
        self._side_gate: asyncio.Semaphore | None = None
        self._prewarmed: dict[str, set] = {}
        self.held: set[tuple[str, str]] = set()   # (scan_id, still_id) claims this worker holds (beat_claims)
        self.stopping = threading.Event()   # set on shutdown: inspection threads stop before their next model call
        self._beater: asyncio.Task | None = None

    def hold(self, scan_id: str, still_id: str) -> None:
        """Record a claim this worker now holds and make sure its heartbeat runs (in every mode: run_forever,
        drain / --once, a direct call), for as long as any claim is held."""
        self.held.add((scan_id, still_id))
        if self._beater is None or self._beater.done():
            self._beater = asyncio.get_running_loop().create_task(self._beat_while_held())

    async def _beat_while_held(self) -> None:
        while self.held:
            await asyncio.sleep(CLAIM_BEAT_S)
            try:
                await beat_claims(self)
            except Exception as e:  # noqa: BLE001 - a missed beat is retried; the claim goes stale only after many
                log.error("claim heartbeat failed: %s", e)

    def spawn(self, coro: Any) -> asyncio.Task:
        """Run side work (identity, mesh) in the background, one item at a time, never holding a job slot: the
        report path (stills, fuse, locate, report) never waits on it."""
        if self._side_gate is None:
            self._side_gate = asyncio.Semaphore(1)

        async def gated():
            async with self._side_gate:
                return await coro
        task = asyncio.create_task(gated())
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def start(self, coro: Any) -> asyncio.Task:
        """Run network-bound side work (web searches) in the background, outside the one-at-a-time gate of spawn,
        so a slow search never holds back the mesh or the identity."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def settle(self) -> None:
        """Wait for background side work (tests, shutdown)."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    def scan_lock(self, scan_id: str) -> asyncio.Lock:
        """Still-selection decisions of one scan, one at a time (this worker process; one event loop)."""
        if scan_id not in self._scan_locks:
            self._scan_locks[scan_id] = asyncio.Lock()
        return self._scan_locks[scan_id]

    def slot(self, name: str) -> Any:
        """One instance per slot, shared by every job and thread of this worker (the detector loads once)."""
        with self._lock:
            if name not in self._slots:
                inst = self.registry.slot(name)
                if name == "DET" and hasattr(inst, "tile_px"):
                    inst.tile_px = self.config["detector_tile_px"]
                if name == "DET" and hasattr(inst, "half"):
                    inst.half = self.config["detector_half"]
                self._slots[name] = inst
            return self._slots[name]

    @property
    def vlm_pool(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._vlm_pool is None:
                self._vlm_pool = ThreadPoolExecutor(max_workers=self.config["vlm_calls_at_once"],
                                                    thread_name_prefix="vlm")
            return self._vlm_pool

    def share(self, scan_id: str) -> Any:
        from repairos.pipeline.regions import SameRegions

        with self._lock:
            if scan_id not in self._shares:
                self._shares[scan_id] = SameRegions(self.config["same_region_m"])
            return self._shares[scan_id]

    def drop_share(self, scan_id: str) -> None:
        with self._lock:
            self._shares.pop(scan_id, None)

    def has_detections(self, ref: str) -> bool:
        with self._lock:
            f = self._detections.get(ref)
            return bool(f is not None and f.done() and f.exception() is None)

    def detections_for(self, scan_id: str, ref: str, image: Any = None) -> tuple[list, bool]:
        """(the detector's boxes for the still image `ref`, whether they were already computed). One pass per image
        per worker: a second caller waits for the first (an upload-time prefetch and the run share one pass)."""
        with self._lock:
            fut = self._detections.get(ref)
            mine = fut is None
            if mine:
                fut = self._detections[ref] = Future()
                while len(self._detections) > DETECTIONS_KEPT:
                    self._detections.popitem(last=False)
        if not mine:
            return fut.result(), True
        try:
            from repairos.vision.crops import load_still

            im = image if image is not None else load_still(self.local_file(scan_id, ref))
            det = self.slot("DET")
            if self.stopping.is_set():
                raise WorkerStopping("the worker is stopping")
            with self.gpu:
                dets = det.detect(im)
        except BaseException as e:
            fut.set_exception(e)
            with self._lock:
                if self._detections.get(ref) is fut:
                    del self._detections[ref]
            raise
        fut.set_result(dets)
        return dets, False

    def depth(self, scan_id: str, still: dict) -> Any:
        """The still's depth grid in metres, or None when it has none or it cannot be read."""
        if not still.get("depth_file"):
            return None
        from repairos.importers.images import read_depth_mm

        try:
            return read_depth_mm(self.local_file(scan_id, still["depth_file"]))
        except Exception as e:  # noqa: BLE001 - no depth only means no geometry; the still is still inspected
            log.warning("still %s: depth map unreadable (%s); judged without geometry", still["still_id"], e)
            return None

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
    ctx.spawn(_mesh_quietly(ctx, scan_id))   # the 3D view, built beside the report path, never in front of it
    return {"stage": "capture", "scan_id": scan_id, "mesh": "building"}


async def _mesh_quietly(ctx: Context, scan_id: str) -> None:
    try:
        await ensure_segment_alignment(ctx, scan_id)   # a flipped bottom segment joins the mesh once registered
    except Exception as e:  # noqa: BLE001 - recorded on the row by ensure_segment_alignment; never blocks the mesh
        log.warning("segment alignment for scan %s: %s", scan_id, e)
    try:
        await ensure_mesh(ctx, scan_id)
    except Exception as e:  # noqa: BLE001 - ensure_mesh records its own failures; this only guards the task
        log.warning("mesh for scan %s: %s", scan_id, e)
    from repairos.recreation.service import recreation_quietly
    ctx.spawn(recreation_quietly(ctx, scan_id))   # the photo-real recreation, after the mesh, also never in front


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


def still_words(stills: list[dict]) -> dict[str, str]:
    """{still_id: "photo N"}: stills numbered from one by capture time, then id (the order the user took them)."""
    order = sorted(stills, key=lambda s: (s.get("captured_at") or "", s["still_id"]))
    return {s["still_id"]: f"photo {i}" for i, s in enumerate(order, 1)}


def regions_for(findings: list[dict], detections: list[dict], whole: str,
                rollups: dict[str, dict] | None = None) -> dict[str, dict]:
    """finding_id -> {part_class, inspected_by} for one still's findings from vision.inspect. The part class is the
    VLM's generic name for what it looked at (detector labels are often wrong, e.g. a damage prompt on an intact
    part); a region no VLM looked at keeps the detector's label from the generic vocabulary, or `unknown_part` when
    that label was a weak match (vision.inspect.region_part_class)."""
    from repairos.vision.inspect import TILE_REF   # close tiles (vision.inspect): judged by the VLM, no detector box

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
        tile = f["subject"]["ref"].rpartition("/")[2].startswith(TILE_REF)
        looked = f["state"] != "not_inspected" and (idx is not None or tile)
        part = (vlm_part_class(f) if looked else None) or (labels[idx] if idx is not None else whole)
        out[f["finding_id"]] = {"part_class": part, "inspected_by": (["vlm"] if tile else ["detector", "vlm"])
                                if looked else ["detector"]}
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


OBJECT_ROI_GROW = 0.15   # the object box, grown by this share of each side, projected into a still: its regions


def object_roi(scan: Mapping[str, Any] | None, still: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    """Where the scanned object can be in a still (x0, y0, x1, y1 full-resolution pixels): the object box of the
    still's own capture segment (segment 0's, or a flipped segment's own box in its own world, where its photos' poses
    are) grown by OBJECT_ROI_GROW and projected. None when there is no box or pose, or the box is partly behind the
    camera (then the whole photo is inspected)."""
    from repairos.locate.camera import Pinhole, Pose, project
    from repairos.recon.tsdf import ObjectBox

    if not scan or not still.get("camera") or not still.get("intrinsics"):
        return None
    k = int(still.get("segment") or 0)
    box = scan.get("crop_box")
    cap = None
    if k and k not in segments.same_frame_segments(scan):
        box = next((g.get("crop_box") for g in scan.get("segments") or [] if int(g.get("segment") or 0) == k), None)
        # the phone's box round a flipped object is generous (40 x 57 cm round a controller): no side longer than
        # the object's longest side in segment 0 (it is the same object, turned over)
        cap = max(scan["crop_box"]["extent_m"]) if (scan.get("crop_box") or {}).get("extent_m") else None
    if not box:
        return None
    try:
        if cap:
            box = {**box, "extent_m": [min(float(e), cap) for e in box["extent_m"]]}
        b = ObjectBox.from_contract(box)
        pose = Pose.from_contract(still["camera"])
        cam = Pinhole.from_contract(still["intrinsics"], still.get("width_px"), still.get("height_px"))
    except (KeyError, ValueError, TypeError):
        return None
    c = np.array([[x, y, z] for x in (-0.5, 0.5) for y in (-0.5, 0.5) for z in (-0.5, 0.5)]) * (1 + 2 * OBJECT_ROI_GROW)
    corners = c @ b.matrix[:3, :3].T + b.matrix[:3, 3]
    uv, z = project(corners, pose, cam)
    if not (z > 0.05).all() or not (cam.width and cam.height):
        return None
    x0, y0 = np.clip(uv.min(0), 0, [cam.width, cam.height])
    x1, y1 = np.clip(uv.max(0), 0, [cam.width, cam.height])
    return None if x1 - x0 < 1 or y1 - y0 < 1 else (float(x0), float(y0), float(x1), float(y1))


def on_object(dets: list, roi: tuple[float, float, float, float] | None) -> list:
    """The detector's boxes that are on the object (at least half inside its region; all when there is none):
    clutter beside it (a cup, a coin on the table) is not the object and is never inspected as part of it."""
    if roi is None:
        return list(dets)
    out = []
    for d in dets:
        x0, y0, x1, y1 = (float(v) for v in d.box)
        area = max(1e-9, (x1 - x0) * (y1 - y0))
        w = max(0.0, min(x1, roi[2]) - max(x0, roi[0]))
        h = max(0.0, min(y1, roi[3]) - max(y0, roi[1]))
        if w * h >= 0.5 * area:
            out.append(d)
    return out


def _inspect_sync(ctx: Context, scan_id: str, still: dict, hints: list[str] | None = None,
                  focus: list[str] | None = None, scan: Mapping[str, Any] | None = None
                  ) -> tuple[list[dict], list[dict], str, str, dict, dict]:
    from repairos.vision.crops import load_still
    from repairos.vision.inspect import inspect_still
    from repairos.vision.scale import PixelScale

    detector, judge = ctx.slot("DET"), ctx.slot("VLM")
    t0 = time.perf_counter()
    im = load_still(ctx.local_file(scan_id, still["image_file"]))
    t1 = time.perf_counter()
    dets, cached = ctx.detections_for(scan_id, still["image_file"], im)
    dets = on_object(dets, object_roi(scan, still))
    t2 = time.perf_counter()
    depth = ctx.depth(scan_id, still)
    share = ctx.share(scan_id).for_still(still, depth)
    crop_dir = ctx.workdir(scan_id) / "crops" / still["still_id"]
    res = inspect_still(im, still_id=still["still_id"], scan_id=scan_id, detector=detector, judge=judge,
                        crop_dir=crop_dir, sharpness=still.get("sharpness"), detections=dets, pool=ctx.vlm_pool,
                        share=share, max_crops=ctx.config["max_crops_per_still"], hints=hints, focus=focus,
                        stop=ctx.stopping, scale=PixelScale.from_still(still, depth))
    timing = {"load_s": round(t1 - t0, 3), "detect_s": round(t2 - t1, 3), "detections_ready": cached,
              "vlm_s": round(res.timing.get("vlm_s", 0.0), 3), "vlm_calls": int(res.timing.get("vlm_calls", 0)),
              "reused_regions": int(res.timing.get("reused_regions", 0))}
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
    return findings, dets, getattr(detector, "model_id", type(detector).__name__), vlm, res.rollups, timing


async def _inspect_one(ctx: Context, scan: dict, still: dict, covered: str | None = None) -> dict:
    sid, scan_id = still["still_id"], scan["scan_id"]
    t0 = time.monotonic()
    stage: dict[str, Any] = {}
    if covered:
        findings = [not_inspected_finding(scan_id, sid, "whole", covered, "repairos.vision")]
        regions = {findings[0]["finding_id"]: {"part_class": "whole photo", "inspected_by": []}}
        stage["covered"] = covered
    elif still.get("user_skipped"):
        findings = [not_inspected_finding(scan_id, sid, "whole", SKIPPED, "repairos.vision")]
        regions = {findings[0]["finding_id"]: {"part_class": "whole photo", "inspected_by": []}}
    elif still["kind"] == "label":
        ocr = ctx.selected("OCRB")
        if ocr != "none":
            raise PipelineError(f"OCRB={ocr} is selected, but the pipeline has no OCR stage until task M8")
        findings = [not_inspected_finding(scan_id, sid, "label", OCR_NOT_BUILT, "repairos.ocr")]
        regions = {findings[0]["finding_id"]: {"part_class": "label", "inspected_by": []}}
    else:
        run_row = (await _run_row(ctx, scan_id)) or {}
        hints = (run_row.get("hints") or {}).get("hints") or None
        focus = [x["area"] for x in (run_row.get("leads") or {}).get("items") or [] if x.get("area")] or None
        findings, dets, det_id, vlm, rollups, timing = await asyncio.to_thread(_inspect_sync, ctx, scan_id, still,
                                                                               hints, focus, scan)
        regions = regions_for(findings, dets, "whole photo", rollups)
        stage.update(detections=dets, detector=det_id, vlm=vlm, timing=timing)
    ids, kept = await _store_findings(ctx, findings)
    stage.update(status="done", finding_ids=ids, regions=regions, finished_at=_now(),
                 seconds=round(time.monotonic() - t0, 3))
    if kept:
        stage["kept_earlier_answers"] = kept
    return stage


def is_photo(still: dict) -> bool:
    """A still the selection decides on (labels go to OCR, a skipped still has nothing to inspect)."""
    return not still.get("user_skipped") and still.get("kind") != "label"


async def _scan_or_pending(ctx: Context, still: dict) -> dict:
    """The still's scan package, or a pending scan ({scan_id, session_id, pending}) when the still arrived first:
    a still is inspected from its own image, pose and intrinsics, before the scan package is posted (at Done)."""
    row = await ctx.state.get("scan-package", still["scan_id"])
    if row is not None:
        return row.data
    return {"scan_id": still["scan_id"], "session_id": still["session_id"], "pending": True}


async def _claim(ctx: Context, scan: dict, still_id: str) -> tuple[str, dict | None]:
    """("mine", None) when this worker now owns the still; ("done", stage) when it was inspected already;
    ("wait", None) when another job is inspecting it."""
    try:
        await ctx.state.transition("pipeline-run", run_id_for(scan["scan_id"]), "pipeline.claim_still",
                                   {"still_id": still_id, "worker": ctx.worker, "at": _now(),
                                    "scan_id": scan["scan_id"], "session_id": scan["session_id"]})
    except AlreadyClaimed as e:
        if e.status == "done":
            return "done", (await _run_row(ctx, scan["scan_id"]))["stills"][still_id]
        return "wait", None
    ctx.hold(scan["scan_id"], still_id)
    return "mine", None


class ClaimLost(PipelineError):
    """This worker's claim on a still was released or taken over while it inspected: its result is not written."""


async def _write_still(ctx: Context, scan: dict, still_id: str, stage: dict) -> None:
    """The still's result, only while this worker still holds its claim (pipeline.finish_still, one transaction)."""
    try:
        await ctx.state.transition("pipeline-run", run_id_for(scan["scan_id"]), "pipeline.finish_still",
                                   {"still_id": still_id, "worker": ctx.worker, "stage": stage,
                                    "scan_id": scan["scan_id"], "session_id": scan["session_id"]})
    except AlreadyClaimed as e:
        raise ClaimLost(f"still {still_id}: the claim of {ctx.worker} was released or taken over (now {e.status}, "
                        f"worker {e.worker}); this result is dropped") from None
    finally:
        ctx.held.discard((scan["scan_id"], still_id))


async def _release_mine(ctx: Context, scan: dict, still_id: str) -> None:
    """Give this worker's claim on a still back (pending), only if it is still this worker's (owner-checked)."""
    try:
        await ctx.state.transition("pipeline-run", run_id_for(scan["scan_id"]), "pipeline.release_still",
                                   {"still_id": still_id, "worker": ctx.worker})
    except AlreadyClaimed:
        pass
    finally:
        ctx.held.discard((scan["scan_id"], still_id))


async def _finish(ctx: Context, scan: dict, still: dict, covered: str | None) -> dict:
    still_id = still["still_id"]
    try:
        stage = await _inspect_one(ctx, scan, still, covered)
    except Exception as e:
        if is_stopping(e):   # shutting down: release, never fail (pipeline/stopping.py)
            await _release_mine(ctx, scan, still_id)
            raise WorkerStopping(f"still {still_id} released: the worker is stopping") from e
        msg = f"{type(e).__name__}: {e}"
        await _write_still(ctx, scan, still_id, {"status": "failed", "error": msg[:2000], "finished_at": _now()})
        raise
    await _write_still(ctx, scan, still_id, stage)
    try:
        await announce_progress(ctx, scan)
    except Exception as e:  # noqa: BLE001 - a live count is never worth failing an inspection
        log.warning("progress event for scan %s failed: %s", scan["scan_id"], e)
    ctx.start(_quietly(prewarm_fix(ctx, scan), f"parts prewarm for scan {scan['scan_id']}"))   # as findings come in
    return stage


def _kept_already(run: dict | None, stills: list[dict]) -> list[dict]:
    """Photo stills of the scan already claimed and not covered by another (being or done inspecting)."""
    claimed = (run or {}).get("stills") or {}
    return [s for s in stills if is_photo(s) and s["still_id"] in claimed
            and not claimed[s["still_id"]].get("covered") and claimed[s["still_id"]].get("status") != "failed"]


async def _select(ctx: Context, scan_id: str, undecided: list[dict], stills: list[dict]) -> Any:
    from repairos.pipeline.select import select_stills

    already = _kept_already(await _run_row(ctx, scan_id), stills)
    depth = {s["still_id"]: await asyncio.to_thread(ctx.depth, scan_id, s) for s in undecided + already}
    return await asyncio.to_thread(select_stills, undecided, lambda s: depth[s["still_id"]],
                                   max_stills=ctx.config["max_stills"], already=already)


async def inspect_still_stage(ctx: Context, still_id: str, covered: str | None = None, decide: bool = False) -> dict:
    """Inspect one still once (atomic claim on the run row). Returns the still's stage record. A still already
    inspected is not inspected again; one another worker is inspecting is waited for. `covered`: the reason the
    still selection did not keep it (it is recorded not_inspected with that reason, I3). `decide`: a still that just
    arrived: the still selection decides now, against the stills of its scan kept before it (under the scan's lock,
    so two near-identical stills arriving together are not both kept). Works before the scan package exists."""
    row = await ctx.state.get("still", still_id)
    if row is None:
        raise PipelineError(f"no still {still_id}")
    still = row.data
    scan = await _scan_or_pending(ctx, still)
    if decide or still.get("kind") == "label":   # a still that just arrived: identify the object in the background
        ctx.spawn(_identify_quietly(ctx, scan, still))
    if decide:   # the person's story (if any): its leads onto this scan, announced once each
        from repairos.pipeline.story import apply_story
        ctx.start(_quietly(apply_story(ctx, scan), f"leads for scan {scan['scan_id']}"))
    return await _stage_for(ctx, scan, still, covered, decide)


async def _stage_for(ctx: Context, scan: dict, still: dict, covered: str | None, decide: bool) -> dict:
    still_id = still["still_id"]
    if decide and covered is None and is_photo(still):
        async with ctx.scan_lock(scan["scan_id"]):
            claimed = ((await _run_row(ctx, scan["scan_id"])) or {}).get("stills") or {}
            if still_id not in claimed:
                stills = [r.data for r in await ctx.state.list("still", scan_id=scan["scan_id"])]
                sel = await _select(ctx, scan["scan_id"], [still], stills)
                covered = (sel.covered.get(still_id) or {}).get("why")
                how, stage = await _claim(ctx, scan, still_id)
                if how == "mine" and covered:
                    return await _finish(ctx, scan, still, covered)
            else:
                how, stage = await _claim(ctx, scan, still_id)
    else:
        how, stage = await _claim(ctx, scan, still_id)
    if how == "done":
        return stage
    if how == "wait":
        return await _wait_for_still(ctx, scan, still_id)
    return await _finish(ctx, scan, still, covered)


# ------------------------------------------------------------------------- identity, hints, progress, mesh (demo)
IDENTIFY_CALLS_MAX = 3   # per scan: the first photo, a second photo when the first answer was not sure, a label


async def _identify_quietly(ctx: Context, scan: dict, still: dict) -> None:
    try:
        await maybe_identify(ctx, scan, still)
    except Exception as e:  # noqa: BLE001 - identity is a nicety: it never fails the still; the row says why
        log.warning("identifying scan %s from still %s failed: %s", scan["scan_id"], still["still_id"], e)
        status = "limited" if isinstance(e, SpendLimit) else "failed"   # at the spend cap: skipped, no retry
        await _update(ctx, scan, {"identity": {"status": status, "error": f"{type(e).__name__}: {e}"[:500]}})


def load_preview(path: Path, side: int = 1568) -> Any:
    """A still decoded at reduced size (JPEG draft mode: a fraction of a full 12 MP decode), EXIF orientation applied,
    for whole-photo calls (identity) that never need full resolution."""
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im.draft("RGB", (side, side))
        im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((side, side))
    return im


def _identify_plan(identity: dict | None, still: dict) -> list[str] | None:
    """Still ids to send now, or None: the first photo still; once more with a second photo when the answer was
    not sure; a label still always (read what is printed), up to IDENTIFY_CALLS_MAX calls."""
    ident = identity or {}
    calls = int(ident.get("calls") or 0)
    used = list(ident.get("from_still_ids") or [])
    if calls >= IDENTIFY_CALLS_MAX or still["still_id"] in used or ident.get("status") == "running":
        return None
    if still.get("kind") == "label":
        return used[-1:] + [still["still_id"]]
    if still.get("user_skipped"):
        return None
    if calls == 0:
        return [still["still_id"]]
    if calls == 1 and ident.get("confidence") != "high" and not ident.get("label_visible"):
        return used[:1] + [still["still_id"]]
    return None


async def maybe_identify(ctx: Context, scan: dict, still: dict) -> dict | None:
    """Identify the object from this still (and an earlier one) when _identify_plan says so: one Claude call, the
    answer on the run row (`identity`) and the live event `identified`; then reference hints (maybe_hints)."""
    async with ctx.scan_lock(scan["scan_id"]):
        run = await _run_row(ctx, scan["scan_id"]) or {}
        plan = _identify_plan(run.get("identity"), still)
        if plan is None:
            return None
        prev = run.get("identity") or {}
        await _update(ctx, scan, {"identity": {"status": "running", "calls": int(prev.get("calls") or 0) + 1}})
    t0 = time.monotonic()
    rows = [await ctx.state.get("still", sid) for sid in plan]
    paths = [ctx.local_file(scan["scan_id"], r.data["image_file"]) for r in rows if r]
    images = await asyncio.to_thread(lambda: [load_preview(p) for p in paths])
    ans = await asyncio.to_thread(ctx.slot("VLM").identify, images)
    ident = {**ans, "status": "done", "calls": int(prev.get("calls") or 0) + 1, "from_still_ids": plan,
             "seconds": round(time.monotonic() - t0, 3), "at": _now()}
    await _update(ctx, scan, {"identity": ident, "announce": "identified"})
    ctx.start(_quietly(maybe_hints(ctx, scan, ident), f"reference hints for scan {scan['scan_id']}"))
    ctx.start(_quietly(prewarm_fix(ctx, scan), f"parts prewarm for scan {scan['scan_id']}"))
    return ident


async def _quietly(coro: Any, what: str) -> None:
    try:
        await coro
    except Exception as e:  # noqa: BLE001 - optional side work never fails a still or a report
        log.warning("%s failed: %s", what, e)


async def maybe_hints(ctx: Context, scan: dict, identity: dict) -> None:
    """Reference hints for an identified product when Bright Data is configured (repairos.references): one search,
    one Claude call turning result titles and descriptions into at most three "where to look first" hints. Only
    the hints and the result links are kept. Without a key, or without a brand or model, nothing happens."""
    from repairos.references import brightdata as bd

    creds = bd.credentials()
    query = bd.query_for(identity)
    if creds is None or query is None:
        return
    run = await _run_row(ctx, scan["scan_id"]) or {}
    if (run.get("hints") or {}).get("query") == query:
        return
    t0 = time.monotonic()
    kept = bd.cached_hints(query)
    if kept is not None:   # the same product within a day: no search and no Claude call
        await _update(ctx, scan, {"hints": {"status": "done", "query": query, "hints": kept["hints"],
                                            "sources": kept["sources"], "cached": True,
                                            "seconds": round(time.monotonic() - t0, 3)}})
        return
    await _update(ctx, scan, {"hints": {"status": "searching", "query": query}})
    try:
        results = await asyncio.to_thread(bd.search, query, *creds)
        hints = await asyncio.to_thread(ctx.slot("VLM").reference_hints, identity, results)
    except Exception as e:  # noqa: BLE001 - hints are optional; the row says why there are none
        status = "limited" if bd.is_spend_limit(e) else "failed"   # at the spend cap: skipped, no retry
        await _update(ctx, scan, {"hints": {"status": status, "query": query, "error": f"{type(e).__name__}"}})
        return
    sources = [{"title": r["title"], "link": r["link"]} for r in results[:5]]
    await asyncio.to_thread(bd.remember_hints, query, hints, sources)
    await _update(ctx, scan, {"hints": {"status": "done", "query": query, "hints": hints, "sources": sources,
                                        "seconds": round(time.monotonic() - t0, 3)}})


PREWARM_MAX = 3   # distinct (object, part) searches started during one scan


def live_top_part(findings: list[dict], run: dict) -> str | None:
    """The part class of the most pressing issue found so far in the scan (safety, then functional, then not
    judged; then confidence), from the regions the stills recorded: the same part class the report will show."""
    from repairos.references.parts import NOT_A_PART

    regions = {fid: r for st in (run.get("stills") or {}).values() for fid, r in (st.get("regions") or {}).items()}
    rank = {"safety": 0, "functional": 1}
    best = None
    for f in findings:
        if f["state"] in ("ok", "not_inspected", "not_seen_in_photo", "insufficient_evidence", "unsure") or \
                f.get("severity") == "cosmetic" or f["finding_id"] not in regions or \
                (f.get("category") == "setup" and f.get("severity") != "safety"):   # a setup state needs no new part
            continue
        part = str(regions[f["finding_id"]].get("part_class") or "").replace("_", " ").strip()
        if part.lower() in NOT_A_PART:
            continue
        key = (rank.get(f.get("severity") or "", 2), -float(f.get("confidence") or 0.0))
        if best is None or key < best[0]:
            best = (key, part)
    return best[1] if best else None


async def prewarm_fix(ctx: Context, scan: dict) -> bool:
    """During the scan: start the "Fix it" search for the object and the most pressing part found so far (cache and
    in-flight sharing in repairos.references.parts), so it is ready, or nearly, when the report is. Off without a
    Bright Data key, before the object is identified, and after PREWARM_MAX pairs for one scan."""
    from repairos.references import parts
    from repairos.report.build import object_header

    creds = parts.credentials()
    if creds is None:
        return False
    run = await _run_row(ctx, scan["scan_id"]) or {}
    obj = parts.object_words(object_header(run.get("identity")))
    if obj is None:
        return False
    findings = [r.data for r in await ctx.state.list("finding", scan_id=scan["scan_id"])]
    part = live_top_part(findings, run)
    if part is None:
        return False
    tried = ctx._prewarmed.setdefault(scan["scan_id"], set())
    if (obj, part) in tried or len(tried) >= PREWARM_MAX:
        return False
    tried.add((obj, part))
    started = parts.prewarm(obj, part, creds)
    if started:
        log.info("parts search for scan %s started during the scan", scan["scan_id"])
    return started


def progress_counts(findings: list[dict], run: dict, stills_received: int) -> dict:
    """Live counts while the scan goes on: places a model judged (each place once: a region judged in another
    still counts there), places it could not judge, and places to look at (a possible problem)."""
    seen: dict[tuple, str] = {}
    for f in findings:
        if f["state"] in ("not_inspected", "not_seen_in_photo"):
            continue
        refs = f.get("evidence_refs") or {}
        key = ((refs.get("still_ids") or [""])[0], tuple(refs.get("crop_box_px") or ()), f["subject"]["ref"]
               if not refs.get("crop_box_px") else "")
        seen.setdefault(key, f["state"])
    states = list(seen.values())
    done = [st for st in (run.get("stills") or {}).values() if st.get("status") == "done"]
    return {"stills_received": stills_received, "stills_done": len(done),
            "stills_inspected": sum(1 for st in done if st.get("timing")),
            "areas_checked": sum(1 for x in states if x not in ("insufficient_evidence", "unsure")),
            "could_not_judge": sum(1 for x in states if x in ("insufficient_evidence", "unsure")),
            "to_look_at": sum(1 for x in states if x not in ("ok", "insufficient_evidence", "unsure"))}


async def announce_progress(ctx: Context, scan: dict) -> dict:
    """Recount the scan's findings and publish the live event `progress` (after every still)."""
    sid = scan["scan_id"]
    findings = [r.data for r in await ctx.state.list("finding", scan_id=sid)]
    stills = await ctx.state.list("still", scan_id=sid)
    counts = progress_counts(findings, await _run_row(ctx, sid) or {}, len(stills))
    await _update(ctx, scan, {"progress": {**counts, "at": _now()}, "announce": "progress"})
    return counts


def mesh_input_key(rev: int, stills: list[dict], merged: list[int]) -> str:
    """What a mesh build used: the scan package revision, its stills (a thin object's 3D view is its photo slab, so
    stills that land after the capture job change it) and the registered bottoms fused in."""
    key = f"{rev}:" + ",".join(sorted(str(s["still_id"]) for s in stills))
    return key + (f"|bottom:{','.join(str(k) for k in sorted(merged))}" if merged else "")


async def ensure_mesh(ctx: Context, scan_id: str) -> dict:
    """The scan's mesh for what the scan has now (pipeline/mesh.py, `mesh_input_key`: package revision, stills, the
    registered bottoms that passed the merge gate), built once per input: a blob, recorded on the run row (`mesh`)
    with the live event `mesh_ready`. A scan with no usable depth records why. One build per scan at a time."""
    async with ctx.scan_lock(f"mesh:{scan_id}"):
        return await _ensure_mesh(ctx, scan_id)


async def _ensure_mesh(ctx: Context, scan_id: str) -> dict:
    from repairos.pipeline.mesh import MeshUnavailable, build_scan_mesh

    row = await ctx.state.get("scan-package", scan_id)
    if row is None:
        raise PipelineError(f"no scan-package {scan_id}")
    scan, rev = row.data, row.rev
    have = (await _run_row(ctx, scan_id) or {}).get("mesh") or {}
    seg_rec = await _segment_record(ctx, scan_id, rev)
    still_rows = [r.data for r in await ctx.state.list("still", scan_id=scan_id)]
    merged = segments.merged_transforms(seg_rec)
    key = mesh_input_key(rev, still_rows, list(merged))
    if have.get("status") in ("done", "unavailable") and (
            have.get("input_key") == key or (have.get("input_key") is None and have.get("scan_rev") == rev
                                             and not still_rows)):
        return have
    t0 = time.monotonic()
    try:
        local = await asyncio.to_thread(_local_scan, ctx, scan)
        local_stills = await asyncio.to_thread(   # a thin object's 3D view is built from its photos (recreation.flat)
            lambda: [map_files("still", s, lambda v, _b: str(ctx.local_file(scan_id, v))) for s in still_rows])
        m = await asyncio.to_thread(build_scan_mesh, local, max_triangles=ctx.config["mesh_max_triangles"],
                                    segment_transforms=merged, stills_local=local_stills)
    except Exception as e:  # noqa: BLE001 - the 3D view never fails the capture or the report; the row says why
        unavailable = isinstance(e, MeshUnavailable)
        if not unavailable:
            log.warning("mesh for scan %s failed: %s", scan_id, e)
        reason = str(e) if unavailable else f"building the mesh failed ({type(e).__name__}: {e})"[:500]
        if have.get("status") == "done":   # a rebuild that failed: the earlier mesh stays served
            rec = {**have, "rebuild_error": reason, "rebuild_scan_rev": rev, "rebuild_input_key": key}
        else:
            rec = {"status": "unavailable" if unavailable else "failed", "reason": reason, "scan_rev": rev,
                   "input_key": key}
        await _update(ctx, scan, {"mesh": rec})
        return rec
    ref = await asyncio.to_thread(ctx.blobs.put, m.ply, "application/x-ply")
    cloud_ref = await asyncio.to_thread(ctx.blobs.put, m.cloud_ply, "application/x-ply") if m.cloud_ply else None
    why = m.hero_reason
    left_out = segments.merge_left_out(seg_rec)
    if left_out and not m.method.startswith("flat"):   # the gate kept a registered bottom out of the model
        why += "; built from the top scan alone (" + "; ".join(left_out) + ")"
    rec = {"status": "done", "ref": ref, "format": "ply", "scan_rev": rev, "rev": int(have.get("rev") or 0) + 1,
           "input_key": key, "vertices": m.vertices, "triangles": m.triangles, "keyframes": m.keyframes,
           "frame": "arkit_world", "bytes": len(m.ply), "cloud_ref": cloud_ref, "cloud_points": m.cloud_points,
           "hero": m.hero, "hero_reason": why, "size_m": m.size_m, "method": m.method,
           "bottom_segments": list(m.bottom_segments),
           "seconds": {**m.seconds, "with_io_s": round(time.monotonic() - t0, 3)}, "built_at": _now()}
    await _update(ctx, scan, {"mesh": rec, "announce": "mesh_ready"})
    return rec


async def _segment_record(ctx: Context, scan_id: str, rev: int) -> dict | None:
    """The run row's `segments` record for this scan-package revision (None when not computed for it)."""
    rec = (await _run_row(ctx, scan_id) or {}).get("segments") or {}
    return rec if rec.get("scan_rev") == rev else None


async def ensure_segment_alignment(ctx: Context, scan_id: str) -> dict:
    """Register every object-moved segment (the flipped bottom) to segment 0 (pipeline/segments.py), once per
    scan-package revision: the run row's `segments` record (per segment: aligned, fitness, rmse_mm, transform),
    and a merged cloud blob (`cloud_ref`) that locate ray-casts. A scan with no moved segment records
    status none. Runs before the mesh and before locate; bounded by segments.TIME_BUDGET_S."""
    async with ctx.scan_lock(f"segments:{scan_id}"):
        row = await ctx.state.get("scan-package", scan_id)
        if row is None:
            raise PipelineError(f"no scan-package {scan_id}")
        scan, rev = row.data, row.rev
        have = await _segment_record(ctx, scan_id, rev)
        if have is not None:
            return have
        if not segments.moved_segments(scan):
            rec = {"status": "none", "scan_rev": rev, "segments": []}
            await _update(ctx, scan, {"segments": rec})
            return rec
        t0 = time.monotonic()
        try:
            local = await asyncio.to_thread(_local_scan, ctx, scan)
            still_rows = [r.data for r in await ctx.state.list("still", scan_id=scan_id)]
            local_stills = await asyncio.to_thread(   # a thin object's flip is registered by its photos
                lambda: [map_files("still", s, lambda v, _b: str(ctx.local_file(scan_id, v))) for s in still_rows])
            rec, clouds = await asyncio.to_thread(segments.align_scan_segments, local, stills_local=local_stills)
            merged = await asyncio.to_thread(segments.merged_cloud_ply, local.get("points_file"), clouds)
            if merged:
                rec["cloud_ref"] = await asyncio.to_thread(ctx.blobs.put, merged, "application/x-ply")
        except Exception as e:  # noqa: BLE001 - never fails the scan: its findings stay unplaced, the row says why
            log.warning("segment alignment for scan %s failed: %s", scan_id, e)
            reason = f"aligning the flipped segment failed ({type(e).__name__}: {e})"[:500]
            rec = {"status": "failed", "segments": [
                {"segment": int(sg["segment"]), "reason": sg.get("reason"), "object_moved": True, "aligned": False,
                 "fitness": None, "rmse_mm": None, "why_not": reason} for sg in segments.moved_segments(scan)]}
        rec.update(scan_rev=rev, with_io_s=round(time.monotonic() - t0, 3), finished_at=_now())
        await _update(ctx, scan, {"segments": rec})
        return rec


async def _wait_for_still(ctx: Context, scan: dict, still_id: str) -> dict:
    """Wait for a still another job is inspecting. A claim whose worker is gone (claim_is_stale) is taken over and
    inspected here, so a restart never leaves a run waiting out STILL_WAIT_S on a dead claim."""
    deadline = time.monotonic() + STILL_WAIT_S
    while time.monotonic() < deadline:
        st = ((await _run_row(ctx, scan["scan_id"])) or {}).get("stills", {}).get(still_id, {})
        if st.get("status") == "done":
            return st
        if st.get("status") == "failed":
            raise PipelineError(f"still {still_id} failed on worker {st.get('worker')}: {st.get('error')}")
        if not st or claim_is_stale(st, datetime.now(UTC)):
            row = await ctx.state.get("still", still_id)
            if row is not None:
                take = {"still_id": still_id, "worker": ctx.worker, "at": _now(), "scan_id": scan["scan_id"],
                        "session_id": scan["session_id"]}
                if st:   # take over exactly the stale claim seen; if anyone changed it since, keep waiting
                    take["expect"] = {k: st.get(k) for k in ("worker", "started_at", "heartbeat_at")}
                try:
                    await ctx.state.transition("pipeline-run", run_id_for(scan["scan_id"]), "pipeline.claim_still",
                                               take)
                except AlreadyClaimed:
                    await asyncio.sleep(POLL_S)
                    continue
                ctx.hold(scan["scan_id"], still_id)
                log.info("still %s: took over a claim of %s, which is gone", still_id, st.get("worker"))
                return await _finish(ctx, scan, row.data, None)
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
                              "report_id": None, "finished_at": None, "continuation": None,
                              "stages": {"capture": capture_facts(scan),
                                         **{s: {"status": "pending"} for s in ("select", "inspect", "fuse",
                                                                               "locate", "report")}}})
    try:
        return await _run_stages(ctx, scan)
    finally:
        ctx.drop_share(scan_id)


async def _inspect_all(ctx: Context, scan: dict, stills: list[dict]) -> dict[str, dict]:
    """select + inspect: every still of the scan ends with a stage record (inspected, covered, label or skipped).
    Stills inspected while the scan was going on keep their answers; the rest are decided together (batch
    selection), then inspected `stills_at_once` at a time, close-ups and sharpest first."""
    scan_id = scan["scan_id"]
    t0 = time.monotonic()
    await _update(ctx, scan, {"stages": {"select": {"status": "running", "started_at": _now()}}})
    stages: dict[str, dict] = {}
    mine: list[str] = []
    async with ctx.scan_lock(scan_id):
        claimed = ((await _run_row(ctx, scan_id)) or {}).get("stills") or {}
        undecided = [s for s in stills if is_photo(s) and s["still_id"] not in claimed]
        try:
            sel = await _select(ctx, scan_id, undecided, stills)
            for sid, c in sel.covered.items():
                stages[sid] = await inspect_still_stage(ctx, sid, covered=c["why"])
            for sid in sel.kept:
                how, stage = await _claim(ctx, scan, sid)
                if how == "mine":
                    mine.append(sid)
                elif how == "done":
                    stages[sid] = stage
        except Exception as e:
            raise await _fail(ctx, scan, "select", e) from e
    await _update(ctx, scan, {"stages": {"select": {"status": "done", "finished_at": _now(),
                                                    "decided_during_scan": len(claimed),
                                                    "decided_now": len(undecided), **sel.as_record(),
                                                    "seconds": round(time.monotonic() - t0, 3)}}})

    t0 = time.monotonic()
    await _update(ctx, scan, {"stages": {"inspect": {"status": "running", "started_at": _now()}}})
    by_id = {s["still_id"]: s for s in stills}
    others = sorted(sid for sid in by_id if sid not in stages and sid not in mine)
    gate = asyncio.Semaphore(ctx.config["stills_at_once"])

    async def one(sid: str) -> tuple[str, dict]:
        async with gate:
            if sid in mine:
                return sid, await _finish(ctx, scan, by_id[sid], None)
            return sid, await inspect_still_stage(ctx, sid)   # labels, skipped, and stills claimed during the scan

    results = await asyncio.gather(*(one(sid) for sid in mine + others), return_exceptions=True)
    stopped = [r for r in results if isinstance(r, BaseException) and is_stopping(r)]
    if stopped:   # shutting down: the run is not failed; its job goes back to the queue
        for sid in mine:
            if (scan["scan_id"], sid) in ctx.held:
                await _release_mine(ctx, scan, sid)
        raise WorkerStopping(f"scan {scan['scan_id']}: inspection stopped, the worker is stopping") from stopped[0]
    for sid, r in zip(mine + others, results):
        if isinstance(r, BaseException):
            raise await _fail(ctx, scan, "inspect", r, sid) from r
        stages[r[0]] = r[1]
    timings = [st.get("timing") or {} for st in stages.values()]
    await _update(ctx, scan, {"stages": {"inspect": {
        "status": "done", "finished_at": _now(), "stills": len(stills),
        "inspected": sum(1 for st in stages.values() if st.get("timing")),
        "vlm_calls": sum(int(t.get("vlm_calls", 0)) for t in timings),
        "reused_regions": sum(int(t.get("reused_regions", 0)) for t in timings),
        "detector_seconds": round(sum(float(t.get("detect_s", 0.0)) for t in timings), 3),
        "seconds": round(time.monotonic() - t0, 3)}}})
    return stages


# ------------------------------------------------------------------- assembly: complete and correctly assembled?
ASSEMBLY_NOT_AVAILABLE = "the configured vision model has no assembly check"


def _object_words(run_row: dict) -> str | None:
    ident = run_row.get("identity") or {}
    words = " ".join(str(ident[k]) for k in ("brand", "model", "category") if ident.get(k) and ident[k] != "unknown")
    return words or None


def _assembly_sync(ctx: Context, scan_id: str, picked: list[dict], object_words: str | None, rev: int) -> Any:
    from repairos.vision.assembly import check_assembly
    from repairos.vision.crops import load_still
    from repairos.vision.scale import PixelScale

    views, scales = [], {}
    for st in picked:
        views.append((st, load_still(ctx.local_file(scan_id, st["image_file"]))))
        sc = PixelScale.from_still(st, ctx.depth(scan_id, st))
        scales[st["still_id"]] = sc.px_per_mm if sc is not None else None
    cfg = ctx.config["assembly"]
    res = check_assembly(views, scan_id=scan_id, judge=ctx.slot("VLM"), object_words=object_words,
                         max_confirm=cfg["max_confirm"], max_gaps=cfg["max_gaps"],
                         crop_dir=ctx.workdir(scan_id) / "crops" / "assembly", px_per_mm_of=scales,
                         stop=ctx.stopping, rev=rev)
    for f in res.findings:
        refs = f.get("evidence_refs") or {}
        crop = refs.get("crop_file")
        if crop and not crop.startswith(REF_PREFIX):
            refs["crop_file"] = ctx.blobs.put(Path(crop).read_bytes(), "image/jpeg")
        validate("finding", f)
    return res


async def run_assembly(ctx: Context, scan: dict, stills: list[dict]) -> tuple[dict[str, dict], set[str]]:
    """The whole-object assembly check (repairos.vision.assembly), beside the inspection: its findings stored like
    any other (finding.create), its record on the run row (`assembly`). Returns (the report regions of every assembly
    finding of the scan, the ids of earlier checks' findings this check replaces). The same photos as the last check:
    that answer stands (no new call). Other photos (more were added), or a last check that was paused or failed: a new
    check, revision `rev` (in its row ids, so a new answer is never hidden behind an earlier row of the same place).
    A check the overview answered replaces the earlier rows, not added to (`superseded`: left out of fusion and the
    report, like an earlier report revision). Off in config, or a vision slot without the check: nothing, and the run
    row says so. A check with no answer (a failed, declined or paused overview) is not a verdict: one not_inspected
    row says the check was not done and why (I3), the last answered check's rows stand beside it (`answer_ids`), and
    the next report does it again; the report still builds."""
    from repairos.vision.assembly import finding_not_run, pick_stills

    scan_id = scan["scan_id"]
    cfg = ctx.config["assembly"]
    run_row = (await _run_row(ctx, scan_id)) or {}
    before = run_row.get("assembly") or {}
    regions_before = dict(before.get("regions") or {})   # every assembly finding of the scan keeps its region
    superseded_before = set(before.get("superseded") or [])
    if not cfg["enabled"]:
        await _update(ctx, scan, {"assembly": {"status": "off"}})
        return regions_before, superseded_before
    judge = ctx.slot("VLM")
    if not (hasattr(judge, "assembly_check") and hasattr(judge, "inspect_state")):
        await _update(ctx, scan, {"assembly": {"status": "not_available", "reason": ASSEMBLY_NOT_AVAILABLE}})
        return regions_before, superseded_before
    picked = pick_stills(stills, cfg["max_stills"])
    ids = [s["still_id"] for s in picked]
    if not picked or (before.get("status") == "done" and before.get("stills") == ids):
        return regions_before, superseded_before
    rev = int(before.get("rev") or 0) + 1
    answered_before = list(before["answer_ids"] if "answer_ids" in before else
                           (before.get("finding_ids") or []) if before.get("status") == "done" else [])
    t0 = time.monotonic()
    try:
        res = await asyncio.to_thread(_assembly_sync, ctx, scan_id, picked, _object_words(run_row), rev)
        status = "paused" if res.record.get("paused") else "failed" if res.record.get("failed") else "done"
        findings, record, regions = res.findings, {**res.record, "status": status}, res.regions
    except Exception as e:
        if is_stopping(e):
            raise
        log.warning("assembly check for scan %s failed: %s", scan_id, e)
        f = finding_not_run(scan_id=scan_id, reason=f"the vision model call failed ({type(e).__name__}); it is "
                                                    "tried again on the next report", still_ids=ids, rev=rev)
        findings, record = [f], {"status": "failed", "stills": ids, "error": type(e).__name__}
        regions = {f["finding_id"]: {"part_class": "assembly check", "inspected_by": []}}
    answered = bool(record.get("answered"))
    standing = [] if answered else answered_before   # no answer never replaces the last answer with nothing
    if standing:
        for f in findings:
            f["evidence"].append("the earlier assembly check's answers stand until this one is done")
    new_ids, _kept = await _store_findings(ctx, findings)   # a conflict is only a retry of this same revision
    finding_ids = list(dict.fromkeys([*standing, *new_ids]))
    superseded = sorted((superseded_before | set(before.get("finding_ids") or [])) - set(finding_ids))
    regions = {**regions_before, **regions}   # rows that stand keep their region on the next check too
    await _update(ctx, scan, {"assembly": {**record, "rev": rev, "finding_ids": finding_ids,
                                           "answer_ids": new_ids if answered else standing, "regions": regions,
                                           "superseded": superseded, "seconds": round(time.monotonic() - t0, 3)}})
    return regions, set(superseded)


async def _run_stages(ctx: Context, scan: dict) -> dict:
    scan_id = scan["scan_id"]
    stills = [r.data for r in await ctx.state.list("still", scan_id=scan_id)]
    assembly = asyncio.create_task(run_assembly(ctx, scan, stills))   # beside the inspection: no wait of its own
    try:
        stages = await _inspect_all(ctx, scan, stills)
    except BaseException:
        assembly.cancel()
        raise
    try:
        assembly_regions, assembly_superseded = await assembly
    except Exception as e:
        if is_stopping(e):
            raise
        raise await _fail(ctx, scan, "assembly", e) from e

    # fuse
    t0 = time.monotonic()
    await _update(ctx, scan, {"stages": {"fuse": {"status": "running", "started_at": _now()}}})
    try:
        from repairos.fusion import fuse

        findings = [r.data for r in await ctx.state.list("finding", scan_id=scan_id)
                    if r.data["finding_id"] not in assembly_superseded]   # an earlier assembly check, replaced
        readings = [r.data for r in await ctx.state.list("meter-reading", scan_id=scan_id)]
        engine = ctx.slot("FUSION") if findings or readings else None
        hints = (((await _run_row(ctx, scan_id)) or {}).get("hints") or {}).get("hints") or None
        result = await asyncio.to_thread(fuse, scan_id, findings, readings, scan.get("symptom"), engine=engine,
                                         still_words=still_words(stills), hints=hints)
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

    # locate (after the flipped segments are registered: their stills are restated in segment 0's frame)
    t0 = time.monotonic()
    try:
        regions = {fid: info for st in stages.values() for fid, info in (st.get("regions") or {}).items()}
        regions.update(assembly_regions)
        seg_rec = await ensure_segment_alignment(ctx, scan_id)
        locations, locate_words = await locate_findings(ctx, scan, stills, findings, regions, seg_rec)
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
        from repairos.fusion.questions import band

        before = await ctx.state.get("report", report_id_for(scan_id))   # more photos were added to the scan
        extra = (await _run_row(ctx, scan_id)) or {}   # identity and mesh when they are ready; never waited for
        draft = build_report(scan=scan, stills=stills, findings=findings, suspects=suspects,
                             next_action=result.next_action, regions=regions, locations=locations, models=models,
                             explains={k: band(j.explains) for k, j in result.judgments.items()},
                             previous=before.data if before else None,
                             identity=extra.get("identity"), mesh=extra.get("mesh"), hints=extra.get("hints"),
                             segments=segments.report_segments(scan, seg_rec,
                                                               findings_by_segment(stills, findings, locations)))
        draft["build_id"] = uuid.uuid4().hex   # this build; its later patches (fix, mesh) check it (_patch_report)
        from repairos.pipeline.story import STORY_KIND
        from repairos.references.leads import user_lead

        story = await ctx.state.get(STORY_KIND, scan["session_id"])
        if story is not None and (ul := user_lead(story.data, draft)):
            draft["user_lead"] = ul   # what the photos showed about the person's guess (cites findings, adds none)
        fix_task = await start_fix(ctx, scan, draft)   # the top finding is known: look for the part meanwhile
        try:
            draft["summary"] = await asyncio.to_thread(write_summary, draft, llm, symptom=scan.get("symptom"))
        except SpendLimit:   # the Claude cap: the report still goes out with what was done, and says so
            draft["summary"] = dict(PAUSED_SUMMARY)
        if any(PAUSED in " ".join(f.get("evidence") or []) for f in findings):
            draft["limits"] = [*(draft.get("limits") or []), PAUSED_LIMIT]
        if fix_task is not None and fix_task.done():   # ready before the report (cached): it goes out with it
            fix, rec = _fix_result(fix_task)
            if fix:
                draft["fix"] = fix
            else:   # nothing, or the spend cap: say so before the report is out, so the app never waits for it
                await _update(ctx, scan, {"fix": {**rec, "finished_at": _now()}})
        validate("report", draft)
        problems = honesty_violations(draft, findings)
        if problems:
            raise PipelineError("honesty check failed: " + "; ".join(problems))
        try:
            published = await ctx.state.transition("report", draft["report_id"], "report.publish", draft)
        except StaleReport as e:
            # A run with more stills published first (two /complete calls ran at once): its report and its run
            # record stand. This run writes nothing more, not even a stage, and returns the report that stands.
            log.info("scan %s: %s", scan_id, e)
            if fix_task is not None:
                fix_task.cancel()
            current = await ctx.state.get("report", draft["report_id"])
            return current.data if current else draft
    except Exception as e:
        raise await _fail(ctx, scan, "report", e) from e
    await _update(ctx, scan, {"status": "done", "report_id": draft["report_id"], "finished_at": _now(),
                              "stages": {"report": {"status": "done", "finished_at": _now(),
                                                    "verdict": draft["verdict"],
                                                    "seconds": round(time.monotonic() - t0, 3)}}})
    if fix_task is not None:
        task = asyncio.create_task(attach_fix(ctx, scan, published, fix_task))
        ctx._background.add(task)
        task.add_done_callback(ctx._background.discard)
    ctx.spawn(_models_after_report(ctx, scan, published))   # stills that landed after capture: rebuilt only if new
    return draft


async def _models_after_report(ctx: Context, scan: dict, published: Any) -> None:
    """After the report: the mesh and the recreation for the scan as it is now (stills that arrived after the capture
    job: a thin object's 3D view is its photo slab), rebuilt only when their inputs changed. A new mesh also goes into
    the published report (one more revision of the same report; never over a newer one)."""
    from repairos.recreation.service import recreation_quietly

    sid = scan["scan_id"]
    try:
        mesh = await ensure_mesh(ctx, sid)
        if mesh.get("status") == "done" and (published.data.get("mesh") or {}).get("rev") != mesh.get("rev"):
            from repairos.report.build import mesh_section
            await _patch_report(ctx, published, {"mesh": mesh_section(sid, mesh)})
    except Exception as e:  # noqa: BLE001 - recorded by ensure_mesh; this only guards the task
        log.warning("mesh after the report for scan %s: %s", sid, e)
    await recreation_quietly(ctx, sid)


async def _patch_report(ctx: Context, published: Any, patch: dict, tries: int = 3) -> bool:
    """One more revision of the published report with `patch` merged in, only while it is still the same report build
    (its `build_id`, unique per report.publish and kept by every patch: a patch from this run, like the fix or the
    mesh, may land first, then it is re-read and patched again). False when a newer report (more photos) replaced it,
    even one built in the same second (created_at has whole seconds)."""
    def build(data: Mapping[str, Any]) -> tuple:
        return data.get("build_id"), data.get("created_at")   # created_at alone only for rows from before build_id

    row = published
    for _ in range(tries):
        try:
            await ctx.state.put("report", {**row.data, **patch}, expect_rev=row.rev)
            return True
        except RevConflict:
            row = await ctx.state.get("report", published.data["report_id"])
            if row is None or build(row.data) != build(published.data):
                return False
    return False


# ------------------------------------------------------------------------------------- "Fix it" (parts, demo)
async def start_fix(ctx: Context, scan: dict, draft: dict) -> asyncio.Task | None:
    """Start the "Fix it" search for the draft report's top finding (repairos.references.parts) and return its task,
    or None when it is off (no Bright Data key) or there is nothing to search. It runs beside the summary call and
    is bounded by parts.BUDGET_S; the report never waits for it (attach_fix)."""
    from repairos.references import parts

    creds = parts.credentials()
    if creds is None or parts.part_for(draft) is None:
        return None
    await _update(ctx, scan, {"fix": None})   # an earlier run's record goes
    await _update(ctx, scan, {"fix": {"status": "searching", "started_at": _now()}})
    return asyncio.create_task(asyncio.wait_for(asyncio.to_thread(parts.find_fix, draft, creds), parts.BUDGET_S))


def _fix_result(task: asyncio.Task) -> tuple[dict | None, dict]:
    try:
        return task.result()
    except (asyncio.TimeoutError, TimeoutError):
        from repairos.references import parts
        return None, {"status": "timeout", "seconds": parts.BUDGET_S}
    except Exception as e:  # noqa: BLE001 - optional; the run row says why
        return None, {"status": "failed", "error": type(e).__name__}


async def attach_fix(ctx: Context, scan: dict, published: Any, task: asyncio.Task) -> dict | None:
    """When the search is ready: the published report gains `fix` (one more revision of the same report, only if no
    newer report replaced it), and the run row records it with the live event `fix_ready`. No result, a timeout, an
    error or the spend cap: the report is left as it is, and `fix_ready` carries only the status (no offers)."""
    from repairos.references import parts

    if not task.done():
        await asyncio.wait([task])
    fix, rec = _fix_result(task)
    report = published.data
    if fix and report.get("fix") != fix:
        if not await _patch_report(ctx, published, {"fix": fix}):   # a newer report (more photos) is out: this
            await _update(ctx, scan, {"fix": {**rec, "status": "stale", "finished_at": _now()}})   # search was old
            return None
    if not fix:   # the live event ends the app's "Searching for parts…" (`limited`: "not available right now")
        await _update(ctx, scan, {"fix": {**rec, "finished_at": _now()}, "announce": "fix_ready"})
        return None
    await _update(ctx, scan, {"fix": {**rec, **fix, "finished_at": _now()}, "announce": "fix_ready"})
    log.info("fix for scan %s: %d option(s) (%s, %.1f s)", scan["scan_id"], len(fix["options"]),
             "cached" if rec.get("cached") else "searched", float(rec.get("seconds") or 0.0))
    return parts.payload(scan["scan_id"], fix)


NOT_ALIGNED = ("this photo's capture segment is not lined up with the scan (the object was flipped for its underside, "
               "or ARKit relocalisation failed), so it is not placed on the scan")


def unaligned_segments(scan: dict, seg_rec: dict | None = None) -> set[int]:
    """Capture segments whose photos' poses are not in the scan's frame: marked `aligned: false`, or `object_moved`
    (a flipped mini-scan of the bottom, in its own frame) and not registered by the server (`seg_rec`, the run row's
    `segments` record: pipeline/segments.py). Their findings are inspected and listed but not placed on the model.
    The phone's own `transform_to_segment0` is never used unverified."""
    return segments.unplaced_segments(scan, seg_rec)


def not_aligned_reason(scan: dict, seg_rec: dict | None, segment: int) -> str:
    seg = next((s for s in scan.get("segments") or [] if int(s["segment"]) == segment), {})
    if not segments.is_moved(seg):
        return NOT_ALIGNED
    r = next((x for x in (seg_rec or {}).get("segments") or [] if int(x["segment"]) == segment), {})
    why = r.get("why_not") or "it has not been registered to the main scan yet"
    if r.get("aligned") and not (r.get("merge") or {}).get("ok", True):
        why = f"its best fit does not make sense: {r['merge'].get('why')}"
    return (f"this photo is from the flipped scan of the bottom, which could not be lined up with the main scan "
            f"({why}), so it is not placed on the model")


def findings_by_segment(stills: list[dict], findings: list[dict], locations: dict[str, dict]) -> dict[int, list[str]]:
    """{segment >= 1: finding ids from its photos that got no 3D point}, for the report's capture.segments."""
    seg_of = {s["still_id"]: int(s.get("segment") or 0) for s in stills}
    out: dict[int, list[str]] = {}
    for f in findings:
        sid = ((f.get("evidence_refs") or {}).get("still_ids") or [None])[0]
        k = seg_of.get(sid, 0)
        if k and (locations.get(f["finding_id"]) or {}).get("status") != "computed":
            out.setdefault(k, []).append(f["finding_id"])
    return out


def _local_scan(ctx: Context, scan: dict) -> dict:
    """The scan package with every file field (points, mesh, keyframes) as an absolute local path."""
    sid = scan["scan_id"]
    out = map_files("scan-package", scan, lambda v, _b: str(ctx.local_file(sid, v)))
    if scan.get("mesh_file"):
        out["mesh_file"] = str(ctx.local_file(sid, scan["mesh_file"]))
    return out


async def locate_findings(ctx: Context, scan: dict, stills: list[dict], findings: list[dict],
                          regions: dict[str, dict], seg_rec: dict | None = None) -> tuple[dict[str, dict], str]:
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

    moved_to0 = segments.merged_transforms(seg_rec)   # flipped segments the server registered (and merged)

    def local_still(sid: str) -> dict:
        if sid not in still_local:
            st = map_files("still", by_still[sid], lambda v, _b: str(ctx.local_file(scan["scan_id"], v)))
            k = int(st.get("segment") or 0)
            still_local[sid] = segments.restate_still(st, moved_to0[k]) if k in moved_to0 else st
        return still_local[sid]

    unaligned = unaligned_segments(scan, seg_rec)
    for f in findings:
        refs = f.get("evidence_refs") or {}
        sid = (refs.get("still_ids") or [None])[0]
        box = refs.get("crop_box_px")
        if locator is None:
            out[f["finding_id"]] = locate_iface.not_computed(locate_iface.NOT_INSTALLED)
        elif sid in by_still and int(by_still[sid].get("segment") or 0) in unaligned:
            out[f["finding_id"]] = locate_iface.not_computed(
                not_aligned_reason(scan, seg_rec, int(by_still[sid].get("segment") or 0)))
        elif not sid or sid not in by_still or not box:
            out[f["finding_id"]] = locate_iface.not_computed(locate_iface.NO_BOX)
        elif "camera" not in by_still[sid]:
            out[f["finding_id"]] = locate_iface.not_computed(locate_iface.NO_POSE)
        elif lidar:
            if scan_local is None:
                scan_local = await asyncio.to_thread(_local_scan, ctx, scan)
                if (seg_rec or {}).get("cloud_ref"):   # segment 0's cloud plus the registered bottom's
                    scan_local["points_file"] = str(ctx.local_file(scan["scan_id"], seg_rec["cloud_ref"]))
            out[f["finding_id"]] = await asyncio.to_thread(locator.locate, scan_local, local_still(sid), tuple(box))
        else:
            views = locate_iface.triangulation_views(f, findings, by_still, part_class)
            out[f["finding_id"]] = (await asyncio.to_thread(locator.triangulate, views) if views
                                    else locate_iface.not_computed(locate_iface.NO_SECOND_VIEW))
    return out, words


ABANDONED = ("more stills were added after the report (Continue scanning) but the scan was not completed in time; "
             "the latest report stands, and these stills are folded in on the next POST .../complete")
WORKER_GONE = "the worker inspecting it stopped before it finished; it is inspected again on the next complete"


def _age_s(stamp: str, now: datetime) -> float:
    return (now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()


# ------------------------------------------------------------------------------------- claim liveness (restarts)
def worker_alive(name: str | None) -> bool | None:
    """Whether the worker that holds a claim still runs: a worker name `host:pid` on this host is checked with its
    pid (True / False); a worker elsewhere is unknown (None; its heartbeat decides)."""
    import socket

    host, _, pid = (name or "").rpartition(":")
    if not pid.isdigit() or host != socket.gethostname():
        return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def claim_is_stale(st: dict, now: datetime, me: str | None = None) -> bool:
    """A `running` still claim whose worker is gone: its process on this host has exited, or it has had no
    heartbeat (or start, before its first beat) for CLAIM_STALE_S. A claim of `me` (this worker) is never stale."""
    if st.get("status") != "running" or (me and st.get("worker") == me):
        return False
    if worker_alive(st.get("worker")) is False:
        return True
    stamp = st.get("heartbeat_at") or st.get("started_at")
    return stamp is None or _age_s(stamp, now) > CLAIM_STALE_S


async def beat_claims(ctx: Context) -> int:
    """Refresh `heartbeat_at` on every still claim this worker holds (Context.hold runs it every CLAIM_BEAT_S while
    any claim is held, in every mode). Each beat is owner-checked in one transaction (pipeline.beat_still)."""
    n = 0
    for scan_id, still_id in list(ctx.held):
        try:
            await ctx.state.transition("pipeline-run", run_id_for(scan_id), "pipeline.beat_still",
                                       {"still_id": still_id, "worker": ctx.worker, "at": _now()})
        except AlreadyClaimed:   # finished, released or taken over: not ours to beat any more
            ctx.held.discard((scan_id, still_id))
            continue
        n += 1
    return n


async def release_claims(ctx: Context, *, worker: str | None = None, now: datetime | None = None
                         ) -> list[tuple[str, str]]:
    """Release `running` still claims back to pending and queue their stills again: the claims of `worker` (this
    worker shutting down), or, with no `worker`, every stale claim (claim_is_stale: its worker is gone; a worker
    starting up, and every sweep). A live claim of another worker is never touched. Returns (scan_id, still_id)."""
    import hashlib

    from repairos.slots.state import Job

    now = now or datetime.now(UTC)
    out = []
    for row in await ctx.state.list("pipeline-run"):
        run = row.data
        for sid, st in (run.get("stills") or {}).items():
            mine = worker is not None and st.get("status") == "running" and st.get("worker") == worker
            if not (mine or (worker is None and claim_is_stale(st, now, me=ctx.worker))):
                continue
            try:   # only the claim exactly as seen: a claim that was beaten, finished or taken since is left alone
                await ctx.state.transition("pipeline-run", row.id, "pipeline.release_still", {
                    "still_id": sid, "expect": {k: st.get(k) for k in ("worker", "started_at", "heartbeat_at")}})
            except AlreadyClaimed:
                continue
            ctx.held.discard((run["scan_id"], sid))
            still = await ctx.state.get("still", sid)
            if still is not None:
                kind = "ocr" if still.data.get("kind") == "label" else "detect"
                key = f"still:{sid}:released:{now.timestamp():.0f}"
                job_id = f"job_{kind}_{hashlib.sha256(key.encode()).hexdigest()[:16]}"
                await ctx.state.enqueue(Job(kind=kind, job_id=job_id, session_id=run["session_id"],
                                            scan_id=run["scan_id"], inputs={"kind": "still", "id": sid}))
            log.info("released the claim of %s on still %s (%s)", st.get("worker"), sid,
                     "shutting down" if mine else "its worker is gone")
            out.append((run["scan_id"], sid))
    return out


async def close_abandoned(ctx: Context, now: datetime | None = None) -> list[str]:
    """Close every continuation nobody finished: a run row `inspecting` (new stills after a finished report) with no
    change for `continuation_timeout_s` becomes `closed`. The scan keeps its latest finished report; the new stills
    keep their inspections (done claims stay done) and the next complete folds them in. A still still claimed
    `running` by then belongs to a stopped worker: it is marked failed with the reason, so the next run inspects it
    again. A scan with no finished report is left as it is (there is nothing to fall back to). Returns the scan ids
    closed."""
    now = now or datetime.now(UTC)
    limit = ctx.config["continuation_timeout_s"]
    closed = []
    for row in await ctx.state.list("pipeline-run"):
        run = row.data
        if run.get("status") != "inspecting" or _age_s(row.updated_at, now) < limit:
            continue
        rep = await ctx.state.get("report", report_id_for(run["scan_id"]))
        if rep is None:
            continue
        stills = run.get("stills") or {}
        gone = {sid: {"status": "failed", "error": WORKER_GONE, "finished_at": _now()}
                for sid, st in stills.items() if st.get("status") == "running"}
        in_report = int((rep.data.get("capture") or {}).get("stills") or 0)
        await ctx.state.transition("pipeline-run", row.id, "pipeline.update", {
            "scan_id": run["scan_id"], "session_id": run["session_id"], "status": "closed",
            "report_id": rep.data["report_id"], "stills": gone,
            "continuation": {"closed_at": _now(), "reason": ABANDONED, "report_rev": rep.rev,
                             "stills_in_report": in_report, "stills_since": max(0, len(stills) - in_report)}})
        log.info("closed abandoned continuation of scan %s (report rev %d stands)", run["scan_id"], rep.rev)
        closed.append(run["scan_id"])
    return closed


async def run_session(session_id: str, state: Any, blobs: Any, registry: Any = None, worker: str = "replay"
                      ) -> list[dict]:
    """Every scan of a session through the pipeline (tools/replay.py, `make replay`). Returns the reports."""
    ctx = Context(state=state, blobs=blobs, registry=registry, worker=worker)
    scans = sorted(r.id for r in await state.list("scan-package", session_id=session_id))
    if not scans:
        raise PipelineError(f"session {session_id} has no scan package")
    return [await run_scan(ctx, scan_id) for scan_id in scans]
