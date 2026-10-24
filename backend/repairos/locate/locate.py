"""Locate a box seen in a close-up still on the scan (the owner's workflow: issues are FOUND on the close-ups, then
LOCATED on the scan).

    loc = locate(scan, still, box)          # Location | None
    res = try_locate(scan, still, box)      # Location | NotLocated (with the reason)
    res = triangulate([(still_a, box_a), (still_b, box_b)])   # no-LiDAR: >= 2 stills of the same part

`scan` is a `ScanCloud` or a ScanPackage dict (its `points_file` / `mesh_file` resolved against `base_dir`, the
session folder). `still` is a Still dict (contract), `box` is `(x0, y0, x1, y1)` in that still's pixels (the
detector's box).

Rays go from the still's camera through a 3 x 3 grid of points in the box (the centre included). Two methods:

- `ray_cast_mesh` (preferred, when the scan has a `mesh_file` and Open3D is installed): Open3D's
  `t.geometry.RaycastingScene` gives each ray's first hit on the fused mesh and the hit triangle's normal.
- `ray_cast_point_cloud` (no mesh, or no Open3D): the rays are marched through the cloud's grid index; a ray's hit is
  the first surface along it, the nearest run of at least `min_support` points within `radius_m` of the ray and
  `band_m` of each other in depth.

The location is the centre ray's hit (or the median of the hits when the centre ray misses), the region is the box
of the hit (and, for points, supporting) points. All in arkit_world, metres (camera model: `repairos.locate.camera`).

Confidence is a word (I6, uncalibrated), decided by the rules in `_confidence` from what fraction of rays hit,
whether the centre ray hit, whether the hits sit at one depth and how reliable the still's pose is. Numbers stay in
code; the word and the reasons go on.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from repairos.locate.camera import Pinhole, Pose, pixel_rays, project
from repairos.locate.cloud import ScanCloud

Box = tuple[float, float, float, float]
FRAME = "arkit_world"
RAY_CAST = "ray_cast_point_cloud"
RAY_CAST_MESH = "ray_cast_mesh"
TRIANGULATION = "triangulation"
WORDS = ("low", "medium", "high")
SAMPLE_FRACTIONS = (0.1, 0.5, 0.9)   # per axis of the box: a 3 x 3 grid of rays, the centre included


@dataclass(frozen=True)
class Location:
    """Where a still's box is on the scan, in arkit_world metres."""
    scan_id: str
    still_ids: tuple[str, ...]
    point_m: tuple[float, float, float]
    region_min_m: tuple[float, float, float]
    region_max_m: tuple[float, float, float]
    distance_m: float            # from the (first) still's camera centre to point_m
    confidence: str              # low | medium | high (words, I6)
    method: str                  # ray_cast_mesh | ray_cast_point_cloud | triangulation
    rays_cast: int
    rays_hit: int
    support_points: int          # cloud points behind the region (0 for the mesh and triangulation)
    reasons: tuple[str, ...] = ()
    normal: tuple[float, float, float] | None = None   # unit surface normal facing the camera (mesh only)
    frame: str = FRAME

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("still_ids", "point_m", "region_min_m", "region_max_m", "reasons", "normal"):
            if d[k] is not None:
                d[k] = list(d[k])
        return d


@dataclass(frozen=True)
class NotLocated:
    """Why a box could not be put on the scan (I4: a real answer with a reason)."""
    reason: str
    scan_id: str = ""
    still_ids: tuple[str, ...] = ()
    method: str = RAY_CAST
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["still_ids"] = list(d["still_ids"])
        return d


def _tuple3(a: np.ndarray) -> tuple[float, float, float]:
    return float(a[0]), float(a[1]), float(a[2])


