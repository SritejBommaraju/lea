"""Align an object-moved capture segment (the object flipped for its underside) to segment 0, on the server (Linux,
CPU, Open3D only; no Apple frameworks).

    seg0, sup0 = segment_object_cloud(frames0, box0, plane0, voxel_m=v)   # the object alone, segment 0 (+ its table)
    segk, supk = segment_object_cloud(framesk, boxk, planek, voxel_m=v)   # the same for segment k, in its own world
    res = align_clouds(seg0, segk, voxel_m=v, ref_frames=frames0, mov_frames=framesk,
                       ref_plane_y=sup0.height_m, mov_plane_y=supk.height_m, expect_flip=True)

The object alone (segment_object_cloud; measured on four real flips, 2026-10-24, see PLAN.md):
- The table is cut per depth frame along that frame's own plane: in a frame the table is flat to a millimetre but
  tilted 1-4 deg in the world (LiDAR and pose error), so one horizontal cut left a centimetre-thick layer of table in
  the object (30-60% of its points). The cut sits at what the table's noise reaches below its plane, mirrored above
  (nothing real lies under the table, so that side is pure noise).
- Surface from medium-confidence depth too: the sides of dark plastic seen obliquely are confidence 1 (90% of the
  side pixels on a controller); high confidence keeps only the tops, and a flip shares nothing but its sides.
- A voxel needs points from MIN_VIEWS frames (one frame's noise tail does not repeat in the others), and points that
  most high-confidence views see through (CARVE_MARGIN_M; mixed pixels at silhouettes) are carved away.

Registration (Open3D, MIT):
1. Both clouds voxel-downsampled (`voxel_m`), normals facing the cameras that saw them (outward from the centre
   without frames), FPFH features (radius 5 voxels).
2. Starts: the phone's estimate (never trusted as is), feature-matching random-sample consensus (a few seeds), the
   gravity sweep (upright, upside down, the four sides at several headings; one gravity-aligned world per segment),
   the best one turned about its principal axes (symmetric objects), and footprint starts: per resting pose
   (upright, upside down, each side), the headings whose outline seen from above best matches segment 0's
   (cross-correlation of the two top-view silhouettes), put on segment 0's table. A flipped object shares little
   surface with its first scan, but its outline from above is the same.
3. Refinement: point-to-plane ICP, coarse to fine. With both tables known, footprint starts and every other pose that
   makes sense are refined on matches whose normals agree (an underside laid on the top face matches surfaces facing
   opposite ways) while both objects rest on their tables (the points touching a table, or under it, pull onto it).
4. Score per candidate: fitness (share of the moving cloud matched, normals agreeing, within 1.5 voxels), inlier RMSE,
   colour on the matched points, and physical sense, each a reason to drop it:
   - free space: the share of points that the other segment's high-confidence cameras saw as empty, judged against
     the nearest surface within FREE_SPACE_DRIFT_M (ARKit drift moves silhouettes) and by most of the views that could
     see the point, never one; a view where the point lies hidden behind the measured surface says nothing;
   - outside the outline segment 0's cameras saw from above (the free-space margin is blind low over the table);
   - under either table, floating above segment 0's table (the moved object rested on it), a flipped object kept
     upright, or nothing matched at all.
5. Among the acceptable candidates within `tie_fitness` of the best fitness, poses turned apart are told apart by free
   space, then by the outline from above, then by colour (standardised per cloud), then by a confident phone
   estimate; otherwise refused as symmetric.
6. Accepted when fitness >= `min_fitness` and RMSE <= `max_rmse_voxels` voxels; or, both tables known and both
   segments' cameras judging free space, when the pose touches both tables (CONTACT_M) with fitness >=
   MIN_FITNESS_ON_TABLES: a flipped object's sides are a small share of what each segment sees (11-24% on real flips),
   and the two table contacts with the free space pin what that overlap leaves loose. Without depth frames a
   turned-about pose fits a low-overlap flip as well (seen on a real mouse), so only the overlap rule applies.

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
FREE_SPACE_DRIFT_M = 0.01    # empty space is judged against the nearest surface a camera measured within this of the
                             # point's pixel (fx * drift / depth pixels): drift moves silhouettes by about this much
FREE_SPACE_VOTE = 0.5        # a point is in empty space when at least this share of the views that see it say so;
                             # one view seeing through it is drift or a silhouette (own clouds: 42% -> 0% on a real
                             # controller with any-one-view, 0.6% with the vote alone)
FREE_SPACE_MIN_CONFIDENCE = 2   # only high-confidence depth claims a place is empty
SURFACE_MIN_CONFIDENCE = 1   # medium-confidence depth is surface: dark plastic seen obliquely is never high
MIN_VIEWS = 3                # a surface voxel needs points from this many frames (capped at a third of the frames)
CARVE_MARGIN_M = 0.01        # a cloud's own point that most high-confidence views see through by more than this is
CARVE_VOTE = 0.5             # carved away (a mixed pixel at a silhouette; the true surface stops the ray)
SUPPORT_WINDOW_M = 0.03      # a frame's table is fitted among depth points this close to the segment's table height
SUPPORT_MAX_TILT_DEG = 8.0   # a frame's table may lean this far in the world (measured 0.1-3.8 deg on real captures)
SUPPORT_MIN_MARGIN_M = 0.003  # never cut closer to a frame's table than this
SUPPORT_RING_CLEAR = 0.05    # a frame's table is confirmed outside the box footprint grown by this share of each side
SUPPORT_RING_GROW = 0.3      # ... and inside it grown by this share (a ring of table round the object, never its top)
SUPPORT_RING_MIN = 200       # ring points needed to check (or fit) a frame's table; fewer: the fit in the box stands
SUPPORT_TOP_M = 0.006        # a fit this far above the ring's table (median) is a low object's top, not the table
PLANE_MARGIN_M = 0.015       # below the support plane by more than this is impossible (ARKit drifts up to ~1.5 cm)
CONTACT_M = 0.015            # the moved object rested on segment 0's table: its lowest points (2nd percentile) must
                             # come this close to it (the same vertical drift); more is floating
MIN_FITNESS_ON_TABLES = 0.10  # both tables touched: the shared sides may be this small a share (real flips 0.11-0.24)
MIN_MATCHED_ON_TABLES = 150   # ... and at least this many matched points (the phone's FlipAligner minimum)
MAX_OUTSIDE_OUTLINE = 0.02   # segment 0's cameras saw the object's whole outline from above: a pose that puts more than
                             # this share of the moved points outside it (grown by the drift) is dropped; the free-space
                             # margin is blind that low over the table (real right poses: 0.0%, wrong headings 5-21%)
OUTLINE_DECIDES = 0.05       # poses turned apart that fit alike: the one whose outline from above matches segment 0's
                             # by this much more (IoU) wins
FREE_SPACE_DECIDES = 0.01    # poses turned apart that fit alike: the one with 1% fewer of its points in empty space
                             # wins (own clouds sit at 0-0.7% with the vote: noise does not make 1%)
SHARE_TOO_LITTLE = ("turned over, the bottom scan shares too little surface with the main scan to fix its pose (each "
                    "saw a different face and few of the same sides): keep the sides in view in both scans, or lay a "
                    "flat object on an edge")
CONTACT_BAND_M = 0.003       # points this close to (or under) a table touch it: a flat underside is levelled on it
LOW_SHARE = 0.01             # the lowest 1% of a cloud is what rests on its table (a few points on a curved underside)
CONTACT_WEIGHT = 0.1         # the table rows together weigh a tenth of the surface matches
FOOTPRINT_PER_POSE = 2       # footprint starts kept per resting pose (distinct headings)
FOOTPRINT_STEP_DEG = 3.0     # headings tried for the footprint starts
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
    gap0_m: float | None = None    # moved object's lowest points (2nd pct) above segment 0's table, in its world
    gap1_m: float | None = None    # segment 0's lowest points above the segment's table, in the segment's world
    matched: int = 0               # moving points matched (normals agreeing, within 1.5 voxels)
    outline_iou: float | None = None   # top-view silhouettes' overlap (segment 0's world)
    outside_outline: float | None = None   # share of the moved points outside segment 0's silhouette from above
    free_space_judged: int = 0     # segments whose cameras judged this pose's free space (0-2)

    def brief(self) -> dict:
        def r(x, k=4):
            return None if x is None else round(float(x), k)
        return {"source": self.source, "fitness": round(self.fitness, 4), "rmse_mm": round(self.rmse_m * 1000, 3),
                "colour_distance": r(self.colour_distance), "free_space_violation": r(self.free_space),
                "free_space_excess": r(self.free_space_excess), "below_table": r(self.below_plane),
                "gap_to_table_mm": None if self.gap0_m is None else round(self.gap0_m * 1000, 1),
                "gap_to_own_table_mm": None if self.gap1_m is None else round(self.gap1_m * 1000, 1),
                "matched": self.matched, "outline_iou": r(self.outline_iou, 3),
                "outside_outline": r(self.outside_outline, 3)}


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
                      colours: bool = True, min_views: int = 1, views_voxel_m: float | None = None) -> ObjectCloud:
    """World points of the kept (non-zero) depth pixels of every frame, voxel-averaged (Open3D), with colours.
    `min_views`: a point is kept only when at least that many frames put a point in its voxel of `views_voxel_m`
    (default `voxel_m`)."""
    import open3d as o3d

    from repairos.locate.camera import backproject

    pts, cols, fid = [], [], []
    for i, (f, d) in enumerate(zip(frames, depths, strict=True)):
        keep = d > 0
        if not keep.any():
            continue
        p = f.pose.to_world(backproject(d, f.depth_cam)[keep])
        pts.append(p)
        fid.append(np.full(len(p), i))
        c = _frame_colours(f, keep) if colours else None
        cols.append(c if c is not None else np.full((len(p), 3), 128, np.uint8))
    if not pts:
        return ObjectCloud(np.zeros((0, 3)), None)
    p, c = np.concatenate(pts), np.concatenate(cols)
    if min_views > 1:
        key = np.floor(p / (views_voxel_m or voxel_m)).astype(np.int64)
        _, vox = np.unique(key, axis=0, return_inverse=True)
        vox = vox.ravel()
        pairs = np.unique(np.stack([vox, np.concatenate(fid)], 1), axis=0)
        views = np.bincount(pairs[:, 0], minlength=int(vox.max()) + 1)
        keep = views[vox] >= min_views
        p, c = p[keep], c[keep]
        if not len(p):
            return ObjectCloud(np.zeros((0, 3)), None)
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(p))
    pc.colors = o3d.utility.Vector3dVector(c.astype(float) / 255.0)
    pc = pc.voxel_down_sample(voxel_m)
    return ObjectCloud(np.asarray(pc.points), (np.asarray(pc.colors) * 255).round().astype(np.uint8) if colours
                       else None)


# --- the object alone, from one segment's depth ---------------------------------------------------------------------

@dataclass
class Support:
    """The table under the object in one segment's world, from the per-frame fits."""
    height_m: float | None          # median over frames of the fitted table's height under the box centre
    spread_m: float | None = None   # robust spread of that height between frames (vertical drift)
    frames: int = 0                 # frames whose table was fitted
    tilt_deg: float | None = None   # median lean of the frames' tables (LiDAR and pose error)
    cut_mm: float | None = None     # median cut above each frame's table

    def to_dict(self) -> dict:
        def r(x, k):
            return None if x is None else round(float(x), k)
        return {"height_m": r(self.height_m, 4), "spread_mm": None if self.spread_m is None else
                round(self.spread_m * 1000, 2), "frames": self.frames, "tilt_deg": r(self.tilt_deg, 2),
                "cut_mm": r(self.cut_mm, 2)}


