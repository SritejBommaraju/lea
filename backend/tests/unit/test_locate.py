"""repairos.locate on synthetic geometry (math only: sampled planes and boxes, no images).

Scene, arkit_world metres, Y up: a floor (y = 0) and an asymmetric box (14 x 6 x 9 cm) standing on it off-centre. A
camera above and in front looks down at it, yawed and rolled, like a close-up.
"""
from __future__ import annotations

import numpy as np
import pytest

from repairos.importers.ply import mesh_ply_bytes, ply_bytes
from repairos.locate import Location, NotLocated, ScanCloud, locate, triangulate, try_locate
from repairos.locate.camera import Pinhole, Pose, project
from repairos.sessions import files

BOX_MIN = np.array([0.03, 0.0, -0.62])
BOX_MAX = np.array([0.17, 0.06, -0.53])
CAM = Pinhole(1450.0, 1450.0, 960.0, 720.0, 1920, 1440)


def rot(axis: str, deg: float) -> np.ndarray:
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return {"x": np.array([[1, 0, 0], [0, c, -s], [0, s, c]]), "y": np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]]),
            "z": np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])}[axis]


def look_at(eye, target, roll_deg=0.0) -> Pose:
    """Camera-to-world pose (ARKit camera axes) at `eye` looking at `target`, world Y up, then rolled."""
    eye, target = np.asarray(eye, float), np.asarray(target, float)
    back = eye - target
    back /= np.linalg.norm(back)                    # camera +z points away from the target
    right = np.cross([0.0, 1.0, 0.0], back)
    right /= np.linalg.norm(right)
    up = np.cross(back, right)
    return Pose(np.stack([right, up, back], axis=1) @ rot("z", roll_deg), eye)


def grid(lo, hi, step):
    axes = [np.arange(a, b + step / 2, step) if b > a else np.array([a]) for a, b in zip(lo, hi, strict=True)]
    return np.stack(np.meshgrid(*axes, indexing="ij"), -1).reshape(-1, 3)


def box_surface(lo, hi, step=0.002):
    lo, hi = np.asarray(lo), np.asarray(hi)
    faces = []
    for ax in range(3):
        for v in (lo[ax], hi[ax]):
            a, b = lo.copy(), hi.copy()
            a[ax] = b[ax] = v
            faces.append(grid(a, b, step))
    return np.unique(np.concatenate(faces).round(6), axis=0)


def scene_points():
    floor = grid([-0.3, 0.0, -0.9], [0.4, 0.0, -0.3], 0.003)
    inside = np.all((floor[:, [0, 2]] > BOX_MIN[[0, 2]]) & (floor[:, [0, 2]] < BOX_MAX[[0, 2]]), axis=1)
    return np.concatenate([floor[~inside], box_surface(BOX_MIN, BOX_MAX)])


POSE = look_at([0.02, 0.38, -0.25], [0.10, 0.06, -0.575], roll_deg=9.0)
TOP_CENTRE = np.array([(BOX_MIN[0] + BOX_MAX[0]) / 2, BOX_MAX[1], (BOX_MIN[2] + BOX_MAX[2]) / 2])


def still(pose=POSE, **kw) -> dict:
    s = {"still_id": "s4", "scan_id": "scan_00c0ffee", "session_id": "sess_x", "kind": "guided",
         "width_px": CAM.width, "height_px": CAM.height, "camera": pose.to_contract(),
         "intrinsics": {"fx": CAM.fx, "fy": CAM.fy, "cx": CAM.cx, "cy": CAM.cy}, "tracking_state": "normal",
         "targets": []}
    s.update(kw)
    return s


def box_around(points, pose=POSE, pad=0.0):
    uv, depth = project(points, pose, CAM)
    assert (depth > 0).all()
    lo, hi = uv.min(axis=0), uv.max(axis=0)
    return (lo[0] - pad, lo[1] - pad, hi[0] + pad, hi[1] + pad)


def top_face_patch(half=0.02):
    c = TOP_CENTRE
    return np.array([[c[0] - half, c[1], c[2] - half], [c[0] + half, c[1], c[2] + half],
                     [c[0] - half, c[1], c[2] + half], [c[0] + half, c[1], c[2] - half]])


@pytest.fixture(scope="module")
def cloud():
    return ScanCloud(scene_points(), "scan_00c0ffee")


