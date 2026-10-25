"""The same surface point found across stills. Corners (Shi-Tomasi, one per grid cell) in each still are lifted
with its LiDAR depth, projected into the stills that see the same side, and found there by pyramidal
Lucas-Kanade from that prediction (bias and gain normalised), each patch warped by the local surface plane seen from
the two stills (orbiting an object turns and foreshortens a patch in the image). A match is kept only when the patches correlate
(NCC) and tracking back lands on the corner again. ARKit poses and LiDAR depth only seed the search; the matched
pixels are what the adjustment trusts.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from repairos.locate.camera import project
from repairos.measure.views import View, shrink

CELL_PX = 24            # one corner per cell of this size (working pixels)
BORDER_PX = 16
PATCH_R = 7             # 15 x 15 patches
LEVELS = 3
ITERS = 12
MIN_NCC = 0.8
MAX_BACK_PX = 1.0
MAX_AXIS_DEG = 45.0     # stills whose optical axes differ more than this rarely see a patch alike
NEIGHBOURS = 12
DEDUP_PX = 2.0          # a corner this close to a point an earlier track already has is that point


@dataclass
class Tracks:
    """Observations of track points: track[i] is seen in view[i] at uv[i] (that view's working pixels).
    `seed` is each track's first 3D guess (arkit_world metres, from its reference still's depth)."""
    track: np.ndarray   # (M,) int
    view: np.ndarray    # (M,) int
    uv: np.ndarray      # (M, 2)
    seed: np.ndarray    # (P, 3)

    @property
    def n_points(self) -> int:
        return len(self.seed)


def box(img: np.ndarray, r: int) -> np.ndarray:
    """Mean over a (2r+1)^2 window, same size (edges use the clipped window)."""
    c = np.pad(img, r, mode="edge").cumsum(0).cumsum(1)
    c = np.pad(c, ((1, 0), (1, 0)))
    k = 2 * r + 1
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def corners(gray: np.ndarray, cell: int = CELL_PX, border: int = BORDER_PX) -> np.ndarray:
    """Shi-Tomasi corners (N x 2, pixel centres), the strongest in each cell, above 5% of the image's 99th
    percentile response."""
    g = box(gray, 1)
    gy, gx = np.gradient(g)
    a, b, c = box(gx * gx, 2), box(gx * gy, 2), box(gy * gy, 2)
    lam = (a + c) / 2 - np.sqrt(((a - c) / 2) ** 2 + b * b)
    h, w = gray.shape
    floor = 0.05 * float(np.percentile(lam, 99))
    out = []
    for y0 in range(border, h - border - cell + 1, cell):
        for x0 in range(border, w - border - cell + 1, cell):
            win = lam[y0:y0 + cell, x0:x0 + cell]
            j = int(np.argmax(win))
            if win.flat[j] > floor and floor > 0:
                out.append((x0 + j % cell + 0.5, y0 + j // cell + 0.5))
    return np.array(out, dtype=float).reshape(-1, 2)


def sample(img: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Bilinear samples at continuous pixel coordinates (pixel centres at +0.5), edge-clamped."""
    h, w = img.shape
    xs, ys = np.clip(x - 0.5, 0, w - 1.001), np.clip(y - 0.5, 0, h - 1.001)
    x0, y0 = np.floor(xs).astype(int), np.floor(ys).astype(int)
    fx, fy = xs - x0, ys - y0
    return (img[y0, x0] * (1 - fx) * (1 - fy) + img[y0, x0 + 1] * fx * (1 - fy)
            + img[y0 + 1, x0] * (1 - fx) * fy + img[y0 + 1, x0 + 1] * fx * fy)


def pyramid(gray: np.ndarray, levels: int = LEVELS) -> list[np.ndarray]:
    out = [gray]
    for _ in range(levels - 1):
        out.append(shrink(out[-1], 2))
    return out


_OFF = np.stack(np.meshgrid(np.arange(-PATCH_R, PATCH_R + 1), np.arange(-PATCH_R, PATCH_R + 1)), -1).reshape(-1, 2)