def _fit_table(q: np.ndarray, rng: np.random.Generator, tol: float = 0.004, iters: int = 150
               ) -> tuple[np.ndarray, float] | None:
    """A near-horizontal plane n.p + d = 0 (n_y > 0) through most of `q` (random-sample consensus, then least squares
    on the inliers); None when nothing leans less than SUPPORT_MAX_TILT_DEG."""
    if len(q) < 200:
        return None
    if len(q) > 20_000:
        q = q[rng.choice(len(q), 20_000, replace=False)]
    cmin = np.cos(np.radians(SUPPORT_MAX_TILT_DEG))
    best, best_n = 0, None
    for _ in range(iters):
        a = q[rng.choice(len(q), 3, replace=False)]
        n = np.cross(a[1] - a[0], a[2] - a[0])
        nn = np.linalg.norm(n)
        if nn < 1e-12:
            continue
        n = n / nn * (1.0 if n[1] >= 0 else -1.0)
        if n[1] < cmin:
            continue
        k = int((np.abs((q - a[0]) @ n) < tol).sum())
        if k > best:
            best, best_n = k, (n, -float(n @ a[0]))
    if best_n is None:
        return None
    n, d = best_n
    inl = q[np.abs(q @ n + d) < tol]
    c = inl.mean(0)
    _, _, vt = np.linalg.svd(inl - c, full_matrices=False)
    n = vt[2] * (1.0 if vt[2][1] >= 0 else -1.0)
    if n[1] < cmin:
        return None
    return n, -float(n @ c)