def _scan(scan: ScanCloud | Mapping, base_dir: str | Path | None, radius_m: float | None) -> tuple[ScanCloud, float]:
    if isinstance(scan, ScanCloud):
        cloud = scan
        r = radius_m if radius_m is not None else cloud.cell_m
    else:
        voxel_m = float(scan.get("voxel_mm") or 3.0) / 1000.0
        r = radius_m if radius_m is not None else max(0.004, 2.0 * voxel_m)
        cloud = _load_cached(scan, base_dir, r)
    if r > cloud.cell_m:   # the grid answers radii up to its cell size: re-index coarser (cheap)
        cloud = ScanCloud(cloud.points, cloud.scan_id, cloud.depth_source, cell_m=r, mesh_file=cloud.mesh_file)
    return cloud, r


_CACHE: dict[tuple, ScanCloud] = {}


def _load_cached(pkg: Mapping, base_dir: str | Path | None, cell_m: float) -> ScanCloud:
    p = pkg.get("points_file")
    if (pkg.get("depth_source") or "lidar") == "none" or not p:
        return ScanCloud.from_package(pkg, base_dir, cell_m=cell_m)
    path = Path(p) if base_dir is None or Path(p).is_absolute() else Path(base_dir) / p
    st = path.stat()
    key = (str(path.resolve()), st.st_mtime_ns, st.st_size, cell_m)
    if key not in _CACHE:
        if len(_CACHE) >= 4:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = ScanCloud.from_package(pkg, base_dir, cell_m=cell_m)
    return _CACHE[key]


_SCENES: dict[tuple, Any] = {}


def _mesh_scene(path: Path):
    """An Open3D RaycastingScene of a mesh PLY (cached by file), or None when Open3D is missing or the mesh is
    empty."""
    try:
        import open3d as o3d
    except ImportError:
        return None
    st = path.stat()
    key = (str(path.resolve()), st.st_mtime_ns, st.st_size)
    if key not in _SCENES:
        mesh = o3d.io.read_triangle_mesh(str(path))
        if len(mesh.triangles) == 0:
            return None
        scene = o3d.t.geometry.RaycastingScene()
        scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(mesh))
        if len(_SCENES) >= 4:
            _SCENES.pop(next(iter(_SCENES)))
        _SCENES[key] = scene
    return _SCENES[key]


def _cast_mesh(scene, origin: np.ndarray, dirs: np.ndarray, t_min: float, t_max: float):
    """(hit mask, t, normals facing the rays) of each ray's first hit on the mesh."""
    import open3d as o3d

    rays = np.hstack([np.repeat(origin[None], len(dirs), axis=0), dirs]).astype(np.float32)
    ans = scene.cast_rays(o3d.core.Tensor(rays))
    t = ans["t_hit"].numpy().astype(float)
    n = ans["primitive_normals"].numpy().astype(float)
    hit = np.isfinite(t) & (t >= t_min) & (t <= t_max)
    flip = np.sum(n * dirs, axis=1) > 0
    n[flip] *= -1.0
    return hit, t, n


def _pose_problem(still: Mapping) -> str | None:
    if still.get("kind") == "macro_detail":
        return ("macro_detail stills are taken outside the AR session and have no reliable pose; locate the guided "
                "still they are attached to (attached_to_target_id)")
    if still.get("user_skipped"):
        return "the user skipped this still: nothing was photographed"
    return None


def _pose_cap(still: Mapping) -> tuple[str, str | None]:
    """The best confidence word the still's pose allows, and why it is capped."""
    if still.get("pose_reliable") is False:
        return "low", "the still's pose is marked unreliable"
    ts = still.get("tracking_state")
    if ts == "limited":
        return "low", "ARKit tracking was limited when the still was taken"
    if ts == "unknown":
        return "medium", "the capture app did not record the tracking state"
    return "high", None


def _clip_box(box: Box, still: Mapping) -> Box | None:
    x0, y0, x1, y1 = (float(v) for v in box)
    if not (x1 > x0 and y1 > y0):
        raise ValueError(f"empty box {box}")
    w, h = still.get("width_px"), still.get("height_px")
    if w and h:
        x0, y0, x1, y1 = max(x0, 0.0), max(y0, 0.0), min(x1, float(w)), min(y1, float(h))
        if not (x1 > x0 and y1 > y0):
            return None
    return x0, y0, x1, y1


