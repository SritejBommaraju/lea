"""Align an object-moved capture segment (the object flipped for its underside) to segment 0, on the server (Linux,
CPU, Open3D only; no Apple frameworks).

    seg0 = cloud_from_frames(frames0, depths0, voxel_m=v)   # ObjectCloud: in-box LiDAR points (+ colours), segment 0
    segk = cloud_from_frames(framesk, depthsk, voxel_m=v)   # the same for segment k, in its own (moved-object) world
    res = align_clouds(seg0, segk, voxel_m=v, init=phone_estimate, ref_frames=frames0, mov_frames=framesk)

Method (Open3D, MIT):
1. Both clouds voxel-downsampled (`voxel_m`, scaled to the object), normals oriented outward from each cloud's
   centre, FPFH features (radius 5 voxels).
2. Global registration: feature-matching random-sample consensus (mutual filter, edge-length and distance
   checkers), a few seeds. Each result is a candidate. The phone's estimate is one more candidate. It is never
   trusted as is: it is refined and scored like the others.
3. More starts, because feature matching can miss on a plain object:
   - Gravity sweep: both segments share one gravity-aligned ARKit world, so the object lies upright or upside
     down at some heading. 2 x 12 headings, centre on centre.
   - Symmetric objects: the best candidate turned 180 deg about each principal axis of the segment-0 cloud. A
     box-shaped object fits equally well several ways round.
4. Each candidate is refined by point-to-plane ICP, coarse to fine (3, 1.5 and 1 voxel; the sweep starts at 10),
   and scored by:
   - fitness: the share of the moving cloud within 1.5 voxels of segment 0;
   - inlier RMSE;
   - free space: the share of its points that the other segment's cameras saw as empty (in front of their
     measured depth). A wrong pose that slides the underside onto the top face fits the surface but floats in
     front of what the cameras measured. Candidates over `max_free_space` are dropped.
5. Among the remaining candidates within `tie_fitness` of the best fitness, the one whose matched points agree
   best in colour wins. Colours are standardised per cloud, so a different exposure after the flip does not count.
6. Accepted only when fitness >= `min_fitness` and RMSE <= `max_rmse_voxels` voxels; otherwise `aligned` is false,
   with the reason.

The transform maps the segment's world (its ARKit world at capture, where the object lay flipped) to segment 0's
world: p0 = T @ pk. Row-major 4x4 in metres, like the contract's matrices.
"""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

MIN_FITNESS = 0.30          # a flipped segment shares only its sides with segment 0: about a third to a half
MAX_RMSE_VOXELS = 0.9       # inlier RMSE, in voxels (inliers are within 1.5 voxels)
TIE_FITNESS = 0.05          # candidates this close in fitness are told apart by colour
RANSAC_SEEDS = (0, 1, 2)
RANSAC_ITERATIONS = 60_000
MAX_POINTS = 12_000         # each cloud is thinned to about this many points for registration (time bound)
MAX_FREE_SPACE = 0.10       # a candidate is dropped when this much more of its points than of the segment's own
                            # points sit in space the cameras saw empty (the own-cloud share is the baseline)
FREE_SPACE_MARGIN_M = 0.025  # "in front of the measured surface" by more than this: ARKit drift and LiDAR noise
                             # put a cloud's own points up to ~2 cm in front of single frames (measured, 1e61)
PLANE_MARGIN_M = 0.015       # below the support plane by more than this is impossible (ARKit drifts up to ~1.5 cm)
FLIP_MIN_TILT_DEG = 60.0     # a flipped object's up direction turned at least this far (it may lie on its side)
MAX_BELOW_PLANE = 0.03       # a candidate is dropped when more than this share of its points lies under the table
SAME_POSE_DEG = 20.0         # candidates turned less than this from each other are the same pose
MIN_COLOUR_SPREAD = 4.0      # 0-255: a cloud whose colours vary less than this has no colour to compare
COLOUR_MARGIN = 0.15         # colour decides between symmetric poses only when 15% better than the runner-up
MIN_PHONE_QUALITY = 0.5      # the phone's estimate may pick among symmetric poses only at this quality or above
PHONE_OK_DEG, PHONE_OK_M = 2.0, 0.005   # the phone's estimate is "confirmed" when the result is this close to it