def _confirmed_table(near: np.ndarray, ring: np.ndarray, rng: np.random.Generator) -> tuple[np.ndarray, float] | None:
    """A frame's table near the measured height (`near`: points in the box), checked against the ring of table round
    the object (`ring`: points outside its footprint): seen from above, a low object's top (a remote, a phone, a
    board) can outnumber the table inside the box, and a fit to it would cut the object away. When the frame sees
    enough ring and the ring's own table lies SUPPORT_TOP_M or more below the fit, the fit was that top and the ring's
    table is used instead; with too little ring to tell, the fit stands."""
    fit = _fit_table(near, rng)
    if fit is None or len(ring) < SUPPORT_RING_MIN:
        return fit   # no fit: the measured table is cut, as before
    around = _fit_table(ring, rng)
    if around is None:
        return fit
    n0, d0 = around
    table = ring[np.abs(ring @ n0 + d0) < 0.004]   # the ring's table points (not other things beside the object)
    if not len(table) or float(np.median(table @ fit[0] + fit[1])) >= -SUPPORT_TOP_M:
        return fit   # the ring's table is not below the fit (a cushion sags, a table leans a little: still the fit)
    return around


def segment_object_cloud(frames: Sequence[Any], box: Any, plane_y: float | None, *, voxel_m: float,
                         colours: bool = True) -> tuple[ObjectCloud, Support]:
    """The object alone from one segment's depth frames: in the box, above each frame's own table (fitted near
    `plane_y`, cut at the table noise mirrored from below the plane), medium confidence or better, seen by MIN_VIEWS
    frames, with points most high-confidence views see through carved away. `plane_y` None (a hand-held capture
    in the object's frame) cuts no table. Returns the cloud and the table it measured (`Support`)."""
    depths, sup = segment_object_depths(frames, box, plane_y)
    views = max(1, min(MIN_VIEWS, len(frames) // 3))
    # Two frames' samples of one surface land within about two pixel footprints of each other.
    foot = [float(np.median(d[d > 0])) / f.depth_cam.fx for f, d in zip(frames, depths, strict=True) if (d > 0).any()]
    cloud = cloud_from_frames(frames, depths, voxel_m=voxel_m, colours=colours, min_views=views,
                              views_voxel_m=max(voxel_m, 2.0 * float(np.median(foot))) if foot else None)
    if len(cloud) and frames:   # carve what most views that could see a point see through (one pixel of rounding)
        seen, empty = FreeSpace(frames, max_frames=40, drift_m=0.0, min_window_px=1).judge(
            cloud.points, margin_m=CARVE_MARGIN_M, vote=CARVE_VOTE)
        k = ~empty
        cloud = ObjectCloud(cloud.points[k], None if cloud.colours is None else cloud.colours[k])
    return cloud, sup


def segment_object_depths(frames: Sequence[Any], box: Any, plane_y: float | None, *,
                          min_confidence: int = SURFACE_MIN_CONFIDENCE, edge_px: int = 0
                          ) -> tuple[list[np.ndarray], Support]:
    """Per frame, the depth kept for the object: `min_confidence` or better, in the box, above that frame's own
    table (fitted near `plane_y` and checked against the table round the object, `_confirmed_table`; cut at the
    table's noise below its plane, mirrored). `edge_px`: below-high-confidence
    pixels within this many pixels of a dropped one are dropped too (mixed pixels at the object's silhouette, where
    a pixel's depth blends the object and the table behind it). Returns the depths and the measured table."""
    from repairos.locate.camera import backproject

    rng = np.random.default_rng(0)
    depths, heights, tilts, cuts = [], [], [], []
    c = box.centre
    for f in frames:
        pts = f.pose.to_world(backproject(f.depth_m, f.depth_cam))
        valid = np.isfinite(f.depth_m) & (f.depth_m > 0)
        conf = f.confidence
        surface = valid if conf is None else valid & (conf >= min_confidence)
        sure = valid if conf is None else valid & (conf >= FREE_SPACE_MIN_CONFIDENCE)
        a = np.abs(box.local(pts))
        inbox = np.all(a <= 0.52, axis=-1)
        keep = surface & inbox
        if plane_y is not None:
            band = sure & (np.abs(pts[..., 1] - plane_y) < SUPPORT_WINDOW_M)
            near = band & inbox
            ring = band & ((a[..., 0] > 0.5 + SUPPORT_RING_CLEAR) | (a[..., 2] > 0.5 + SUPPORT_RING_CLEAR)) & \
                (a[..., 0] <= 0.5 + SUPPORT_RING_GROW) & (a[..., 2] <= 0.5 + SUPPORT_RING_GROW)
            fit = _confirmed_table(pts[near], pts[ring], rng)
            if fit is not None:
                n, d = fit
                h = pts @ n + d
                hn = h[near]
                under = -hn[(hn < 0) & (hn > -SUPPORT_WINDOW_M)]
                cut = max(float(np.percentile(under, 99.5)) if len(under) >= 100 else 3 * float(np.std(hn)),
                          SUPPORT_MIN_MARGIN_M)
                keep &= h > cut
                heights.append(-(n[0] * c[0] + n[2] * c[2] + d) / n[1])
                tilts.append(float(np.degrees(np.arccos(np.clip(n[1], -1, 1)))))
                cuts.append(cut)
            else:
                keep &= pts[..., 1] > plane_y + SUPPORT_MIN_MARGIN_M
        if edge_px and conf is not None:
            near_dropped = _window_min(keep.astype(np.int8), edge_px) == 0
            keep &= ~(near_dropped & (conf < FREE_SPACE_MIN_CONFIDENCE))
        depths.append(np.where(keep, f.depth_m, 0.0))
    if heights:
        hs = np.asarray(heights)
        sup = Support(float(np.median(hs)), float(1.4826 * np.median(np.abs(hs - np.median(hs)))), len(hs),
                      float(np.median(tilts)), float(np.median(cuts)) * 1000)
    else:
        sup = Support(plane_y)
    return depths, sup


def table_height_from_depth(frames: Sequence[Any], box: Any) -> float | None:
    """The lowest well-supported horizontal layer of high-confidence depth inside the box: the fallback when no table
    is seen around the box (a stool or cushion smaller than the box)."""
    from repairos.locate.camera import backproject
    from repairos.recon.tsdf import find_support_plane

    pts = []
    for f in list(frames)[:: max(1, len(frames) // 12)]:
        p = f.pose.to_world(backproject(f.depth_m, f.depth_cam))
        ok = np.isfinite(f.depth_m) & (f.depth_m > 0) & box.contains(p, 0.0)
        if f.confidence is not None:
            ok &= f.confidence >= FREE_SPACE_MIN_CONFIDENCE
        pts.append(p[ok])
    pts = np.concatenate(pts) if pts else np.zeros((0, 3))
    plane = find_support_plane(pts) if len(pts) >= 200 else None
    return plane.height_m if plane else None


def segment_frames(pkg_local: Mapping[str, Any], segment: int) -> list[Any]:
    """DepthFrames of one segment's keyframes (file fields are absolute local paths)."""
    from pathlib import Path

    from repairos.recon.tsdf import frames_from_scan_package

    kfs = [kf for kf in pkg_local.get("keyframes") or [] if int(kf.get("segment") or 0) == segment]
    return frames_from_scan_package({**pkg_local, "keyframes": kfs}, Path("/"))


# --- registration ---------------------------------------------------------------------------------------------------

def camera_centres(frames: Sequence[Any] | None, n: int = 24) -> np.ndarray | None:
    if not frames:
        return None
    cams = np.array([f.pose.to_world(np.zeros((1, 3)))[0] for f in frames])
    return cams[np.linspace(0, len(cams) - 1, min(n, len(cams))).astype(int)]


def _o3d_cloud(c: ObjectCloud, voxel: float, max_points: int, cams: np.ndarray | None = None):
    """Voxel-downsampled Open3D cloud with normals: facing the cameras that saw each point (a vote over `cams`; a
    visible surface faces its cameras), or outward from the centre without cameras."""
    import open3d as o3d

    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.asarray(c.points, float)))
    if c.colours is not None:
        pc.colors = o3d.utility.Vector3dVector(np.asarray(c.colours, float) / 255.0)
    pc = pc.voxel_down_sample(voxel)
    if len(pc.points) > max_points:
        pc = pc.random_down_sample(max_points / len(pc.points))
    pc.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=2.5 * voxel, max_nn=30))
    p, n = np.asarray(pc.points), np.asarray(pc.normals)
    if cams is not None and len(cams):
        vote = np.zeros(len(p))
        for cam in cams:
            vote += np.sign(np.einsum("ij,ij->i", n, cam - p))
        n = n * np.where(vote >= 0, 1.0, -1.0)[:, None]
    else:
        n = n * np.where(np.einsum("ij,ij->i", n, p - p.mean(axis=0)) >= 0, 1.0, -1.0)[:, None]
    pc.normals = o3d.utility.Vector3dVector(n)
    return pc