def _norm(p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    m = p.mean(axis=1, keepdims=True)
    s = p.std(axis=1, keepdims=True) + 1e-6
    return (p - m) / s, s


def klt(src: list[np.ndarray], dst: list[np.ndarray], p_src: np.ndarray, p_guess: np.ndarray,
        warp: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Track points p_src of pyramid `src` into pyramid `dst` from p_guess. `warp` (N x 2 x 2) maps a step in src
    pixels to the step in dst pixels around each point (the surface seen from the two stills); identity if None.
    Only the translation is solved; the warp keeps a rotated or foreshortened patch comparable. Returns (positions
    N x 2, NCC N)."""
    a = np.broadcast_to(np.eye(2), (len(p_src), 2, 2)) if warp is None else warp
    off_d = np.einsum("nij,kj->nki", a, _OFF)                  # N x K x 2 warped offsets in dst pixels
    p = p_guess.astype(float).copy()
    for lev in range(len(src) - 1, -1, -1):
        s = 2.0 ** lev
        t, _ = _norm(sample(src[lev], p_src[:, None, 0] / s + _OFF[None, :, 0], p_src[:, None, 1] / s + _OFF[None, :, 1]))
        gy_img, gx_img = np.gradient(dst[lev])
        q = p / s
        for _ in range(ITERS):
            xs, ys = q[:, None, 0] + off_d[..., 0], q[:, None, 1] + off_d[..., 1]
            i_n, sd = _norm(sample(dst[lev], xs, ys))
            gx, gy = sample(gx_img, xs, ys) / sd, sample(gy_img, xs, ys) / sd
            e = t - i_n
            h11, h12, h22 = (gx * gx).sum(1), (gx * gy).sum(1), (gy * gy).sum(1)
            b1, b2 = (gx * e).sum(1), (gy * e).sum(1)
            det = h11 * h22 - h12 * h12
            ok = det > 1e-9
            d = np.zeros_like(q)
            d[ok, 0] = (h22[ok] * b1[ok] - h12[ok] * b2[ok]) / det[ok]
            d[ok, 1] = (h11[ok] * b2[ok] - h12[ok] * b1[ok]) / det[ok]
            q += np.clip(d, -2.0, 2.0)
            if np.abs(d).max(initial=0) < 0.01:
                break
        p = q * s
    i_n, _ = _norm(sample(dst[0], p[:, None, 0] + off_d[..., 0], p[:, None, 1] + off_d[..., 1]))
    t, _ = _norm(sample(src[0], p_src[:, None, 0] + _OFF[None, :, 0], p_src[:, None, 1] + _OFF[None, :, 1]))
    return p, (t * i_n).mean(axis=1)


def normals(v: View, c: np.ndarray, depth_of=None, step_px: float = 12.0) -> np.ndarray:
    """Surface normals (world, N x 3, towards the camera) at pixels c: a plane through the points lifted at a 3 x 3
    grid of pixels step_px apart around each. Where that fails, the direction back to the camera."""
    grid = np.array([(dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1)], dtype=float) * step_px
    uv = (c[:, None, :] + grid[None]).reshape(-1, 2)
    pts = lift(v, uv, depth_of(uv) if depth_of else None).reshape(len(c), 9, 3)
    out = np.empty((len(c), 3))
    back = v.pose.r @ np.array([0.0, 0.0, 1.0])
    for k in range(len(c)):
        q = pts[k][np.isfinite(pts[k]).all(axis=1)]
        if len(q) < 5:
            out[k] = back
            continue
        n = np.linalg.svd(q - q.mean(0))[2][2]
        out[k] = n if n @ back > 0 else -n
    return out


def warps(v: View, w: View, c: np.ndarray, x: np.ndarray, n: np.ndarray) -> np.ndarray:
    """N x 2 x 2: how a one-pixel step in v at c moves in w, through the plane (x, n)."""
    out = np.empty((len(c), 2, 2))
    base = project(x, w.pose, w.cam)[0]
    for col, e in enumerate(((1.0, 0.0), (0.0, 1.0))):
        uv = c + np.array(e)
        d = np.stack([(uv[:, 0] - v.cam.cx) / v.cam.fx, -(uv[:, 1] - v.cam.cy) / v.cam.fy, -np.ones(len(uv))], 1)
        d = d @ v.pose.r.T
        lam = ((x - v.pose.t) * n).sum(1) / np.where(np.abs((d * n).sum(1)) < 1e-9, 1e-9, (d * n).sum(1))
        out[:, :, col] = project(v.pose.t + lam[:, None] * d, w.pose, w.cam)[0] - base
    return out


def _axis(v: View) -> np.ndarray:
    return v.pose.r @ np.array([0.0, 0.0, -1.0])


def neighbours(views: list[View], k: int = NEIGHBOURS, max_deg: float = MAX_AXIS_DEG, min_baseline_m: float = 0.01
               ) -> dict[int, list[int]]:
    """For each view, up to k others looking the same way (axis angle under max_deg) from somewhere else, spread over
    the angles (nearest and furthest both): near ones match easily, far ones give the parallax that fixes depth."""
    out: dict[int, list[int]] = {}
    for i, a in enumerate(views):
        cand = []
        for j, b in enumerate(views):
            if i == j or np.linalg.norm(a.pose.t - b.pose.t) < min_baseline_m:
                continue
            ang = np.degrees(np.arccos(np.clip(_axis(a) @ _axis(b), -1, 1)))
            if ang < max_deg:
                cand.append((ang, j))
        cand.sort()
        pick = sorted({int(round(x)) for x in np.linspace(0, len(cand) - 1, min(k, len(cand)))}) if cand else []
        out[i] = [cand[p][1] for p in pick]
    return out


def lift(v: View, uv: np.ndarray, z: np.ndarray | None = None) -> np.ndarray:
    """World points of gray pixels uv at depths z (default: v's LiDAR depth; NaN rows where there is none)."""
    z = v.depth_at(uv) if z is None else z
    xc = (uv[:, 0] - v.cam.cx) / v.cam.fx * z
    yc = -(uv[:, 1] - v.cam.cy) / v.cam.fy * z
    return v.pose.to_world(np.stack([xc, yc, -z], axis=1))


def surface_depth(view: View, points: np.ndarray, k: int = 6, max_px: float = 30.0):
    """A depth function for `view` from known 3D points (an earlier adjustment's): at each pixel the median depth of
    the k points that project nearest, NaN where none is within max_px. Far sharper than one LiDAR cell."""
    pc = view.pose.to_camera(points)
    front = pc[:, 2] < 0
    uvp, dp = project(points[front], view.pose, view.cam)[0], -pc[front, 2]

    def depth(uv: np.ndarray) -> np.ndarray:
        if not len(uvp):
            return np.full(len(uv), np.nan)
        d2 = ((uv[:, None, :] - uvp[None]) ** 2).sum(-1)
        near = np.argsort(d2, axis=1)[:, :k]
        z = np.median(dp[near], axis=1)
        z[np.sqrt(np.take_along_axis(d2, near[:, :1], 1)[:, 0]) > max_px] = np.nan
        return z
    return depth


def find_tracks(views: list[View], pairs: dict[int, list[int]] | None = None, depth_fns: list | None = None
                ) -> Tracks:
    """Tracks across `views` (see module doc). Each corner is followed into every still in `pairs` (by default all
    that look the same way), so one surface point gathers the stills that see it: that is what fixes its depth. A
    corner already observed by an earlier track (within DEDUP_PX) does not start another one. `depth_fns[i](uv)`
    replaces still i's LiDAR depth as the seed (a second pass seeds from the first adjustment's surface)."""
    pyr = [pyramid(v.gray) for v in views]
    pairs = neighbours(views) if pairs is None else pairs
    taken: list[list[np.ndarray]] = [[] for _ in views]
    tr, vi, uvs, seeds = [], [], [], []
    for i, v in enumerate(views):
        c = corners(v.gray)
        if not len(c):
            continue
        c = c[~v.depth_edge(c)]
        if taken[i] and len(c):
            near = np.min(np.linalg.norm(c[:, None] - np.stack(taken[i])[None], axis=2), axis=1)
            c = c[near > DEDUP_PX]
        if not len(c):
            continue
        depth_of = depth_fns[i] if depth_fns else None
        x = lift(v, c, depth_of(c) if depth_of else None)
        ok = np.isfinite(x).all(axis=1)
        c, x = c[ok], x[ok]
        if not len(c):
            continue
        nrm = normals(v, c, depth_of)
        seen: list[list] = [[] for _ in range(len(c))]
        for j in pairs.get(i, []):
            w = views[j]
            pred, depth = project(x, w.pose, w.cam)
            ww, wh = w.size
            inside = (depth > 0) & (pred[:, 0] > BORDER_PX) & (pred[:, 0] < ww - BORDER_PX) \
                & (pred[:, 1] > BORDER_PX) & (pred[:, 1] < wh - BORDER_PX)
            zj = w.depth_at(np.where(inside[:, None], pred, 0.0))
            inside &= ~(np.isfinite(zj) & (np.abs(zj - depth) > np.maximum(0.02, 0.06 * depth)))  # occluded there
            idx = np.flatnonzero(inside)
            if not len(idx):
                continue
            a = warps(v, w, c[idx], x[idx], nrm[idx])
            invertible = np.abs(np.linalg.det(a)) > 0.05
            idx, a = idx[invertible], a[invertible]
            if not len(idx):
                continue
            found, ncc = klt(pyr[i], pyr[j], c[idx], pred[idx], a)
            back, _ = klt(pyr[j], pyr[i], found, c[idx], np.linalg.inv(a))
            good = (ncc >= MIN_NCC) & (np.linalg.norm(back - c[idx], axis=1) <= MAX_BACK_PX) \
                & (found[:, 0] > PATCH_R) & (found[:, 0] < ww - PATCH_R) \
                & (found[:, 1] > PATCH_R) & (found[:, 1] < wh - PATCH_R)
            for k, f in zip(idx[good], found[good], strict=True):
                seen[k].append((j, f))
        for k, obs in enumerate(seen):
            if not obs:
                continue
            t = len(seeds)
            seeds.append(x[k])
            for j, f in [(i, c[k]), *obs]:
                tr.append(t)
                vi.append(j)
                uvs.append(f)
                taken[j].append(f)
    return Tracks(np.array(tr, dtype=int), np.array(vi, dtype=int), np.array(uvs, dtype=float).reshape(-1, 2),
                  np.array(seeds, dtype=float).reshape(-1, 3))
