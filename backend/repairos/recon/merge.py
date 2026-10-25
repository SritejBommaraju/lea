"""Merge a flipped segment (the object turned over for its underside) into the reference segment's frame.

The object moved between segments, so the room is no help: every image step looks only at the object.

1. **Feature bridge** (`feature_merge`): SIFT (OpenCV, Apache-2.0 since 4.4; the patent expired in 2020) on stills of
   both segments, with everything but the object masked out (pixels whose LiDAR point is inside the segment's
   object box and above its support plane; the projected box when a still has no depth). Matches pass Lowe's ratio
   test and a fundamental-matrix random-sample consensus per image pair, are lifted to 3D (triangulated within
   each segment from ARKit poses, or each still's LiDAR depth), and a rigid random-sample consensus +
   Umeyama gives T(moving -> reference).
2. **ICP refinement** (`icp_refine`): Open3D point-to-plane ICP of the two segments' fused object clouds from that
   start (the same in numpy without Open3D), coarse to fine. The refined transform is kept only when ICP agrees with the
   features (moves the object by less than `max_icp_shift_m` / `max_icp_rotation_deg`); otherwise the feature
   transform stays and the report says why.
3. **Cross-check** (`apple_cross_check`): Apple PhotogrammetrySession on all stills of both segments together
   (object masking on) puts every still in one object frame; aligning that frame to each segment's ARKit cameras
   (sim(3) on camera centres) gives an independent T(moving -> reference). The report gives both and how far apart
   they are at the object.

`merge_segments` runs all three and decides; it never fakes a merge: below the gates the result is `merged=False`
with the reason, and the segments stay in separate frames.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from repairos.locate.camera import project
from repairos.recon.align import Sim3, robust_umeyama, residuals, rotation_angle_deg
from repairos.recon.cli import ApplePose
from repairos.recon.tsdf import DepthFrame, ObjectBox, Segment, mask_segment

FEATURE_MAX_DIM = 1600          # px: SIFT runs on stills downscaled to this long side
RATIO = 0.8                     # Lowe's ratio test
F_RANSAC_PX = 2.0               # fundamental-matrix random-sample consensus threshold at FEATURE_MAX_DIM
RIGID_RANSAC_M = 0.008          # 3D inlier threshold: LiDAR noise + ARKit drift at 20-30 cm are several mm each
MIN_FEATURE_INLIERS = 12
MIN_ICP_FITNESS = 0.3


def cv2_available() -> bool:
    try:
        import cv2  # noqa: F401
    except ImportError:
        return False
    return True


# --- images -----------------------------------------------------------------------------------------------------

def load_gray(path: Path, max_dim: int = FEATURE_MAX_DIM, cache_dir: Path | None = None) -> tuple[np.ndarray, float]:
    """Grayscale pixels in the file's STORED orientation (EXIF orientation is not applied: intrinsics describe the
    stored buffer) and the scale from full resolution. HEIC is decoded with macOS `sips`."""
    from PIL import Image

    path = Path(path)
    src = path
    if path.suffix.lower() in (".heic", ".heif"):
        src = gray_cache_path(path, max_dim, cache_dir)
        src.parent.mkdir(parents=True, exist_ok=True)
        if not src.exists():
            subprocess.run(["sips", "-s", "format", "png", "-Z", str(max_dim), str(path), "--out", str(src)],
                           check=True, capture_output=True)
    with Image.open(src) as im:
        g = np.asarray(im.convert("L"))
    full = _full_size(path)
    scale = g.shape[1] / full[0] if full else 1.0
    if not full and max(g.shape) > max_dim:
        scale = max_dim / max(g.shape)
        im2 = Image.fromarray(g).resize((round(g.shape[1] * scale), round(g.shape[0] * scale)))
        g = np.asarray(im2)
    return g, scale


def gray_cache_path(path: Path, max_dim: int, cache_dir: Path | None = None) -> Path:
    """Where a HEIC's decoded PNG is cached: keyed by the resolved file and its size and modification time, so two
    captures' images of the same name (IMG_0001.HEIC in both segments) or a replaced file never share an entry."""
    path = Path(path)
    st = path.stat()
    key = hashlib.sha256(f"{path.resolve()}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:16]
    cache = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "repairos-recon-cache"
    return cache / f"{path.stem}_{key}_{max_dim}.png"


def _full_size(path: Path) -> tuple[int, int] | None:
    """(width, height) of the stored image buffer."""
    from PIL import Image

    if path.suffix.lower() in (".heic", ".heif"):
        out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)], capture_output=True,
                             text=True).stdout
        vals = {line.split(":")[0].strip(): int(line.split(":")[1]) for line in out.splitlines()[1:] if ":" in line}
        if "pixelWidth" in vals and "pixelHeight" in vals:
            return vals["pixelWidth"], vals["pixelHeight"]
        return None
    try:
        with Image.open(path) as im:
            return im.size
    except OSError:
        return None