def _first_surface(t: np.ndarray, band_m: float, min_support: int) -> slice | None:
    """The first run (by t) of >= min_support points within band_m of its nearest point."""
    if len(t) < min_support:
        return None
    end = np.searchsorted(t, t + band_m, side="right")
    ok = np.nonzero(end - np.arange(len(t)) >= min_support)[0]
    if len(ok) == 0:
        return None
    i = int(ok[0])
    return slice(i, int(end[i]))


def _down(word: str, steps: int = 1) -> str:
    return WORDS[max(0, WORDS.index(word) - steps)]


def _confidence(centre_hit: bool, frac: float, spread_m: float, distance_m: float, cap: str,
                cap_reason: str | None) -> tuple[str, list[str]]:
    reasons = []
    if centre_hit and frac >= 0.8:
        word = "high"
    elif (centre_hit and frac >= 0.5) or frac >= 0.8:
        word = "medium"
    else:
        word = "low"
    if not centre_hit:
        reasons.append("the ray through the box centre found no scan surface")
    if frac < 0.8:
        reasons.append(f"only {frac:.0%} of the rays through the box found a scan surface")
    if spread_m > max(0.05, 0.25 * distance_m):
        word = _down(word)
        reasons.append(f"the box spans surfaces {spread_m * 100:.0f} cm apart in depth (an edge or the background)")
    if WORDS.index(word) > WORDS.index(cap):
        word = cap
    if cap_reason:
        reasons.append(cap_reason)
    return word, reasons


