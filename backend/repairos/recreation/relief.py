"""Raised parts on a thin object's slab (recreation.flat): a height map from the posed stills by plane-sweep stereo,
then blocks on the slab's top. LiDAR cannot see a board's 8-11 mm parts as shapes (its points are 2.5 mm voxels with
millimetres of noise), but the stills see them from several sides at ~0.1 mm per pixel.

    model = add_relief(model, views, tex, table_y, lidar)     # the slab with blocks; unchanged when relief fails

1. Frame: the slab's top grid (`tex`, the frame its texture is rectified on) at RES_M. Each grid pixel is the top
   still's ray through that point of the top plane; a height h moves the point along that ray, so the texture (the
   top still at the top plane) stays registered on whatever is built.
2. Poses: a still's pose can be millimetres off on its own, so each other still is first shifted on the top plane to
   land on the top still there (phase correlation over the outline): the board's own surface is then height 0, and
   what is left between the stills is parallax, i.e. height.
3. Plane sweep: for h from -STEP_M to MAX_M in STEP_M, each other still sampled at the grid's points at height h
   (torch grid_sample, every height and still in one batch), scored against the top still by NCC over a WIN_M
   window. A pixel's cost at h is the mean of its best half of the stills (a part hides the board behind it from
   some sides), its height the lowest cost (sub-step parabola), its confidence the best NCC and the gap to the next
   minimum.
4. Clean-up: confident heights only, median filtered (edges stay steps), clamped to [0, CLAMP_M], quantised to
   QUANT_M; raised pieces smaller than MIN_BLOB_M square dropped; low-confidence pixels fall back to 0 (LiDAR is
   only a check: its median height over each raised piece is reported next to the photos').
5. Mesh: the grid coarsened to MESH_CELL_M, equal-height cells merged into rectangles (at most MAX_QUAD_M a side,
   so vertex colours keep detail), walls where neighbouring cells differ, all on the slab's flat top (which keeps
   its texture), within MAX_TRIANGLES.

Generic: no product, part or class anywhere; any thin object (a board's parts, a remote's buttons, a keyboard's keys).
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

RES_M = 0.00025          # height map grid (top view)
STEP_M = 0.0005          # height step of the sweep
MAX_M = 0.025            # highest part searched above the top
CLAMP_M = 0.03
WIN_M = 0.002            # NCC window side (~2 mm: a pin header row, a chip's edge)
MAX_VIEWS = 4            # other stills used (most parallax, most varied directions)
MIN_PARALLAX = 0.25      # a still must move a point at least this much (grid pixels per mm of height)
MIN_NCC = 0.55           # confident: best NCC at least this
MIN_NCC_RAISED = 0.65    # ... and this for a raised height (a stray match on a plain board is a spike)
MIN_GAP = 0.04           # ... and the next minimum's cost at least this much higher
MIN_TEXTURE = 3.0        # top still's grey standard deviation in the window below this: too plain to match
MEDIAN_M = 0.00125       # median filter side
MAX_HOLE_M = 0.012       # ... up to this square (a connector's shell)
CLOSE_M = 0.0005         # gaps this wide between raised pixels are closed before filling enclosed unknowns
QUANT_M = 0.0005
SPIKE_M = 0.00075         # grey opening radius
RAISED_M = 0.001         # a cell this much above the top is raised
MIN_BLOB_M = 0.002       # raised pieces smaller than this square go
MESH_CELL_M = 0.0005
MAX_QUAD_M = 0.003
MAX_TRIANGLES = 80_000
ALIGN_MAX_M = 0.012      # a still shifted more than this to land on the top still is not used

_CACHE: OrderedDict = OrderedDict()   # (stills, poses, grid) -> Relief: the mesh and the recreation build the same slab


class ReliefUnavailable(RuntimeError):
    pass


@dataclass
class Relief:
    heights: np.ndarray          # H x W metres above the slab top, on `grid`
    confident: np.ndarray        # H x W bool
    grid: Any                    # flat.Grid
    stats: dict = field(default_factory=dict)


# --- stereo ----------------------------------------------------------------------------------------------------------

def _device():
    import torch

    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def _ray_points(grid: Any, top_y: float, c: np.ndarray, h: np.ndarray) -> np.ndarray:
    """World points (len(h) x N x 3) of the grid's pixels moved along the top still's rays (centre c) to top_y + h."""
    x0 = grid.world(top_y)
    s = (top_y + h - c[1]) / (top_y - c[1])
    return c + s[:, None, None] * (x0 - c)[None]


def _grey(still: Any, factor: int):
    """The still in grey, box-downsampled by `factor`, as a torch tensor (1 x 1 x H x W) and its intrinsics."""
    import torch
    import torch.nn.functional as tf

    img = torch.from_numpy(np.array(still.image, copy=True)).to(_device())
    g = (img[..., 0].float() * 0.299 + img[..., 1].float() * 0.587 + img[..., 2].float() * 0.114)[None, None]
    if factor > 1:
        g = tf.avg_pool2d(g, factor)
    cam = still.cam
    k = 1.0 / factor
    return g, (cam.fx * k, cam.fy * k, cam.cx * k, cam.cy * k)


def _sample(g, intr, pose: Any, pts: np.ndarray):
    """Grey values of world points (B x N x 3) in one still (bilinear); NaN out of view."""
    import torch
    import torch.nn.functional as tf

    dev = g.device
    p = torch.from_numpy(pts.astype(np.float32)).to(dev)
    r = torch.from_numpy(pose.r.astype(np.float32)).to(dev)
    t = torch.from_numpy(pose.t.astype(np.float32)).to(dev)
    pc = (p - t) @ r                       # camera frame (R^T (p - t)), row vectors
    z = -pc[..., 2]
    fx, fy, cx, cy = intr
    u = cx + fx * pc[..., 0] / z
    v = cy - fy * pc[..., 1] / z
    h, w = g.shape[-2:]
    gx = (u - 0.5) / (w - 1) * 2 - 1        # pixel centres at +0.5 (camera module convention)
    gy = (v - 0.5) / (h - 1) * 2 - 1
    out = tf.grid_sample(g.expand(1, 1, h, w), torch.stack([gx, gy], -1)[None].reshape(1, -1, pts.shape[1], 2),
                         mode="bilinear", padding_mode="zeros", align_corners=True)[0, 0]
    bad = (z < 0.05) | (gx.abs() > 1) | (gy.abs() > 1)
    out = out.reshape(pts.shape[0], pts.shape[1])
    out[bad] = float("nan")
    return out


def _box(x, k: int):
    import torch.nn.functional as tf

    return tf.avg_pool2d(x, k, stride=1, padding=k // 2, count_include_pad=False)


def _shift(a: np.ndarray, b: np.ndarray, mask: np.ndarray) -> tuple[float, float, float]:
    """(drow, dcol, peak): the shift that moves b onto a (phase correlation over the mask, sub-pixel by parabola)."""
    wa = np.where(mask, a - a[mask].mean(), 0.0)
    wb = np.where(mask, b - b[mask].mean(), 0.0)
    fa, fb = np.fft.fft2(wa), np.fft.fft2(wb)
    r = fa * np.conj(fb)
    r /= np.abs(r) + 1e-9
    c = np.fft.ifft2(r).real
    k = np.unravel_index(int(np.argmax(c)), c.shape)
    out = []
    for ax in (0, 1):
        n = c.shape[ax]
        i = k[ax]
        idx = [k[0], k[1]]
        idx[ax] = (i - 1) % n
        lo = c[tuple(idx)]
        idx[ax] = (i + 1) % n
        hi = c[tuple(idx)]
        mid = c[k]
        den = lo - 2 * mid + hi
        sub = 0.5 * (lo - hi) / den if den < 0 else 0.0
        d = i + sub
        out.append(d if d < n / 2 else d - n)
    return out[0], out[1], float(c[k])


def _pick_views(ref: Any, others: Sequence[Any], grid: Any, top_y: float) -> list[tuple[Any, np.ndarray]]:
    """Up to MAX_VIEWS other stills with the most parallax and the most varied directions: (still, parallax
    direction on the grid in pixels per mm)."""
    from repairos.locate.camera import project

    centre = np.array([[grid.shape[0] / 2, grid.shape[1] / 2]], int)
    p = _ray_points(grid, top_y, ref.pose.t, np.array([0.0, 0.01]))   # all pixels; only the centre is needed
    i = centre[0, 0] * grid.shape[1] + centre[0, 1]
    cand = []
    for v in others:
        uv, z = project(p[:, i], v.pose, v.cam)
        if (z <= 0).any():
            continue
        # the still's image displacement for 10 mm, back on the grid: pixels in the still per grid pixel
        foot = float(np.median(z)) / v.cam.fx
        d = (uv[1] - uv[0]) * foot / grid.res / 10.0                    # grid pixels per mm, in image axes
        cand.append((v, d))
    cand = [(v, d) for v, d in cand if np.linalg.norm(d) >= MIN_PARALLAX]
    chosen: list = []
    while cand and len(chosen) < MAX_VIEWS:
        def score(item):
            v, d = item
            n = np.linalg.norm(d)
            sim = max((abs(float(d @ e)) / (n * np.linalg.norm(e)) for _, e in chosen), default=0.0)
            return min(n, 1.5) * (1.2 - sim) * min(v.sharpness, 2000) ** 0.25
        best = max(cand, key=score)
        chosen.append(best)
        cand.remove(best)
    return chosen


def relief_heights(ref: Any, others: Sequence[Any], grid: Any, top_y: float, mask: np.ndarray | None = None
                   ) -> Relief:
    """The height map above the top plane on `grid` (module docstring 1-4, without LiDAR). `ref` is the still the top
    is textured from; `mask` (grid shape) the outline. Raises ReliefUnavailable."""
    import torch

    t0 = time.perf_counter()
    chosen = _pick_views(ref, others, grid, top_y)
    if len(chosen) < 2:
        raise ReliefUnavailable(f"{len(chosen)} other photo(s) see the object from a different enough angle (need 2)")
    hh, ww = grid.shape
    mask = np.ones(grid.shape, bool) if mask is None else mask
    hs = np.arange(-STEP_M, MAX_M + 1e-9, STEP_M)
    c = ref.pose.t.astype(float)
    pts = _ray_points(grid, top_y, c, hs)                                  # L x N x 3
    foot = float(np.linalg.norm(grid.world(top_y, np.array([[hh // 2, ww // 2]]))[0] - c)) / ref.cam.fx
    factor = max(1, int(round(grid.res / foot / 1.5)))
    stats: dict = {"views": [ref.name], "downsample": factor}
    with torch.no_grad():
        g_ref, i_ref = _grey(ref, factor)
        r = _sample(g_ref, i_ref, ref.pose, pts[:1])[0].reshape(hh, ww)
        valid_ref = ~torch.isnan(r)
        r = torch.nan_to_num(r)
        k = max(3, int(round(WIN_M / grid.res)) | 1)
        r4 = r[None, None]
        mr = _box(r4, k)
        sr = (_box(r4 * r4, k) - mr * mr).clamp_min(0).sqrt()
        costs, shifts = [], {}
        m_np = mask & valid_ref.cpu().numpy()
        i0 = int(np.argmin(np.abs(hs)))
        for v, _d in chosen:
            g, intr = _grey(v, factor)
            w0 = _sample(g, intr, v.pose, pts[i0:i0 + 1])[0].reshape(hh, ww)
            ok = m_np & ~torch.isnan(w0).cpu().numpy()
            if ok.mean() < 0.3:
                continue
            dr, dc, peak = _shift(r.cpu().numpy(), torch.nan_to_num(w0).cpu().numpy(), ok)
            off = -(dc * grid.u + dr * grid.v) * grid.res                 # move v's samples by this on the plane
            if np.linalg.norm(off) > ALIGN_MAX_M:
                shifts[v.name] = None
                continue
            shifts[v.name] = round(float(np.linalg.norm(off)) * 1000, 2)
            pose = replace(v.pose, t=v.pose.t - np.array([off[0], 0.0, off[1]]))
            w = _sample(g, intr, pose, pts).reshape(len(hs), 1, hh, ww)  # L x 1 x H x W
            nan = torch.isnan(w)
            w = torch.nan_to_num(w)
            mw = _box(w, k)
            sw = (_box(w * w, k) - mw * mw).clamp_min(0).sqrt()
            ncc = (_box(w * r4, k) - mw * mr) / (sw * sr + 1e-3)
            cost = (1 - ncc.clamp(-1, 1))[:, 0]
            cost[_box(nan.float(), k)[:, 0] > 0.2] = float("nan")
            costs.append(cost)
            stats["views"].append(v.name)
            del g
        stats["align_shift_mm"] = shifts
        if len(costs) < 2:
            raise ReliefUnavailable(f"only {len(costs)} other photo(s) land on the top photo within "
                                    f"{ALIGN_MAX_M * 1000:.0f} mm")
        cv = torch.stack(costs)                                            # V x L x H x W
        nv = cv.shape[0]
        keep = max(1, (nv + 1) // 2)
        cv = torch.nan_to_num(cv, nan=3.0)
        best_half = torch.topk(cv, keep, dim=0, largest=False).values.mean(0)   # L x H x W
        agg = best_half
        ib = torch.argmin(agg, 0)
        cb = torch.gather(agg, 0, ib[None])[0]
        # sub-step parabola
        lo = torch.gather(agg, 0, (ib - 1).clamp_min(0)[None])[0]
        hi = torch.gather(agg, 0, (ib + 1).clamp_max(len(hs) - 1)[None])[0]
        den = lo - 2 * cb + hi
        sub = torch.where(den > 1e-6, 0.5 * (lo - hi) / den, torch.zeros_like(den)).clamp(-0.5, 0.5)
        h = torch.from_numpy(hs.astype(np.float32)).to(agg.device)[ib] + sub * STEP_M
        # the next minimum away from the best (two steps either side excluded)
        lvl = torch.arange(len(hs), device=agg.device)[:, None, None]
        far = (lvl - ib[None]).abs() > 2
        second = torch.where(far, agg, torch.full_like(agg, 9.0)).min(0).values
        min_ncc = torch.where(h >= RAISED_M, torch.full_like(h, MIN_NCC_RAISED), torch.full_like(h, MIN_NCC))
        conf = ((1 - cb) >= min_ncc) & ((second - cb) >= MIN_GAP) & (sr[0, 0] >= MIN_TEXTURE) & (cb < 2.5)
        h_np, conf_np = h.cpu().numpy().astype(float), conf.cpu().numpy() & m_np
        ncc_np = (1 - cb).cpu().numpy()
    stats["stereo_s"] = round(time.perf_counter() - t0, 3)
    stats["device"] = str(agg.device)
    stats["confident_share"] = round(float(conf_np[mask].mean()), 3) if mask.any() else 0.0
    stats["median_ncc"] = round(float(np.median(ncc_np[mask])), 3) if mask.any() else 0.0
    return Relief(h_np, conf_np, grid, stats)


# --- clean-up --------------------------------------------------------------------------------------------------------

def _nanmedian(a: np.ndarray, k: int, min_share: float) -> np.ndarray:
    """Median over a k x k window of the finite pixels; NaN where fewer than min_share of the window are finite."""
    import torch
    import torch.nn.functional as tf

    x = torch.from_numpy(a.astype(np.float32))[None, None]
    cols = tf.unfold(tf.pad(x, (k // 2,) * 4, value=float("nan")), k)[0]      # k*k x H*W
    n = (~torch.isnan(cols)).float().mean(0)
    med = torch.nanmedian(cols, 0).values
    med[n < min_share] = float("nan")
    return med.reshape(a.shape).numpy().astype(float)


def _window(a: np.ndarray, r: int, fn) -> np.ndarray:
    """fn (np.min / np.max) over a (2r+1) square window, separably."""
    from numpy.lib.stride_tricks import sliding_window_view

    k = 2 * r + 1
    a = fn(sliding_window_view(np.pad(a, ((r, r), (0, 0)), mode="edge"), k, axis=0), axis=-1)
    return fn(sliding_window_view(np.pad(a, ((0, 0), (r, r)), mode="edge"), k, axis=1), axis=-1)


def _components(m: np.ndarray) -> tuple[np.ndarray, int]:
    from repairos.recreation.flat import _components as comp

    lab = comp(m)
    return lab, int(lab.max())


def clean(rel: Relief, mask: np.ndarray) -> np.ndarray:
    """Confident heights, median filtered, clamped, quantised, small raised pieces dropped (module docstring 4)."""
    res = rel.grid.res
    k = max(3, int(round(MEDIAN_M / res)) | 1)
    from repairos.recreation.flat import _dilate, _erode, _fill_holes

    h = _nanmedian(np.where(rel.confident, rel.heights, np.nan), k, 0.3)
    # Plain tops (a metal shell, a chip's face) match only at their edges: unknown pixels enclosed by raised ones
    # (after closing gaps of CLOSE_M) take the median of the raised pixels round them; every other unknown pixel is
    # the top (0). Confident heights are never overwritten.
    raised = np.nan_to_num(h, nan=0.0) >= RAISED_M
    r = max(1, int(round(CLOSE_M / res)))
    closed = _erode(_dilate(raised, r), r)
    holes = _fill_holes(closed) & ~closed
    lab, n = _components(holes)
    if n:   # holes bigger than MAX_HOLE_M square are not a part's plain top but the board between parts
        sizes = np.bincount(lab.ravel(), minlength=n + 1)
        big = sizes > (MAX_HOLE_M / res) ** 2
        big[0] = True
        holes &= ~big[lab]
    enclosed = (holes | closed) & np.isnan(h)
    if enclosed.any():
        known = np.where(raised, h, np.nan)
        for kk in (2 * k + 1, 4 * k + 1, 8 * k + 1):
            fill = _nanmedian(known, kk, 0.05)
            take = enclosed & np.isnan(known) & ~np.isnan(fill)
            known[take] = fill[take]
        h = np.where(enclosed, known, h)
    h = np.nan_to_num(h, nan=0.0)
    # grey opening: needles and walls narrower than 2 x SPIKE_M (a stray match along an edge) come down to what is
    # round them; blocks keep their sides
    ro = max(1, int(round(SPIKE_M / res)))
    h = _window(_window(h, ro, np.min), ro, np.max)
    h = np.clip(h, 0.0, CLAMP_M)
    h = np.round(h / QUANT_M) * QUANT_M
    h[~mask] = 0.0
    raised = h >= RAISED_M
    lab, n = _components(raised)
    if n:
        sizes = np.bincount(lab.ravel(), minlength=n + 1)
        small = sizes < (MIN_BLOB_M / res) ** 2
        small[0] = False
        h[small[lab]] = 0.0
    h[h < RAISED_M] = 0.0
    return h


def lidar_check(h: np.ndarray, grid: Any, top_y: float, lidar: np.ndarray | None) -> dict:
    """LiDAR's word on the raised pieces: per piece (largest first) the photos' median height and LiDAR's median
    height of its points over it (mm above the top), and over the flat part."""
    if lidar is None or not len(lidar):
        return {"lidar": "none"}
    rc = np.floor(grid.to_pixel(lidar[:, [0, 2]])).astype(int)
    inb = (rc[:, 0] >= 0) & (rc[:, 0] < h.shape[0]) & (rc[:, 1] >= 0) & (rc[:, 1] < h.shape[1])
    rc, ly = rc[inb], lidar[inb, 1] - top_y
    if not len(rc):
        return {"lidar": "no points over the object"}
    lab, n = _components(h >= RAISED_M)
    at = lab[rc[:, 0], rc[:, 1]]
    flat = ly[at == 0]
    out = {"lidar_points": int(len(rc)),
           "lidar_flat_median_mm": None if not len(flat) else round(float(np.median(flat)) * 1000, 1), "pieces": []}
    sizes = np.bincount(lab.ravel(), minlength=n + 1)
    for k in np.argsort(-sizes[1:])[:12] + 1:
        sel = at == k
        out["pieces"].append({"area_mm2": round(float(sizes[k]) * grid.res ** 2 * 1e6, 1),
                              "photo_mm": round(float(np.median(h[lab == k])) * 1000, 1),
                              "lidar_mm": None if sel.sum() < 3 else round(float(np.median(ly[sel])) * 1000, 1),
                              "lidar_n": int(sel.sum())})
    return out


# --- mesh ------------------------------------------------------------------------------------------------------------

def _coarsen(h: np.ndarray, f: int) -> np.ndarray:
    """f x f blocks: the median height (a block raised when most of it is)."""
    if f <= 1:
        return h
    hh, ww = (h.shape[0] // f) * f, (h.shape[1] // f) * f
    b = h[:hh, :ww].reshape(hh // f, f, ww // f, f).transpose(0, 2, 1, 3).reshape(hh // f, ww // f, f * f)
    return np.median(b, axis=2)


def _rectangles(h: np.ndarray, cap: int) -> list[tuple[int, int, int, int, float]]:
    """Greedy rectangles (r0, c0, r1, c1 exclusive, height) of equal raised height, at most cap cells a side."""
    used = np.zeros(h.shape, bool)
    out = []
    rows, cols = h.shape
    for r in range(rows):
        c = 0
        while c < cols:
            v = h[r, c]
            if v <= 0 or used[r, c]:
                c += 1
                continue
            c1 = c + 1
            while c1 < cols and c1 - c < cap and h[r, c1] == v and not used[r, c1]:
                c1 += 1
            r1 = r + 1
            while r1 < rows and r1 - r < cap and (h[r1, c:c1] == v).all() and not used[r1, c:c1].any():
                r1 += 1
            used[r:r1, c:c1] = True
            out.append((r, c, r1, c1, float(v)))
            c = c1
    return out


def block_mesh(h: np.ndarray, grid: Any, top_y: float, c: np.ndarray, uv_of: Any
               ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Blocks (vertices, uv, faces) of a cell height map on `grid` (cells of grid.res), standing on the top plane:
    rectangle tops and walls where neighbours differ. Points move along the top still's rays (centre c) with height;
    uv_of(xz at top_y) gives texture coordinates (walls take the colour just inside their higher cell)."""
    verts: list = []
    uvs: list = []
    faces: list = []
    res = grid.res

    def xz(r: np.ndarray, col: np.ndarray) -> np.ndarray:
        return grid.origin + col[:, None] * res * grid.u + r[:, None] * res * grid.v

    def lift(p: np.ndarray, hv: np.ndarray) -> np.ndarray:
        x0 = np.c_[p[:, 0], np.full(len(p), top_y), p[:, 1]]
        s = (top_y + hv - c[1]) / (top_y - c[1])
        return c + s[:, None] * (x0 - c)

    up = np.cross(np.r_[grid.u[0], 0, grid.u[1]], np.r_[grid.v[0], 0, grid.v[1]])[1] > 0   # (col, row) seen from above

    def quad(p4: np.ndarray, uv4: np.ndarray, flip: bool):
        n = sum(len(x) for x in verts)
        verts.append(p4)
        uvs.append(uv4)
        a, b, cc, d = n, n + 1, n + 2, n + 3
        faces.extend([[a, cc, b], [a, d, cc]] if flip else [[a, b, cc], [a, cc, d]])

    cap = max(1, int(round(MAX_QUAD_M / res)))
    for r0, c0, r1, c1, v in _rectangles(h, cap):
        rr = np.array([r0, r0, r1, r1], float)
        cc_ = np.array([c0, c1, c1, c0], float)
        p = xz(rr, cc_)
        quad(lift(p, np.full(4, v)), uv_of(p), not up)
    rows, cols = h.shape
    hp = np.pad(h, 1)
    # walls between horizontal neighbours (shared vertical grid line at column j), merged along runs of rows
    for axis in (0, 1):
        if axis == 0:   # neighbours (r, j-1) | (r, j): edge along rows at column j
            a = hp[1:-1, :-1]
            b = hp[1:-1, 1:]
        else:           # neighbours (i-1, c) / (i, c): edge along columns at row i
            a = hp[:-1, 1:-1].T
            b = hp[1:, 1:-1].T
        diff = a != b
        for line in range(diff.shape[1]):
            run = diff[:, line]
            k = 0
            n = len(run)
            while k < n:
                if not run[k]:
                    k += 1
                    continue
                ha, hb = a[k, line], b[k, line]
                k1 = k + 1
                while k1 < n and run[k1] and a[k1, line] == ha and b[k1, line] == hb:
                    k1 += 1
                lo, hi_ = min(ha, hb), max(ha, hb)
                inside = -0.5 if ha > hb else 0.5          # towards the higher cell
                s = np.array([k, k1, k1, k], float)
                if axis == 0:
                    rr, cc_ = s, np.full(4, float(line))
                    uvp = xz(rr, cc_ + inside)
                else:
                    rr, cc_ = np.full(4, float(line)), s
                    uvp = xz(rr + inside, cc_)
                p = xz(rr, cc_)
                hv = np.array([lo, lo, hi_, hi_])
                flip = (ha > hb) != (axis == 0)
                quad(lift(p, hv), uv_of(uvp), flip if up else not flip)
                k = k1
    if not faces:
        return np.zeros((0, 3)), np.zeros((0, 2)), np.zeros((0, 3), np.int64)
    return np.vstack(verts), np.vstack(uvs), np.asarray(faces, np.int64)