def object_mask(frame: DepthFrame, masked_depth: np.ndarray | None, box: ObjectBox | None,
                shape: tuple[int, int], scale: float, dilate_px: int = 2) -> np.ndarray:
    """Object pixels at the feature image's resolution: the LiDAR object mask (upsampled, dilated by `dilate_px`
    depth pixels), else the projected object box, else everything."""
    h, w = shape
    if masked_depth is not None and (masked_depth > 0).any():
        m = masked_depth > 0
        if dilate_px:
            p = np.pad(m, dilate_px)
            acc = np.zeros_like(m)
            for dy in range(-dilate_px, dilate_px + 1):
                for dx in range(-dilate_px, dilate_px + 1):
                    acc |= p[dilate_px + dy:dilate_px + dy + m.shape[0], dilate_px + dx:dilate_px + dx + m.shape[1]]
            m = acc
        ys = np.minimum((np.arange(h) + 0.5) * m.shape[0] / h, m.shape[0] - 1).astype(int)
        xs = np.minimum((np.arange(w) + 0.5) * m.shape[1] / w, m.shape[1] - 1).astype(int)
        return m[ys][:, xs]
    if box is not None and frame.image_cam is not None:
        import cv2

        uv, depth = project(box.corners(), frame.pose, frame.image_cam)
        if (depth > 0).all():
            hull = cv2.convexHull((uv * scale).astype(np.float32)).astype(np.int32)
            out = np.zeros((h, w), np.uint8)
            cv2.fillConvexPoly(out, hull, 1)
            return out.astype(bool)
    return np.ones((h, w), bool)


@dataclass
class Features:
    frame: DepthFrame
    keypoints: np.ndarray        # N x 2, FULL-resolution stored-image pixels (pixel-centre convention)
    descriptors: np.ndarray      # N x 128 float32
    points_world: np.ndarray     # N x 3, the segment's world; NaN where the LiDAR depth is missing


def lift(frame: DepthFrame, uv_full: np.ndarray, min_confidence: int = 1) -> np.ndarray:
    """World points of full-resolution pixels from the frame's depth grid (nearest sample); NaN where there is no
    depth or its confidence is below `min_confidence`."""
    cam = frame.image_cam
    hd, wd = frame.depth_m.shape
    j = np.clip((uv_full[:, 0] * wd / cam.width).astype(int), 0, wd - 1)
    i = np.clip((uv_full[:, 1] * hd / cam.height).astype(int), 0, hd - 1)
    z = frame.depth_m[i, j].astype(float)
    ok = np.isfinite(z) & (z > 0)
    if frame.confidence is not None:
        ok &= frame.confidence[i, j] >= min_confidence
    pc = np.stack([(uv_full[:, 0] - cam.cx) / cam.fx * z, -(uv_full[:, 1] - cam.cy) / cam.fy * z, -z], axis=1)
    out = frame.pose.to_world(pc)
    out[~ok] = np.nan
    return out


def extract(seg: Segment, *, max_frames: int = 12, n_features: int = 4000, cache_dir: Path | None = None,
            masked: list[np.ndarray] | None = None) -> list[Features]:
    import cv2

    frames = [f for f in seg.frames if f.image_path is not None and f.image_cam is not None]
    if not frames:
        return []
    idx = np.unique(np.linspace(0, len(frames) - 1, min(max_frames, len(frames))).round().astype(int))
    if masked is None:
        masked, _ = mask_segment(seg)
    by_name = {f.name: d for f, d in zip(seg.frames, masked, strict=True)}
    sift = cv2.SIFT_create(nfeatures=n_features)
    out = []
    for k in idx:
        f = frames[int(k)]
        gray, scale = load_gray(f.image_path, cache_dir=cache_dir)
        mask = object_mask(f, by_name.get(f.name), seg.box, gray.shape, scale)
        kps, desc = sift.detectAndCompute(gray, mask.astype(np.uint8) * 255)
        if desc is None or len(kps) == 0:
            continue
        uv = (np.array([kp.pt for kp in kps], float) + 0.5) / scale   # OpenCV puts pixel centres at integers
        out.append(Features(f, uv, desc.astype(np.float32), lift(f, uv)))
    return out