def try_locate(scan: ScanCloud | Mapping, still: Mapping, box: Box, *, base_dir: str | Path | None = None,
               method: str = "auto", radius_m: float | None = None, band_m: float = 0.01, min_support: int = 2,
               min_range_m: float = 0.05, max_range_m: float = 5.0) -> Location | NotLocated:
    """`locate` with the reason when it cannot place the box. `method`: auto (the mesh when there is one and Open3D
    is installed, else the points), mesh or points."""
    if method not in ("auto", "mesh", "points"):
        raise ValueError(f"method {method!r}: auto, mesh or points")
    sid = str(still.get("still_id", ""))
    cloud, radius = _scan(scan, base_dir, radius_m)
    scan_id = cloud.scan_id or str(still.get("scan_id", ""))
    fail = lambda reason, **d: NotLocated(reason, scan_id, (sid,), RAY_CAST, d)  # noqa: E731
    if cloud.depth_source == "none" or len(cloud.points) == 0:
        return fail("the scan has no point cloud (depth_source none: a no-LiDAR capture); locate the part with "
                    "triangulate() across two or more stills that show it")
    if cloud.scan_id and still.get("scan_id") and still["scan_id"] != cloud.scan_id:
        return fail(f"still {sid} is registered to scan {still['scan_id']}, not {cloud.scan_id}")
    problem = _pose_problem(still)
    if problem:
        return fail(problem)
    clipped = _clip_box(box, still)
    if clipped is None:
        return fail("the box lies outside the still", box=list(box))
    x0, y0, x1, y1 = clipped
    pose = Pose.from_contract(still["camera"])
    cam = Pinhole.from_contract(still["intrinsics"], still.get("width_px"), still.get("height_px"))
    uv = np.array([(x0 + fx * (x1 - x0), y0 + fy * (y1 - y0)) for fy in SAMPLE_FRACTIONS for fx in SAMPLE_FRACTIONS])
    centre_i = len(uv) // 2
    origin, dirs = pixel_rays(uv, pose, cam)
    cap, cap_reason = _pose_cap(still)

    notes: list[str] = []
    scene = None
    if method in ("auto", "mesh") and cloud.mesh_file is not None:
        scene = _mesh_scene(cloud.mesh_file)
        if scene is None:
            notes.append("the scan's mesh could not be ray-cast (Open3D not installed or an empty mesh): used the "
                         "point cloud")
    if method == "mesh" and scene is None:
        return fail("method mesh asked for, but the scan has no mesh that can be ray-cast (no mesh_file, Open3D "
                    "not installed, or an empty mesh)")
    if scene is not None:
        hit, t, normals = _cast_mesh(scene, origin, dirs, min_range_m, max_range_m)
        if hit.any():
            pts = origin + t[hit, None] * dirs[hit]
            centre_hit = bool(hit[centre_i])
            point = pts[np.nonzero(np.nonzero(hit)[0] == centre_i)[0][0]] if centre_hit else np.median(pts, axis=0)
            nrm = normals[centre_i] if centre_hit else normals[hit].mean(axis=0)
            nrm = nrm / max(float(np.linalg.norm(nrm)), 1e-12)
            distance = float(np.linalg.norm(point - origin))
            word, reasons = _confidence(centre_hit, float(hit.mean()), float(t[hit].max() - t[hit].min()),
                                        distance, cap, cap_reason)
            return Location(scan_id=scan_id, still_ids=(sid,), point_m=_tuple3(point),
                            region_min_m=_tuple3(pts.min(axis=0)), region_max_m=_tuple3(pts.max(axis=0)),
                            distance_m=distance, confidence=word, method=RAY_CAST_MESH, rays_cast=len(uv),
                            rays_hit=int(hit.sum()), support_points=0, reasons=tuple(notes + reasons),
                            normal=_tuple3(nrm))
        if method == "mesh":
            return fail(f"none of the {len(uv)} rays through the box hit the scan's mesh between {min_range_m} and "
                        f"{max_range_m} m", rays_cast=len(uv))
        notes.append("no ray hit the scan's mesh: tried the point cloud")

    hits: dict[int, np.ndarray] = {}
    depths: dict[int, float] = {}
    support: list[np.ndarray] = []
    for i, d in enumerate(dirs):
        idx, t, _ = cloud.near_ray(origin, d, radius, min_range_m, max_range_m)
        run = _first_surface(t, band_m, min_support)
        if run is None:
            continue
        pts = cloud.points[idx[run]]
        hits[i] = pts.mean(axis=0)
        depths[i] = float(np.median(t[run]))
        support.append(idx[run])
    if not hits:
        return fail(f"no scan surface within {radius * 1000:.0f} mm of any of the {len(uv)} rays through the box "
                    f"between {min_range_m} and {max_range_m} m (the part is outside the scanned area, or the "
                    f"still's pose does not match the scan)", rays_cast=len(uv))

    centre_hit = centre_i in hits
    point = hits[centre_i] if centre_hit else np.median(np.stack(list(hits.values())), axis=0)
    sup = cloud.points[np.unique(np.concatenate(support))]
    distance = float(np.linalg.norm(point - origin))
    spread = max(depths.values()) - min(depths.values())
    word, reasons = _confidence(centre_hit, len(hits) / len(uv), spread, distance, cap, cap_reason)
    return Location(scan_id=scan_id, still_ids=(sid,), point_m=_tuple3(point), region_min_m=_tuple3(sup.min(axis=0)),
                    region_max_m=_tuple3(sup.max(axis=0)), distance_m=distance, confidence=word, method=RAY_CAST,
                    rays_cast=len(uv), rays_hit=len(hits), support_points=len(sup), reasons=tuple(notes + reasons))


def locate(scan: ScanCloud | Mapping, still: Mapping, box: Box, **options) -> Location | None:
    """The 3D point and region on `scan` that `box` (x0, y0, x1, y1 in `still`'s pixels) shows, or None when it cannot
    be placed (no point cloud, unusable pose, nothing along the rays); `try_locate` says why."""
    res = try_locate(scan, still, box, **options)
    return res if isinstance(res, Location) else None


