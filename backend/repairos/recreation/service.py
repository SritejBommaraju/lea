"""The scan's recreation on the gateway: built once per input (scan package revision + its stills), recorded on the
run row (`recreation`), announced with the live event `recreation_ready {scan_id, kind, url, rev}` and served by
GET /v1/scans/{id}/recreation.

Engines (config/pipeline.yaml `recreation.engine`):
- `textured_mesh` (default, any CPU): surface.scan_surface + texture.texture_mesh, written as a USDZ. Seconds.
- `splat`: the textured mesh first (as above, announced at once), then a Gaussian splat trained on the GPU pod from
  the same posed stills (recreation.splat), recorded beside it on the run row (`splat`) and announced with
  `recreation_ready` again, `kind: splat` (GET /v1/scans/{id}/recreation?kind=splat). The mesh stays the
  fallback: it is what GET .../recreation serves without `kind`, and what stays when the splat cannot be trained
  here (no CUDA, gsplat not installed or its kernels still compiling) or fails (the `splat` record says why).
  One splat at a time per process, outside the side-work gate, every training step yielding the GPU to the
  detector (Context.gpu_low_priority). The env `LEA_RECREATION_ENGINE` overrides the config's engine.
- `off`: nothing.

Bounded: the inputs are capped (stills, keyframes, voxels, triangles, atlas size), each build checks its wall clock
between stages against `timeout_s`, and one build runs at a time per process (the pipeline's side-work gate). GPU
time is ours (no paid API); nothing here calls a paid service, so nothing goes through repairos.spend.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ENGINES = ("textured_mesh", "splat", "off")
DEFAULTS = {"engine": "textured_mesh", "atlas_px": 4096, "max_stills": 60, "max_keyframes": 80, "timeout_s": 120,
            "splat_steps": 3000, "splat_max_gaussians": 450_000, "splat_timeout_s": 110, "splat_gpu_mem_gb": 10,
            "splat_max_mb": 15, "splat_log_every": 500}
INT_KEYS = ("atlas_px", "max_stills", "max_keyframes", "timeout_s", "splat_steps", "splat_max_gaussians",
            "splat_timeout_s", "splat_log_every")
ENGINE_ENV = "LEA_RECREATION_ENGINE"


class RecreationUnavailable(RuntimeError):
    """No recreation can be built from this scan (the reason is the message)."""


class RecreationTimeout(RuntimeError):
    """A build passed its time bound between two stages."""


def recreation_config(raw: Mapping[str, Any] | None = None) -> dict:
    """The `recreation:` section of config/pipeline.yaml over DEFAULTS, validated."""
    if raw is None:
        import yaml

        from repairos.pipeline.config import PIPELINE_CONFIG

        path = Path(os.environ.get("REPAIROS_PIPELINE_CONFIG") or PIPELINE_CONFIG)
        raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("recreation") or {}
    out = {**DEFAULTS, **dict(raw)}
    if os.environ.get(ENGINE_ENV):
        out["engine"] = os.environ[ENGINE_ENV].strip()
    if out["engine"] not in ENGINES:
        raise ValueError(f"recreation.engine must be one of {ENGINES}, not {out['engine']!r}")
    for k in INT_KEYS:
        if not isinstance(out[k], int) or isinstance(out[k], bool) or out[k] < 1:
            raise ValueError(f"recreation.{k} must be a whole number of at least 1")
    for k in ("splat_gpu_mem_gb", "splat_max_mb"):
        if not isinstance(out[k], (int, float)) or isinstance(out[k], bool) or out[k] <= 0:
            raise ValueError(f"recreation.{k} must be a number above 0")
    if not 512 <= out["atlas_px"] <= 8192:
        raise ValueError("recreation.atlas_px must be between 512 and 8192")
    return out


@dataclass
class Recreation:
    kind: str                 # textured_mesh | splat
    format: str               # usdz | splat
    data: bytes
    focus_center: list[float]
    focus_radius: float
    seconds: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    note: str = ""


def _deadline(t0: float, timeout_s: float, stage: str) -> None:
    if time.monotonic() - t0 > timeout_s:
        raise RecreationTimeout(f"the recreation took longer than {timeout_s:.0f} s (stopped before {stage})")


def build_recreation(pkg_local: Mapping[str, Any], stills_local: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any]
                     ) -> Recreation:
    """The recreation from a scan package and its stills whose file fields are local paths (CPU textured mesh, or
    the GPU splat when configured and available). Raises RecreationUnavailable or RecreationTimeout."""
    from repairos.pipeline.mesh import MeshUnavailable
    from repairos.recon.tsdf import ObjectBox

    t0 = time.monotonic()
    if not pkg_local.get("crop_box"):
        raise RecreationUnavailable("the scan has no object box")
    box = ObjectBox.from_contract(pkg_local["crop_box"])
    focus = [float(x) for x in box.centre]
    radius = float(0.5 * (box.extent ** 2).sum() ** 0.5)
    stills = sorted(stills_local, key=lambda s: -(s.get("sharpness") or 0.0))[: int(cfg["max_stills"])]
    note = ""
    from repairos.recreation.surface import scan_surface
    from repairos.recreation.texture import scan_views, texture_mesh
    from repairos.recreation.usdz import usdz_bytes

    try:
        surf = scan_surface(pkg_local)
    except MeshUnavailable as e:
        raise RecreationUnavailable(str(e)) from e
    _deadline(t0, cfg["timeout_s"], "texturing")
    views = scan_views(pkg_local, stills, box=box, max_keyframes=int(cfg["max_keyframes"]))
    if not views:
        raise RecreationUnavailable("the scan has no posed photos")
    _deadline(t0, cfg["timeout_s"], "texturing")
    tex = texture_mesh(surf.vertices, surf.faces, views, atlas_px=int(cfg["atlas_px"]))
    _deadline(t0, cfg["timeout_s"], "writing")
    t1 = time.monotonic()
    data = usdz_bytes(tex.vertices, tex.uv, tex.faces, tex.atlas_jpeg())
    from repairos.recreation.surface import object_focus

    centre, radius = object_focus(surf.vertices, surf.ground_y, box)
    focus = [float(x) for x in centre]
    seconds = {**{f"surface_{k}": v for k, v in surf.seconds.items()}, **tex.stats.pop("seconds", {}),
               "usdz_s": round(time.monotonic() - t1, 3), "total_s": round(time.monotonic() - t0, 3)}
    return Recreation("textured_mesh", "usdz", data, focus, radius, seconds, {**tex.stats, **surf.params}, note)


def build_splat(pkg_local: Mapping[str, Any], stills_local: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any], *,
                gate: Any = None, label: str = "") -> Recreation:   # pragma: no cover - needs CUDA
    """The scan's Gaussian splat (recreation.splat.train_scan within the `splat_*` bounds)."""
    from repairos.recon.tsdf import ObjectBox
    from repairos.recreation import splat

    if not pkg_local.get("crop_box"):
        raise RecreationUnavailable("the scan has no object box")
    box = ObjectBox.from_contract(pkg_local["crop_box"])
    stills = sorted(stills_local, key=lambda s: -(s.get("sharpness") or 0.0))[: int(cfg["max_stills"])]
    out = splat.train_scan(pkg_local, stills, box, steps=int(cfg["splat_steps"]),
                           max_gaussians=int(cfg["splat_max_gaussians"]), timeout_s=float(cfg["splat_timeout_s"]),
                           max_bytes=int(float(cfg["splat_max_mb"]) * 1e6), mem_cap_gb=float(cfg["splat_gpu_mem_gb"]),
                           log_every=int(cfg["splat_log_every"]), gate=gate, label=label)
    if out.count == 0:
        raise RecreationUnavailable("the trained splat kept no Gaussians inside the object box")
    focus = [float(x) for x in box.centre]
    radius = float(0.5 * (box.extent ** 2).sum() ** 0.5)
    return Recreation("splat", "splat", out.data, focus, radius, out.seconds, out.stats)


