"""Marked points in 3D and the dimensions between them, each with an uncertainty.

A mark is a pixel on a still (full-resolution pixels, as the person or a detector placed it). Its 3D point is
- `parallax`: the least-squares meeting point of the refined rays of two or more marks of the same point seen from
  stills at least MIN_ANGLE_DEG apart, or
- `surface`: its refined ray meeting a plane fitted to the adjusted track points around it, or
- `lidar`: its ray meeting a plane fitted to the still's own LiDAR cells around it (no adjusted points there).
Every point lives in the adjusted frame (unscaled); a distance or diameter is multiplied by the LiDAR scale `s` at the
end. Uncertainties are 1 sigma, propagated by sampling the points from their covariances (the shape), with the size's
own sigma added for the total; they cover random error only, which is why a holdout length decides `verified`
(repairos.measure.reference).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from repairos.locate.camera import Pose
from repairos.measure.adjust import Intrinsics
from repairos.measure.views import View

MARK_SIGMA_PX = 1.5        # a tap or a detector corner, in full-resolution still pixels
RADIUS_PX = 40             # neighbourhood of a mark for its local plane (working pixels)
MIN_PLANE_POINTS = 8
MIN_ANGLE_DEG = 3.0
MEET_PX = 6.0              # a triangulated point must land this close to every mark (full-resolution pixels)
MIN_INCIDENCE = 0.2        # |cos| between the ray and the plane normal; flatter is too grazing to trust
LIDAR_FLOOR_M = 0.003      # a LiDAR plane is never trusted to better than this along its normal
SAMPLES = 400


@dataclass
class Scene:
    views: list[View]
    poses: list[Pose]
    intr: Intrinsics
    points: np.ndarray | None          # adjusted track points (unscaled frame) or None
    point_sigma: np.ndarray | None
    s: float = 1.0
    sigma_s: float = 0.0
    b_m: float = 0.0
    pose_sigma_m: float = 0.0        # error of the poses the rays come from (0 after an adjustment: it is in the points)
    pose_sigma_rad: float = 0.0
    index: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        self.index = {v.still_id: i for i, v in enumerate(self.views)}


@dataclass
class Point3:
    name: str
    x: np.ndarray            # adjusted frame, metres
    cov: np.ndarray          # 3 x 3
    method: str
    stills: list[str]
    detail: dict = field(default_factory=dict)


class Unmeasurable(ValueError):
    """A point or dimension that cannot be measured here (the message says why)."""


def _ray(scene: Scene, i: int, uv_still: Sequence[float]) -> tuple[np.ndarray, np.ndarray, float]:
    v = scene.views[i]
    uv = np.asarray(uv_still, dtype=float)[None] * v.scale
    d_cam = scene.intr.ray(uv, v.cam)[0]
    pose = scene.poses[i]
    return pose.t, pose.r @ d_cam, scene.intr.s * v.cam.fx


def _plane(pts: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    c = pts.mean(axis=0)
    w, vec = np.linalg.eigh(np.cov((pts - c).T))
    return c, vec[:, 0], float(np.sqrt(max(w[0], 0.0)))


def _meet(o, d, c, n, sigma_plane, f_px, sigma_px) -> tuple[np.ndarray, np.ndarray]:
    cos = float(n @ d)
    if abs(cos) < MIN_INCIDENCE:
        raise Unmeasurable("the surface is seen too edge-on there; mark it on a still that faces it")
    lam = float(n @ (c - o)) / cos
    if lam <= 0:
        raise Unmeasurable("the surface fitted there is behind the camera")
    x = o + lam * d
    s_ray = sigma_plane / abs(cos)
    s_lat = lam * sigma_px / f_px
    dd = np.outer(d, d)
    return x, s_ray ** 2 * dd + s_lat ** 2 * (np.eye(3) - dd)


def _surface(scene: Scene, i: int, uv_still, sigma_px) -> tuple[np.ndarray, np.ndarray, dict] | None:
    if scene.points is None or not len(scene.points):
        return None
    v, pose = scene.views[i], scene.poses[i]
    pc = pose.to_camera(scene.points)
    front = pc[:, 2] < 0
    uv = scene.intr.project(pc[front], v.cam)
    mark = np.asarray(uv_still, dtype=float) * v.scale
    near = np.linalg.norm(uv - mark, axis=1) <= RADIUS_PX
    if near.sum() < MIN_PLANE_POINTS:
        return None
    pts = scene.points[front][near]
    depth = -pc[front][near][:, 2]
    keep = np.abs(depth - np.median(depth)) <= max(0.015, 3 * 1.4826 * np.median(np.abs(depth - np.median(depth))))
    pts = pts[keep]
    if len(pts) < MIN_PLANE_POINTS:
        return None
    c, n, scatter = _plane(pts)
    sig = scene.point_sigma[front][near][keep] if scene.point_sigma is not None else np.zeros(len(pts))
    # noise averages over the points; a curved surface does not (its plane is off at the centre by about a third of
    # the scatter), so a third of the scatter stays as a floor
    sigma_plane = float(np.sqrt(scatter ** 2 / len(pts) + np.nanmean(sig) ** 2 / len(pts) + (scatter / 3) ** 2))
    o, d, f = _ray(scene, i, uv_still)
    x, cov = _meet(o, d, c, n, sigma_plane, f, sigma_px * v.scale)
    return x, cov, {"plane_points": int(len(pts)), "plane_scatter_mm": round(scatter * 1000, 3)}


def _lidar(scene: Scene, i: int, uv_still, sigma_px) -> tuple[np.ndarray, np.ndarray, dict]:
    v, pose = scene.views[i], scene.poses[i]
    if v.depth is None:
        raise Unmeasurable(f"still {v.still_id} has no depth and no matched points near the mark")
    h, w = v.depth.shape
    gw, gh = v.size
    mark = np.asarray(uv_still, dtype=float) * v.scale
    gx, gy = mark[0] * w / gw, mark[1] * h / gh
    r = max(2, int(np.ceil(RADIUS_PX * w / gw)))
    xs, ys = np.meshgrid(np.arange(max(0, int(gx) - r), min(w, int(gx) + r + 1)),
                         np.arange(max(0, int(gy) - r), min(h, int(gy) + r + 1)))
    z = v.depth[ys, xs].astype(float).reshape(-1)
    ok = np.isfinite(z) & (z > 0.05) & (z < 3.0)
    if v.confidence is not None:
        ok &= v.confidence[ys, xs].reshape(-1) >= 1
    if ok.sum() < MIN_PLANE_POINTS:
        raise Unmeasurable(f"still {v.still_id}: too little LiDAR depth around the mark")
    uv_cells = np.stack([(xs.reshape(-1)[ok] + 0.5) * gw / w, (ys.reshape(-1)[ok] + 0.5) * gh / h], 1)
    z_adj = (z[ok] - scene.b_m) / scene.s           # back into the adjusted (unscaled) frame
    rays = scene.intr.ray(uv_cells, v.cam)
    pc = rays * (z_adj / -rays[:, 2])[:, None]
    pts = pose.to_world(pc)
    med = np.median(z_adj)
    keep = np.abs(z_adj - med) <= max(0.015, 3 * 1.4826 * np.median(np.abs(z_adj - med)))
    pts = pts[keep]
    c, n, scatter = _plane(pts)
    sigma_plane = float(np.sqrt(scatter ** 2 / max(1, len(pts) // 9) + LIDAR_FLOOR_M ** 2))  # 3x3 cells share a dot
    o, d, f = _ray(scene, i, uv_still)
    x, cov = _meet(o, d, c, n, sigma_plane, f, sigma_px * v.scale)
    return x, cov, {"lidar_cells": int(len(pts)), "plane_scatter_mm": round(scatter * 1000, 3)}


def _triangulate(scene: Scene, marks: list[tuple[int, np.ndarray]], sigma_px: float
                 ) -> tuple[np.ndarray, np.ndarray, dict] | None:
    def solve(uvs):
        a, b = np.zeros((3, 3)), np.zeros(3)
        for (i, _), uv in zip(marks, uvs, strict=True):
            o, d, _ = _ray(scene, i, uv)
            p = np.eye(3) - np.outer(d, d)
            a += p
            b += p @ o
        return np.linalg.solve(a, b)

    dirs = [_ray(scene, i, uv)[1] for i, uv in marks]
    angle = max(np.degrees(np.arccos(np.clip(a @ b, -1, 1))) for k, a in enumerate(dirs) for b in dirs[k + 1:])
    if angle < MIN_ANGLE_DEG:
        return None
    uvs = [np.asarray(uv, dtype=float) for _, uv in marks]
    x = solve(uvs)
    for (i, _), uv in zip(marks, uvs, strict=True):   # the rays must meet: in front of each still, on each mark
        v, pose = scene.views[i], scene.poses[i]
        pc = pose.to_camera(x[None])
        if pc[0, 2] >= 0:
            raise Unmeasurable(f"the marks of this point do not meet: it would lie behind still {v.still_id} "
                               "(different spots marked?)")
        miss = float(np.linalg.norm(scene.intr.project(pc, v.cam)[0] / v.scale - uv))
        if miss > max(MEET_PX, 4 * sigma_px):
            raise Unmeasurable(f"the marks of this point do not meet: {miss:.0f} px off on still {v.still_id} "
                               "(different spots marked?)")
    jac, eps = [], 0.25
    for k in range(len(uvs)):
        for c in range(2):
            up = [u.copy() for u in uvs]
            up[k][c] += eps
            jac.append((solve(up) - x) / eps)
    jac = np.array(jac).T                             # 3 x 2K, per still pixel
    cov = jac @ jac.T * sigma_px ** 2
    return x, cov, {"rays": len(marks), "max_angle_deg": round(float(angle), 2)}


def locate_point(scene: Scene, name: str, marks: Sequence[Mapping]) -> Point3:
    """The 3D point of one named point from its marks [{still_id, uv: [u, v]} (full-resolution pixels)]."""
    usable = []
    for mk in marks:
        i = scene.index.get(str(mk.get("still_id")))
        if i is None:
            raise Unmeasurable(f"point {name}: still {mk.get('still_id')} is not a measuring still")
        usable.append((i, np.asarray(mk["uv"], dtype=float), float(mk.get("sigma_px") or MARK_SIGMA_PX)))
    if not usable:
        raise Unmeasurable(f"point {name} has no marks")
    stills = [scene.views[i].still_id for i, _, _ in usable]
    if len(usable) >= 2:
        tri = _triangulate(scene, [(i, uv) for i, uv, _ in usable], max(s for _, _, s in usable))
        if tri is not None:
            return Point3(name, tri[0], _with_pose_error(scene, usable, tri[0], tri[1]), "parallax", stills, tri[2])
    est = []
    for i, uv, sp in usable:
        got = _surface(scene, i, uv, sp)
        method = "surface"
        if got is None:
            got, method = _lidar(scene, i, uv, sp), "lidar"
        est.append((got, method))
    info = sum(np.linalg.inv(g[1]) for g, _ in est)
    cov = np.linalg.inv(info)
    x = cov @ sum(np.linalg.inv(g[1]) @ g[0] for g, _ in est)
    method = "surface" if any(m == "surface" for _, m in est) else "lidar"
    return Point3(name, x, _with_pose_error(scene, usable, x, cov), method, stills, est[0][0][2] | {"marks": len(est)})


def _with_pose_error(scene: Scene, usable, x: np.ndarray, cov: np.ndarray) -> np.ndarray:
    """cov plus the error of unadjusted poses: their position error and their angle error at the point's distance."""
    if not (scene.pose_sigma_m or scene.pose_sigma_rad):
        return cov
    dist = min(float(np.linalg.norm(x - scene.poses[i].t)) for i, _, _ in usable)
    return cov + np.eye(3) * (scene.pose_sigma_m ** 2 + (dist * scene.pose_sigma_rad) ** 2)