def test_locates_a_box_on_the_part_not_the_floor_behind(cloud):
    loc = locate(cloud, still(), box_around(top_face_patch()))
    assert isinstance(loc, Location)
    assert loc.method == "ray_cast_point_cloud" and loc.frame == "arkit_world"
    assert np.linalg.norm(np.array(loc.point_m) - TOP_CENTRE) < 0.006
    assert loc.region_min_m[1] > 0.05                          # the region is on the top face, not the floor
    assert loc.rays_hit == loc.rays_cast == 9 and loc.confidence == "high"
    assert loc.distance_m == pytest.approx(np.linalg.norm(TOP_CENTRE - POSE.t), abs=0.01)
    lo, hi = np.array(loc.region_min_m), np.array(loc.region_max_m)
    assert (hi - lo)[0] > 0.02 and (hi - lo)[2] > 0.02       # spans the patch, not a single point
    d = loc.to_dict()
    assert d["point_m"] == list(loc.point_m) and d["still_ids"] == ["s4"]


def test_floor_beside_the_part_is_located_on_the_floor(cloud):
    patch = np.array([[-0.15, 0.0, -0.70], [-0.11, 0.0, -0.66], [-0.15, 0.0, -0.66], [-0.11, 0.0, -0.70]])
    loc = locate(cloud, still(), box_around(patch))
    assert loc is not None and abs(loc.point_m[1]) < 0.004
    assert np.linalg.norm(np.array(loc.point_m)[[0, 2]] - [-0.13, -0.68]) < 0.006


def test_box_on_an_edge_spans_depths_and_drops_confidence(cloud):
    # half on the top face, half on the floor far behind the box's back edge
    pts = np.array([[0.08, 0.06, -0.60], [0.12, 0.06, -0.60], [0.08, 0.0, -0.75], [0.12, 0.0, -0.75]])
    loc = locate(cloud, still(), box_around(pts))
    assert loc is not None and loc.confidence != "high"
    assert any("spans surfaces" in r for r in loc.reasons)


def test_box_on_empty_space_is_not_located_with_a_reason(cloud):
    sky = look_at([0.0, 0.3, 0.0], [0.0, 1.5, -1.0])
    res = try_locate(cloud, still(pose=sky), (900, 650, 1020, 790))
    assert isinstance(res, NotLocated) and "no scan surface" in res.reason
    assert locate(cloud, still(pose=sky), (900, 650, 1020, 790)) is None


def test_no_lidar_scan_returns_none_and_says_triangulate():
    pkg = {"scan_id": "scan_00c0ffee", "depth_source": "none", "keyframes": []}
    res = try_locate(pkg, still(), (900, 650, 1020, 790))
    assert isinstance(res, NotLocated) and "depth_source none" in res.reason and "triangulate" in res.reason
    assert locate(pkg, still(), (900, 650, 1020, 790)) is None


@pytest.mark.parametrize("extra, word", [({"tracking_state": "unknown"}, "medium"),
                                         ({"tracking_state": "limited"}, "low"),
                                         ({"pose_reliable": False}, "low")])
def test_pose_quality_caps_the_confidence_word(cloud, extra, word):
    loc = locate(cloud, still(**extra), box_around(top_face_patch()))
    assert loc is not None and loc.confidence == word and loc.reasons


@pytest.mark.parametrize("extra, why", [({"kind": "macro_detail"}, "macro_detail"),
                                        ({"scan_id": "scan_deadbeef"}, "registered to scan"),
                                        ({"user_skipped": True}, "skipped")])
def test_unusable_stills_are_refused(cloud, extra, why):
    res = try_locate(cloud, still(**extra), box_around(top_face_patch()))
    assert isinstance(res, NotLocated) and why in res.reason


def test_box_outside_the_still_and_empty_box():
    c = ScanCloud(scene_points(), "scan_00c0ffee")
    assert "outside the still" in try_locate(c, still(), (2000, 1500, 2100, 1600)).reason
    with pytest.raises(ValueError):
        try_locate(c, still(), (10, 10, 10, 20))


def test_from_a_scan_package_and_session_folder(tmp_path):
    pts = scene_points()
    (tmp_path / "scans" / "scan_00c0ffee").mkdir(parents=True)
    rel = "scans/scan_00c0ffee/points_arkit_world.ply"
    (tmp_path / rel).write_bytes(ply_bytes(pts, np.full((len(pts), 3), 128, np.uint8)))
    with (tmp_path / rel).open("rb") as f:
        assert files.ply_header(f, (tmp_path / rel).stat().st_size)["vertex_count"] == len(pts)
    pkg = {"scan_id": "scan_00c0ffee", "points_file": rel, "point_count": len(pts), "voxel_mm": 3}
    loc = locate(pkg, still(), box_around(top_face_patch()), base_dir=tmp_path)
    assert loc is not None and np.linalg.norm(np.array(loc.point_m) - TOP_CENTRE) < 0.006