# ------------------------------------------------------------------------------------------------ the pipeline side
_locks: dict[str, asyncio.Lock] = {}
_splat_locks: dict[str, asyncio.Lock] = {}
_splat_gate: asyncio.Semaphore | None = None


def payload(scan_id: str, rec: Mapping[str, Any], fallback: Mapping[str, Any] | None = None) -> dict:
    """The live event `recreation_ready` (and the pipeline view's `recreation`). A splat names its own URL
    (`kind=splat`) and the textured mesh it falls back to (`fallback_url`)."""
    q = "kind=splat&" if rec["kind"] == "splat" else ""
    out = {"scan_id": scan_id, "kind": rec["kind"], "url": f"/v1/scans/{scan_id}/recreation?{q}rev={rec['rev']}",
           "rev": rec["rev"], "format": rec["format"], "frame": "arkit_world", "bytes": rec.get("bytes")}
    if rec["kind"] == "splat" and fallback and fallback.get("status") == "done":
        out.update(fallback_url=f"/v1/scans/{scan_id}/recreation?rev={fallback['rev']}",
                   fallback_kind=fallback.get("kind"), fallback_rev=fallback.get("rev"))
    return out


def input_key(scan_rev: int, stills: Sequence[Mapping[str, Any]]) -> str:
    """What a build used: the scan package revision and its stills (a continuation adds both)."""
    return f"{scan_rev}:" + ",".join(sorted(s["still_id"] for s in stills))