# --- the hook --------------------------------------------------------------------------------------------------------

def add_relief(model: Any, views: Sequence[Any], tex: Any, table_y: float, lidar: np.ndarray | None) -> Any:
    """The slab (flat.FlatModel) with its raised parts as blocks on its top, from the stills (`views`, best first: the
    first textures the top) on the top's texture frame `tex`. Unchanged, with the reason in stats["relief"], when the
    photos cannot give heights."""
    from repairos.recreation.flat import Grid, _inside

    t0 = time.perf_counter()
    try:
        import torch  # noqa: F401
    except ImportError:
        model.stats["relief"] = "not built: torch is not installed (backend[vision])"
        return model
    top_y = float(model.vertices[0, 1])
    k = len(model.outline)
    shape = (int(np.ceil(tex.shape[0] * tex.res / RES_M)), int(np.ceil(tex.shape[1] * tex.res / RES_M)))
    grid = Grid(tex.origin, tex.u, tex.v, RES_M, shape)
    centres = grid.world(top_y)[:, [0, 2]]
    mask = _inside(centres, model.outline).reshape(shape)
    key = (tuple(v.name for v in views), tuple(float(x) for v in views for x in v.pose.t), tuple(grid.origin),
           shape, round(top_y, 5))
    try:
        if key in _CACHE:
            rel, h = _CACHE[key]
        else:
            rel = relief_heights(views[0], views[1:], grid, top_y, mask)
            h = clean(rel, mask)
            _CACHE[key] = (rel, h)
            while len(_CACHE) > 4:
                _CACHE.popitem(last=False)
    except ReliefUnavailable as e:
        model.stats["relief"] = f"not built: {e}"
        return model
    # the texture's affine map on the top, fitted on the outline's corners (exact)
    A = np.c_[model.outline, np.ones(k)]
    uv_map, *_ = np.linalg.lstsq(A, np.asarray(model.uv[:k], float), rcond=None)

    def uv_of(xz: np.ndarray) -> np.ndarray:
        return np.c_[xz, np.ones(len(xz))] @ uv_map

    f = max(1, int(round(MESH_CELL_M / RES_M)))
    hc = _coarsen(h, f)
    cgrid = Grid(grid.origin, grid.u, grid.v, RES_M * f, hc.shape)
    hc[~_inside(cgrid.world(top_y)[:, [0, 2]], model.outline).reshape(hc.shape)] = 0.0
    budget = MAX_TRIANGLES - len(model.faces)
    v, uv, fc = block_mesh(hc, cgrid, top_y, views[0].pose.t.astype(float), uv_of)
    while len(fc) > budget and f < 16:
        f *= 2
        hc = _coarsen(h, f)
        cgrid = Grid(grid.origin, grid.u, grid.v, RES_M * f, hc.shape)
        v, uv, fc = block_mesh(hc, cgrid, top_y, views[0].pose.t.astype(float), uv_of)
    check = lidar_check(h, grid, top_y, lidar)
    raised = h >= RAISED_M
    model.stats["relief"] = (f"{int(raised.sum() * RES_M ** 2 * 1e6)} mm2 raised, up to "
                             f"{h.max() * 1000:.1f} mm, from {len(rel.stats['views'])} photos")
    model.stats["relief_detail"] = {**rel.stats, "lidar": check, "cell_mm": RES_M * f * 1000,
                                    "triangles": int(len(fc)), "total_s": round(time.perf_counter() - t0, 3)}
    if len(fc):
        n0 = len(model.vertices)
        model.vertices = np.vstack([model.vertices, v.astype(np.float32)])
        model.uv = np.vstack([model.uv, uv.astype(np.float32)])
        model.faces = np.vstack([model.faces, (fc + n0).astype(np.int32)])
    model.relief = Relief(h, rel.confident, grid, rel.stats)
    return model
