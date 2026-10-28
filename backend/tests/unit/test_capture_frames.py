"""Frame conventions of the capture pipeline (repairos.importers.camera): pure geometry on points.

The scene is deliberately asymmetric (distinct x, y, z offsets, a camera that is yawed, pitched AND rolled), so a
swapped axis, a missing flip, a transposed matrix or a wrong sign moves the projected pixels and fails a test.
"""
from __future__ import annotations

import numpy as np
import pytest

from repairos.importers.nerfcapture import nerfcapture_pose
from repairos.importers.stray import stray_pose
from repairos.importers.camera import (FLIP_YZ, Pinhole, Pose, backproject, matrix_to_quat, opencv_to_arkit_camera,
                                    pixel_rays, project, quat_to_matrix)

CAM = Pinhole(1450.0, 1452.0, 961.3, 718.7, 1920, 1440)
POINTS = np.array([[0.11, 0.42, -0.83], [-0.07, 0.31, -0.95], [0.02, 0.55, -1.12], [0.19, 0.38, -0.71]])


def rot(axis: str, deg: float) -> np.ndarray:
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return {"x": np.array([[1, 0, 0], [0, c, -s], [0, s, c]]), "y": np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]]),
            "z": np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])}[axis]


def hamilton(a, b):
    """Quaternion product a * b, [x, y, z, w] (simd_quatf's `*`)."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz]


# A camera 0.6 m up, looking down at the points: yawed 12 deg, pitched -25 deg, rolled 7 deg (ARKit camera axes).
POSE = Pose(rot("y", 12) @ rot("x", -25) @ rot("z", 7), np.array([0.05, 0.75, -0.12]))


def test_quaternion_round_trip_on_many_rotations():
    rng = np.random.default_rng(3)
    for _ in range(200):
        q = rng.normal(size=4)
        q /= np.linalg.norm(q)
        q2 = np.array(matrix_to_quat(quat_to_matrix(q)))
        assert np.allclose(q2, q) or np.allclose(q2, -q)
        r = quat_to_matrix(q)
        assert np.allclose(r @ r.T, np.eye(3)) and np.isclose(np.linalg.det(r), 1.0)


def test_arkit_camera_axes_x_right_y_up_looking_down_minus_z():
    ident = Pose(np.eye(3), np.zeros(3))
    uv, depth = project(np.array([[0.0, 0.0, -2.0], [0.3, 0.0, -2.0], [0.0, 0.3, -2.0]]), ident, CAM)
    assert np.allclose(uv[0], [CAM.cx, CAM.cy]) and depth[0] == pytest.approx(2.0)
    assert uv[1, 0] > CAM.cx and np.isclose(uv[1, 1], CAM.cy)       # +x is to the right
    assert uv[2, 1] < CAM.cy and np.isclose(uv[2, 0], CAM.cx)       # +y (world up) is higher in the image
    _, behind = project(np.array([[0.0, 0.0, 1.0]]), ident, CAM)
    assert behind[0] < 0                                             # +z is behind the camera


def test_pose_is_camera_to_world_and_rays_invert_projection():
    uv, depth = project(POINTS, POSE, CAM)
    assert (depth > 0).all()
    origin, dirs = pixel_rays(uv, POSE, CAM)
    assert np.allclose(origin, POSE.t)
    # each point lies on its pixel's ray
    rel = POINTS - origin
    t = np.sum(rel * dirs, axis=1)
    assert np.allclose(origin + t[:, None] * dirs, POINTS, atol=1e-9)
    # the camera centre is t, and the optical axis is -z of R
    assert np.allclose(POSE.to_world(np.zeros(3)), POSE.t)
    assert np.allclose(POSE.to_camera(POSE.to_world(POINTS)), POINTS)


def test_contract_pose_round_trip():
    back = Pose.from_contract(POSE.to_contract())
    assert np.allclose(back.r, POSE.r) and np.allclose(back.t, POSE.t)
    with pytest.raises(ValueError):
        Pose.from_contract({**POSE.to_contract(), "frame": "design"})


def test_stray_quaternion_is_opencv_camera_axes_and_converts_back():
    # Stray writes q = q_WA * q_AC, q_AC = (x 1, y 0, z 0, w 0): reproduce the Swift arithmetic exactly.
    q_wa = matrix_to_quat(POSE.r)
    q = hamilton(q_wa, [1.0, 0.0, 0.0, 0.0])
    r_cv = quat_to_matrix(q)
    assert np.allclose(r_cv, POSE.r @ FLIP_YZ)
    row = {"x": POSE.t[0], "y": POSE.t[1], "z": POSE.t[2], "qx": q[0], "qy": q[1], "qz": q[2], "qw": q[3]}
    got = stray_pose(row)
    assert np.allclose(got.r, POSE.r) and np.allclose(got.t, POSE.t)
    # the OpenCV pinhole with Stray's raw pose sees the same pixels as our ARKit model
    p_cv = (POINTS - POSE.t) @ r_cv
    uv_cv = np.stack([CAM.fx * p_cv[:, 0] / p_cv[:, 2] + CAM.cx, CAM.fy * p_cv[:, 1] / p_cv[:, 2] + CAM.cy], 1)
    assert (p_cv[:, 2] > 0).all()
    assert np.allclose(uv_cv, project(POINTS, got, CAM)[0])
    # forgetting the flip would put the camera looking the other way
    wrong = Pose(r_cv, POSE.t)
    assert (project(POINTS, wrong, CAM)[1] < 0).all()
    assert np.allclose(opencv_to_arkit_camera(r_cv), POSE.r)


def test_nerfcapture_transform_matrix_is_row_major_camera_to_world():
    m = np.eye(4)
    m[:3, :3], m[:3, 3] = POSE.r, POSE.t
    frame = {"transform_matrix": m.tolist()}       # Utils.arrayFromTransform writes rows
    got = nerfcapture_pose(frame)
    assert np.allclose(project(POINTS, got, CAM)[0], project(POINTS, POSE, CAM)[0])
    # reading it column-major would be a different camera: the asymmetric scene notices
    mt = m.T.copy()
    mt[3] = [0, 0, 0, 1]
    mt[:3, 3] = POSE.t
    other = Pose.from_matrix(mt)
    assert not np.allclose(project(POINTS, other, CAM)[0], project(POINTS, POSE, CAM)[0], atol=1.0)


def test_scaled_intrinsics_keep_the_field_of_view():
    small = CAM.scaled(256, 192)
    uv_big, _ = project(POINTS, POSE, CAM)
    uv_small, _ = project(POINTS, POSE, small)
    assert np.allclose(uv_small, uv_big * [256 / 1920, 192 / 1440])


def test_backproject_inverts_project_on_a_depth_map():
    small = CAM.scaled(32, 24)
    # depth of a tilted plane through the asymmetric points, rendered by ray casting (geometry, no images)
    n = np.cross(POINTS[1] - POINTS[0], POINTS[2] - POINTS[0])
    n /= np.linalg.norm(n)
    uu, vv = np.meshgrid(np.arange(32) + 0.5, np.arange(24) + 0.5)
    origin, dirs = pixel_rays(np.stack([uu.ravel(), vv.ravel()], 1), POSE, small)
    t = ((POINTS[0] - origin) @ n) / (dirs @ n)
    hit = origin + t[:, None] * dirs
    depth = -POSE.to_camera(hit)[:, 2].reshape(24, 32)
    world = POSE.to_world(backproject(depth, small).reshape(-1, 3))
    assert np.allclose(world, hit, atol=1e-9)
    assert np.allclose((world - POINTS[0]) @ n, 0, atol=1e-9)