def _samples(p: Point3, rng: np.random.Generator) -> np.ndarray:
    return rng.multivariate_normal(p.x, p.cov, size=SAMPLES, check_valid="ignore")


def distance(scene: Scene, a: Point3, b: Point3, seed: int = 0) -> tuple[float, float, float]:
    """(mm, 1-sigma mm of the shape, 1-sigma mm with the size's uncertainty too) between two points, LiDAR-scaled.
    A known length shares the size error, so a reference correction keeps only the shape sigma."""
    rng = np.random.default_rng(seed)
    value = float(np.linalg.norm(a.x - b.x) * scene.s * 1000)
    shape = float((np.linalg.norm(_samples(a, rng) - _samples(b, rng), axis=1) * scene.s * 1000).std())
    return value, shape, float(np.hypot(shape, value * scene.sigma_s / scene.s))


def circle_diameter(pts: np.ndarray) -> float:
    """Diameter of the circle through rim points (3D, at least three): best plane, then a least-squares circle."""
    c, n, _ = _plane(pts) if len(pts) > 3 else (pts.mean(0), np.cross(pts[1] - pts[0], pts[2] - pts[0]), 0.0)
    n = n / np.linalg.norm(n)
    e1 = np.cross(n, [1.0, 0, 0] if abs(n[0]) < 0.9 else [0, 1.0, 0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(n, e1)
    q = np.stack([(pts - c) @ e1, (pts - c) @ e2], 1)
    a = np.column_stack([2 * q, np.ones(len(q))])
    sol = np.linalg.lstsq(a, (q ** 2).sum(1), rcond=None)[0]
    return float(2 * np.sqrt(max(sol[2] + sol[0] ** 2 + sol[1] ** 2, 0.0)))


def diameter(scene: Scene, rim: Sequence[Point3], seed: int = 0) -> tuple[float, float, float]:
    """(mm, shape sigma, total sigma) of the hole or boss whose rim the points mark (sigmas as in `distance`)."""
    if len(rim) < 3:
        raise Unmeasurable("a diameter needs at least three rim points")
    rng = np.random.default_rng(seed)
    value = circle_diameter(np.stack([p.x for p in rim])) * scene.s * 1000
    sams = np.stack([_samples(p, rng) for p in rim], axis=1)    # SAMPLES x K x 3
    shape = float((np.array([circle_diameter(sams[k]) for k in range(SAMPLES)]) * scene.s * 1000).std())
    return float(value), shape, float(np.hypot(shape, value * scene.sigma_s / scene.s))