class _Nearest:
    """Nearest target point for many queries (Open3D's tensor KNN, built once)."""

    def __init__(self, pts: np.ndarray):
        import open3d as o3d

        self.o3d = o3d
        self.nns = o3d.core.nns.NearestNeighborSearch(o3d.core.Tensor(np.ascontiguousarray(pts, np.float64)))
        self.nns.knn_index()

    def __call__(self, q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        idx, d2 = self.nns.knn_search(self.o3d.core.Tensor(np.ascontiguousarray(q, np.float64)), 1)
        return idx.numpy()[:, 0].astype(int), np.sqrt(np.maximum(d2.numpy()[:, 0], 0.0))


COS_AGREE = 0.5   # matched normals within 60 deg (a face matched to the opposite face is ~180 deg)


def _matches(src_p, src_n, tgt_p, tgt_n, nearest: _Nearest, t: np.ndarray, dist: float):
    """(source index, target index) of the moving points within `dist` of segment 0 whose normals agree."""
    p = src_p @ t[:3, :3].T + t[:3, 3]
    j, d = nearest(p)
    ok = d <= dist
    i = np.nonzero(ok)[0]
    j = j[ok]
    agree = np.einsum("ij,ij->i", src_n[i] @ t[:3, :3].T, tgt_n[j]) > COS_AGREE
    return i[agree], j[agree]


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


@dataclass
class _Geometry:
    """One registration problem's clouds as arrays (points, normals, standardised colours) and the KNN on segment 0."""
    S: np.ndarray
    Sn: np.ndarray
    T: np.ndarray
    Tn: np.ndarray
    src_col: np.ndarray | None
    tgt_col: np.ndarray | None
    nearest: _Nearest


def _score(g: _Geometry, t: np.ndarray, voxel: float) -> tuple[float, float, float | None, int]:
    """(fitness, inlier RMSE, colour distance, matched) of transform t (src -> tgt) at 1.5 voxels, counting only
    matches whose normals agree."""
    i, j = _matches(g.S, g.Sn, g.T, g.Tn, g.nearest, t, 1.5 * voxel)
    if not len(i):
        return 0.0, float("inf"), None, 0
    d = np.linalg.norm(g.S[i] @ t[:3, :3].T + t[:3, 3] - g.T[j], axis=1)
    colour = None
    if g.src_col is not None and g.tgt_col is not None and len(i) >= 20:
        colour = float(np.mean(np.linalg.norm(g.src_col[i] - g.tgt_col[j], axis=1)))
    return len(i) / len(g.S), float(np.sqrt(np.mean(d ** 2))), colour, int(len(i))


def _small_rigid(x: np.ndarray) -> np.ndarray:
    """4x4 from a small twist (wx, wy, wz, tx, ty, tz), its rotation re-orthonormalised."""
    wx, wy, wz = x[:3]
    r = np.array([[1, -wz, wy], [wz, 1, -wx], [-wy, wx, 1]])
    u, _, vt = np.linalg.svd(r)
    m = np.eye(4)
    m[:3, :3] = u @ vt
    m[:3, 3] = x[3:]
    return m


def _icp_on_tables(g: _Geometry, start: np.ndarray, voxel: float, ref_plane_y: float | None,
                   mov_plane_y: float | None, schedule=(4.0, 3.0, 2.0, 1.5, 1.0), iters: int = 15,
                   low_share: float | None = None, contact_weight: float | None = None) -> np.ndarray:
    """Point-to-plane ICP on matches whose normals agree, plus each segment's table (when known): the moving cloud's
    lowest points (in segment 0's world) touch segment 0's table and none go below it; segment 0's cloud in the
    segment's world on the segment's table. One-sided: only points under a table or among the lowest `low_share`
    pull. The table rows weigh `contact_weight` of the surface matches together."""
    low_share = LOW_SHARE if low_share is None else low_share
    contact_weight = CONTACT_WEIGHT if contact_weight is None else contact_weight
    t = np.asarray(start, float).copy()
    up = np.array([0.0, 1.0, 0.0])
    for f in schedule:
        for _ in range(iters):
            rows, rhs, wts = [], [], []
            i, j = _matches(g.S, g.Sn, g.T, g.Tn, g.nearest, t, f * voxel)
            if len(i) >= 6:
                p = g.S[i] @ t[:3, :3].T + t[:3, 3]
                q, qn = g.T[j], g.Tn[j]
                b = np.einsum("ij,ij->i", q - p, qn)
                rows.append(np.hstack([np.cross(p, qn), qn]))
                rhs.append(b)
                wts.append(np.where(np.abs(b) < voxel, 1.0, voxel / np.maximum(np.abs(b), 1e-9)))   # Huber
            n_surf = max(1, len(i))
            if ref_plane_y is not None and mov_plane_y is not None:
                p = g.S @ t[:3, :3].T + t[:3, 3]
                k = max(5, int(low_share * len(p)))
                sel = np.union1d(np.argsort(p[:, 1])[:k], np.nonzero(p[:, 1] < ref_plane_y + CONTACT_BAND_M)[0])
                p = p[sel]
                z, o = np.zeros(len(p)), np.ones(len(p))
                rows.append(np.stack([-p[:, 2], z, p[:, 0], z, o, z], 1))
                rhs.append(ref_plane_y - p[:, 1])
                wts.append(np.full(len(p), np.sqrt(contact_weight * n_surf / len(p))))
                ti = np.linalg.inv(t)
                q = g.T @ ti[:3, :3].T + ti[:3, 3]
                k = max(5, int(low_share * len(q)))
                sel = np.union1d(np.argsort(q[:, 1])[:k], np.nonzero(q[:, 1] < mov_plane_y + CONTACT_BAND_M)[0])
                gy, r0 = t[:3, :3] @ up, g.T[sel]     # d(T^-1 r)_y = -(r x gy).w - gy.dt
                rows.append(np.hstack([-np.cross(r0, gy), -np.tile(gy, (len(r0), 1))]))
                rhs.append(mov_plane_y - q[sel, 1])
                wts.append(np.full(len(r0), np.sqrt(contact_weight * n_surf / len(r0))))
            if not rows or sum(len(x) for x in rhs) < 6:
                return t
            a, b, w = np.vstack(rows), np.concatenate(rhs), np.concatenate(wts)
            x = np.linalg.lstsq(a * w[:, None], b * w, rcond=None)[0]
            t = _small_rigid(x) @ t
            if np.linalg.norm(x) < 1e-5:
                break
    return t


def _outline(a: np.ndarray, b: np.ndarray, cell: float) -> tuple[float, float]:
    """Two clouds' silhouettes seen from above (x, z occupancy at `cell`, small holes closed), on a grid around `b`:
    their overlap (IoU), and the share of `a`'s points outside `b`'s silhouette grown by FREE_SPACE_DRIFT_M (points
    beyond the grid count as outside)."""
    lo = np.percentile(b[:, [0, 2]], 1, axis=0) - 0.03
    hi = np.percentile(b[:, [0, 2]], 99, axis=0) + 0.03
    shape = tuple(np.minimum(np.ceil((hi - lo) / cell).astype(int), 400))
    ga, gb = _occupancy(a[:, [0, 2]], lo, shape, cell), _occupancy(b[:, [0, 2]], lo, shape, cell)
    union = (ga | gb).sum()
    grown = -_window_min(-gb.astype(np.int8), int(np.ceil(FREE_SPACE_DRIFT_M / cell))) > 0
    ij = np.floor((a[:, [0, 2]] - lo) / cell).astype(int)
    inside = np.all((ij >= 0) & (ij < np.asarray(shape)), axis=1)
    inside[inside] = grown[ij[inside, 0], ij[inside, 1]]
    return (float((ga & gb).sum() / union) if union else 0.0), float(1.0 - inside.mean()) if len(a) else 0.0


def _window_min(a: np.ndarray, r: int) -> np.ndarray:
    """Minimum over a (2r+1)^2 window (edges repeat)."""
    if r <= 0:
        return a
    from numpy.lib.stride_tricks import sliding_window_view

    b = np.pad(a, r, mode="edge")
    b = sliding_window_view(b, 2 * r + 1, axis=0).min(axis=-1)
    return sliding_window_view(b, 2 * r + 1, axis=1).min(axis=-1)


def _occupancy(xz: np.ndarray, lo: np.ndarray, shape: tuple[int, int], cell: float) -> np.ndarray:
    ij = np.floor((xz - lo) / cell).astype(int)
    ok = np.all((ij >= 0) & (ij < np.asarray(shape)), axis=1)
    g = np.zeros(shape, np.int8)
    g[ij[ok, 0], ij[ok, 1]] = 1
    grown = -_window_min(-g, 2)              # close holes up to ~4 cells (a sparse oblique view): grow,
    return _window_min(grown, 2) > 0         # then shrink back


_RESTING = {"upright": np.eye(3), "flipped": np.diag([1.0, -1.0, -1.0]),
            "side_px": np.array([[1.0, 0, 0], [0, 0, -1], [0, 1, 0]]),
            "side_nx": np.array([[1.0, 0, 0], [0, 0, 1], [0, -1, 0]]),
            "side_pz": np.array([[0, -1.0, 0], [1, 0, 0], [0, 0, 1]]),
            "side_nz": np.array([[0, 1.0, 0], [-1, 0, 0], [0, 0, 1]])}


def _footprint_starts(src_pts: np.ndarray, tgt_pts: np.ndarray, *, cell: float, tgt_plane_y: float,
                      per_pose: int = FOOTPRINT_PER_POSE, step_deg: float = FOOTPRINT_STEP_DEG
                      ) -> list[tuple[str, np.ndarray]]:
    """Starts whose outline from above matches segment 0's: per resting pose (upright, upside down, each side), the
    headings and shift that best overlap the two top-view silhouettes (FFT cross-correlation), the moved object put
    on segment 0's table (its lowest points at `tgt_plane_y`). `per_pose` distinct headings (20 deg apart) each."""
    lo_t, hi_t = np.percentile(tgt_pts, 1, axis=0), np.percentile(tgt_pts, 99, axis=0)
    lo_s, hi_s = np.percentile(src_pts, 1, axis=0), np.percentile(src_pts, 99, axis=0)
    src = src_pts[np.all((src_pts >= lo_s - 0.01) & (src_pts <= hi_s + 0.01), axis=1)]
    span = max(float(np.max(hi_t - lo_t)), float(np.max(hi_s - lo_s))) * 2.2 + 0.04
    n = int(min(256, 2 ** np.ceil(np.log2(max(16.0, span / cell)))))
    cell = max(cell, span / n)
    centre = (lo_t + hi_t) / 2
    lo = centre[[0, 2]] - n * cell / 2
    a = _occupancy(tgt_pts[:, [0, 2]], lo, (n, n), cell).astype(float)
    fa = np.fft.rfft2(a)
    yaw = lambda r: np.array([[np.cos(r), 0, np.sin(r)], [0, 1, 0], [-np.sin(r), 0, np.cos(r)]])   # noqa: E731
    out = []
    for name, base in _RESTING.items():
        scored = []
        for deg in np.arange(0.0, 360.0, step_deg):
            r = yaw(np.radians(deg)) @ base
            q = src @ r.T
            shift0 = centre - (np.percentile(q, 1, axis=0) + np.percentile(q, 99, axis=0)) / 2
            b = _occupancy((q + shift0)[:, [0, 2]], lo, (n, n), cell)
            if not b.any():
                continue
            x = np.fft.irfft2(fa * np.conj(np.fft.rfft2(b.astype(float))), s=(n, n))
            k = np.unravel_index(int(np.argmax(x)), x.shape)
            inter = float(x[k])
            iou = inter / max(1.0, a.sum() + b.sum() - inter)
            d = np.array([k[0] if k[0] < n // 2 else k[0] - n, k[1] if k[1] < n // 2 else k[1] - n]) * cell
            m = np.eye(4)
            m[:3, :3] = r
            m[:3, 3] = shift0 + np.array([d[0], 0.0, d[1]])
            m[1, 3] += tgt_plane_y - float(np.percentile((src @ m[:3, :3].T + m[:3, 3])[:, 1], 1))
            scored.append((iou, deg, m))
        scored.sort(key=lambda s: -s[0])
        kept: list[tuple[float, float, np.ndarray]] = []
        for s in scored:
            if all(min(abs(s[1] - k[1]), 360 - abs(s[1] - k[1])) >= 20 for k in kept):
                kept.append(s)
            if len(kept) >= per_pose:
                break
        out += [(f"footprint_{name}_{round(deg)}", m) for _, deg, m in kept]
    return out


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


class FreeSpace:
    """Where one segment's cameras saw empty space, for many queries (the per-frame windows are built once)."""

    def __init__(self, frames: Sequence[Any], *, max_frames: int = 24, min_confidence: int = FREE_SPACE_MIN_CONFIDENCE,
                 drift_m: float = FREE_SPACE_DRIFT_M, min_window_px: int = 0):
        self.views = []
        for f in list(frames)[:: max(1, len(frames) // max_frames)]:
            valid = np.isfinite(f.depth_m) & (f.depth_m > 0)
            if not valid.any():
                continue
            r = int(np.ceil(f.depth_cam.fx * drift_m / max(float(np.median(f.depth_m[valid])), 0.1))) if drift_m else 0
            r = max(r, min_window_px)
            near = _window_min(np.where(valid, f.depth_m, np.inf), r)   # nearest surface around, any confidence
            sure = valid if f.confidence is None else valid & (f.confidence >= min_confidence)
            # the pixel itself must be sure of its depth; far: the pixel's own depth (is the point hidden behind it?)
            self.views.append((f, np.where(sure, near, -1.0), np.where(valid, f.depth_m, np.inf)))

    def violation(self, points: np.ndarray, *, margin_m: float, vote: float = FREE_SPACE_VOTE) -> float | None:
        """Share of `points` (the frames' world) in empty space (`judge`). None when no view sees any."""
        if not len(points) or not self.views:
            return None
        s, empty = self.judge(points, margin_m=margin_m, vote=vote)
        return float(empty[s].mean()) if s.any() else None

    def judge(self, points: np.ndarray, *, margin_m: float, vote: float = FREE_SPACE_VOTE
              ) -> tuple[np.ndarray, np.ndarray]:
        """Per point: whether any view could see it, and whether it is in empty space: in front of the nearest
        measured surface by more than `margin_m` in at least `vote` of the views that could see it (and at least one).
        A view where the point lies hidden behind the measured surface says nothing about it and is not counted."""
        from repairos.locate.camera import project

        seen = np.zeros(len(points))
        bad = np.zeros(len(points))
        for f, near, far in self.views:
            h, w = near.shape
            uv, z = project(points, f.pose, f.depth_cam)
            ins = (z > 0.05) & (uv[:, 0] >= 0) & (uv[:, 0] < w) & (uv[:, 1] >= 0) & (uv[:, 1] < h)
            idx = np.nonzero(ins)[0]
            i, j = uv[idx, 1].astype(int), uv[idx, 0].astype(int)
            d = near[i, j]
            ok = (d > 0) & (z[idx] <= far[i, j] + margin_m)   # sure pixel, and the point not hidden behind it
            idx, d = idx[ok], d[ok]
            seen[idx] += 1
            bad[idx] += z[idx] < d - margin_m
        return seen > 0, (seen > 0) & (bad >= np.maximum(1.0, vote * seen))


def free_space_violation(points: np.ndarray, frames: Sequence[Any], *, margin_m: float, max_frames: int = 24,
                         min_confidence: int = FREE_SPACE_MIN_CONFIDENCE, drift_m: float = FREE_SPACE_DRIFT_M,
                         vote: float = FREE_SPACE_VOTE) -> float | None:
    """Share of `points` (in the frames' world) that the frames saw as empty space: in front of the nearest measured
    surface within `drift_m` of the point's pixel by more than `margin_m`, in at least `vote` of the views that see
    it; only high-confidence pixels claim emptiness. A right alignment puts the moved segment's surface on or behind
    what segment 0's cameras measured; a wrong one floats points in front of it. None when no view sees any point.
    (drift_m=0, vote=0: the raw test, any one frame.)"""
    if not frames or not len(points):
        return None
    return FreeSpace(frames, max_frames=max_frames, min_confidence=min_confidence,
                     drift_m=drift_m).violation(points, margin_m=margin_m, vote=vote)


def below_plane(points: np.ndarray, plane_y: float, margin_m: float) -> float:
    """Share of points more than `margin_m` below the support plane y = plane_y (world Y up)."""
    return float((np.asarray(points)[:, 1] < plane_y - margin_m).mean()) if len(points) else 0.0


def _gravity_starts(src_pts: np.ndarray, tgt_pts: np.ndarray, steps: int = 12, src_plane_y: float | None = None,
                    tgt_plane_y: float | None = None, side_steps: int = 6) -> list[tuple[str, np.ndarray]]:
    """Starts for an object moved on the table, in one gravity-aligned (Y up) world: upright or upside down (180 deg
    about a horizontal axis), at `steps` headings, centre on centre across the table. Global registration can miss on
    a plain, symmetric object; these cover every resting pose on the same face, the opposite one or a side.
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
    # On one of its four sides (90 deg about a horizontal axis): many things cannot rest upside down (a mouse rocks,
    # a controller stands on its triggers), so the person lays them on a side. Centre on centre, fewer headings;
    # ICP and the free-space / under-the-table checks decide as for the others.
    sides = {k: v for k, v in _RESTING.items() if k.startswith("side_")}
    for name, base in sides.items():
        for k in range(side_steps):
            a = 2 * np.pi * k / side_steps
            yaw = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])
            r = yaw @ base
            m = np.eye(4)
            m[:3, :3] = r
            m[:3, 3] = ct - r @ cs
            out.append((f"gravity_{name}_{round(np.degrees(a))}", m))
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
    tgt = _o3d_cloud(ref, voxel_m, max_points, camera_centres(ref_frames))
    src = _o3d_cloud(mov, voxel_m, max_points, camera_centres(mov_frames))
    base["points"].update(segment0_used=len(tgt.points), segment_used=len(src.points))
    src_col = _standardised(np.asarray(src.colors) * 255) if src.has_colors() else None
    tgt_col = _standardised(np.asarray(tgt.colors) * 255) if tgt.has_colors() else None
    src_pts, tgt_pts = np.asarray(src.points), np.asarray(tgt.points)
    geo = _Geometry(src_pts, np.asarray(src.normals), tgt_pts, np.asarray(tgt.normals), src_col, tgt_col,
                    _Nearest(tgt_pts))
    free0 = FreeSpace(ref_frames) if ref_frames else None
    free1 = FreeSpace(mov_frames) if mov_frames else None
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
    tables = ref_plane_y is not None and mov_plane_y is not None
    # Each segment's cameras are calibrated by their own cloud: the share of it they see as empty space is their
    # drift and noise (ARKit drifts a centimetre or two over one capture). A candidate is judged per segment against
    # that segment's own baseline, so a drifting segment does not hide what the steadier one sees.
    base_ref = free0.violation(tgt_pts, margin_m=margin) if free0 else None
    base_mov = free1.violation(src_pts, margin_m=margin) if free1 else None

    def evaluate(name: str, start: np.ndarray, schedule=(3.0, 1.5, 1.0), on_tables: bool = False) -> Candidate:
        if on_tables:
            t = _icp_on_tables(geo, start, voxel_m, ref_plane_y, mov_plane_y, schedule)
        else:
            t = _icp(src, tgt, start, voxel_m, schedule)
        f, e, c, matched = _score(geo, t, voxel_m)
        ti = np.linalg.inv(t)
        moved, back = src_pts @ t[:3, :3].T + t[:3, 3], tgt_pts @ ti[:3, :3].T + ti[:3, 3]
        fs_parts = []   # (violation, that camera set's own baseline)
        if free0:
            fs_parts.append((free0.violation(moved, margin_m=margin), base_ref))
        if free1:
            fs_parts.append((free1.violation(back, margin_m=margin), base_mov))
        below = []
        if ref_plane_y is not None:
            below.append(below_plane(moved, ref_plane_y, plane_margin))
        if mov_plane_y is not None:
            below.append(below_plane(back, mov_plane_y, plane_margin))
        fs_parts = [(x, b or 0.0) for x, b in fs_parts if x is not None]
        cand = Candidate(name, t, f, e, c, max(x for x, _ in fs_parts) if fs_parts else None,
                         max(below) if below else None,
                         max(x - b for x, b in fs_parts) if fs_parts else None,
                         None if ref_plane_y is None else float(np.percentile(moved[:, 1], 2)) - ref_plane_y,
                         None if mov_plane_y is None else float(np.percentile(back[:, 1], 2)) - mov_plane_y,
                         matched, *_outline(moved, tgt_pts, max(voxel_m, 0.003)), free_space_judged=len(fs_parts))
        cands.append(cand)
        return cand

    for name, start in starts:
        evaluate(name, start)
    if gravity_sweep:   # same ARKit world, Y up: the object lies upright or upside down, at some heading
        for name, start in _gravity_starts(src_pts, tgt_pts, src_plane_y=mov_plane_y, tgt_plane_y=ref_plane_y):
            evaluate(name, start, (10.0, 3.0, 1.5, 1.0))
        if ref_plane_y is not None:   # the outline from above, on segment 0's table (kept there when both are known)
            for name, start in _footprint_starts(src_pts, tgt_pts, cell=max(voxel_m, 0.003), tgt_plane_y=ref_plane_y):
                evaluate(name, start, (4.0, 3.0, 2.0, 1.5, 1.0), on_tables=tables)
    up = np.array([0.0, 1.0, 0.0])

    def tilt(c: Candidate) -> float:
        return float(np.degrees(np.arccos(np.clip(up @ c.transform[:3, :3] @ up, -1.0, 1.0))))

    def faults(c: Candidate) -> list[str]:
        out = []
        if c.matched == 0:
            out.append("matches nothing")
        if c.free_space_excess is not None and c.free_space_excess > max_free_space:
            out.append("empty space")
        if c.below_plane is not None and c.below_plane > MAX_BELOW_PLANE:
            out.append("under the table")
        if c.gap0_m is not None and c.gap0_m > CONTACT_M:
            out.append("floating")
        if c.outside_outline is not None and c.outside_outline > MAX_OUTSIDE_OUTLINE:
            out.append("outside the outline")
        if expect_flip and tilt(c) < FLIP_MIN_TILT_DEG:
            out.append("upright")
        return out

    if cands:
        best = max(cands, key=lambda c: c.fitness)
        for axis, start in _flips(best.transform, np.asarray(tgt.points)):
            evaluate(f"{best.source}+flip_{axis}", start)
    if tables:   # every other pose that makes sense so far is put down on both tables (side bands leave height loose)
        for c in [c for c in cands if not c.source.startswith("footprint_") and not faults(c)]:
            cands.remove(c)
            evaluate(f"{c.source}+on_tables", c.transform, (1.5, 1.0), on_tables=True)
    secs["refine_s"] = time.perf_counter() - t2
    secs["total_s"] = time.perf_counter() - t0
    secs = {k: round(v, 3) for k, v in secs.items()}
    if not cands:
        return SegmentAlignment(aligned=False, transform=None, fitness=None, rmse_mm=None, **base,
                                reason="global registration found no transform", seconds=secs)

    plausible = [c for c in cands if not faults(c)]
    if not plausible:
        best = max(cands, key=lambda c: c.fitness)
        counts = {k: sum(k in faults(c) for c in cands) for k in
                  ("empty space", "under the table", "floating", "outside the outline", "upright", "matches nothing")}
        return SegmentAlignment(aligned=False, transform=None, fitness=round(best.fitness, 4),
                                rmse_mm=round(best.rmse_m * 1000, 3), **{**base, "method": "none"},
                                candidates=[{**c.brief(), "tilt_deg": round(tilt(c), 1), "faults": faults(c)}
                                            for c in sorted(cands, key=lambda c: -c.fitness)][:12],
                                reason=f"no candidate pose makes physical sense ({len(cands)} tried): "
                                       f"{counts['empty space']} put {max_free_space:.0%} more of their points than a "
                                       f"segment's own cloud ({_pct(base_ref)} for segment 0, {_pct(base_mov)} for "
                                       f"this one) where that segment's cameras saw empty space, "
                                       f"{counts['under the table']} put over {MAX_BELOW_PLANE:.0%} of them under a "
                                       f"table, {counts['floating']} leave the moved object floating over "
                                       f"{CONTACT_M * 1000:.0f} mm above the table it rested on, "
                                       f"{counts['outside the outline']} put over {MAX_OUTSIDE_OUTLINE:.0%} of it "
                                       f"outside the outline segment 0's cameras saw from above"
                                       + (f", {counts['upright']} keep it upright although it was flipped"
                                          if expect_flip else "")
                                       + f", {counts['matches nothing']} match no surface"
                                       + (f"; {SHARE_TOO_LITTLE}" if max((c.fitness for c in cands if
                                          not (expect_flip and tilt(c) < FLIP_MIN_TILT_DEG)), default=0.0)
                                          < MIN_FITNESS_ON_TABLES else ""),
                                seconds=secs)
    rmse_cap = max_rmse_voxels * voxel_m

    def touches(c: Candidate) -> bool:
        return (tables and c.gap0_m is not None and c.gap1_m is not None and abs(c.gap0_m) <= CONTACT_M
                and abs(c.gap1_m) <= CONTACT_M)

    def acceptable(c: Candidate) -> bool:
        if c.rmse_m > rmse_cap:
            return False
        # A small overlap is enough only with both tables touched and both segments' cameras judging the free space:
        # without depth frames a turned-about pose fits the shape and the tables as well (measured on a real flip).
        return c.fitness >= min_fitness or (touches(c) and c.free_space_judged == 2
                                            and c.fitness >= MIN_FITNESS_ON_TABLES
                                            and c.matched >= MIN_MATCHED_ON_TABLES)

    top = max(c.fitness for c in plausible)
    win = max(plausible, key=lambda c: (c.fitness, -c.rmse_m))
    # Rivals are the poses the data would also accept; with no acceptable pose the best plausible one is reported.
    tied = [c for c in plausible if acceptable(c) and c.fitness >= top - tie_fitness] if acceptable(win) else [win]

    def turned(a: Candidate, b: Candidate) -> float:
        return rotation_deg(a.transform[:3, :3] @ b.transform[:3, :3].T)

    tie, undecided = None, False
    rivals = [c for c in tied if turned(c, win) > SAME_POSE_DEG]
    def excess(c: Candidate) -> float:
        return c.free_space_excess or 0.0

    def outline(c: Candidate) -> float:
        return c.outline_iou or 0.0

    def clearly_best(key, margin: float, lower: bool) -> Candidate | None:
        """The tied candidate that beats every candidate turned away from it by `margin` on `key`, if any."""
        best = (min if lower else max)(tied, key=key)
        away = [c for c in tied if turned(c, best) > SAME_POSE_DEG]
        if away and all((key(c) - key(best) if lower else key(best) - key(c)) >= margin for c in away):
            return best
        return None

    by_space = clearly_best(excess, FREE_SPACE_DECIDES, lower=True) if rivals else None
    by_outline = clearly_best(outline, OUTLINE_DECIDES, lower=False) if rivals and not by_space else None
    if by_space:   # every other pose puts points where the cameras saw empty space
        win = by_space
        tie = (f"{len(tied)} candidates fit within {tie_fitness:.2f} of the best, turned apart; free space decided "
               f"(this one puts {excess(win):.1%} of its points where the cameras saw empty space, the others "
               f"{min(excess(c) for c in tied if turned(c, win) > SAME_POSE_DEG):.1%} or more)")
    elif by_outline:   # its outline from above matches segment 0's clearly better than any other pose
        win = by_outline
        tie = (f"{len(tied)} candidates fit within {tie_fitness:.2f} of the best, turned apart; the outline from above "
               f"decided (IoU {outline(win):.2f} vs "
               f"{max(outline(c) for c in tied if turned(c, win) > SAME_POSE_DEG):.2f})")
    elif rivals:   # a symmetric object: poses turned apart fit the shape equally well
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
    on_tables = tables and (win.source.startswith("footprint_") or win.source.endswith("+on_tables"))
    method = f"{win.source} + " + ("point-to-plane ICP kept on both tables" if on_tables
                                   else "point-to-plane ICP (Open3D)")
    ok = acceptable(win)
    reason = None
    if not ok:
        reason = (f"best fit too weak: fitness {win.fitness:.2f} (needs >= {min_fitness:.2f}, or >= "
                  f"{MIN_FITNESS_ON_TABLES:.2f} with {MIN_MATCHED_ON_TABLES} matched points when the pose touches both "
                  f"tables), {win.matched} matched, RMSE {win.rmse_m * 1000:.1f} mm (needs <= {rmse_cap * 1000:.1f} mm)"
                  + ("" if touches(win) or not tables else
                     f"; it does not rest on both tables within {CONTACT_M * 1000:.0f} mm")
                  + (f"; {SHARE_TOO_LITTLE}" if win.fitness < MIN_FITNESS_ON_TABLES else ""))
    elif undecided:
        ok, reason = False, f"symmetric object: {tie}"
    return SegmentAlignment(aligned=ok, transform=[float(v) for v in win.transform.reshape(-1)] if ok else None,
                            fitness=round(win.fitness, 4), rmse_mm=round(win.rmse_m * 1000, 3), **{**base,
                            "method": method}, phone_estimate=phone, tie_break=tie,
                            candidates=[{**c.brief(), "turned_from_chosen_deg": round(turned(c, win), 1),
                                         "tilt_deg": round(tilt(c), 1), "faults": faults(c)}
                                        for c in sorted(cands, key=lambda c: (bool(faults(c)), -c.fitness))][:12],
                            reason=reason,
                            seconds=secs)
