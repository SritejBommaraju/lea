"""Hand-held ("Turn it") scans: pose_source object_tracked puts points and poses in the object frame, which includes
the underside the person flipped into view, so the mesh and the cloud cut nothing at the support plane; and a still
whose tracked pose quality is low caps locate's confidence."""
from __future__ import annotations

import numpy as np

from repairos.locate.locate import OBJECT_POSE_MEDIUM, _pose_cap
from repairos.pipeline.mesh import object_cloud, plane_height
from repairos.recon.tsdf import ObjectBox

PLANE = {"transform": [1, 0, 0, 0, 0, 1, 0, 0.74, 0, 0, 1, -0.35, 0, 0, 0, 1], "extent_m": [0.8, 0.6]}
BOX = {"frame": "arkit_world", "center_m": [0.0, 0.77, -0.35], "extent_m": [0.08, 0.06, 0.08],
       "quaternion_xyzw": [0, 0, 0, 1]}


def test_plane_height_is_kept_for_world_scans():
    assert plane_height({"support_plane": PLANE}) == 0.74
    assert plane_height({"support_plane": PLANE, "pose_source": "arkit"}) == 0.74


def test_plane_height_is_ignored_for_object_tracked_scans():
    assert plane_height({"support_plane": PLANE, "pose_source": "object_tracked"}) is None


def _cube_ply(path, centre, side, n=31):
    """Points on all six faces of a cube (underside included), binary PLY via the project's writer."""
    from repairos.importers.ply import ply_bytes

    g = np.linspace(-side / 2, side / 2, n)
    a, b = np.meshgrid(g, g)
    a, b = a.ravel(), b.ravel()
    h = np.full_like(a, side / 2)
    faces = [np.c_[a, b, h], np.c_[a, b, -h], np.c_[a, h, b], np.c_[a, -h, b], np.c_[h, a, b], np.c_[-h, a, b]]
    p = np.vstack(faces) + np.asarray(centre)
    path.write_bytes(ply_bytes(p, np.full((len(p), 3), 200, np.uint8)))
    return p


def test_object_cloud_keeps_the_underside_when_the_plane_is_not_applied(tmp_path):
    # A 6 cm cube resting on the plane: its bottom face lies at the plane's height.
    pts = _cube_ply(tmp_path / "cube.ply", [0.0, 0.74 + 0.03, -0.35], 0.06)
    box = ObjectBox.from_contract(BOX)
    world, _ = object_cloud(tmp_path / "cube.ply", box, plane_height({"support_plane": PLANE}))
    held, _ = object_cloud(tmp_path / "cube.ply", box, plane_height({"support_plane": PLANE, "pose_source": "object_tracked"}))
    bottom = lambda p: int((np.abs(p[:, 1] - 0.74) < 0.002).sum())  # noqa: E731
    assert bottom(pts) > 100
    assert bottom(world) == 0          # a world scan: the table's height is cut
    assert bottom(held) >= bottom(pts)  # hand-held: the underside stays


def test_locate_caps_low_quality_tracked_poses_at_medium():
    still = {"pose_reliable": True, "tracking_state": "normal", "pose_source": "object_tracked"}
    assert _pose_cap({**still, "pose_quality": 0.9}) == ("high", None)
    word, why = _pose_cap({**still, "pose_quality": OBJECT_POSE_MEDIUM - 0.1})
    assert word == "medium" and "turned in the hand" in why
    # World-tracked stills are unaffected by the field.
    assert _pose_cap({"pose_reliable": True, "tracking_state": "normal", "pose_quality": 0.1}) == ("high", None)


def test_flipped_bottom_segments_are_not_placed_until_their_transform_is_applied():
    from repairos.pipeline.run import unaligned_segments

    scan = {"segments": [
        {"segment": 1, "reason": "continued"},
        {"segment": 2, "reason": "continued", "aligned": False},
        {"segment": 3, "reason": "flipped", "object_moved": True, "aligned": False},
        {"segment": 4, "reason": "flipped", "object_moved": True, "aligned": True,
         "transform_to_segment0": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], "transform_quality": 0.8},
    ]}
    assert unaligned_segments(scan) == {2, 3, 4}


