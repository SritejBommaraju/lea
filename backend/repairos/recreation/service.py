"""The scan's recreation on the gateway: built once per input (scan package revision + its stills), recorded on the
run row (`recreation`), announced with the live event `recreation_ready {scan_id, kind, url, rev}` and served by
GET /v1/scans/{id}/recreation.

Engines (config/pipeline.yaml `recreation.engine`):
- `textured_mesh` (default, any CPU): surface.scan_surface + texture.texture_mesh, written as a USDZ. Seconds.
- `splat`: a Gaussian splat trained on the GPU pod from the posed stills (recreation.splat). Needs CUDA + gsplat;
  without them the textured mesh is built instead and the record says why.
- `off`: nothing.

Bounded: the inputs are capped (stills, keyframes, voxels, triangles, atlas size), each build checks its wall clock
between stages against `timeout_s`, and one build runs at a time per process (the pipeline's side-work gate). GPU
time is ours (no paid API); nothing here calls a paid service, so nothing goes through repairos.spend.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

ENGINES = ("textured_mesh", "splat", "off")
DEFAULTS = {"engine": "textured_mesh", "atlas_px": 4096, "max_stills": 60, "max_keyframes": 80, "timeout_s": 120,
            "splat_steps": 4000, "splat_max_gaussians": 600_000}


SURFACE_METHOD = "object-only-1"   # part of every build's input key: a change to the surface rebuilds old scans once
                                   # (object-only-1: the table is cut from the mesh; earlier builds kept a table patch)


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
    if out["engine"] not in ENGINES:
        raise ValueError(f"recreation.engine must be one of {ENGINES}, not {out['engine']!r}")
    for k in ("atlas_px", "max_stills", "max_keyframes", "timeout_s", "splat_steps", "splat_max_gaussians"):
        if not isinstance(out[k], int) or isinstance(out[k], bool) or out[k] < 1:
            raise ValueError(f"recreation.{k} must be a whole number of at least 1")
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


def build_recreation(pkg_local: Mapping[str, Any], stills_local: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any],
                     segment_transforms: Mapping[int, Any] | None = None) -> Recreation:
    """The recreation from a scan package and its stills whose file fields are local paths (CPU textured mesh, or
    the GPU splat when configured and available). `segment_transforms` ({segment: 4x4}, pipeline/segments.py): the
    flipped segments the server registered close the textured mesh underneath. Raises RecreationUnavailable or
    RecreationTimeout."""
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
    if cfg["engine"] == "splat":
        from repairos.recreation import splat

        why = splat.unavailable_reason()
        if why is None:
            out = splat.train_scan(pkg_local, stills, box, steps=int(cfg["splat_steps"]),
                                   max_gaussians=int(cfg["splat_max_gaussians"]), timeout_s=float(cfg["timeout_s"]))
            return Recreation("splat", "splat", out.data, focus, radius, out.seconds, out.stats)
        note = f"splat engine not available here ({why}); built the textured mesh"
    from repairos.recreation.surface import scan_surface
    from repairos.recreation.texture import scan_views, texture_mesh
    from repairos.recreation.usdz import usdz_bytes

    if stills_local:   # a thin object (a board, a remote, a phone): its photos on a slab from segment 0
        # (recreation.flat); a flipped bottom, registered or not, stays photos
        from repairos.recreation.flat import scan_flat_model

        flat, why = scan_flat_model(pkg_local, stills_local)
        if flat is not None:
            buf = io.BytesIO()
            flat.atlas.save(buf, "JPEG", quality=92)
            data = usdz_bytes(flat.vertices, flat.uv, flat.faces, buf.getvalue())
            lo, hi = flat.vertices.min(0), flat.vertices.max(0)
            stats = {k: v for k, v in flat.stats.items() if isinstance(v, (int, float, str))}
            return Recreation("flat_slab", "usdz", data, [float(x) for x in (lo + hi) / 2],
                              float(np.linalg.norm(hi - lo) / 2), {"total_s": round(time.monotonic() - t0, 3)},
                              {**stats, "measured_mm": "x".join(str(x) for x in flat.stats["measured_mm"])}, why)
        if why.startswith("thin"):   # a thin object whose slab failed its checks: say why, build the usual mesh
            note = (note + "; " if note else "") + why
    try:
        surf = scan_surface(pkg_local, segment_transforms=segment_transforms)
    except MeshUnavailable as e:
        if not segment_transforms:
            raise RecreationUnavailable(str(e)) from e
        try:   # the merge left no surface: the top scan alone
            surf = scan_surface(pkg_local)
        except MeshUnavailable as e2:
            raise RecreationUnavailable(str(e2)) from e2
        note = (note + "; " if note else "") + f"built from the top scan alone (with the bottom: {e})"
        segment_transforms = None
    _deadline(t0, cfg["timeout_s"], "texturing")
    views = scan_views(pkg_local, stills, box=box, max_keyframes=int(cfg["max_keyframes"]),
                       segment_transforms=segment_transforms)
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


# ------------------------------------------------------------------------------------------------ the pipeline side
_locks: dict[str, asyncio.Lock] = {}


def payload(scan_id: str, rec: Mapping[str, Any]) -> dict:
    """The live event `recreation_ready` (and the pipeline view's `recreation`)."""
    return {"scan_id": scan_id, "kind": rec["kind"], "url": f"/v1/scans/{scan_id}/recreation?rev={rec['rev']}",
            "rev": rec["rev"], "format": rec["format"], "frame": "arkit_world", "bytes": rec.get("bytes")}


def input_key(scan_rev: int, stills: Sequence[Mapping[str, Any]], bottom: Sequence[int] = ()) -> str:
    """What a build used: the scan package revision, its stills (a continuation adds both) and the flipped segments
    the server registered (`bottom`)."""
    key = f"{scan_rev}:" + ",".join(sorted(s["still_id"] for s in stills)) + f"|{SURFACE_METHOD}"
    return key + (f"|bottom:{','.join(str(k) for k in sorted(bottom))}" if bottom else "")


async def ensure_recreation(ctx: Any, scan_id: str) -> dict:
    """The scan's recreation for its current inputs, built once: a blob recorded on the run row (`recreation`)
    with the live event `recreation_ready`. A scan it cannot be built for records why; a failed rebuild keeps the
    earlier recreation served."""
    from repairos.pipeline import segments
    from repairos.pipeline.run import _local_scan, _run_row, _segment_record, _update
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
        # the registered bottom, when its merge passed the gate (pipeline/segments.merge_gate)
        moved = segments.merged_transforms(await _segment_record(ctx, scan_id, rev))
        key = input_key(rev, stills, list(moved))
        have = (await _run_row(ctx, scan_id) or {}).get("recreation") or {}
        if have.get("input_key") == key and have.get("status") in ("done", "unavailable"):
            return have
        t0 = time.monotonic()
        try:
            local = await asyncio.to_thread(_local_scan, ctx, scan)
            local_stills = await asyncio.to_thread(
                lambda: [map_files("still", s, lambda v, _b: str(ctx.local_file(scan_id, v))) for s in stills])
            r = await asyncio.to_thread(build_recreation, local, local_stills, cfg, moved)
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
               "bottom_segments": [int(k) for k in r.stats.get("bottom_segments") or []],
               "seconds": {**r.seconds, "with_io_s": round(time.monotonic() - t0, 3)},
               "stats": {k: v for k, v in r.stats.items() if isinstance(v, (int, float, str))}, "note": r.note or None,
               "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        await _update(ctx, scan, {"recreation": rec, "announce": "recreation_ready"})
        return rec


async def recreation_quietly(ctx: Any, scan_id: str) -> None:
    try:
        await ensure_recreation(ctx, scan_id)
    except Exception as e:  # noqa: BLE001 - ensure_recreation records its own failures; this only guards the task
        log.warning("recreation for scan %s: %s", scan_id, e)
