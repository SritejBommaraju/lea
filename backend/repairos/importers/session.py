"""Turn imported frames into a recorded session folder (samples/README.md), the same layout the gateway and the app
upload write, through `repairos.sessions.writer.SessionWriter`.

Each imported keyframe is written twice: as a ScanPackage keyframe (the scan) and as an `unguided` Still (the photo
the design engine looks at), with the same pose and intrinsics. `import.json` next to session.json records where the
session came from and every assumption the importer had to make.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from repairos.importers.camera import Pinhole, Pose
from repairos.sessions import ids
from repairos.sessions.layout import ext_for
from repairos.sessions.writer import SessionWriter

GRAVITY_ARKIT_WORLD = [0.0, -1.0, 0.0]   # ARKit worldAlignment .gravity (the default): Y up


@dataclass
class CaptureMeta:
    """What the operator tells the importer; the exports do not record it. Unknown stays 'unknown'."""
    device_model: str = "unknown"
    ios: str = "unknown"
    app_version: str | None = None
    symptom: str | None = None
    product_id: str | None = None
    object_id: str | None = None
    operator: str | None = None
    lighting: str | None = None
    notes: str | None = None
    captured_at: datetime | None = None


@dataclass
class ImportedFrame:
    source_frame: str          # the frame's name in the export
    timestamp_s: float         # seconds since the first frame of the capture
    pose: Pose                 # camera-to-world, ARKit camera axes, arkit_world
    cam: Pinhole               # intrinsics of `jpeg`, width/height set
    jpeg: bytes
    sharpness: float
    depth_png: bytes | None = None    # uint16 mm, the depth grid
    conf_png: bytes | None = None     # uint8 0-2


@dataclass
class ImportResult:
    path: Path
    session_id: str
    scan_id: str
    stills: int
    point_count: int | None
    problems: list[str] = field(default_factory=list)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_session(out_root: Path, *, meta: CaptureMeta, captured_at: datetime, frames: list[ImportedFrame],
                  depth_source: str, ply: bytes | None, point_count: int | None, voxel_mm: float | None,
                  provenance: dict[str, Any], mesh_ply: bytes | None = None) -> ImportResult:
    from repairos.sessions.validate import validate_session

    if not frames:
        raise ValueError("no frames to import")
    if (ply is None) != (depth_source == "none") or (mesh_ply is not None and ply is None):
        raise ValueError("a PLY (and optionally a mesh) is written exactly when depth_source is not none")
    session_id = ids.new_session_id(captured_at)
    if (Path(out_root) / session_id).exists():
        raise FileExistsError(f"{Path(out_root) / session_id} already exists")
    scan_id = ids.new_scan_id()
    w = SessionWriter(Path(out_root), session_id)
    mem: dict[str, bytes] = {}

    def put(data: bytes) -> str:
        key = f"mem:{len(mem)}"
        mem[key] = data
        return key

    def fetch(key: str) -> tuple[bytes, str]:
        data = mem[key]
        return data, ext_for(data)

    device = {"model": meta.device_model, "ios": meta.ios}
    if meta.app_version:
        device["app_version"] = meta.app_version
    session_meta = {"device": dict(device), "source": "converter", "created_at": _iso(datetime.now(UTC)),
                    "symptom": meta.symptom, "product_id": meta.product_id, "object_id": meta.object_id,
                    "operator": meta.operator, "lighting": meta.lighting,
                    "notes": meta.notes or f"imported from a {provenance['source_app']} export by "
                                           "tools/import_capture.py (see import.json)"}
    w.write_session(session_meta)

    keyframes = []
    for i, f in enumerate(frames):
        kf = {"keyframe_id": f"kf_{i:03d}", "timestamp_s": round(f.timestamp_s, 6), "image_file": put(f.jpeg),
              "camera": f.pose.to_contract(),
              "intrinsics": {"fx": f.cam.fx, "fy": f.cam.fy, "cx": f.cam.cx, "cy": f.cam.cy,
                             "width_px": f.cam.width, "height_px": f.cam.height},
              "tracking_state": "unknown", "sharpness": round(f.sharpness, 3)}
        if f.depth_png is not None:
            kf["depth_file"], kf["depth_encoding"] = put(f.depth_png), "uint16_mm"
        if f.conf_png is not None:
            kf["confidence_file"] = put(f.conf_png)
        keyframes.append(kf)
    pkg: dict[str, Any] = {"scan_id": scan_id, "product_id": meta.product_id or "unidentified",
                           "session_id": session_id, "captured_at": _iso(captured_at), "device": device,
                           "frame": "arkit_world", "units": "m", "depth_source": depth_source,
                           "gravity_arkit_world": GRAVITY_ARKIT_WORLD, "keyframes": keyframes,
                           "supporting_plane_removed": False}
    if meta.symptom:
        pkg["symptom"] = meta.symptom
    if ply is not None:
        pkg.update(points_file=put(ply), point_count=int(point_count), voxel_mm=float(voxel_mm))
    if mesh_ply is not None:
        pkg["mesh_file"] = put(mesh_ply)
    w.write_scan(pkg, fetch)

    for i, (f, kf) in enumerate(zip(frames, keyframes, strict=True), start=1):
        still = {"still_id": ids.still_id(i), "scan_id": scan_id, "session_id": session_id, "kind": "unguided",
                 "captured_at": _iso(captured_at + timedelta(seconds=f.timestamp_s)), "image_file": kf["image_file"],
                 "width_px": f.cam.width, "height_px": f.cam.height, "camera": kf["camera"],
                 "intrinsics": {"fx": f.cam.fx, "fy": f.cam.fy, "cx": f.cam.cx, "cy": f.cam.cy},
                 "targets": [], "tracking_state": "unknown", "sharpness": kf["sharpness"]}
        if "depth_file" in kf:
            still["depth_file"] = kf["depth_file"]
        if "confidence_file" in kf:
            still["confidence_file"] = kf["confidence_file"]
        w.write_still(still, fetch)

    prov = {**provenance, "scan_id": scan_id, "depth_source": depth_source,
            "keyframes": [{"keyframe_id": kf["keyframe_id"], "still_id": ids.still_id(i),
                           "source_frame": f.source_frame} for i, (f, kf) in enumerate(zip(frames, keyframes,
                                                                                            strict=True), start=1)]}
    (w.dir / "import.json").write_text(json.dumps(prov, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return ImportResult(w.dir, session_id, scan_id, len(frames), point_count, validate_session(w.dir))
