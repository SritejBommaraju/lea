"""Import a Stray Scanner export (MIT, https://github.com/strayrobots/scanner, format: docs/format.md).

Export folder (one per recording):

    rgb.mp4            HEVC video of ARFrame.capturedImage, sensor orientation (1920x1440 on current iPhones)
    odometry.csv       header + one row per saved frame: timestamp, frame, x, y, z, qx, qy, qz, qw, fx, fy, cx, cy,
                       distortion_center_x, distortion_center_y  (older exports stop after qw)
    camera_matrix.csv  3x3 intrinsics of the last frame (used when odometry.csv has no fx..cy columns)
    depth/<frame>.png  256x192 uint16 depth in millimetres (ARKit sceneDepth); older exports: .npy
    confidence/<frame>.png  256x192 uint8 ARKit confidence 0/1/2
    imu.csv            raw accelerometer + gyro (not used: poses come from ARKit)
    distortion/<frame>.bin  optional AVCameraCalibrationData lens-distortion lookup tables (float32 LE)

What the app's code does (OdometryEncoder.swift, DatasetEncoder.swift, VideoEncoder.swift at commit ec3e1dc):

- x, y, z is `ARFrame.camera.transform`'s translation: the camera centre in ARKit's world (gravity-aligned, Y up,
  origin where the AR session started), metres.
- The quaternion is `q_WA * q_AC` with `q_AC = (x 1, y 0, z 0, w 0)`, a 180 degree turn about x: the camera's
  rotation with **OpenCV camera axes** (x right, y down, z forward). We turn it back into ARKit camera axes
  (`camera.opencv_to_arkit_camera`), the contracts' convention.
- fx, fy, cx, cy are `ARFrame.camera.intrinsics`, in pixels of the captured image (= the video frame).
- Video frame n and odometry row n are appended together on one serial queue, so they pair in order. The video's
  presentation time is (ARKit frame counter - 1) / 60 s, so the first saved frame sits at -1/60 s, before the
  writer's start time: `-ignore_editlist` keeps it. If the file lacks it (the first presentation time is >= 0 and
  the video is one frame short), video frame n pairs with odometry row n + 1. Timing between the two is checked.
- Depth PNG values are `round(depth_m * 1000)` (PngEncoder.mm); the depth grid covers the same field of view as
  the captured image, so its intrinsics are the image's scaled to 256x192.
- The tracking state is not recorded (written as `unknown`). Lens distortion tables, when present, are not
  applied: ARKit's intrinsics are a pinhole model of the captured image, which is what everything here assumes.
"""
from __future__ import annotations

import csv
import io
import math
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import numpy as np

from repairos.importers import images, video
from repairos.importers.fuse import make_fuser, measure_spacing
from repairos.importers.images import ImportFormatError
from repairos.importers.keyframes import select_keyframes
from repairos.importers.ply import mesh_ply_bytes, ply_bytes
from repairos.importers.session import CaptureMeta, ImportedFrame, ImportResult, write_session
from repairos.importers.camera import Pinhole, Pose, opencv_to_arkit_camera, quat_to_matrix

POSE_COLUMNS = ("x", "y", "z", "qx", "qy", "qz", "qw")
INTRINSIC_COLUMNS = ("fx", "fy", "cx", "cy")
WRITER_TICKS_PER_S = 60   # VideoEncoder.swift: CMTimeScale(60), value = ARKit frame counter


def is_stray_export(path: Path) -> bool:
    return (path / "odometry.csv").is_file() and (path / "rgb.mp4").is_file()


def read_odometry(text: str) -> list[dict[str, float | str]]:
    """Rows of odometry.csv by column name (spaces after commas allowed); `frame` stays a string ('000005')."""
    reader = csv.reader(io.StringIO(text), skipinitialspace=True)
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration:
        raise ImportFormatError("odometry.csv is empty") from None
    missing = [c for c in ("timestamp", *POSE_COLUMNS) if c not in header]
    if missing:
        raise ImportFormatError(f"odometry.csv header lacks {missing} (got {header})")
    rows = []
    for n, raw in enumerate(reader, start=2):
        if not raw or all(not v.strip() for v in raw):
            continue
        rec = dict(zip(header, (v.strip() for v in raw), strict=False))
        row: dict[str, float | str] = {"frame": rec.get("frame") or f"{len(rows):06d}"}
        try:
            for c in ("timestamp", *POSE_COLUMNS):
                row[c] = float(rec[c])
            for c in INTRINSIC_COLUMNS:
                if rec.get(c):
                    row[c] = float(rec[c])
        except (KeyError, ValueError) as e:
            raise ImportFormatError(f"odometry.csv line {n}: {e}") from None
        rows.append(row)
    if not rows:
        raise ImportFormatError("odometry.csv has no rows")
    return rows