def test_triangulation_across_two_stills_without_a_cloud():
    target = np.array([0.083, 0.047, -0.571])
    other = look_at([0.31, 0.29, -0.41], target, roll_deg=-6.0)
    a = still(still_id="s1", tracking_state="normal")
    b = still(pose=other, still_id="s2")
    patch = target + np.array([[-0.01, 0, -0.01], [0.01, 0, 0.01], [-0.01, 0, 0.01], [0.01, 0, -0.01]])
    loc = triangulate([(a, box_around(patch)), (b, box_around(patch, pose=other))])
    assert isinstance(loc, Location) and loc.method == "triangulation"
    assert np.linalg.norm(np.array(loc.point_m) - target) < 0.004
    assert loc.still_ids == ("s1", "s2") and loc.confidence in ("medium", "high")


def test_triangulation_refuses_a_tiny_baseline_and_disagreeing_boxes():
    target = np.array([0.083, 0.047, -0.571])
    near = Pose(POSE.r, POSE.t + [0.003, 0, 0])
    patch = target + np.array([[-0.01, 0, -0.01], [0.01, 0, 0.01]])
    res = triangulate([(still(still_id="s1"), box_around(patch)), (still(pose=near, still_id="s2"),
                                                                    box_around(patch, pose=near))])
    assert isinstance(res, NotLocated) and "same direction" in res.reason
    other = look_at([0.31, 0.29, -0.41], target)
    elsewhere = np.array([[-0.2, 0.0, -0.8], [-0.18, 0.0, -0.78]])
    res = triangulate([(still(still_id="s1"), box_around(patch)), (still(pose=other, still_id="s2"),
                                                                    box_around(elsewhere, pose=other))])
    assert isinstance(res, NotLocated)
    assert isinstance(triangulate([(still(), box_around(patch))]), NotLocated)


# --- the mesh path (Open3D) -------------------------------------------------------------------------------------

def _box_mesh(lo, hi):
    o3d = pytest.importorskip("open3d")
    m = o3d.geometry.TriangleMesh.create_box(*(np.asarray(hi) - np.asarray(lo)))
    m.translate(lo)
    floor = o3d.geometry.TriangleMesh.create_box(0.7, 0.001, 0.6)
    floor.translate([-0.3, -0.001, -0.9])
    m += floor
    return np.asarray(m.vertices), np.asarray(m.triangles)


def test_mesh_ray_cast_gives_the_point_and_the_normal(tmp_path):
    v, f = _box_mesh(BOX_MIN, BOX_MAX)
    (tmp_path / "mesh.ply").write_bytes(mesh_ply_bytes(v, f))
    with (tmp_path / "mesh.ply").open("rb") as fh:
        files.ply_header(fh, (tmp_path / "mesh.ply").stat().st_size)
    cloud = ScanCloud(scene_points(), "scan_00c0ffee", mesh_file=tmp_path / "mesh.ply")
    loc = locate(cloud, still(), box_around(top_face_patch()))
    assert loc is not None and loc.method == "ray_cast_mesh" and loc.confidence == "high"
    assert np.linalg.norm(np.array(loc.point_m) - TOP_CENTRE) < 0.001
    assert np.allclose(loc.normal, [0, 1, 0], atol=1e-6)        # the top face, facing up at the camera
    assert loc.rays_hit == 9
    # the front face (+z side, facing the camera) has a +z normal
    front = np.array([[0.08, 0.02, BOX_MAX[2]], [0.12, 0.04, BOX_MAX[2]]])
    loc = locate(cloud, still(), box_around(front))
    assert loc is not None and np.allclose(loc.normal, [0, 0, 1], atol=1e-6)
    assert abs(loc.point_m[2] - BOX_MAX[2]) < 1e-4
    # forcing the points path still works on the same scan
    pts = locate(cloud, still(), box_around(top_face_patch()), method="points")
    assert pts is not None and pts.method == "ray_cast_point_cloud"


def test_mesh_method_without_a_mesh_says_so():
    cloud = ScanCloud(scene_points(), "scan_00c0ffee")
    res = try_locate(cloud, still(), box_around(top_face_patch()), method="mesh")
    assert isinstance(res, NotLocated) and "no mesh" in res.reason