def _midpoints(o1: np.ndarray, d1: np.ndarray, o2: np.ndarray, d2: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Closest-point midpoints of ray pairs (o1 + s d1, o2 + t d2; d unit, N x 3) and the ray parameters s, t."""
    w = o1 - o2
    b = np.sum(d1 * d2, axis=1)
    d, e = np.sum(d1 * w, axis=1), np.sum(d2 * w, axis=1)
    den = 1.0 - b * b
    den = np.where(den < 1e-12, np.nan, den)
    s = (b * e - d) / den
    t = (e - b * d) / den
    return 0.5 * ((o1 + s[:, None] * d1) + (o2 + t[:, None] * d2)), np.stack([s, t], axis=1)


def triangulate_segment(feats: list[Features], *, neighbours: int = 3, min_angle_deg: float = 3.0,
                        max_reproj_px: float = 6.0, max_spread_m: float = 0.003) -> int:
    """Replace each keypoint's 3D point by its triangulation from matches with the segment's other stills (ARKit
    poses, rays through the matched pixels; nearest `neighbours` stills in capture order, which drift least between
    them). A keypoint keeps a point only when its triangulations agree within `max_spread_m`; the others become NaN.
    LiDAR depth on a few-cm object is too coarse (its mixed pixels at edges put features centimetres off), so this
    is what the feature bridge uses. Returns how many keypoints got a point."""
    from repairos.locate.camera import pixel_rays

    acc: list[list[list[np.ndarray]]] = [[[] for _ in range(len(f.keypoints))] for f in feats]
    for i in range(len(feats)):
        for j in range(i + 1, min(len(feats), i + 1 + neighbours)):
            a, b = feats[i], feats[j]
            s = FEATURE_MAX_DIM / max(a.frame.image_cam.width, a.frame.image_cam.height)
            m = match_pair(a, b, scale=s)
            if len(m) == 0:
                continue
            oa, da = pixel_rays(a.keypoints[m[:, 0]], a.frame.pose, a.frame.image_cam)
            ob, db = pixel_rays(b.keypoints[m[:, 1]], b.frame.pose, b.frame.image_cam)
            ang = np.degrees(np.arccos(np.clip(np.sum(da * db, axis=1), -1, 1)))
            p, st = _midpoints(np.broadcast_to(oa, da.shape), da, np.broadcast_to(ob, db.shape), db)
            ok = (ang >= min_angle_deg) & np.isfinite(p).all(axis=1) & (st > 0).all(axis=1)
            for f, col in ((a, 0), (b, 1)):
                uv, dep = project(np.nan_to_num(p), f.frame.pose, f.frame.image_cam)
                err = np.linalg.norm(uv - f.keypoints[m[:, col]], axis=1)
                ok &= (dep > 0) & (err <= max_reproj_px)
            for k in np.nonzero(ok)[0]:
                acc[i][m[k, 0]].append(p[k])
                acc[j][m[k, 1]].append(p[k])
    total = 0
    for f, lists in zip(feats, acc, strict=True):
        pts = np.full((len(f.keypoints), 3), np.nan)
        for k, ps in enumerate(lists):
            if not ps:
                continue
            arr = np.array(ps)
            med = np.median(arr, axis=0)
            if len(arr) == 1 or np.max(np.linalg.norm(arr - med, axis=1)) <= max_spread_m:
                pts[k] = med
        f.points_world = pts
        total += int(np.isfinite(pts).all(axis=1).sum())
    return total


@dataclass
class FeatureMatchResult:
    transform: Sim3 | None             # rigid, moving -> reference
    pairs_tried: int
    pairs_verified: int
    matches_2d: int
    matches_3d: int
    inliers: int
    residual: dict
    reason: str | None = None

    def to_dict(self) -> dict:
        return {"transform": self.transform.to_dict() if self.transform else None, "pairs_tried": self.pairs_tried,
                "pairs_verified": self.pairs_verified, "matches_2d_verified": self.matches_2d,
                "matches_3d": self.matches_3d, "inliers": self.inliers, "inlier_residual": self.residual,
                "reason": self.reason}


def match_pair(a: Features, b: Features, *, ratio: float = RATIO, f_px: float = F_RANSAC_PX,
               scale: float = 1.0) -> np.ndarray:
    """Index pairs (i in a, j in b) that pass the ratio test and the fundamental-matrix random-sample consensus."""
    import cv2

    if len(a.descriptors) < 8 or len(b.descriptors) < 8:
        return np.zeros((0, 2), int)
    bf = cv2.BFMatcher(cv2.NORM_L2)
    knn = bf.knnMatch(a.descriptors, b.descriptors, k=2)
    good = [(m.queryIdx, m.trainIdx) for m, *rest in knn if rest and m.distance < ratio * rest[0].distance]
    if len(good) < 8:
        return np.zeros((0, 2), int)
    g = np.array(good)
    pa, pb = a.keypoints[g[:, 0]], b.keypoints[g[:, 1]]
    _, inl = cv2.findFundamentalMat(pa.astype(np.float32), pb.astype(np.float32), cv2.FM_RANSAC, f_px / scale,
                                    0.999)
    if inl is None:
        return np.zeros((0, 2), int)
    return g[inl.ravel().astype(bool)]


def feature_merge(ref: list[Features], mov: list[Features], *, threshold_m: float = RIGID_RANSAC_M,
                  min_inliers: int = MIN_FEATURE_INLIERS, min_pair_matches: int = 8) -> FeatureMatchResult:
    """T(moving -> reference) from SIFT matches between the two segments' stills, lifted to 3D."""
    tried = verified = n2d = 0
    src, dst = [], []
    for a in ref:
        for b in mov:
            tried += 1
            s = FEATURE_MAX_DIM / max(a.frame.image_cam.width, a.frame.image_cam.height)
            m = match_pair(a, b, scale=s)
            if len(m) < min_pair_matches:
                continue
            verified += 1
            n2d += len(m)
            pa, pb = a.points_world[m[:, 0]], b.points_world[m[:, 1]]
            ok = np.isfinite(pa).all(axis=1) & np.isfinite(pb).all(axis=1)
            dst.append(pa[ok])
            src.append(pb[ok])
    if not src or sum(len(s) for s in src) < 3:
        return FeatureMatchResult(None, tried, verified, n2d, 0, 0, {},
                                  "too few verified matches with depth on both sides between the segments' stills")
    x, y = np.concatenate(src), np.concatenate(dst)
    try:
        t, inl = robust_umeyama(x, y, threshold_m=threshold_m, with_scale=False, min_inliers=min_inliers)
    except ValueError as e:
        return FeatureMatchResult(None, tried, verified, n2d, len(x), 0, {},
                                  f"rigid random-sample consensus failed: {e}")
    return FeatureMatchResult(t, tried, verified, n2d, len(x), int(inl.sum()), residuals(t, x[inl], y[inl]))


# --- ICP --------------------------------------------------------------------------------------------------------

@dataclass
class IcpResult:
    transform: Sim3
    fitness: float               # fraction of moving points with a reference point within max_corr_m
    rmse_m: float
    shift_m: float               # how far ICP moved the moving object's centre from the start
    rotation_deg: float          # how far ICP rotated it from the start
    method: str
    degenerate: list = field(default_factory=list)   # translation directions the overlap cannot fix (kept)

    def to_dict(self) -> dict:
        return {"transform": self.transform.to_dict(), "fitness": round(self.fitness, 4),
                "rmse_mm": round(self.rmse_m * 1000, 3), "shift_mm": round(self.shift_m * 1000, 3),
                "rotation_change_deg": round(self.rotation_deg, 3), "method": self.method,
                "unobservable_translation_directions": self.degenerate}


def _nn(src: np.ndarray, dst: np.ndarray, chunk: int = 2048) -> tuple[np.ndarray, np.ndarray]:
    idx = np.empty(len(src), int)
    dist = np.empty(len(src))
    for k in range(0, len(src), chunk):
        d2 = ((src[k:k + chunk, None, :] - dst[None]) ** 2).sum(-1)
        j = d2.argmin(1)
        idx[k:k + chunk] = j
        dist[k:k + chunk] = np.sqrt(d2[np.arange(len(j)), j])
    return idx, dist


def _normals(pts: np.ndarray, k: int = 12, chunk: int = 1024) -> np.ndarray:
    """Unit normals by PCA of each point's k nearest neighbours (brute force in chunks; fine for a few thousand)."""
    out = np.empty_like(pts)
    for a in range(0, len(pts), chunk):
        d2 = ((pts[a:a + chunk, None, :] - pts[None]) ** 2).sum(-1)
        idx = np.argpartition(d2, min(k, len(pts) - 1), axis=1)[:, :k]
        nb = pts[idx] - pts[idx].mean(axis=1, keepdims=True)
        _, _, vt = np.linalg.svd(nb, full_matrices=False)
        out[a:a + chunk] = vt[:, -1, :]
    return out


def _point_to_plane_step(p: np.ndarray, q: np.ndarray, n: np.ndarray) -> Sim3:
    """One linearised point-to-plane least-squares step (small rotation omega, translation v) moving p onto the
    planes (q, n)."""
    a = np.hstack([np.cross(p, n), n])
    b = np.sum(n * (q - p), axis=1)
    x, *_ = np.linalg.lstsq(a, b, rcond=None)
    w, v = x[:3], x[3:]
    th = float(np.linalg.norm(w))
    if th < 1e-12:
        r = np.eye(3)
    else:
        kx = np.array([[0, -w[2], w[1]], [w[2], 0, -w[0]], [-w[1], w[0], 0]]) / th
        r = np.eye(3) + np.sin(th) * kx + (1 - np.cos(th)) * kx @ kx
    return Sim3(1.0, r, v)


def icp_refine(moving: np.ndarray, reference: np.ndarray, init: Sim3, *, max_corr_m: float = 0.005,
               iterations: int = 50, prefer: str = "auto", max_points: int = 4000, seed: int = 0,
               schedule: tuple[float, ...] = (2.0, 1.0, 0.5), degenerate_ratio: float = 0.1) -> IcpResult:
    """Rigid ICP of `moving` onto `reference` starting from `init`: point-to-plane (normals from the reference), run
    coarse to fine with correspondence distances `max_corr_m` x `schedule` (a top and a bottom segment overlap only
    on the sides: a loose final distance pairs the underside with the sides and tilts the result). Open3D when
    available, else the same in numpy. Fitness and RMSE are measured at `max_corr_m`."""
    rng = np.random.default_rng(seed)
    mv = np.asarray(moving, float)
    rf = np.asarray(reference, float)
    if len(mv) > max_points:
        mv = mv[rng.choice(len(mv), max_points, replace=False)]
    if len(rf) > 4 * max_points:
        rf = rf[rng.choice(len(rf), 4 * max_points, replace=False)]
    use_o3d = prefer in ("auto", "open3d")
    if use_o3d:
        try:
            import open3d as o3d
        except ImportError:
            if prefer == "open3d":
                raise
            use_o3d = False
    cur = init
    if use_o3d:
        s = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(mv))
        t = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(rf))
        t.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=max_corr_m * 3, max_nn=30))
        for f in schedule:
            reg = o3d.pipelines.registration.registration_icp(
                s, t, max_corr_m * f, cur.matrix, o3d.pipelines.registration.TransformationEstimationPointToPlane(),
                o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=iterations))
            cur = Sim3.from_matrix(reg.transformation)
        method = "open3d_point_to_plane_icp"
    else:
        normals = _normals(rf)
        for f in schedule:
            for _ in range(iterations):
                p = cur.apply(mv)
                j, d = _nn(p, rf)
                ok = d < max_corr_m * f
                if ok.sum() < 6:
                    break
                step = _point_to_plane_step(p[ok], rf[j[ok]], normals[j[ok]])
                cur = step @ cur
                if np.linalg.norm(step.t) < 1e-7 and rotation_angle_deg(step.r) < 1e-4:
                    break
        method = "numpy_point_to_plane_icp"
    out, degenerate = _keep_unobservable(init, cur, mv, rf, max_corr_m, degenerate_ratio)
    _, d = _nn(out.apply(mv), rf)
    ok = d < max_corr_m
    fitness = float(ok.mean())
    rmse = float(np.sqrt(np.mean(d[ok] ** 2))) if ok.any() else float("inf")
    c = mv.mean(axis=0)
    shift = float(np.linalg.norm(out.apply(c[None])[0] - init.apply(c[None])[0]))
    return IcpResult(out, fitness, rmse, shift, rotation_angle_deg(out.r @ init.r.T), method, degenerate)