def read_camera_matrix(text: str) -> tuple[float, float, float, float]:
    m = np.array([[float(v) for v in line.split(",")] for line in text.strip().splitlines()])
    if m.shape != (3, 3):
        raise ImportFormatError(f"camera_matrix.csv must be 3x3, got {m.shape}")
    return float(m[0, 0]), float(m[1, 1]), float(m[0, 2]), float(m[1, 2])


def stray_pose(row: dict) -> Pose:
    """odometry.csv row -> camera-to-world pose with ARKit camera axes (see the module docstring)."""
    r_cv = quat_to_matrix([row["qx"], row["qy"], row["qz"], row["qw"]])
    return Pose(opencv_to_arkit_camera(r_cv), np.array([row["x"], row["y"], row["z"]], dtype=float))


def video_ticks(pts: list[int], time_base: Fraction) -> list[int]:
    """Presentation times in the writer's ticks (1/60 s, = ARKit frame counter - 1)."""
    return [round(p * time_base * WRITER_TICKS_PER_S) for p in pts]


def align_video(ticks: list[int], timestamps: list[float]) -> int:
    """Offset such that video frame k pairs with odometry row k + offset (0 or 1); checks the timing agrees."""
    n_v, n_o = len(ticks), len(timestamps)
    if n_v == n_o:
        offset = 0
    elif n_v == n_o - 1:
        offset = 0 if ticks and ticks[0] < 0 else 1
    else:
        raise ImportFormatError(f"rgb.mp4 has {n_v} frames but odometry.csv has {n_o} rows: they cannot be paired")
    if n_v >= 3:
        da = np.diff(np.asarray(ticks, dtype=float))
        dt = np.diff(np.asarray(timestamps[offset:offset + n_v], dtype=float))
        ok = dt > 0
        if ok.sum() >= 2:
            rate = float(np.median(da[ok] / dt[ok]))
            bad = np.abs(da[ok] - rate * dt[ok]) > 0.5
            if bad.mean() > 0.02:
                raise ImportFormatError(f"rgb.mp4 frame times do not follow odometry.csv timestamps "
                                        f"({int(bad.sum())} of {int(ok.sum())} steps disagree): frames and poses "
                                        "would be mis-paired")
    return offset


def _frame_file(folder: Path, frame: str, exts: tuple[str, ...]) -> Path | None:
    for ext in exts:
        p = folder / f"{frame}{ext}"
        if p.is_file():
            return p
    return None