async def ensure_recreation(ctx: Any, scan_id: str) -> dict:
    """The scan's recreation for its current inputs, built once: a blob recorded on the run row (`recreation`)
    with the live event `recreation_ready`. A scan it cannot be built for records why; a failed rebuild keeps the
    earlier recreation served."""
    from repairos.pipeline.run import _local_scan, _run_row, _update
    from repairos.sessions.layout import map_files

    cfg = recreation_config()
    if cfg["engine"] == "off":
        return {"status": "off"}
    lock = _locks.setdefault(scan_id, asyncio.Lock())
    async with lock:
        row = await ctx.state.get("scan-package", scan_id)
        if row is None:
            raise RecreationUnavailable(f"no scan-package {scan_id}")
        scan, rev = row.data, row.rev
        stills = [r.data for r in await ctx.state.list("still", scan_id=scan_id)]
        key = input_key(rev, stills)
        have = (await _run_row(ctx, scan_id) or {}).get("recreation") or {}
        if have.get("input_key") == key and have.get("status") in ("done", "unavailable"):
            return have
        t0 = time.monotonic()
        try:
            local = await asyncio.to_thread(_local_scan, ctx, scan)
            local_stills = await asyncio.to_thread(
                lambda: [map_files("still", s, lambda v, _b: str(ctx.local_file(scan_id, v))) for s in stills])
            r = await asyncio.to_thread(build_recreation, local, local_stills, cfg)
        except Exception as e:  # noqa: BLE001 - the recreation never fails the report; the row says why
            unavailable = isinstance(e, RecreationUnavailable)
            if not unavailable:
                log.warning("recreation for scan %s failed: %s", scan_id, e)
            reason = str(e) if unavailable else f"building the recreation failed ({type(e).__name__}: {e})"[:500]
            if have.get("status") == "done":
                rec = {**have, "rebuild_error": reason, "rebuild_input_key": key}
            else:
                rec = {"status": "unavailable" if unavailable else "failed", "reason": reason, "input_key": key}
            await _update(ctx, scan, {"recreation": rec})
            return rec
        content_type = "model/vnd.usdz+zip" if r.format == "usdz" else "application/octet-stream"
        ref = await asyncio.to_thread(ctx.blobs.put, r.data, content_type)
        rec = {"status": "done", "ref": ref, "kind": r.kind, "format": r.format, "rev": int(have.get("rev") or 0) + 1,
               "input_key": key, "scan_rev": rev, "frame": "arkit_world", "bytes": len(r.data),
               "focus_center": [round(x, 5) for x in r.focus_center], "focus_radius": round(r.focus_radius, 5),
               "seconds": {**r.seconds, "with_io_s": round(time.monotonic() - t0, 3)},
               "stats": {k: v for k, v in r.stats.items() if isinstance(v, (int, float, str))}, "note": r.note or None,
               "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        await _update(ctx, scan, {"recreation": rec, "announce": "recreation_ready"})
        return rec


async def ensure_splat(ctx: Any, scan_id: str) -> dict:
    """The scan's splat for its current inputs (engine `splat`), built once after the textured mesh and recorded on
    the run row as `splat` with the live event `recreation_ready` (kind splat). Not trainable here now (no CUDA,
    gsplat missing or still compiling): recorded without an input key, so a later trigger tries again. A failure
    is recorded for this input (no retry loop) and the textured mesh stays the recreation."""
    global _splat_gate
    from repairos.pipeline.run import _local_scan, _run_row, _update
    from repairos.recreation import splat
    from repairos.sessions.layout import map_files

    cfg = recreation_config()
    if cfg["engine"] != "splat":
        return {"status": "off"}
    if _splat_gate is None:
        _splat_gate = asyncio.Semaphore(1)
    lock = _splat_locks.setdefault(scan_id, asyncio.Lock())
    async with lock, _splat_gate:
        row = await ctx.state.get("scan-package", scan_id)
        if row is None:
            raise RecreationUnavailable(f"no scan-package {scan_id}")
        scan, rev = row.data, row.rev
        stills = [r.data for r in await ctx.state.list("still", scan_id=scan_id)]
        key = input_key(rev, stills)
        run = await _run_row(ctx, scan_id) or {}
        have = run.get("splat") or {}
        if have.get("input_key") == key and have.get("status") in ("done", "unavailable", "failed"):
            return have
        why = await asyncio.to_thread(splat.unavailable_reason)
        if why:
            log.info("splat for scan %s skipped: %s; the textured mesh stays the recreation", scan_id, why)
            if have.get("status") != "done":
                rec = {"status": "unavailable", "reason": why, "input_key": None}
                await _update(ctx, scan, {"splat": rec})
                return rec
            return have
        t0 = time.monotonic()
        gate = getattr(ctx, "gpu_low_priority", None)
        try:
            local = await asyncio.to_thread(_local_scan, ctx, scan)
            local_stills = await asyncio.to_thread(
                lambda: [map_files("still", s, lambda v, _b: str(ctx.local_file(scan_id, v))) for s in stills])
            r = await asyncio.to_thread(build_splat, local, local_stills, cfg, gate=gate, label=scan_id)
        except Exception as e:  # noqa: BLE001 - a splat never fails the report or the mesh; the row says why
            unavailable = isinstance(e, RecreationUnavailable)
            log.warning("splat for scan %s failed after %.1f s (%s: %s); the textured mesh stays the recreation",
                        scan_id, time.monotonic() - t0, type(e).__name__, e)
            reason = str(e) if unavailable else f"training the splat failed ({type(e).__name__}: {e})"[:500]
            if have.get("status") == "done":
                rec = {"rebuild_error": reason, "rebuild_input_key": key}
            else:
                rec = {"status": "unavailable" if unavailable else "failed", "reason": reason, "input_key": key}
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001 - only frees the cache after an out-of-memory
                pass
            await _update(ctx, scan, {"splat": rec})
            return rec
        ref = await asyncio.to_thread(ctx.blobs.put, r.data, "application/octet-stream")
        rec = {"status": "done", "ref": ref, "kind": "splat", "format": "splat", "rev": int(have.get("rev") or 0) + 1,
               "input_key": key, "scan_rev": rev, "frame": "arkit_world", "bytes": len(r.data),
               "focus_center": [round(x, 5) for x in r.focus_center], "focus_radius": round(r.focus_radius, 5),
               "seconds": {**r.seconds, "with_io_s": round(time.monotonic() - t0, 3)},
               "stats": {k: v for k, v in r.stats.items() if isinstance(v, (int, float, str))},
               "reason": None, "rebuild_error": None, "rebuild_input_key": None,
               "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        await _update(ctx, scan, {"splat": rec, "announce": "splat_ready"})
        log.info("splat for scan %s ready: rev %d, %.2f MB, %s Gaussians, %.1f s with I/O", scan_id, rec["rev"],
                 len(r.data) / 1e6, r.stats.get("gaussians_kept"), rec["seconds"]["with_io_s"])
        return rec


async def splat_quietly(ctx: Any, scan_id: str) -> None:
    try:
        await ensure_splat(ctx, scan_id)
    except Exception as e:  # noqa: BLE001 - ensure_splat records its own failures; this only guards the task
        log.warning("splat for scan %s: %s", scan_id, e)


async def recreation_quietly(ctx: Any, scan_id: str) -> None:
    try:
        rec = await ensure_recreation(ctx, scan_id)
    except Exception as e:  # noqa: BLE001 - ensure_recreation records its own failures; this only guards the task
        log.warning("recreation for scan %s: %s", scan_id, e)
        return
    try:
        engine = recreation_config()["engine"]
    except Exception:  # noqa: BLE001 - a bad config was already reported by ensure_recreation
        return
    if engine == "splat" and rec.get("status") in ("done", "unavailable", "failed"):
        # outside the side-work gate: a 2-minute training must not hold back the next scan's identity or mesh
        starter = getattr(ctx, "start", None) or ctx.spawn
        starter(splat_quietly(ctx, scan_id))
