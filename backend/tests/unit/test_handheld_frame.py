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
