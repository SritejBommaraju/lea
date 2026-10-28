"""Capture importers (B12) on synthetic geometry and format text: odometry parsing, video/pose pairing, keyframe
choice, measured spacing, depth fusion (numpy and Open3D), and the session they write.

Depth maps here are rendered by ray casting an analytic scene (a floor and an asymmetric box): geometry, not images.
The one image used is a flat grey JPEG as a format fixture for the session layout (like tests/replay/conftest.py's
format_session); it is not a capture and never goes into samples/. Real exports: tests/replay/test_imports.py.
"""
from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction

import numpy as np
import pytest

from repairos.importers.fuse import Open3DFuser, VoxelFuser, make_fuser, measure_spacing, open3d_available
from repairos.importers.images import ImportFormatError, jpeg_bytes, sharpness
from repairos.importers.keyframes import select_keyframes
from repairos.importers.nerfcapture import nerfcapture_pose
from repairos.importers.ply import mesh_ply_bytes, ply_bytes, read_ply
from repairos.importers.session import CaptureMeta, ImportedFrame, write_session
from repairos.importers.stray import align_video, read_camera_matrix, read_odometry, video_ticks
from repairos.importers.camera import Pinhole, Pose, pixel_rays
from repairos.sessions import files
from repairos.sessions.reader import open_session

BOX_MIN = np.array([0.03, 0.0, -0.62])
BOX_MAX = np.array([0.17, 0.06, -0.53])
FLOOR = (np.array([-0.3, -0.9]), np.array([0.4, -0.3]))     # x, z extents of the y = 0 floor
CAM = Pinhole(1450.0, 1450.0, 960.0, 720.0, 1920, 1440)
DEPTH_CAM = CAM.scaled(256, 192)


def look_at(eye, target) -> Pose:
    eye, target = np.asarray(eye, float), np.asarray(target, float)
    back = (eye - target) / np.linalg.norm(eye - target)
    right = np.cross([0.0, 1.0, 0.0], back)
    right /= np.linalg.norm(right)
    return Pose(np.stack([right, np.cross(back, right), back], axis=1), eye)


def render_depth(pose: Pose, cam: Pinhole) -> np.ndarray:
    """View-axis depth (metres; 0 = nothing) of the floor + box scene, by exact ray intersection."""
    uu, vv = np.meshgrid(np.arange(cam.width) + 0.5, np.arange(cam.height) + 0.5)
    o, d = pixel_rays(np.stack([uu.ravel(), vv.ravel()], 1), pose, cam)
    t = np.full(len(d), np.inf)
    with np.errstate(divide="ignore", invalid="ignore"):
        tf = -o[1] / d[:, 1]
        p = o + tf[:, None] * d
        on = (tf > 0) & (p[:, 0] >= FLOOR[0][0]) & (p[:, 0] <= FLOOR[1][0]) & (p[:, 2] >= FLOOR[0][1]) & \
             (p[:, 2] <= FLOOR[1][1])
        t[on] = tf[on]
        t1, t2 = (BOX_MIN - o) / d, (BOX_MAX - o) / d
        tn, tx = np.minimum(t1, t2).max(axis=1), np.maximum(t1, t2).min(axis=1)
    hit = (tx >= tn) & (tn > 0)
    t[hit] = np.minimum(t[hit], tn[hit])
    pts = o + np.where(np.isfinite(t), t, 0)[:, None] * d
    depth = -pose.to_camera(pts)[:, 2]
    depth[~np.isfinite(t)] = 0.0
    return depth.reshape(cam.height, cam.width)


def surface_distance(p: np.ndarray) -> np.ndarray:
    """Distance of points to the scene surface (floor or box faces)."""
    q = np.maximum(BOX_MIN - p, 0) + np.maximum(p - BOX_MAX, 0)
    outside = np.linalg.norm(q, axis=1)
    inside = np.min(np.minimum(p - BOX_MIN, BOX_MAX - p), axis=1)
    box = np.where(outside > 0, outside, inside)
    return np.minimum(box, np.abs(p[:, 1]))