@dataclass
class ObjectCloud:
    points: np.ndarray                    # N x 3, metres, the segment's world
    colours: np.ndarray | None = None     # N x 3 uint8

    def __len__(self) -> int:
        return len(self.points)


@dataclass
class Candidate:
    source: str                 # phone_estimate | fpfh_ransac_seed<n> | <source>+flip_<axis>
    transform: np.ndarray       # 4x4, segment -> segment 0
    fitness: float
    rmse_m: float
    colour_distance: float | None
    free_space: float | None = None     # share of points that a camera of the other segment saw as empty space
    below_plane: float | None = None    # share of points under the other segment's support plane
    free_space_excess: float | None = None   # free space beyond what those cameras see of their own segment's cloud

    def brief(self) -> dict:
        return {"source": self.source, "fitness": round(self.fitness, 4), "rmse_mm": round(self.rmse_m * 1000, 3),
                "colour_distance": None if self.colour_distance is None else round(self.colour_distance, 4),
                "free_space_violation": None if self.free_space is None else round(self.free_space, 4),
                "free_space_excess": None if self.free_space_excess is None else round(self.free_space_excess, 4),
                "below_table": None if self.below_plane is None else round(self.below_plane, 4)}


@dataclass
class SegmentAlignment:
    segment: int
    aligned: bool
    transform: list[float] | None          # row-major 4x4, segment -> segment 0 (None when not aligned)
    fitness: float | None
    rmse_mm: float | None
    method: str
    voxel_mm: float
    points: dict = field(default_factory=dict)            # cloud sizes used
    phone_estimate: dict | None = None                    # the phone's transform checked: given, quality, error
    tie_break: str | None = None                          # how symmetric candidates were told apart
    candidates: list[dict] = field(default_factory=list)
    reason: str | None = None
    seconds: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def matrix(self) -> np.ndarray | None:
        return None if self.transform is None else np.asarray(self.transform, float).reshape(4, 4)


# --- clouds ---------------------------------------------------------------------------------------------------------

def _frame_colours(frame: Any, keep: np.ndarray) -> np.ndarray | None:
    """Colours of the kept depth pixels from the keyframe's image (the depth grid covers the same field of view)."""
    if frame.image_path is None:
        return None
    from PIL import Image

    try:
        with Image.open(frame.image_path) as im:
            rgb = np.asarray(im.convert("RGB"))
    except OSError:
        return None
    h, w = rgb.shape[:2]
    dh, dw = keep.shape
    jj, ii = np.nonzero(keep)[1], np.nonzero(keep)[0]
    u = np.clip(((jj + 0.5) * w / dw).astype(int), 0, w - 1)
    v = np.clip(((ii + 0.5) * h / dh).astype(int), 0, h - 1)
    return rgb[v, u]


def cloud_from_frames(frames: Sequence[Any], depths: Sequence[np.ndarray], *, voxel_m: float,
                      colours: bool = True) -> ObjectCloud:
    """World points of the kept (non-zero) depth pixels of every frame, voxel-averaged (Open3D), with colours."""
    import open3d as o3d

    from repairos.locate.camera import backproject

    pts, cols = [], []
    for f, d in zip(frames, depths, strict=True):
        keep = d > 0
        if not keep.any():
            continue
        p = f.pose.to_world(backproject(d, f.depth_cam)[keep])
        pts.append(p)
        c = _frame_colours(f, keep) if colours else None
        cols.append(c if c is not None else np.full((len(p), 3), 128, np.uint8))
    if not pts:
        return ObjectCloud(np.zeros((0, 3)), None)
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.concatenate(pts)))
    pc.colors = o3d.utility.Vector3dVector(np.concatenate(cols).astype(float) / 255.0)
    pc = pc.voxel_down_sample(voxel_m)
    return ObjectCloud(np.asarray(pc.points), (np.asarray(pc.colors) * 255).round().astype(np.uint8) if colours
                       else None)