def _keep_unobservable(init: Sim3, out: Sim3, mv: np.ndarray, rf: np.ndarray, max_corr_m: float,
                       ratio: float) -> tuple[Sim3, list[list[float]]]:
    """Degeneracy check (Zhang, Kaess, Singh, ICRA 2016): eigen-decompose the point-to-plane normal matrix at the
    solution; translation directions it barely constrains (eigenvalue < `ratio` x the largest) are not observable
    from the overlap (a top and a bottom share only vertical sides, so the height is free). ICP's move along such
    a direction is dropped: the start (the feature transform) keeps it. Returns (transform, degenerate directions
    in the reference frame)."""
    p = out.apply(mv)
    j, d = _nn(p, rf)
    ok = d < max_corr_m
    if ok.sum() < 6:
        return out, []
    n = _normals(rf)[j[ok]] if len(rf) <= 20000 else None
    if n is None:
        return out, []
    a = np.hstack([np.cross(p[ok] - p[ok].mean(0), n), n])
    a[:, :3] /= max(float(np.linalg.norm(p[ok] - p[ok].mean(0), axis=1).mean()), 1e-9)   # rotations in metres
    w, v = np.linalg.eigh(a.T @ a)
    weak = [v[:, k] for k in range(6) if w[k] < ratio * w[-1] and np.linalg.norm(v[3:, k]) > 0.7]
    if not weak:
        return out, []
    delta = out @ init.inverse()
    c = mv.mean(axis=0)
    move = out.apply(c[None])[0] - init.apply(c[None])[0]   # how ICP moved the object centre
    dirs = []
    for e in weak:
        u = e[3:] / np.linalg.norm(e[3:])
        move = move - (move @ u) * u
        dirs.append([round(float(x), 4) for x in u])
    # Same rotation as ICP, the centre moved only along observable directions.
    fixed_c = init.apply(c[None])[0] + move
    t = fixed_c - delta.r @ init.apply(c[None])[0]
    return Sim3(1.0, delta.r, t) @ init, dirs