POSES = [look_at([0.02 + 0.08 * i, 0.40 - 0.02 * i, -0.22 - 0.03 * i], [0.10, 0.03, -0.575]) for i in range(4)]


# --- Stray Scanner text formats ---------------------------------------------------------------------------------

ODOMETRY = """timestamp, frame, x, y, z, qx, qy, qz, qw, fx, fy, cx, cy, distortion_center_x, distortion_center_y
4012.50, 000000, 0.01, 0.02, -0.03, 1.0, 0.0, 0.0, 0.0, 1450.1, 1450.2, 960.5, 720.5, ,
4012.5166, 000001, 0.011, 0.021, -0.031, 0.9999, 0.01, 0.0, 0.0, 1450.1, 1450.2, 960.5, 720.5, 2010.0, 1500.0
"""


def test_odometry_csv_current_and_old_headers():
    rows = read_odometry(ODOMETRY)
    assert [r["frame"] for r in rows] == ["000000", "000001"]
    assert rows[1]["fx"] == 1450.1 and rows[0]["qx"] == 1.0 and rows[1]["timestamp"] == pytest.approx(4012.5166)
    old = read_odometry("timestamp, frame, x, y, z, qx, qy, qz, qw\n1.0, 000000, 0, 0, 0, 1, 0, 0, 0\n")
    assert "fx" not in old[0]
    with pytest.raises(ImportFormatError):
        read_odometry("timestamp, x, y\n1, 2, 3\n")
    with pytest.raises(ImportFormatError):
        read_odometry("timestamp, frame, x, y, z, qx, qy, qz, qw\n1, 0, a, 0, 0, 1, 0, 0, 0\n")
    assert read_camera_matrix("1450.0, 0.0, 960.0\n0.0, 1451.0, 720.0\n0.0, 0.0, 1.0\n") == (1450.0, 1451.0, 960.0,
                                                                                           720.0)


def test_video_frames_pair_with_odometry_rows():
    ts = [10.0 + i / 60 for i in range(6)]
    ticks = video_ticks([-1, 0, 1, 2, 3, 4], Fraction(1, 60))
    assert align_video(ticks, ts) == 0
    assert align_video([-1, 0, 1, 2, 3], ts) == 0          # the first frame is in the file: the last one is missing
    assert align_video([0, 1, 2, 3, 4], ts) == 1           # the -1/60 s frame was not written: shift by one
    # fps divider 4 with one dropped frame: the gaps agree in both files
    ts4 = [0.0, 4 / 60, 8 / 60, 16 / 60, 20 / 60]
    assert align_video([-1, 3, 7, 15, 19], ts4) == 0
    with pytest.raises(ImportFormatError, match="cannot be paired"):
        align_video([-1, 0, 1], ts)
    with pytest.raises(ImportFormatError, match="mis-paired"):
        align_video([-1, 3, 7, 11, 15, 23, 27, 31], [0, 4 / 60, 8 / 60, 16 / 60, 20 / 60, 24 / 60, 28 / 60, 32 / 60])
    assert video_ticks([0, 1000, 2000], Fraction(1, 60000)) == [0, 1, 2]


def test_keyframes_follow_travel_and_rotation_and_pick_the_sharpest():
    poses = [Pose(np.eye(3), np.array([0.01 * i, 0, 0])) for i in range(40)]   # 1 cm per frame
    sharp = np.ones(40)
    sharp[11] = 5.0
    keys = select_keyframes(poses, sharp, travel_m=0.10, angle_deg=15, window=3)
    assert keys[0] == 0 and keys[1] == 11                  # frame 10 crosses 10 cm; 11 is sharper in the window
    assert all(b - a >= 9 for a, b in zip(keys, keys[1:], strict=False))
    turning = [look_at([0, 0, 0], [np.sin(np.radians(a)), 0, -np.cos(np.radians(a))]) for a in range(0, 61, 5)]
    assert select_keyframes(turning, np.ones(len(turning)), travel_m=1.0, angle_deg=15) == [0, 3, 6, 9, 12]