SLAB = (np.array([-0.06, 0.0, -0.04]), np.array([0.06, 0.03, 0.04]))   # a flat 12 x 3 x 8 cm part, bottom on y = 0


def _slab_depth(pose, cam):
    """Analytic depth (along the view axis) of SLAB alone: the part turned in the hand, nothing behind it."""
    from repairos.locate.camera import pixel_rays

    u, v = np.meshgrid(np.arange(cam.width) + 0.5, np.arange(cam.height) + 0.5)
    o, d = pixel_rays(np.stack([u.ravel(), v.ravel()], 1), pose, cam)
    lo, hi = SLAB
    with np.errstate(divide="ignore", invalid="ignore"):
        t0, t1 = (lo - o) / d, (hi - o) / d
        tn, tf = np.minimum(t0, t1).max(1), np.maximum(t0, t1).min(1)
    t = np.where((tn <= tf) & (tn > 0), tn, np.inf)
    z = t * -(d @ pose.r[:, 2])
    z[~np.isfinite(z)] = 0
    return z.reshape(cam.height, cam.width)


def test_a_hand_held_scan_keeps_its_scanned_underside_in_the_recreation(tmp_path):
    """Turning the part in the hand puts views of its underside in the object frame. Its flat bottom then forms the
    lowest layer of depth in the box (ground_height takes it for the table), so a table cut would remove it: a
    hand-held scan skips the cut and the model stays closed underneath."""
    import pytest

    pytest.importorskip("open3d")
    from PIL import Image

    from repairos.locate.camera import Pinhole, Pose
    from repairos.recreation.surface import scan_surface

    cam = Pinhole(120.0, 120.0, 64.0, 48.0, 128, 96)
    Image.fromarray(np.full((96, 128, 3), 170, np.uint8)).save(tmp_path / "grey.png")
    Image.fromarray(np.full((96, 128), 2, np.uint8)).save(tmp_path / "conf.png")
    kfs = []
    for k in range(16):
        a, up = 2 * np.pi * k / 8, 1 if k < 8 else -1          # 8 views from above, 8 of the underside
        eye = np.array([0.0, 0.015, 0.0]) + [0.2 * np.cos(a), up * 0.2, 0.2 * np.sin(a)]
        back = eye - [0.0, 0.015, 0.0]
        back /= np.linalg.norm(back)
        right = np.cross([0.0, 1.0, 0.0], back)
        right /= np.linalg.norm(right)
        pose = Pose(np.stack([right, np.cross(back, right), back], axis=1), eye)
        Image.fromarray((_slab_depth(pose, cam) * 1000).astype(np.uint16)).save(tmp_path / f"d{k}.png")
        kfs.append({"keyframe_id": f"kf{k}", "image_file": str(tmp_path / "grey.png"),
                    "depth_file": str(tmp_path / f"d{k}.png"), "confidence_file": str(tmp_path / "conf.png"),
                    "depth_encoding": "uint16_mm", "camera": pose.to_contract(), "pose_source": "object_tracked",
                    "intrinsics": {"fx": cam.fx, "fy": cam.fy, "cx": cam.cx, "cy": cam.cy, "width_px": 128,
                                   "height_px": 96}, "timestamp_s": float(k), "tracking_state": "normal"})
    pkg = {"scan_id": "scan_held", "frame": "arkit_world", "pose_source": "object_tracked", "keyframes": kfs,
           "crop_box": {"frame": "arkit_world", "center_m": [0.0, 0.015, 0.0], "extent_m": [0.13, 0.035, 0.09],
                        "quaternion_xyzw": [0, 0, 0, 1]},
           "support_plane": {"transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], "extent_m": [1, 1]}}
    s = scan_surface(pkg, smooth_iters=0)
    assert s.ground_y is not None and abs(s.ground_y) < 0.004     # the underside is the lowest layer
    assert s.params["table_cut_mm"] is None
    tri = s.vertices[s.faces]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    c = tri.mean(1)
    under = (n[:, 1] < -0.7) & (np.abs(c[:, 1]) < 0.006)
    assert under.sum() > 1000                                     # the bottom face is there (a cut leaves ~100)
    assert np.ptp(c[under, 0]) > 0.09 and np.ptp(c[under, 2]) > 0.05   # across the whole underside