# --- Apple joint reconstruction cross-check ---------------------------------------------------------------------

@dataclass
class AppleCheck:
    transform: Sim3 | None                 # moving -> reference
    per_segment: dict                      # name -> {registered, of, sim3, residual}
    reason: str | None = None

    def to_dict(self) -> dict:
        return {"transform": self.transform.to_dict() if self.transform else None, "per_segment": self.per_segment,
                "reason": self.reason}


def segment_sim3(poses: Sequence[ApplePose], centres: Mapping[str, np.ndarray], *,
                 outlier_m: float = 0.02) -> tuple[Sim3 | None, dict]:
    """sim(3) from Apple's frame to a segment's ARKit world on the camera centres of the segment's images that
    Apple posed (`centres`: file -> ARKit camera centre). Random-sample consensus drops cameras more than
    `outlier_m` off (an image
    Apple placed wrongly, or an ARKit jump); the residual is over the rest."""
    pairs = [(p.centre, centres[p.file]) for p in poses if p.file in centres]
    info = {"registered": len(pairs), "of": len(centres)}
    if len(pairs) < 3:
        return None, {**info, "reason": "fewer than 3 of the segment's images were posed by Apple"}
    a, b = np.array([p for p, _ in pairs]), np.array([q for _, q in pairs])
    try:
        s, inl = robust_umeyama(a, b, threshold_m=outlier_m, with_scale=True, min_inliers=max(3, len(a) // 2),
                                iterations=500)
    except ValueError as e:
        return None, {**info, "reason": str(e)}
    return s, {**info, "inliers": int(inl.sum()), "sim3": s.to_dict(), "residual": residuals(s, a[inl], b[inl]),
               "residual_all": residuals(s, a, b)}


def split_joint_poses(poses: Sequence[ApplePose], names: Mapping[str, str]) -> dict[str, list[ApplePose]]:
    """Joint-run poses (files named by `link_images`) by group, with their original file names."""
    out: dict[str, list[ApplePose]] = {}
    for p in poses:
        g = names.get(p.file or "")
        if g is None:
            continue
        out.setdefault(g, []).append(ApplePose(p.sample_id, p.file.split("__", 1)[1], p.matrix, p.intrinsics))
    return out


def apple_cross_check(ref_poses: Sequence[ApplePose], mov_poses: Sequence[ApplePose],
                      ref_centres: Mapping[str, np.ndarray], mov_centres: Mapping[str, np.ndarray]) -> AppleCheck:
    """`*_poses`: one joint Apple run's poses of each segment's images (original file names); `*_centres`: file ->
    the segment's ARKit camera centre."""
    sa, ia = segment_sim3(ref_poses, ref_centres)
    sb, ib = segment_sim3(mov_poses, mov_centres)
    per = {"reference": ia, "moving": ib}
    if sa is None or sb is None:
        return AppleCheck(None, per, "Apple did not pose enough images of both segments")
    t = sa @ sb.inverse()
    # Both ARKit worlds are metric: the similarity should be (close to) rigid; report the scale. Its translation was
    # computed with that scale, so dropping the scale alone moves points by (1 - scale) R x: refit a rigid motion on
    # the moving segment's cameras instead (each ARKit centre -> where Apple puts that camera in the reference).
    per["scale_moving_to_reference"] = t.scale
    pairs = [(mov_centres[p.file], sa.apply(p.centre[None])[0]) for p in mov_poses if p.file in mov_centres]
    src, dst = np.array([a for a, _ in pairs]), np.array([b for _, b in pairs])
    try:
        rigid, inl = robust_umeyama(src, dst, threshold_m=0.02, with_scale=False, min_inliers=max(3, len(src) // 2),
                                    iterations=500)
    except ValueError as e:
        return AppleCheck(None, per, f"no rigid fit of the moving segment's cameras: {e}")
    per["rigid_moving"] = {"inliers": int(inl.sum()), "residual": residuals(rigid, src[inl], dst[inl])}
    return AppleCheck(rigid, per)


def link_images(groups: Mapping[str, Sequence[Path]], out_dir: Path) -> dict[str, str]:
    """One folder with every segment's images (symlinks named `<group>__<file>`) for a joint PhotogrammetrySession
    run; returns link name -> group."""
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    names = {}
    for g, paths in groups.items():
        for p in paths:
            n = f"{g}__{Path(p).name}"
            (out_dir / n).symlink_to(Path(p).resolve())
            names[n] = g
    return names


# --- decision ---------------------------------------------------------------------------------------------------

def compare(t1: Sim3, t2: Sim3, at: np.ndarray) -> dict:
    """How far apart two transforms of the moving segment are: rotation and the displacement of `at`."""
    return {"rotation_deg": round(rotation_angle_deg(t1.r @ t2.r.T), 3),
            "displacement_mm": round(float(np.linalg.norm(t1.apply(at[None])[0] - t2.apply(at[None])[0])) * 1000, 3)}


@dataclass
class MergeResult:
    merged: bool
    transform: Sim3 | None           # moving -> reference (rigid)
    source: str | None               # features+icp | features | apple_joint
    features: FeatureMatchResult | None
    icp: IcpResult | None
    apple: AppleCheck | None
    agreement: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"merged": self.merged, "source": self.source,
                "transform": self.transform.to_dict() if self.transform else None,
                "features": self.features.to_dict() if self.features else None,
                "icp": self.icp.to_dict() if self.icp else None, "apple_joint": self.apple.to_dict() if self.apple
                else None, "agreement": self.agreement, "reasons": self.reasons}


def decide(features: FeatureMatchResult | None, icp: IcpResult | None, apple: AppleCheck | None,
           centre: np.ndarray, *, min_inliers: int = MIN_FEATURE_INLIERS, min_icp_fitness: float = MIN_ICP_FITNESS,
           max_icp_shift_m: float = 0.01, max_icp_rotation_deg: float = 5.0, max_disagreement_m: float = 0.01,
           max_disagreement_deg: float = 5.0, cloud_fit=None, min_cloud_fit: float = 0.5,
           cloud_fit_margin: float = 0.1) -> MergeResult:
    """`cloud_fit(T) -> fraction of the moving LiDAR cloud within 3 mm of the reference cloud` breaks a disagreement
    between the feature merge and Apple's joint reconstruction with data neither used. Without it, or without a
    clear winner, a disagreement refuses the merge."""
    reasons: list[str] = []
    agreement: dict = {}
    t, source = None, None
    if features is not None and features.transform is not None and features.inliers >= min_inliers:
        t, source = features.transform, "features"
        if icp is not None:
            if icp.fitness < min_icp_fitness:
                reasons.append(f"ICP fitness {icp.fitness:.2f} < {min_icp_fitness}: kept the feature transform")
            elif icp.shift_m > max_icp_shift_m or icp.rotation_deg > max_icp_rotation_deg:
                reasons.append(f"ICP moved the object {icp.shift_m * 1000:.1f} mm / {icp.rotation_deg:.1f} deg from "
                               "the feature transform (more than the gate): kept the feature transform")
            else:
                t, source = icp.transform, "features+icp"
    elif features is not None:
        reasons.append(features.reason or f"only {features.inliers} feature inliers (< {min_inliers})")
    if apple is not None and apple.transform is not None:
        if t is not None:
            agreement = compare(t, apple.transform, centre)
            # ARKit drift bounds how well any two methods can agree: Apple's (drift-free) cameras fit each
            # segment's ARKit cameras only to their residual, so the gate grows with it.
            fits = [apple.per_segment[k].get("residual", {}).get("rmse_m") or 0.0 for k in ("reference", "moving")]
            allowed_m = max(max_disagreement_m, 1.5 * sum(fits))
            agreement["allowed_displacement_mm"] = round(allowed_m * 1000, 3)
            if (agreement["displacement_mm"] > allowed_m * 1000
                    or agreement["rotation_deg"] > max_disagreement_deg):
                reasons.append(f"feature merge and Apple's joint reconstruction disagree by "
                               f"{agreement['displacement_mm']:.1f} mm / {agreement['rotation_deg']:.1f} deg")
                if cloud_fit is None:
                    return MergeResult(False, None, None, features, icp, apple, agreement, reasons)
                fa, fb = float(cloud_fit(t)), float(cloud_fit(apple.transform))
                agreement["cloud_fit_3mm"] = {source: round(fa, 4), "apple_joint": round(fb, 4)}
                if fb >= fa:
                    best_t, best_source, best_fit, other_fit = apple.transform, "apple_joint", fb, fa
                else:
                    best_t, best_source, best_fit, other_fit = t, source, fa, fb
                if best_fit < min_cloud_fit or best_fit - other_fit < cloud_fit_margin:
                    reasons.append(f"the LiDAR clouds do not pick one clearly ({fa:.2f} vs {fb:.2f} within 3 mm)")
                    return MergeResult(False, None, None, features, icp, apple, agreement, reasons)
                t, source = best_t, best_source
                reasons.append(f"chose {source}: it fits the two segments' LiDAR clouds better ({best_fit:.2f} vs "
                               f"{other_fit:.2f} of points within 3 mm)")
        else:
            res = [apple.per_segment[k].get("residual", {}).get("rmse_m") for k in ("reference", "moving")]
            if "rigid_moving" in apple.per_segment:   # and the rigid motion actually returned, not only the two fits
                res.append(apple.per_segment["rigid_moving"].get("residual", {}).get("rmse_m"))
            if all(r is not None and r < max_disagreement_m for r in res):
                t, source = apple.transform, "apple_joint"
                reasons.append("used Apple's joint reconstruction (the feature bridge failed)")
            else:
                reasons.append("Apple's joint reconstruction did not fit both segments' cameras within "
                               f"{max_disagreement_m * 1000:.0f} mm")
    elif apple is not None:
        reasons.append(apple.reason or "Apple cross-check unavailable")
    return MergeResult(t is not None, t, source, features, icp, apple, agreement, reasons)


def merge_segments(reference: Segment, moving: Segment, *, ref_points: np.ndarray, mov_points: np.ndarray,
                   apple_poses: Mapping[str, Sequence[ApplePose]] | None = None, cache_dir: Path | None = None,
                   max_frames: int = 16, lift: str = "triangulate", threshold_m: float = 0.005,
                   icp_max_corr_m: float = 0.005, **gates) -> MergeResult:
    """All three steps. `ref_points` / `mov_points`: each segment's fused object cloud in its own world;
    `apple_poses`: {"reference": [...], "moving": [...]} from one joint Apple run (`split_joint_poses`).
    `lift`: how matched features get their 3D points: triangulate (within each segment, ARKit poses; default) or
    lidar (each still's depth)."""
    features = None
    if cv2_available():
        fr = extract(reference, max_frames=max_frames, cache_dir=cache_dir)
        fm = extract(moving, max_frames=max_frames, cache_dir=cache_dir)
        if lift == "triangulate":
            triangulate_segment(fr)
            triangulate_segment(fm)
        elif lift != "lidar":
            raise ValueError(f"lift {lift!r}: triangulate or lidar")
        features = feature_merge(fr, fm, threshold_m=threshold_m)
    icp = None
    if features is not None and features.transform is not None and len(ref_points) and len(mov_points):
        icp = icp_refine(mov_points, ref_points, features.transform, max_corr_m=icp_max_corr_m)
    apple = None
    if apple_poses is not None:
        apple = apple_cross_check(apple_poses.get("reference", []), apple_poses.get("moving", []),
                                  {f.image_path.name: f.pose.t for f in reference.frames if f.image_path},
                                  {f.image_path.name: f.pose.t for f in moving.frames if f.image_path})
    centre = moving.box.centre if moving.box is not None else np.asarray(mov_points, float).mean(axis=0)
    def cloud_fit(t: Sim3, sample: int = 3000) -> float:
        mv = np.asarray(mov_points, float)
        if len(mv) > sample:
            mv = mv[np.random.default_rng(0).choice(len(mv), sample, replace=False)]
        _, d = _nn(t.apply(mv), np.asarray(ref_points, float))
        return float((d < 0.003).mean())

    fit = cloud_fit if len(ref_points) and len(mov_points) else None
    res = decide(features, icp, apple, centre, cloud_fit=fit, **gates)
    if features is None:
        res.reasons.insert(0, "OpenCV is not installed: no feature bridge")
    return res