def import_stray(src: Path, out_root: Path, meta: CaptureMeta | None = None, *, spacing_mm: float | None = None,
                 fuser: str = "auto", min_confidence: int = 2, min_views: int = 2, min_depth_m: float = 0.1,
                 max_depth_m: float = 3.0, fuse_max_frames: int = 400, travel_m: float = 0.10,
                 angle_deg: float = 15.0) -> ImportResult:
    """`spacing_mm` None = measured from the depth (fuse.measure_spacing); `fuser`: auto | open3d | numpy."""
    src, meta = Path(src), meta or CaptureMeta()
    if not is_stray_export(src):
        raise ImportFormatError(f"{src}: not a Stray Scanner export (needs odometry.csv and rgb.mp4)")
    rows = read_odometry((src / "odometry.csv").read_text(encoding="utf-8"))
    fallback_k = None
    if any(c not in r for r in rows for c in INTRINSIC_COLUMNS):
        if not (src / "camera_matrix.csv").is_file():
            raise ImportFormatError("odometry.csv has no fx, fy, cx, cy and camera_matrix.csv is missing")
        fallback_k = read_camera_matrix((src / "camera_matrix.csv").read_text(encoding="utf-8"))

    rgb = src / "rgb.mp4"
    info = video.probe(rgb)
    width, height = info["width"], info["height"]
    offset = align_video(video_ticks(info["pts"], info["time_base"]), [float(r["timestamp"]) for r in rows])
    n = min(len(info["pts"]), len(rows) - offset)
    paired = rows[offset:offset + n]
    poses = [stray_pose(r) for r in paired]
    cams = [Pinhole(*(tuple(float(r[c]) for c in INTRINSIC_COLUMNS) if fallback_k is None else fallback_k),
                    width, height) for r in paired]

    depth_dir, conf_dir = src / "depth", src / "confidence"
    depth_files = [_frame_file(depth_dir, str(r["frame"]), (".png", ".npy")) for r in paired]
    conf_files = [_frame_file(conf_dir, str(r["frame"]), (".png",)) for r in paired]
    with_depth = [k for k, p in enumerate(depth_files) if p is not None]
    if with_depth:
        dh, dw = images.read_depth_mm(depth_files[with_depth[0]]).shape
        stride = max(1, math.ceil(len(with_depth) / fuse_max_frames))
        fuse_set = set(with_depth[::stride])
    else:
        dw, dh, stride, fuse_set = 256, 192, 1, set()

    def depth_frame(k: int):
        conf = images.read_confidence(conf_files[k]) if conf_files[k] is not None else None
        return images.read_depth_mm(depth_files[k]), conf, cams[k].scaled(dw, dh)

    measured = measure_spacing((depth_frame(k) for k in sorted(fuse_set)), min_confidence=min_confidence,
                               min_depth_m=min_depth_m, max_depth_m=max_depth_m)
    if spacing_mm is not None:
        spacing_m, spacing_rule = spacing_mm / 1000.0, "set by the operator"
    elif measured is not None:
        spacing_m, spacing_rule = measured.spacing_m, "measured"
    else:
        spacing_m, spacing_rule = 0.003, "no high-confidence depth to measure; nothing is fused"
    fusion = make_fuser(spacing_m, prefer=fuser, min_views=min_views, min_confidence=min_confidence,
                        min_depth_m=min_depth_m, max_depth_m=max_depth_m)
    low_sharp = np.zeros(n)
    for k, small in video.frames(rgb, size=(dw, dh)):
        if k >= n:
            break
        low_sharp[k] = images.sharpness(small)
        if k in fuse_set:
            depth, conf, cam_d = depth_frame(k)
            fusion.add_depth(depth, conf, small, poses[k], cam_d)

    saved_fps = 1.0 / float(np.median(np.diff([float(r["timestamp"]) for r in paired]))) if n > 1 else 1.0
    keys = select_keyframes(poses, low_sharp, travel_m=travel_m, angle_deg=angle_deg,
                            window=max(1, round(0.3 * saved_fps)))
    t0 = float(paired[0]["timestamp"])
    frames = []
    for k, full in video.frames(rgb, select=keys):
        frames.append(ImportedFrame(
            source_frame=str(paired[k]["frame"]), timestamp_s=float(paired[k]["timestamp"]) - t0, pose=poses[k],
            cam=cams[k], jpeg=images.jpeg_bytes(full), sharpness=images.sharpness(full),
            depth_png=depth_files[k].read_bytes() if depth_files[k] is not None and depth_files[k].suffix == ".png"
            else None,
            conf_png=conf_files[k].read_bytes() if conf_files[k] is not None else None))

    cloud = fusion.result()
    depth_source = "lidar" if len(cloud["xyz"]) else "none"
    comments = ("arkit_world metres Y up", f"fused by repairos.importers.fuse ({cloud['method']})")
    ply = (ply_bytes(cloud["xyz"], cloud["rgb"], confidence=cloud["confidence"], views=cloud["views"],
                     comments=comments) if depth_source == "lidar" else None)
    mesh = cloud["mesh"] if depth_source == "lidar" else None
    mesh_ply = mesh_ply_bytes(mesh["vertices"], mesh["faces"], mesh["rgb"], comments=comments) if mesh else None
    if meta.captured_at is not None:
        captured_at, captured_src = meta.captured_at, "operator"
    else:
        captured_at = datetime.fromtimestamp((src / "odometry.csv").stat().st_mtime, UTC)
        captured_src = "modification time of odometry.csv (written when the recording stopped)"
    dist = sorted((src / "distortion").glob("*.bin")) if (src / "distortion").is_dir() else []
    imu_rows = max(0, len((src / "imu.csv").read_text(encoding="utf-8").splitlines()) - 1) \
        if (src / "imu.csv").is_file() else 0
    provenance = {
        "source_app": "Stray Scanner", "source_url": "https://github.com/strayrobots/scanner",
        "source_folder": src.name, "importer": "repairos.importers.stray",
        "frames": {"odometry_rows": len(rows), "video_frames": len(info["pts"]), "video_offset": offset,
                   "paired": n, "with_depth": len(with_depth), "fused": len(fuse_set), "fuse_stride": stride,
                   "video_size": [width, height], "depth_size": [dw, dh]},
        "fusion": {**cloud["params"], "points": len(cloud["xyz"]), "spacing_source": spacing_rule,
                   "spacing_measurement": measured.to_dict() if measured else None},
        "keyframe_rule": {"travel_m": travel_m, "angle_deg": angle_deg, "sharpest_within_s": 0.3},
        "captured_at_source": captured_src,
        "assumptions": [
            "pose quaternion converted from OpenCV camera axes (Stray's q_WA * q_AC) to ARKit camera axes",
            "tracking state is not recorded by Stray Scanner: written as unknown",
            f"lens distortion: {len(dist)} lookup tables present, not applied (pinhole intrinsics as ARKit gives "
            "them)" if dist else "lens distortion: no lookup tables in the export",
            f"imu.csv: {imu_rows} samples, not used (poses come from ARKit)",
            "no supporting plane or crop box removal: the cloud is everything within the depth range",
        ],
    }
    if depth_source == "none":
        provenance["assumptions"].append("no usable high-confidence depth: no point cloud was written")
    return write_session(Path(out_root), meta=meta, captured_at=captured_at, frames=frames,
                         depth_source=depth_source, ply=ply, point_count=len(cloud["xyz"]) if ply else None,
                         voxel_mm=spacing_m * 1000.0 if ply else None, provenance=provenance, mesh_ply=mesh_ply)
