"""Import a NeRFCapture offline export (MIT, https://github.com/jc211/NeRFCapture): the no-LiDAR path (A8).

Export (a zip from the Files app, or its unzipped folder):

    transforms.json    {w, h, fl_x, fl_y, cx, cy, depth_integer_scale, frames: [{file_path, depth_path,
                       transform_matrix, timestamp, fl_x, fl_y, cx, cy, w, h}]}   (Manifest.swift, snake case)
    images/<n>.png     ARFrame.capturedImage, sensor orientation; file_path is "images/<n>" without the extension
    images/<n>.depth.png  only on LiDAR devices

What the app's code does (DatasetWriter.swift, Utils.swift at commit 312cb01):

- `transform_matrix` is `ARFrame.camera.transform` written row by row: camera-to-world, **ARKit camera axes**
  (x right, y up, looking along -z), ARKit world (gravity-aligned, Y up), metres. Used as is.
- fl_x, fl_y, cx, cy are `ARFrame.camera.intrinsics` in pixels of the w x h image.
- The depth PNG is the float depth map drawn through Core Image into an 8-bit image and resized to the colour
  image's size: values are clipped and colour-managed, not millimetres. It is **not used**; the scan is written with
  `depth_source: none` and no point cloud (locate with `repairos.locate.triangulate` across stills).
- The tracking state is not recorded (written as `unknown`).
"""
from __future__ import annotations

import json
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from repairos.importers import images
from repairos.importers.images import ImportFormatError
from repairos.importers.session import CaptureMeta, ImportedFrame, ImportResult, write_session
from repairos.locate.camera import Pinhole, Pose

IMAGE_EXTS = ("", ".png", ".jpg", ".jpeg")


def find_transforms(path: Path) -> Path | None:
    """transforms.json in the folder or one level below (the zip holds a folder named after the project)."""
    if (path / "transforms.json").is_file():
        return path / "transforms.json"
    found = sorted(path.glob("*/transforms.json"))
    return found[0] if len(found) == 1 else None


def is_nerfcapture_export(path: Path) -> bool:
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            return any(Path(n).name == "transforms.json" for n in z.namelist())
    return path.is_dir() and find_transforms(path) is not None


def nerfcapture_pose(frame: dict) -> Pose:
    m = np.asarray(frame["transform_matrix"], dtype=float)
    if m.shape != (4, 4) or not np.allclose(m[3], [0, 0, 0, 1], atol=1e-4):
        raise ImportFormatError(f"transform_matrix must be a row-major 4x4 with last row 0 0 0 1, got {m.tolist()}")
    return Pose.from_matrix(m)


def _intrinsics(frame: dict, top: dict) -> Pinhole:
    get = lambda k: frame.get(k, top.get(k))  # noqa: E731
    try:
        return Pinhole(float(get("fl_x")), float(get("fl_y")), float(get("cx")), float(get("cy")),
                       int(get("w")), int(get("h")))
    except (TypeError, ValueError):
        raise ImportFormatError("a frame lacks fl_x, fl_y, cx, cy, w or h (and transforms.json has no default)") \
            from None


def _image(root: Path, file_path: str) -> Path:
    base = (root / file_path).resolve()
    if root.resolve() not in base.parents:
        raise ImportFormatError(f"file_path {file_path!r} points outside the export")
    for ext in IMAGE_EXTS:
        p = base.with_name(base.name + ext)
        if p.is_file():
            return p
    raise ImportFormatError(f"image {file_path} is missing")


def _import_dir(root: Path, out_root: Path, meta: CaptureMeta, source_name: str) -> ImportResult:
    tf = find_transforms(root)
    if tf is None:
        raise ImportFormatError(f"{root}: no transforms.json")
    top = json.loads(tf.read_text(encoding="utf-8"))
    raw = top.get("frames") or []
    if not raw:
        raise ImportFormatError("transforms.json has no frames")
    raw = sorted(raw, key=lambda f: float(f.get("timestamp", 0.0)))
    t0 = float(raw[0].get("timestamp", 0.0))
    frames, depth_seen = [], 0
    for f in raw:
        cam = _intrinsics(f, top)
        rgb = images.read_rgb(_image(tf.parent, f["file_path"]))
        h, w = rgb.shape[:2]
        if (w, h) != (cam.width, cam.height):
            if abs(w / h - cam.width / cam.height) > 1e-3:
                raise ImportFormatError(f"{f['file_path']}: image is {w}x{h}, intrinsics are for "
                                        f"{cam.width}x{cam.height} (a different shape: rotated?)")
            cam = cam.scaled(w, h)
        depth_seen += bool(f.get("depth_path"))
        frames.append(ImportedFrame(source_frame=f["file_path"], timestamp_s=float(f.get("timestamp", t0)) - t0,
                                    pose=nerfcapture_pose(f), cam=cam, jpeg=images.jpeg_bytes(rgb),
                                    sharpness=images.sharpness(rgb)))
    if meta.captured_at is not None:
        captured_at, captured_src = meta.captured_at, "operator"
    else:
        captured_at = datetime.fromtimestamp(tf.stat().st_mtime, UTC)
        captured_src = "modification time of transforms.json (written when the capture ended)"
    provenance = {
        "source_app": "NeRFCapture", "source_url": "https://github.com/jc211/NeRFCapture",
        "source_folder": source_name, "importer": "repairos.importers.nerfcapture",
        "frames": {"in_transforms": len(raw), "imported": len(frames), "with_depth_png": depth_seen},
        "captured_at_source": captured_src,
        "assumptions": [
            "transform_matrix used as is: ARKit camera.transform, camera-to-world, ARKit camera axes",
            "tracking state is not recorded by NeRFCapture: written as unknown",
            (f"{depth_seen} depth PNGs present but not used: NeRFCapture writes them as an 8-bit Core Image render "
             "of the depth map (clipped, colour-managed), not millimetres" if depth_seen
             else "no depth in the export (a device without LiDAR, or depth turned off)"),
            "depth_source none: no point cloud; sizes cannot be read off this scan",
        ],
    }
    return write_session(Path(out_root), meta=meta, captured_at=captured_at, frames=frames, depth_source="none",
                         ply=None, point_count=None, voxel_mm=None, provenance=provenance)


def import_nerfcapture(src: Path, out_root: Path, meta: CaptureMeta | None = None) -> ImportResult:
    src, meta = Path(src), meta or CaptureMeta()
    if src.is_file() and src.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory(prefix="nerfcapture-") as tmp:
            with zipfile.ZipFile(src) as z:
                for name in z.namelist():   # refuse absolute paths and '..' before extracting anything
                    target = (Path(tmp) / name).resolve()
                    if Path(tmp).resolve() not in target.parents and target != Path(tmp).resolve():
                        raise ImportFormatError(f"{src.name}: zip entry {name!r} points outside the archive")
                z.extractall(tmp)
            return _import_dir(Path(tmp), out_root, meta, src.name)
    return _import_dir(src, out_root, meta, src.name)