def test_sharpness_ranks_detail_over_flat():
    rng = np.random.default_rng(0)
    flat = np.full((32, 32, 3), 120, np.uint8)
    noisy = rng.integers(0, 255, (32, 32, 3), dtype=np.uint8)
    assert sharpness(flat) == 0.0 < sharpness(noisy)


# --- spacing and fusion ------------------------------------------------------------------------------------------

def test_spacing_is_measured_from_the_depth():
    frames = [(render_depth(p, DEPTH_CAM), None, DEPTH_CAM) for p in POSES]
    sp = measure_spacing(frames)
    median_depth = np.median([np.median(d[d > 0]) for d, _, _ in frames])
    assert sp.spacing_m == pytest.approx(median_depth / DEPTH_CAM.fx)
    assert 0.001 <= sp.spacing_m <= 0.004 and sp.frames == len(POSES)
    assert measure_spacing([(np.zeros((4, 4)), None, Pinhole(1, 1, 2, 2, 4, 4))]) is None
    far = measure_spacing([(np.full((4, 4), 2.9), None, Pinhole(10, 10, 2, 2, 4, 4))])
    assert far.spacing_m == 0.02                           # clamped


def _fuse(fuser):
    for p in POSES:
        depth = render_depth(p, DEPTH_CAM)
        conf = np.where(depth > 0, 2, 0).astype(np.uint8)
        conf[::7, ::5] = 1                                  # medium confidence pixels are left out
        fuser.add_depth(depth, conf, None, p, DEPTH_CAM)
    return fuser.result()


def test_numpy_fusion_puts_points_on_the_surface():
    res = _fuse(VoxelFuser(0.003, min_views=2))
    assert res["method"] == "numpy_voxel_average" and res["mesh"] is None
    xyz = res["xyz"]
    assert len(xyz) > 1000
    assert np.percentile(surface_distance(xyz), 99) < 0.003
    assert (res["views"] >= 2).all() and (res["confidence"] == 2).all()
    on_box = np.all((xyz > BOX_MIN - 0.003) & (xyz < BOX_MAX + 0.003), axis=1)
    assert on_box.sum() > 100 and np.abs(xyz[:, 1]).min() < 0.002


def test_low_confidence_and_out_of_range_depth_is_dropped():
    f = VoxelFuser(0.003, min_views=1, max_depth_m=0.3)
    depth = render_depth(POSES[0], DEPTH_CAM)
    assert f.add_depth(depth, np.ones_like(depth, np.uint8), None, POSES[0], DEPTH_CAM) == 0
    assert f.add_depth(depth, None, None, POSES[0], DEPTH_CAM) == 0      # everything is beyond 0.3 m
    with pytest.raises(ValueError):
        f.add_depth(depth, None, None, POSES[0], CAM)


@pytest.mark.skipif(not open3d_available(), reason="Open3D not installed (the geometry extra)")
def test_open3d_tsdf_fusion_gives_a_clean_cloud_and_a_mesh(tmp_path):
    import open3d as o3d

    res = _fuse(Open3DFuser(0.003))
    assert res["method"] == "open3d_voxel_block_grid_tsdf"
    xyz = res["xyz"]
    assert len(xyz) > 1000 and np.percentile(surface_distance(xyz), 99) < 0.004
    p = res["params"]
    assert p["spacing_mm"] == 3.0 and p["points_after_downsample"] == len(xyz) <= p["points_extracted"]
    mesh = res["mesh"]
    assert mesh is not None and len(mesh["faces"]) > 100
    assert np.percentile(surface_distance(mesh["vertices"]), 99) < 0.004
    path = tmp_path / "mesh.ply"
    path.write_bytes(mesh_ply_bytes(mesh["vertices"], mesh["faces"], mesh["rgb"]))
    with path.open("rb") as fh:
        files.ply_header(fh, path.stat().st_size)
    back = o3d.io.read_triangle_mesh(str(path))
    assert len(back.triangles) == len(mesh["faces"])
    assert isinstance(make_fuser(0.003), Open3DFuser) and isinstance(make_fuser(0.003, prefer="numpy"), VoxelFuser)