def segment_frames(pkg_local: Mapping[str, Any], segment: int) -> list[Any]:
    """DepthFrames of one segment's keyframes (file fields are absolute local paths)."""
    from pathlib import Path

    from repairos.recon.tsdf import frames_from_scan_package

    kfs = [kf for kf in pkg_local.get("keyframes") or [] if int(kf.get("segment") or 0) == segment]
    return frames_from_scan_package({**pkg_local, "keyframes": kfs}, Path("/"))


# --- registration ---------------------------------------------------------------------------------------------------

def _o3d_cloud(c: ObjectCloud, voxel: float, max_points: int):
    import open3d as o3d

    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.asarray(c.points, float)))
    if c.colours is not None:
        pc.colors = o3d.utility.Vector3dVector(np.asarray(c.colours, float) / 255.0)
    pc = pc.voxel_down_sample(voxel)
    if len(pc.points) > max_points:
        pc = pc.random_down_sample(max_points / len(pc.points))
    pc.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=2.5 * voxel, max_nn=30))
    centre = np.asarray(pc.points).mean(axis=0)
    pc.orient_normals_towards_camera_location(centre)
    pc.normals = o3d.utility.Vector3dVector(-np.asarray(pc.normals))   # outward
    return pc


def _fpfh(pc, voxel: float):
    import open3d as o3d

    return o3d.pipelines.registration.compute_fpfh_feature(
        pc, o3d.geometry.KDTreeSearchParamHybrid(radius=5.0 * voxel, max_nn=100))


def _standardised(c: np.ndarray) -> np.ndarray | None:
    """Colours with each channel's mean and spread removed (exposure changes between segments cancel); None when
    the cloud is one colour (a plain object or no image): then colour cannot tell poses apart."""
    c = np.asarray(c, float)
    sd = c.std(axis=0)
    if sd.max() < MIN_COLOUR_SPREAD:
        return None
    return (c - c.mean(axis=0)) / np.maximum(sd, 1.0)


def _score(src, tgt, t: np.ndarray, voxel: float, src_col, tgt_col) -> tuple[float, float, float | None]:
    """(fitness, inlier RMSE, colour distance) of transform t (src -> tgt) at 1.5 voxels."""
    import open3d as o3d

    ev = o3d.pipelines.registration.evaluate_registration(src, tgt, 1.5 * voxel, t)
    corr = np.asarray(ev.correspondence_set)
    rmse = float(ev.inlier_rmse) if len(corr) else float("inf")
    colour = None
    if src_col is not None and tgt_col is not None and len(corr) >= 20:
        colour = float(np.mean(np.linalg.norm(src_col[corr[:, 0]] - tgt_col[corr[:, 1]], axis=1)))
    return float(ev.fitness), rmse, colour


def _icp(src, tgt, init: np.ndarray, voxel: float, schedule=(3.0, 1.5, 1.0)) -> np.ndarray:
    import open3d as o3d

    t = init
    for f in schedule:
        reg = o3d.pipelines.registration.registration_icp(
            src, tgt, f * voxel, t, o3d.pipelines.registration.TransformationEstimationPointToPlane(),
            o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=40))
        t = np.asarray(reg.transformation)
    return t


def _flips(t: np.ndarray, tgt_points: np.ndarray) -> list[tuple[str, np.ndarray]]:
    """t followed by a 180 deg turn about each principal axis of segment 0's cloud, through its centre."""
    c = tgt_points.mean(axis=0)
    _, _, vt = np.linalg.svd(tgt_points - c, full_matrices=False)
    out = []
    for name, axis in zip(("major", "middle", "minor"), vt, strict=True):
        r = 2.0 * np.outer(axis, axis) - np.eye(3)        # rotation by pi about `axis`
        m = np.eye(4)
        m[:3, :3] = r
        m[:3, 3] = c - r @ c
        out.append((name, m @ t))
    return out


