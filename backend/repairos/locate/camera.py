"""The camera model every capture uses: frames, poses, pixels and rays. Pure numpy maths, no capture files.

Conventions (the contracts: scan-package.schema.json, still.schema.json):

- **World** `arkit_world`: metres, right-handed, **Y up** (gravity is -Y, `gravity_arkit_world = [0, -1, 0]`), origin
  and heading fixed where the ARKit session started. Points, keyframe poses and still poses all live in it.
- **Camera axes** are ARKit's: x right, y up, the camera looks along **-z**, in the sensor's own (landscape)
  orientation, i.e. the orientation of the pixel grid the intrinsics describe. This is also the OpenGL / NeRF
  convention.
- **A pose is camera-to-world**: `p_world = R @ p_camera + t`, with `R` from `quaternion_xyzw` (Hamilton, scalar
  last) and `t = position_m` (the camera centre in the world).
- **Pixels**: continuous coordinates with (0, 0) at the top-left corner of the image, u to the right, v down; the
  centre of pixel (column i, row j) is (i + 0.5, j + 0.5). With that convention intrinsics scale exactly with the
  image size (`scale_intrinsics`). Pinhole: a camera point (X, Y, Z) with Z < 0 is at
  `u = cx + fx * X / -Z`, `v = cy - fy * Y / -Z`.
- **OpenCV camera axes** (x right, y down, z forward) are ARKit's camera turned 180 degrees about x:
  `R_opencv = R_arkit @ diag(1, -1, -1)`. Stray Scanner writes its odometry quaternions this way (see
  `repairos.importers.stray`); `opencv_to_arkit_camera` undoes it.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

FLIP_YZ = np.diag([1.0, -1.0, -1.0])  # ARKit camera axes <-> OpenCV camera axes (its own inverse)


def quat_to_matrix(q_xyzw: Sequence[float]) -> np.ndarray:
    """3x3 rotation from a quaternion [x, y, z, w] (normalised here; must not be zero)."""
    x, y, z, w = (float(v) for v in q_xyzw)
    n = x * x + y * y + z * z + w * w
    if n < 1e-12:
        raise ValueError("zero quaternion")
    s = 2.0 / n
    return np.array([
        [1 - s * (y * y + z * z), s * (x * y - z * w), s * (x * z + y * w)],
        [s * (x * y + z * w), 1 - s * (x * x + z * z), s * (y * z - x * w)],
        [s * (x * z - y * w), s * (y * z + x * w), 1 - s * (x * x + y * y)],
    ])


def matrix_to_quat(r: np.ndarray) -> list[float]:
    """Quaternion [x, y, z, w] (unit, w >= 0) of a 3x3 rotation matrix."""
    m = np.asarray(r, dtype=float)
    tr = m[0, 0] + m[1, 1] + m[2, 2]
    if tr > 0:
        s = 2.0 * np.sqrt(tr + 1.0)
        w, x, y, z = 0.25 * s, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = 2.0 * np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2])
        w, x, y, z = (m[2, 1] - m[1, 2]) / s, 0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = 2.0 * np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2])
        w, x, y, z = (m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s
    else:
        s = 2.0 * np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1])
        w, x, y, z = (m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s
    q = np.array([x, y, z, w])
    q /= np.linalg.norm(q)
    if q[3] < 0:
        q = -q
    return [float(v) for v in q]


def opencv_to_arkit_camera(r_world_cv: np.ndarray) -> np.ndarray:
    """Camera-to-world rotation with OpenCV camera axes -> the same camera with ARKit camera axes."""
    return np.asarray(r_world_cv, dtype=float) @ FLIP_YZ


@dataclass(frozen=True)
class Pinhole:
    """Intrinsics in pixels of a `width` x `height` image (pixel convention: module docstring)."""
    fx: float
    fy: float
    cx: float
    cy: float
    width: int | None = None
    height: int | None = None

    @classmethod
    def from_contract(cls, intr: Mapping, width: int | None = None, height: int | None = None) -> Pinhole:
        """From a contract `intrinsics` object (a keyframe's carries width_px/height_px, a still's does not)."""
        return cls(float(intr["fx"]), float(intr["fy"]), float(intr["cx"]), float(intr["cy"]),
                   intr.get("width_px", width), intr.get("height_px", height))

    def scaled(self, width: int, height: int) -> Pinhole:
        """The same camera for the same field of view sampled at width x height (e.g. the 256x192 depth grid)."""
        if not (self.width and self.height):
            raise ValueError("scaling needs the source image size")
        sx, sy = width / self.width, height / self.height
        return Pinhole(self.fx * sx, self.fy * sy, self.cx * sx, self.cy * sy, width, height)


@dataclass(frozen=True)
class Pose:
    """Camera-to-world, ARKit camera axes: p_world = R @ p_camera + t."""
    r: np.ndarray
    t: np.ndarray

    @classmethod
    def from_contract(cls, pose: Mapping) -> Pose:
        if pose.get("frame", "arkit_world") != "arkit_world":
            raise ValueError(f"pose frame {pose.get('frame')!r} is not arkit_world")
        return cls(quat_to_matrix(pose["quaternion_xyzw"]), np.asarray(pose["position_m"], dtype=float))

    @classmethod
    def from_matrix(cls, m: np.ndarray) -> Pose:
        m = np.asarray(m, dtype=float)
        if m.shape != (4, 4):
            raise ValueError(f"pose matrix must be 4x4, got {m.shape}")
        r = m[:3, :3]
        if not np.allclose(r @ r.T, np.eye(3), atol=1e-3) or np.linalg.det(r) < 0:
            raise ValueError("pose matrix rotation is not a proper rotation")
        u, _, vt = np.linalg.svd(r)  # re-orthonormalise float32 noise
        return cls(u @ vt, m[:3, 3].copy())

    def to_contract(self) -> dict:
        return {"frame": "arkit_world", "position_m": [float(v) for v in self.t],
                "quaternion_xyzw": matrix_to_quat(self.r)}

    def to_world(self, p_cam: np.ndarray) -> np.ndarray:
        return np.asarray(p_cam, dtype=float) @ self.r.T + self.t

    def to_camera(self, p_world: np.ndarray) -> np.ndarray:
        return (np.asarray(p_world, dtype=float) - self.t) @ self.r


def project(points_world: np.ndarray, pose: Pose, cam: Pinhole) -> tuple[np.ndarray, np.ndarray]:
    """Pixels (N x 2) and depth along the view axis (N; > 0 in front of the camera) of world points."""
    pc = pose.to_camera(np.atleast_2d(points_world))
    depth = -pc[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        u = cam.cx + cam.fx * pc[:, 0] / depth
        v = cam.cy - cam.fy * pc[:, 1] / depth
    return np.stack([u, v], axis=1), depth


def pixel_rays(uv: np.ndarray, pose: Pose, cam: Pinhole) -> tuple[np.ndarray, np.ndarray]:
    """World ray origin (3,) and unit directions (N x 3) through pixels uv (N x 2)."""
    uv = np.atleast_2d(np.asarray(uv, dtype=float))
    d_cam = np.stack([(uv[:, 0] - cam.cx) / cam.fx, -(uv[:, 1] - cam.cy) / cam.fy, -np.ones(len(uv))], axis=1)
    d = d_cam @ pose.r.T
    return pose.t.copy(), d / np.linalg.norm(d, axis=1, keepdims=True)


def backproject(depth_m: np.ndarray, cam: Pinhole) -> np.ndarray:
    """Camera-frame points (H x W x 3, ARKit camera axes) of a depth map whose values are the distance along the
    view axis in metres (ARKit sceneDepth). `cam` must describe the depth grid (`Pinhole.scaled`)."""
    h, w = depth_m.shape
    u = np.arange(w, dtype=float) + 0.5
    v = np.arange(h, dtype=float) + 0.5
    uu, vv = np.meshgrid(u, v)
    z = depth_m.astype(float)
    return np.stack([(uu - cam.cx) / cam.fx * z, -(vv - cam.cy) / cam.fy * z, -z], axis=-1)