def test_ply_bytes_round_trip(tmp_path):
    xyz = np.array([[0.1, 0.2, -0.3], [0.4, -0.5, 0.6]])
    path = tmp_path / "p.ply"
    path.write_bytes(ply_bytes(xyz, [[1, 2, 3], [4, 5, 6]], confidence=[2, 1], views=[3, 300]))
    v = read_ply(path)
    assert np.allclose(np.stack([v["x"], v["y"], v["z"]], 1), xyz)
    assert list(v["views"]) == [3, 255] and list(v["red"]) == [1, 4]


def test_nerfcapture_rejects_a_non_rigid_transform():
    with pytest.raises(ImportFormatError):
        nerfcapture_pose({"transform_matrix": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 1, 1]]})


# --- the session the importers write ----------------------------------------------------------------------------

def _frames(with_depth: bool):
    grey = jpeg_bytes(np.full((CAM.height, CAM.width, 3), 128, np.uint8))     # format fixture, not a capture
    png16 = None
    if with_depth:
        import io

        from PIL import Image
        buf = io.BytesIO()
        Image.fromarray(np.full((192, 256), 500, np.uint16)).save(buf, "PNG")
        png16 = buf.getvalue()
    return [ImportedFrame(source_frame=f"{i:06d}", timestamp_s=0.5 * i, pose=p, cam=CAM, jpeg=grey, sharpness=1.0,
                          depth_png=png16, conf_png=png16) for i, p in enumerate(POSES)]


def test_lidar_import_session_validates(tmp_path):
    res_f = _fuse(VoxelFuser(0.003, min_views=2))
    ply = ply_bytes(res_f["xyz"], np.full((len(res_f["xyz"]), 3), 128, np.uint8))
    res = write_session(tmp_path, meta=CaptureMeta(device_model="iPhone15,2", ios="18.1"),
                        captured_at=datetime(2026, 10, 24, 18, 0, tzinfo=UTC), frames=_frames(True),
                        depth_source="lidar", ply=ply, point_count=len(res_f["xyz"]), voxel_mm=3.0,
                        provenance={"source_app": "test geometry"})
    assert res.problems == [], res.problems
    s = open_session(res.path)
    pkg, stills = s.scans()[0], s.stills()
    assert pkg["depth_source"] == "lidar" and pkg["point_count"] == len(res_f["xyz"])
    assert len(stills) == len(POSES) and all(st["kind"] == "unguided" and st["targets"] == [] for st in stills)
    assert stills[0]["tracking_state"] == "unknown" and "pose_reliable" not in stills[0]
    assert s.meta["source"] == "converter" and (res.path / "import.json").is_file()


def test_no_lidar_import_session_has_no_ply_and_validates(tmp_path):
    res = write_session(tmp_path, meta=CaptureMeta(), captured_at=datetime(2026, 10, 24, 18, 0, tzinfo=UTC),
                        frames=_frames(False), depth_source="none", ply=None, point_count=None, voxel_mm=None,
                        provenance={"source_app": "test geometry"})
    assert res.problems == [], res.problems
    pkg = open_session(res.path).scans()[0]
    assert pkg["depth_source"] == "none" and "points_file" not in pkg and "point_count" not in pkg
    assert not list(res.path.glob("scans/*/*.ply"))
    with pytest.raises(ValueError):
        write_session(tmp_path, meta=CaptureMeta(), captured_at=datetime.now(UTC), frames=_frames(False),
                      depth_source="none", ply=b"ply\n", point_count=1, voxel_mm=3, provenance={})