def free_space_violation(points: np.ndarray, frames: Sequence[Any], *, margin_m: float, max_frames: int = 16,
                         min_confidence: int = 1) -> float | None:
    """Share of `points` (in the frames' world) that some frame saw as empty space: the point projects inside the
    frame's depth map, in front of the measured surface by more than `margin_m`. A right alignment puts the moved
    segment's surface on or behind what segment 0's cameras measured; a wrong one floats points in front of it.
    None when no point projects into any frame."""
    from repairos.locate.camera import project

    if not frames or not len(points):
        return None
    step = max(1, len(frames) // max_frames)
    seen = np.zeros(len(points), bool)
    bad = np.zeros(len(points), bool)
    for f in list(frames)[::step]:
        uv, z = project(points, f.pose, f.depth_cam)
        h, w = f.depth_m.shape
        ins = (z > 0.05) & (uv[:, 0] >= 0) & (uv[:, 0] < w) & (uv[:, 1] >= 0) & (uv[:, 1] < h)
        if not ins.any():
            continue
        i, j = uv[ins, 1].astype(int), uv[ins, 0].astype(int)
        d = f.depth_m[i, j]
        ok = np.isfinite(d) & (d > 0)
        if f.confidence is not None:
            ok &= f.confidence[i, j] >= min_confidence
        idx = np.nonzero(ins)[0][ok]
        seen[idx] = True
        bad[idx] |= z[idx] < d[ok] - margin_m
    return float(bad[seen].mean()) if seen.any() else None


def below_plane(points: np.ndarray, plane_y: float, margin_m: float) -> float:
    """Share of points more than `margin_m` below the support plane y = plane_y (world Y up)."""
    return float((np.asarray(points)[:, 1] < plane_y - margin_m).mean()) if len(points) else 0.0


def _gravity_starts(src_pts: np.ndarray, tgt_pts: np.ndarray, steps: int = 12, src_plane_y: float | None = None,
                    tgt_plane_y: float | None = None) -> list[tuple[str, np.ndarray]]:
    """Starts for an object moved on the table, in one gravity-aligned (Y up) world: upright or upside down (180 deg
    about a horizontal axis), at `steps` headings, centre on centre across the table. Global registration can miss on
    a plain, symmetric object; these cover every resting pose on the same face or the opposite one.
    With both support planes known, the height comes from them: the object rests on its table in both segments, so
    upright keeps its height above the table, and upside down puts the segment's table contact at the object's top
    (the object's height is the taller of the two clouds above their tables). A segment sees only one side of a flat
    object, so centring the two clouds' middles would lay the moved side onto the other side's face."""
    cs, ct = src_pts.mean(axis=0), tgt_pts.mean(axis=0)
    planes = src_plane_y is not None and tgt_plane_y is not None
    if planes:
        height = max(float(np.percentile(src_pts[:, 1], 98)) - src_plane_y,
                     float(np.percentile(tgt_pts[:, 1], 98)) - tgt_plane_y, 0.0)
    flip = np.diag([1.0, -1.0, -1.0])        # 180 deg about x
    out = []
    for upside, base in (("upright", np.eye(3)), ("flipped", flip)):
        for k in range(steps):
            a = 2 * np.pi * k / steps
            yaw = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])
            r = yaw @ base
            m = np.eye(4)
            m[:3, :3] = r
            m[:3, 3] = ct - r @ cs
            if planes:   # y' = r_yy y + t_y: the segment's table goes to the table (upright) or to the top (flipped)
                m[1, 3] = (tgt_plane_y - src_plane_y) if upside == "upright" else (tgt_plane_y + height + src_plane_y)
            out.append((f"gravity_{upside}_{round(np.degrees(a))}", m))
    return out


def _pct(x: float | None) -> str:
    return "unmeasured" if x is None else f"{x:.0%}"


def rotation_deg(r: np.ndarray) -> float:
    return float(np.degrees(np.arccos(np.clip((np.trace(r) - 1.0) / 2.0, -1.0, 1.0))))