def triangulate(views: Sequence[tuple[Mapping, Box]], *, min_angle_deg: float = 3.0) -> Location | NotLocated:
    """No-LiDAR locating: the point closest (least squares) to the rays through the box centres of two or more
    stills that show the same part. ARKit poses are metric, so the point is in arkit_world metres. The caller
    asserts the boxes show the same part; this checks the geometry agrees (each still sees the point inside its box).
    """
    ids = tuple(str(s.get("still_id", "")) for s, _ in views)
    scan_ids = {s.get("scan_id") for s, _ in views}
    scan_id = next(iter(scan_ids)) if len(scan_ids) == 1 else ""
    fail = lambda reason, **d: NotLocated(reason, scan_id or "", ids, TRIANGULATION, d)  # noqa: E731
    if len(views) < 2:
        return fail("triangulation needs at least two stills that show the part")
    if len(scan_ids) != 1:
        return fail(f"the stills belong to different scans ({sorted(map(str, scan_ids))}): their poses are in "
                    "different world frames")
    rays, cams, boxes, caps = [], [], [], []
    for still, box in views:
        problem = _pose_problem(still)
        if problem:
            return fail(f"still {still.get('still_id')}: {problem}")
        clipped = _clip_box(box, still)
        if clipped is None:
            return fail(f"still {still.get('still_id')}: the box lies outside the still")
        pose = Pose.from_contract(still["camera"])
        cam = Pinhole.from_contract(still["intrinsics"], still.get("width_px"), still.get("height_px"))
        c = np.array([[(clipped[0] + clipped[2]) / 2, (clipped[1] + clipped[3]) / 2]])
        o, d = pixel_rays(c, pose, cam)
        rays.append((o, d[0]))
        cams.append((pose, cam))
        boxes.append(clipped)
        caps.append(_pose_cap(still))
    dirs = np.stack([d for _, d in rays])
    cosines = np.clip(dirs @ dirs.T, -1.0, 1.0)
    angle = float(np.degrees(np.arccos(cosines.min())))
    if angle < min_angle_deg:
        return fail(f"the stills look at the part from almost the same direction ({angle:.1f} deg apart; at least "
                    f"{min_angle_deg} deg is needed for a depth)", angle_deg=angle)
    a = np.zeros((3, 3))
    b = np.zeros(3)
    for o, d in rays:
        m = np.eye(3) - np.outer(d, d)
        a += m
        b += m @ o
    p = np.linalg.solve(a, b)
    errs = []
    for (pose, cam), (x0, y0, x1, y1) in zip(cams, boxes, strict=True):
        uv, depth = project(p[None], pose, cam)
        if depth[0] <= 0:
            return fail("the rays meet behind a camera: the boxes do not show the same part")
        half_diag = 0.5 * float(np.hypot(x1 - x0, y1 - y0))
        errs.append(float(np.hypot(uv[0, 0] - (x0 + x1) / 2, uv[0, 1] - (y0 + y1) / 2)) / half_diag)
    if max(errs) > 1.0:
        return fail("the rays do not meet inside every box: the boxes do not show the same part, or a pose is off",
                    box_errors=errs)
    resid = float(np.sqrt(np.mean([np.linalg.norm((np.eye(3) - np.outer(d, d)) @ (p - o)) ** 2 for o, d in rays])))
    if max(errs) <= 0.25 and angle >= 10:
        word = "high"
    elif max(errs) <= 0.5 and angle >= 5:
        word = "medium"
    else:
        word = "low"
    reasons = [f"{len(views)} stills, rays {angle:.0f} deg apart, the point projects within "
               f"{max(errs):.0%} of each box's half-diagonal of its centre"]
    for cap, why in caps:
        if WORDS.index(word) > WORDS.index(cap):
            word = cap
        if why and why not in reasons:
            reasons.append(why)
    lo, hi = p - resid, p + resid
    return Location(scan_id=scan_id, still_ids=ids, point_m=_tuple3(p), region_min_m=_tuple3(lo),
                    region_max_m=_tuple3(hi), distance_m=float(np.linalg.norm(p - rays[0][0])), confidence=word,
                    method=TRIANGULATION, rays_cast=len(rays), rays_hit=len(rays), support_points=0,
                    reasons=tuple(reasons))