def align_clouds(ref: ObjectCloud, mov: ObjectCloud, *, voxel_m: float, init: np.ndarray | None = None,
                 init_quality: float | None = None, segment: int = 1, min_fitness: float = MIN_FITNESS,
                 max_rmse_voxels: float = MAX_RMSE_VOXELS, tie_fitness: float = TIE_FITNESS,
                 seeds: Sequence[int] = RANSAC_SEEDS, max_points: int = MAX_POINTS,
                 ref_frames: Sequence[Any] | None = None, mov_frames: Sequence[Any] | None = None,
                 max_free_space: float = MAX_FREE_SPACE, gravity_sweep: bool = True,
                 colour_margin: float = COLOUR_MARGIN, min_phone_quality: float = MIN_PHONE_QUALITY,
                 ref_plane_y: float | None = None, mov_plane_y: float | None = None,
                 expect_flip: bool = False) -> SegmentAlignment:
    """Transform taking `mov` (segment k's world) onto `ref` (segment 0's world); see the module docstring.
    `ref_frames` / `mov_frames`: the segments' depth frames, for the free-space check (raw depth, table included).
    `ref_plane_y` / `mov_plane_y`: each segment's support-plane height in its own world (Y up): nothing of the object
    can sit below the table it rests on, so a candidate that puts over MAX_BELOW_PLANE of its points there is dropped
    (the cameras cannot see under the table: only this catches a flat face matched to its opposite face)."""
    import open3d as o3d

    t0 = time.perf_counter()
    secs: dict[str, float] = {}
    base = {"segment": segment, "method": "", "voxel_mm": round(voxel_m * 1000, 3),
            "points": {"segment0": int(len(ref)), "segment": int(len(mov))}}
    if len(ref) < 50 or len(mov) < 50:
        return SegmentAlignment(aligned=False, transform=None, fitness=None, rmse_mm=None, **base,
                                reason=f"too few object points to align (segment 0: {len(ref)}, segment "
                                       f"{segment}: {len(mov)})")
    tgt = _o3d_cloud(ref, voxel_m, max_points)
    src = _o3d_cloud(mov, voxel_m, max_points)
    base["points"].update(segment0_used=len(tgt.points), segment_used=len(src.points))
    src_col = _standardised(np.asarray(src.colors) * 255) if src.has_colors() else None
    tgt_col = _standardised(np.asarray(tgt.colors) * 255) if tgt.has_colors() else None
    secs["prepare_s"] = time.perf_counter() - t0

    t1 = time.perf_counter()
    fs, ft = _fpfh(src, voxel_m), _fpfh(tgt, voxel_m)
    starts: list[tuple[str, np.ndarray]] = []
    if init is not None:
        starts.append(("phone_estimate", np.asarray(init, float).reshape(4, 4)))
    reg = o3d.pipelines.registration
    for s in seeds:
        o3d.utility.random.seed(int(s))
        r = reg.registration_ransac_based_on_feature_matching(
            src, tgt, fs, ft, True, 1.5 * voxel_m, reg.TransformationEstimationPointToPoint(False), 3,
            [reg.CorrespondenceCheckerBasedOnEdgeLength(0.9), reg.CorrespondenceCheckerBasedOnDistance(1.5 * voxel_m)],
            reg.RANSACConvergenceCriteria(RANSAC_ITERATIONS, 0.999))
        if r.fitness > 0:
            starts.append((f"fpfh_ransac_seed{s}", np.asarray(r.transformation)))
    secs["global_s"] = time.perf_counter() - t1

    t2 = time.perf_counter()
    cands: list[Candidate] = []

    margin = max(FREE_SPACE_MARGIN_M, 3.0 * voxel_m)
    plane_margin = max(PLANE_MARGIN_M, 2.0 * voxel_m)
    src_pts, tgt_pts = np.asarray(src.points), np.asarray(tgt.points)
    # Each segment's cameras are calibrated by their own cloud: the share of it they see as empty space is their
    # drift and noise (ARKit drifts a centimetre or two over one capture). A candidate is judged per segment against
    # that segment's own baseline, so a drifting segment does not hide what the steadier one sees.
    base_ref = free_space_violation(tgt_pts, ref_frames, margin_m=margin) if ref_frames else None
    base_mov = free_space_violation(src_pts, mov_frames, margin_m=margin) if mov_frames else None

    def evaluate(name: str, start: np.ndarray, schedule=(3.0, 1.5, 1.0)) -> Candidate:
        t = _icp(src, tgt, start, voxel_m, schedule)
        f, e, c = _score(src, tgt, t, voxel_m, src_col, tgt_col)
        fs_parts = []   # (violation, that camera set's own baseline)
        if ref_frames:
            fs_parts.append((free_space_violation(src_pts @ t[:3, :3].T + t[:3, 3], ref_frames, margin_m=margin),
                             base_ref))
        if mov_frames:
            ti = np.linalg.inv(t)
            fs_parts.append((free_space_violation(tgt_pts @ ti[:3, :3].T + ti[:3, 3], mov_frames, margin_m=margin),
                             base_mov))
        below = []
        if ref_plane_y is not None:
            below.append(below_plane(src_pts @ t[:3, :3].T + t[:3, 3], ref_plane_y, plane_margin))
        if mov_plane_y is not None:
            ti = np.linalg.inv(t)
            below.append(below_plane(tgt_pts @ ti[:3, :3].T + ti[:3, 3], mov_plane_y, plane_margin))
        fs_parts = [(x, b or 0.0) for x, b in fs_parts if x is not None]
        cand = Candidate(name, t, f, e, c, max(x for x, _ in fs_parts) if fs_parts else None,
                         max(below) if below else None,
                         max(x - b for x, b in fs_parts) if fs_parts else None)
        cands.append(cand)
        return cand

    for name, start in starts:
        evaluate(name, start)
    if gravity_sweep:   # same ARKit world, Y up: the object lies upright or upside down, at some heading
        for name, start in _gravity_starts(src_pts, tgt_pts, src_plane_y=mov_plane_y, tgt_plane_y=ref_plane_y):
            evaluate(name, start, (10.0, 3.0, 1.5, 1.0))
    if cands:
        best = max(cands, key=lambda c: c.fitness)
        for axis, start in _flips(best.transform, np.asarray(tgt.points)):
            evaluate(f"{best.source}+flip_{axis}", start)
    secs["refine_s"] = time.perf_counter() - t2
    secs["total_s"] = time.perf_counter() - t0
    secs = {k: round(v, 3) for k, v in secs.items()}
    if not cands:
        return SegmentAlignment(aligned=False, transform=None, fitness=None, rmse_mm=None, **base,
                                reason="global registration found no transform", seconds=secs)

    up = np.array([0.0, 1.0, 0.0])

    def tilt(c: Candidate) -> float:
        return float(np.degrees(np.arccos(np.clip(up @ c.transform[:3, :3] @ up, -1.0, 1.0))))

    plausible = [c for c in cands if (c.free_space_excess is None or c.free_space_excess <= max_free_space)
                 and (c.below_plane is None or c.below_plane <= MAX_BELOW_PLANE)
                 and (not expect_flip or tilt(c) >= FLIP_MIN_TILT_DEG)]
    if not plausible:
        best = max(cands, key=lambda c: c.fitness)
        return SegmentAlignment(aligned=False, transform=None, fitness=round(best.fitness, 4),
                                rmse_mm=round(best.rmse_m * 1000, 3), **{**base, "method": "none"},
                                candidates=[c.brief() for c in sorted(cands, key=lambda c: -c.fitness)][:12],
                                reason=f"every candidate puts {max_free_space:.0%} more of its points than a segment's own "
                                       f"cloud ({_pct(base_ref)} for segment 0, {_pct(base_mov)} for this one) where "
                                       f"that segment's cameras saw empty space, or over {MAX_BELOW_PLANE:.0%} of "
                                       f"them under the table"
                                       + (", or keeps the object upright although it was flipped" if expect_flip
                                          else ""),
                                seconds=secs)
    top = max(c.fitness for c in plausible)
    tied = [c for c in plausible if c.fitness >= top - tie_fitness]
    win = max(tied, key=lambda c: (c.fitness, -c.rmse_m))

    def turned(a: Candidate, b: Candidate) -> float:
        return rotation_deg(a.transform[:3, :3] @ b.transform[:3, :3].T)

    tie, undecided = None, False
    rivals = [c for c in tied if turned(c, win) > SAME_POSE_DEG]
    if rivals:   # a symmetric object: poses turned apart fit the shape equally well
        if all(c.colour_distance is not None for c in tied):
            by_colour = min(tied, key=lambda c: c.colour_distance)
            others = [c for c in tied if turned(c, by_colour) > SAME_POSE_DEG]
            runner = min(c.colour_distance for c in others)
            if runner > 0 and by_colour.colour_distance < (1.0 - colour_margin) * runner:
                win = by_colour
                tie = (f"{len(tied)} candidates fit within {tie_fitness:.2f} of the best, {len(others)} of them "
                       f"turned apart; colour decided ({by_colour.colour_distance:.2f} vs {runner:.2f})")
            else:
                tie = (f"{len(tied)} candidates fit within {tie_fitness:.2f} of the best, turned apart; colours do "
                       f"not tell them apart ({by_colour.colour_distance:.2f} vs {runner:.2f}, needs a "
                       f"{colour_margin:.0%} margin)")
                undecided = True
        else:
            tie = f"{len(tied)} candidates turned apart fit equally well and there are no colours to tell them apart"
            undecided = True
        if undecided and init is not None and (init_quality is None or init_quality >= min_phone_quality):
            e = np.asarray(init, float).reshape(4, 4)
            near = [c for c in tied if rotation_deg(c.transform[:3, :3] @ e[:3, :3].T) <= SAME_POSE_DEG]
            if near:   # the phone's estimate picks one of the poses the shape allows; the shape confirms it
                win = max(near, key=lambda c: c.fitness)
                tie += "; the phone's estimate picked one of them and the shape fits it as well as any"
                undecided = False
    phone = None
    if init is not None:
        e = np.asarray(init, float).reshape(4, 4)
        c0 = np.asarray(src.points).mean(axis=0)
        moved = np.linalg.norm((win.transform[:3, :3] @ c0 + win.transform[:3, 3]) - (e[:3, :3] @ c0 + e[:3, 3]))
        phone = {"quality": init_quality,
                 "rotation_error_deg": round(rotation_deg(win.transform[:3, :3] @ e[:3, :3].T), 3),
                 "translation_error_mm": round(float(moved) * 1000, 2),
                 "verdict": "confirmed" if rotation_deg(win.transform[:3, :3] @ e[:3, :3].T) <= PHONE_OK_DEG
                 and moved <= PHONE_OK_M else "corrected"}
    method = f"{win.source} + point-to-plane ICP (Open3D)"
    ok = win.fitness >= min_fitness and win.rmse_m <= max_rmse_voxels * voxel_m
    reason = None
    if not ok:
        reason = (f"best fit too weak: fitness {win.fitness:.2f} (needs >= {min_fitness:.2f}), RMSE "
                  f"{win.rmse_m * 1000:.1f} mm (needs <= {max_rmse_voxels * voxel_m * 1000:.1f} mm)")
    elif undecided:
        ok, reason = False, f"symmetric object: {tie}"
    return SegmentAlignment(aligned=ok, transform=[float(v) for v in win.transform.reshape(-1)] if ok else None,
                            fitness=round(win.fitness, 4), rmse_mm=round(win.rmse_m * 1000, 3), **{**base,
                            "method": method}, phone_estimate=phone, tie_break=tie,
                            candidates=[{**c.brief(), "turned_from_chosen_deg": round(turned(c, win), 1),
                                         "tilt_deg": round(tilt(c), 1)}
                                        for c in sorted(cands, key=lambda c: -c.fitness)][:12],
                            reason=reason,
                            seconds=secs)
