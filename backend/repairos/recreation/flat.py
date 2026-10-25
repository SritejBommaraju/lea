"""Thin objects (a circuit board, a remote, a phone, a book) as a photo-textured slab on the measured table.

LiDAR cannot model them: a board's 1.6 mm and its low parts sit inside the band where each depth frame's table noise
is cut (3-12 mm), and dark boards are mostly medium confidence. Measured on a real board scan (2026-10-25): of
24,289 in-box points the old horizontal cut (twice a "noise" inflated by the board itself, 11.6 mm) kept 774, and
the object pieces 57. The photos, though, see it sharply and their poses are good. So:

    model = flat_model(pkg_local, stills_local, box, table_y, lidar_points)   # FlatModel, or raises FlatUnavailable

1. Each posed full-resolution still is rectified onto the table plane (every top-view pixel at the slab's top height
   projected into the still: pose + intrinsics + plane).
2. The footprint: in the most top-down sharp still's rectified image, pixels whose colour is far from the table's
   (Mahalanobis distance to the colours round the object, 99.9% of a normal table), opened to drop wires and specks,
   the piece over the LiDAR footprint (or the box centre), holes filled. A rectangle (minimum-area) when the piece
   fills it (most boards, phones, books), else its convex hull.
3. The slab: that outline from the table up to the measured thickness (LiDAR heights inside the footprint), its top
   textured from that one still at full resolution (several photos blended would smear parts that stand up above the
   board: parallax), gaps filled from the next best stills that agree with it; sides and bottom plain.
4. Checks before it is used: the outline is found and fits the crop box, its sides are measured (an identity's size
   is only compared, never imposed), and most of the next CHECK_STILLS stills rectified onto the same plane land on
   the first within ALIGN_MM (poses and plane agree, so photo pixels map onto the slab; one photo with a bad pose
   does not sink it).
"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

THIN_M = 0.02            # an object whose LiDAR points mostly lie this close to the table is thin (75th percentile)
SEG_RES_M = 0.0005       # footprint found on a 0.5 mm top-view grid
TEX_RES_M = 0.0001       # top texture at 0.1 mm per pixel (a still sees ~0.07 mm per pixel from 20 cm)
TABLE_SIGMA = 4.0        # colour distance (Mahalanobis) beyond which a pixel is not table: chi2(3) at 99.9% = 16.3
NEAR_LIDAR_M = 0.006     # the photo's object stays within this of where LiDAR saw something above the table (a
                         # board's raised parts cover it; glossy table streaks and wires off the board do not)
OPEN_M = 0.003           # opening radius: wires (~2 mm) and specks leave the footprint
BODY_SHARE = 0.7         # rows of the main body span at least this share of its typical width (a connector, a
                         # glint on the table or a cable stub at the edge spans less)
RECT_FILL = 0.85         # the piece fills its minimum-area rectangle this much: use the rectangle
ALIGN_MM = 10.0          # another still must land within this on the slab (real UNO scans: still poses 3-9 mm apart)
CHECK_STILLS = 4         # stills compared with the top one; most of them must agree
MIN_TOP_COS = 0.6        # stills looking at least this much down (53 deg from vertical) texture the top


DEBUG: dict | None = None   # tests and diagnosis: the footprint's intermediate masks


class FlatUnavailable(RuntimeError):
    pass


@dataclass
class FlatModel:
    vertices: np.ndarray        # N x 3, arkit_world metres
    uv: np.ndarray              # N x 2, 0..1, v down
    faces: np.ndarray           # M x 3
    atlas: Any                  # PIL.Image
    outline: np.ndarray         # K x 2 (x, z) world, the footprint polygon
    stats: dict = field(default_factory=dict)


def thin_object(points: np.ndarray, table_y: float | None) -> tuple[bool, float | None]:
    """(thin, height): three quarters of the object's LiDAR points (segment_align.segment_object_cloud) within THIN_M
    of the table, or too few to say otherwise. The 75th percentile, not a higher one: a board's frames cut most of
    its 2-3 mm top with the table, so its few tall parts, a cable and anything else in a generous box are a large
    share of what is left (a real UNO board: 95th percentile 35 mm, 75th 9 mm; a mouse 34 mm and a controller 46 mm
    at the 75th)."""
    if table_y is None:
        return False, None
    if len(points) < 200:
        return True, None
    h = float(np.percentile(points[:, 1] - table_y, 75))
    return h <= THIN_M, h


# --- rectification ----------------------------------------------------------------------------------------------------

@dataclass
class Grid:
    """A top-view grid over the box: pixel (row i, col j) is the world point origin + j*res*u + i*res*v at height y."""
    origin: np.ndarray           # x, z of pixel (0, 0)
    u: np.ndarray                # unit x, z of columns
    v: np.ndarray                # unit x, z of rows
    res: float
    shape: tuple[int, int]

    def world(self, y: float, ij: np.ndarray | None = None) -> np.ndarray:
        if ij is None:
            i, j = np.mgrid[0:self.shape[0], 0:self.shape[1]]
            ij = np.stack([i.ravel(), j.ravel()], 1)
        xz = self.origin + (ij[:, 1:2] + 0.5) * self.res * self.u + (ij[:, 0:1] + 0.5) * self.res * self.v
        return np.stack([xz[:, 0], np.full(len(xz), y), xz[:, 1]], 1)

    def to_pixel(self, xz: np.ndarray) -> np.ndarray:
        d = xz - self.origin
        return np.stack([d @ self.v / self.res, d @ self.u / self.res], 1)   # (row, col), fractional


def box_grid(box: Any, res: float, pad: float = 0.0) -> Grid:
    """Grid over the box's footprint (its yaw), grown by `pad` metres."""
    ax = box.matrix[:3, :3] / box.extent
    u = np.array([ax[0, 0], ax[2, 0]]); u /= np.linalg.norm(u)
    v = np.array([ax[0, 2], ax[2, 2]]); v /= np.linalg.norm(v)
    half = np.array([box.extent[0], box.extent[2]]) / 2 + pad
    c = np.array([box.centre[0], box.centre[2]])
    origin = c - half[0] * u - half[1] * v
    return Grid(origin, u, v, res, (int(np.ceil(2 * half[1] / res)), int(np.ceil(2 * half[0] / res))))


def rectify(image: np.ndarray, pose: Any, cam: Any, grid: Grid, y: float, ij: np.ndarray | None = None
            ) -> tuple[np.ndarray, np.ndarray]:
    """Colours (N x 3 float) of the grid pixels (all, or `ij`) seen by one still, and whether each was in view."""
    from repairos.locate.camera import project

    p = grid.world(y, ij)
    uv, z = project(p, pose, cam)
    h, w = image.shape[:2]
    ok = (z > 0.05) & (uv[:, 0] >= 0) & (uv[:, 0] < w - 1) & (uv[:, 1] >= 0) & (uv[:, 1] < h - 1)
    out = np.zeros((len(p), 3))
    x, yy = uv[ok, 0], uv[ok, 1]
    x0, y0 = np.floor(x).astype(int), np.floor(yy).astype(int)
    fx, fy = (x - x0)[:, None], (yy - y0)[:, None]
    im = image.astype(float)
    out[ok] = ((1 - fx) * (1 - fy) * im[y0, x0] + fx * (1 - fy) * im[y0, x0 + 1] + (1 - fx) * fy * im[y0 + 1, x0]
               + fx * fy * im[y0 + 1, x0 + 1])
    return out, ok


@dataclass
class Still:
    name: str
    image: np.ndarray
    pose: Any
    cam: Any
    down: float          # how much it looks down (cos from vertical)
    sharpness: float


def load_stills(stills: Sequence[Mapping[str, Any]], box: Any) -> list[Still]:
    """Posed full-resolution stills that look down at the box, best (most top-down x sharpest) first."""
    from PIL import Image

    from repairos.locate.camera import Pinhole, Pose

    out = []
    for s in stills:
        if s.get("pose_reliable") is False or not s.get("camera") or not s.get("intrinsics"):
            continue
        p = Path(s["image_file"])
        if not p.exists():
            continue
        with Image.open(p) as im:
            img = np.asarray(im.convert("RGB"))
        pose = Pose.from_contract(s["camera"])
        cam = Pinhole.from_contract(s["intrinsics"], img.shape[1], img.shape[0])
        fwd = -pose.r[:, 2]
        to_box = box.centre - pose.t
        if fwd @ to_box <= 0:
            continue
        down = float(-fwd[1])
        if down < MIN_TOP_COS:
            continue
        out.append(Still(s["still_id"], img, pose, cam, down, float(s.get("sharpness") or 1.0)))
    if not out:
        return out
    sh = np.array([s.sharpness for s in out])
    rel = sh / (np.median(sh) or 1.0)
    order = np.argsort([-(s.down ** 4) * min(r, 1.5) for s, r in zip(out, rel, strict=True)])
    return [out[k] for k in order]


# --- footprint -------------------------------------------------------------------------------------------------------

def _dilate(m: np.ndarray, r: int) -> np.ndarray:
    from repairos.recon.segment_align import _window_min

    return -_window_min(-m.astype(np.int8), r) > 0


def _erode(m: np.ndarray, r: int) -> np.ndarray:
    return ~_dilate(~m, r)


def _components(m: np.ndarray) -> np.ndarray:
    """4-connected labels (0 = background)."""
    lab = np.zeros(m.shape, np.int32)
    n = 0
    h, w = m.shape
    for i0, j0 in zip(*np.nonzero(m), strict=True):
        if lab[i0, j0]:
            continue
        n += 1
        lab[i0, j0] = n
        q = deque([(i0, j0)])
        while q:
            i, j = q.popleft()
            for a, b in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                if 0 <= a < h and 0 <= b < w and m[a, b] and not lab[a, b]:
                    lab[a, b] = n
                    q.append((a, b))
    return lab


def _fill_holes(m: np.ndarray) -> np.ndarray:
    outside = np.pad(~m, 1, constant_values=True)
    lab = _components(outside)
    return m | (lab[1:-1, 1:-1] != lab[0, 0])


def _hull(p: np.ndarray) -> np.ndarray:
    """Convex hull (monotone chain), counter-clockwise."""
    p = np.unique(p, axis=0)
    p = p[np.lexsort((p[:, 1], p[:, 0]))]
    if len(p) < 3:
        return p

    def half(pts):
        out: list = []
        for q in pts:
            while len(out) >= 2 and _cross2(out[-1] - out[-2], q - out[-2]) <= 0:
                out.pop()
            out.append(q)
        return out
    lo, hi = half(p), half(p[::-1])
    return np.array(lo[:-1] + hi[:-1])


def _cross2(a: np.ndarray, b: np.ndarray) -> float:
    return float(a[0] * b[1] - a[1] * b[0])


def _min_rect(hull: np.ndarray) -> tuple[np.ndarray, float]:
    """Minimum-area rectangle round a convex hull: (4 corners, area)."""
    best = None
    for k in range(len(hull)):
        e = hull[(k + 1) % len(hull)] - hull[k]
        n = np.linalg.norm(e)
        if n == 0:
            continue
        a = e / n
        b = np.array([-a[1], a[0]])
        pa, pb = hull @ a, hull @ b
        area = (pa.max() - pa.min()) * (pb.max() - pb.min())
        if best is None or area < best[1]:
            corners = np.array([pa.min() * a + pb.min() * b, pa.max() * a + pb.min() * b,
                                pa.max() * a + pb.max() * b, pa.min() * a + pb.max() * b])
            best = (corners, area)
    return best


def _body_span(s: np.ndarray, res: float) -> tuple[float, float]:
    """The contiguous span along an axis where the piece is at least BODY_SHARE as wide as typical (2-pixel bins,
    smoothed over 3, so a grid turned against the axis or pixel centres on bin edges leave no gaps)."""
    res = 2 * res
    lo = float(s.min())
    counts = np.bincount(np.floor((s - lo) / res).astype(int)).astype(float)
    counts = np.convolve(np.pad(counts, 1, mode="edge"), np.ones(3) / 3, mode="valid")   # pixel centres on bin edges
    typical = float(np.median(counts[counts > 0]))
    ok = counts >= BODY_SHARE * typical
    k = int(np.argmax(counts))
    i0 = k
    while i0 > 0 and ok[i0 - 1]:
        i0 -= 1
    i1 = k
    while i1 < len(ok) - 1 and ok[i1 + 1]:
        i1 += 1
    return lo + i0 * res, lo + (i1 + 1) * res


def _polygon_area(p: np.ndarray) -> float:
    return 0.5 * abs(float(np.dot(p[:, 0], np.roll(p[:, 1], 1)) - np.dot(p[:, 1], np.roll(p[:, 0], 1))))


def footprint(still: Still, grid: Grid, y: float, lidar_xz: np.ndarray | None) -> tuple[np.ndarray, dict]:
    """The object's outline (K x 2 world x, z) in one still rectified at height y; see the module docstring."""
    col, ok = rectify(still.image, still.pose, still.cam, grid, y)
    img = col.reshape(*grid.shape, 3)
    seen = ok.reshape(grid.shape)
    lidar = np.zeros(grid.shape, bool)
    if lidar_xz is not None and len(lidar_xz) >= 20:
        rc = np.floor(grid.to_pixel(lidar_xz)).astype(int)
        inb = (rc[:, 0] >= 0) & (rc[:, 0] < grid.shape[0]) & (rc[:, 1] >= 0) & (rc[:, 1] < grid.shape[1])
        lidar[rc[inb, 0], rc[inb, 1]] = True
        lidar = _erode(_dilate(lidar, 4), 2)      # close the sparse LiDAR cover
    near = _dilate(lidar, int(NEAR_LIDAR_M / grid.res)) if lidar.any() else np.zeros(grid.shape, bool)
    far = seen & ~_dilate(lidar, int(0.03 / grid.res)) if lidar.any() else seen.copy()
    if not lidar.any():   # no LiDAR footprint: the table is the outer band of the box
        i, j = np.mgrid[0:grid.shape[0], 0:grid.shape[1]]
        edge = np.minimum(np.minimum(i, grid.shape[0] - 1 - i), np.minimum(j, grid.shape[1] - 1 - j))
        far &= edge < min(grid.shape) * 0.15
    table = img[far]
    if len(table) < 500:
        raise FlatUnavailable("too little table seen round the object to tell it apart")
    mu = table.mean(0)
    cov = np.cov(table.T) + np.eye(3) * 4.0
    d = img.reshape(-1, 3) - mu
    dist = np.sqrt(np.einsum("ij,jk,ik->i", d, np.linalg.inv(cov), d)).reshape(grid.shape)
    obj = seen & (dist > TABLE_SIGMA)
    if lidar.any():
        obj &= near
    r = max(1, int(round(OPEN_M / grid.res)))
    # Close first: a busy top (a board's pins, black chips, silkscreen next to the table's colour) leaves the object
    # full of small gaps, which the opening alone would widen until it falls apart into pieces (measured on a real
    # board: its largest piece was a third of it). Then open: wires and specks go.
    obj = _erode(_dilate(obj, r), r) & seen
    obj = _dilate(_erode(obj, r), r)
    lab = _components(obj)
    if not lab.max():
        raise FlatUnavailable("no object found apart from the table in the top-down photo")
    if lidar.any():
        counts = np.bincount(lab[lidar & (lab > 0)].ravel(), minlength=lab.max() + 1)
        if counts.max() == 0:
            raise FlatUnavailable("the object in the photo does not cover the LiDAR footprint")
        k = int(np.argmax(counts))
    else:
        c = np.array(grid.shape) // 2
        sizes = np.bincount(lab.ravel())
        sizes[0] = 0
        k = int(np.argmax(sizes))
        if lab[c[0], c[1]] and sizes[lab[c[0], c[1]]] > 0.2 * sizes[k]:
            k = int(lab[c[0], c[1]])
    piece = _fill_holes(lab == k)
    if DEBUG is not None:
        DEBUG.update(img=img, piece=piece, obj=obj, lidar=lidar, near=near, dist=dist)
    ii, jj = np.nonzero(piece & ~_erode(piece, 1))      # its boundary is enough for the hull
    edge_pts = np.stack([ii, jj], 1)
    xz = grid.world(y, edge_pts)[:, [0, 2]]
    hull = _hull(np.round(xz / grid.res).astype(np.int64)) * grid.res
    if len(hull) < 3:
        raise FlatUnavailable(f"the object's outline is degenerate ({int(piece.sum())} pixels)")
    rect, rect_area = _min_rect(hull)
    area = piece.sum() * grid.res ** 2
    # The main body: along the rectangle's two axes, the rows where the piece spans at least half its typical width
    # (connectors, a stray glint or a cable stub sticking out of a board's edge are narrower).
    pi, pj = np.nonzero(piece)
    pxz = grid.world(y, np.stack([pi, pj], 1))[:, [0, 2]]
    ax = (rect[1] - rect[0]) / np.linalg.norm(rect[1] - rect[0])
    bx = (rect[3] - rect[0]) / np.linalg.norm(rect[3] - rect[0])
    sa, sb = (pxz - rect[0]) @ ax, (pxz - rect[0]) @ bx
    a0, a1 = _body_span(sa, grid.res)
    b0, b1 = _body_span(sb, grid.res)
    body = np.array([rect[0] + a0 * ax + b0 * bx, rect[0] + a1 * ax + b0 * bx, rect[0] + a1 * ax + b1 * bx,
                     rect[0] + a0 * ax + b1 * bx])
    inside = (sa >= a0) & (sa <= a1) & (sb >= b0) & (sb <= b1)
    body_area = (a1 - a0) * (b1 - b0)
    fill = float(inside.sum() * grid.res ** 2 / body_area) if body_area > 0 else 0.0
    rectangle = fill >= RECT_FILL
    outline = body if rectangle else hull
    sides = sorted([a1 - a0, b1 - b0]) if rectangle else sorted([float(np.linalg.norm(rect[1] - rect[0])),
                                                                  float(np.linalg.norm(rect[2] - rect[1]))])
    info = {"area_cm2": round(float(area) * 1e4, 1), "body_fill": round(fill, 3), "shape": "rectangle" if rectangle
            else "hull", "sides_mm": [round(s * 1000, 1) for s in sides],
            "outside_body_share": round(float(1 - inside.mean()), 3), "table_pixels": int(len(table)),
            "lidar_cover": bool(lidar.any())}
    return outline, info


# --- the slab --------------------------------------------------------------------------------------------------------

def flat_model(pkg_local: Mapping[str, Any], stills: Sequence[Mapping[str, Any]], box: Any, table_y: float,
               lidar: np.ndarray | None, *, size_prior_mm: Sequence[float] | None = None) -> FlatModel:
    """The thin object as a photo-textured slab (module docstring). `lidar`: its LiDAR points (any count; for the
    footprint seed and the thickness). `size_prior_mm`: an identity's known size, only compared, never imposed."""
    from PIL import Image

    views = load_stills(stills, box)
    if not views:
        raise FlatUnavailable("no posed photo looks down at the object")
    # Thickness: LiDAR heights inside the box's middle (refined to the footprint below), at least 1.5 mm.
    seg_grid = box_grid(box, SEG_RES_M)
    lidar_xz = None if lidar is None or not len(lidar) else lidar[:, [0, 2]]
    best = views[0]
    outline, info = footprint(best, seg_grid, table_y + 0.0016, lidar_xz)
    thick = 0.0016
    if lidar is not None and len(lidar) >= 20:
        inside = _inside(lidar[:, [0, 2]], outline)
        hs = lidar[inside, 1] - table_y
        if len(hs) >= 20:
            thick = float(np.clip(np.percentile(hs, 25), 0.0015, 0.01))
    top_y = table_y + thick
    # Other stills must put the object where the first does (poses and plane agree). One photo's pose can be off on
    # its own (measured on a real board: one of five 6-9 mm off, the rest within 1.5 mm), so up to CHECK_STILLS are
    # compared and most of those that overlap must agree; the ones that disagree never fill the texture.
    agree = []
    if len(views) > 1:
        shifts = {v.name: _agreement(views[0], v, outline, top_y) for v in views[1:1 + CHECK_STILLS]}
        known = {k: x for k, x in shifts.items() if x is not None}
        agree = [v for v in views[1:1 + CHECK_STILLS] if shifts[v.name] is not None and shifts[v.name] <= ALIGN_MM]
        info["check_shifts_mm"] = {k: round(x, 2) for k, x in known.items()}
        if agree:
            info["second_still"] = agree[0].name
            info["second_still_shift_mm"] = round(shifts[agree[0].name], 2)
        elif known:
            k = min(known, key=known.get)
            info["second_still"], info["second_still_shift_mm"] = k, round(known[k], 2)
        if known and 2 * len(agree) < len(known):
            worst = sorted(known.values())[len(known) // 2]
            raise FlatUnavailable(f"the photos put the object {worst:.1f} mm apart on the table: the poses or the "
                                  f"table height disagree")
    # Top texture: the outline's rectangle frame at TEX_RES_M, from the best still, gaps from the next ones.
    rect, _ = _min_rect(_hull(np.round(outline / 1e-5).astype(np.int64)) * 1e-5)
    a = rect[1] - rect[0]
    b = rect[3] - rect[0]
    la, lb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    tex = Grid(rect[0], a / la, b / lb, TEX_RES_M, (int(np.ceil(lb / TEX_RES_M)), int(np.ceil(la / TEX_RES_M))))
    col, ok = rectify(best.image, best.pose, best.cam, tex, top_y)
    used = {best.name: int(ok.sum())}
    for v in agree[:2]:   # gaps from photos that agree with the top one
        if ok.all():
            break
        c2, ok2 = rectify(v.image, v.pose, v.cam, tex, top_y)
        fill = ~ok & ok2
        col[fill] = c2[fill]
        ok |= ok2
        used[v.name] = int(fill.sum())
    top = np.clip(col, 0, 255).astype(np.uint8).reshape(*tex.shape, 3)
    grey_rows = 8
    atlas = np.vstack([top, np.full((grey_rows, tex.shape[1], 3), 70, np.uint8)])
    H = atlas.shape[0]
    # Mesh: the outline at the top (textured) and the table (plain), sides and bottom on the grey strip.
    rc = tex.to_pixel(outline)
    uv_top = np.stack([rc[:, 1] / tex.shape[1], rc[:, 0] / H], 1)
    grey_uv = np.array([0.5, (tex.shape[0] + grey_rows / 2) / H])
    k = len(outline)
    top_v = np.stack([outline[:, 0], np.full(k, top_y), outline[:, 1]], 1)
    bot_v = np.stack([outline[:, 0], np.full(k, table_y), outline[:, 1]], 1)
    verts = np.vstack([top_v, top_v, bot_v])          # top (textured), top edge (grey), bottom (grey)
    uv = np.vstack([uv_top, np.tile(grey_uv, (2 * k, 1))])
    faces = []
    ccw = _polygon_area_signed(outline) > 0
    for t in range(1, k - 1):        # fan (convex outline), facing up (+Y): x-z order flips handedness
        faces.append([0, t + 1, t] if ccw else [0, t, t + 1])
        faces.append([2 * k, 2 * k + t, 2 * k + t + 1] if ccw else [2 * k, 2 * k + t + 1, 2 * k + t])
    for t in range(k):
        a0, a1 = k + t, k + (t + 1) % k
        b0, b1 = 2 * k + t, 2 * k + (t + 1) % k
        faces += ([[a0, a1, b1], [a0, b1, b0]] if ccw else [[a0, b1, a1], [a0, b0, b1]])
    sides = sorted([la, lb])
    stats = {**info, "thickness_mm": round(thick * 1000, 2), "texture_px": [int(tex.shape[1]), int(tex.shape[0])],
             "texture_mm_per_px": TEX_RES_M * 1000, "top_still": best.name, "top_still_down_cos": round(best.down, 3),
             "texture_from": used, "measured_mm": [round(s * 1000, 1) for s in sides]}
    if size_prior_mm:
        prior = sorted(float(x) for x in size_prior_mm)[:2]
        stats["size_prior_mm"] = prior
        stats["size_vs_prior_mm"] = [round(s * 1000 - p, 1) for s, p in zip(sides, prior, strict=True)]
    return FlatModel(verts.astype(np.float32), uv.astype(np.float32), np.asarray(faces, np.int32),
                     Image.fromarray(atlas), outline, stats)


def _polygon_area_signed(p: np.ndarray) -> float:
    return 0.5 * float(np.dot(p[:, 0], np.roll(p[:, 1], -1)) - np.dot(p[:, 1], np.roll(p[:, 0], -1)))


def _inside(q: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Points inside a polygon (even-odd)."""
    inside = np.zeros(len(q), bool)
    x, y = q[:, 0], q[:, 1]
    for k in range(len(poly)):
        x0, y0 = poly[k]
        x1, y1 = poly[(k + 1) % len(poly)]
        cross = ((y0 > y) != (y1 > y)) & (x < (x1 - x0) * (y - y0) / ((y1 - y0) or 1e-12) + x0)
        inside ^= cross
    return inside


def _agreement(s1: Still, s2: Still, outline: np.ndarray, y: float) -> float | None:
    """How far apart (mm) two stills put the object's texture on the plane: phase correlation of their rectified
    grey images over the outline's box (0.25 mm grid)."""
    lo, hi = outline.min(0) - 0.005, outline.max(0) + 0.005
    res = 0.00025
    shape = (int((hi[1] - lo[1]) / res), int((hi[0] - lo[0]) / res))
    g = Grid(lo, np.array([1.0, 0.0]), np.array([0.0, 1.0]), res, shape)
    a, oka = rectify(s1.image, s1.pose, s1.cam, g, y)
    b, okb = rectify(s2.image, s2.pose, s2.cam, g, y)
    both = oka & okb
    if both.mean() < 0.5:
        return None
    ga = np.where(both, a.mean(1), 0).reshape(g.shape)
    gb = np.where(both, b.mean(1), 0).reshape(g.shape)
    ga, gb = ga - ga[both.reshape(g.shape)].mean(), gb - gb[both.reshape(g.shape)].mean()
    ga[~both.reshape(g.shape)] = 0
    gb[~both.reshape(g.shape)] = 0
    fa, fb = np.fft.fft2(ga), np.fft.fft2(gb)
    r = fa * np.conj(fb)
    r /= np.abs(r) + 1e-9
    c = np.fft.ifft2(r).real
    k = np.unravel_index(int(np.argmax(c)), c.shape)
    d = np.array([k[0] if k[0] < c.shape[0] // 2 else k[0] - c.shape[0],
                  k[1] if k[1] < c.shape[1] // 2 else k[1] - c.shape[1]])
    return float(np.linalg.norm(d) * res * 1000)


# --- the pipeline's entry points --------------------------------------------------------------------------------------

def scan_flat_model(pkg_local: Mapping[str, Any], stills_local: Sequence[Mapping[str, Any]], *,
                    size_prior_mm: Sequence[float] | None = None) -> tuple[FlatModel | None, str]:
    """(model, why): the flat model when the scan's object is thin (thin_object on its per-frame cut LiDAR cloud) and
    the photos give a checked slab; else (None, the reason). Hand-held scans and scans without a table are never
    flat here."""
    from repairos.pipeline.mesh import keyframes_in_frame, plane_height
    from repairos.pipeline.segments import frames_of
    from repairos.recon.segment_align import segment_object_cloud
    from repairos.recon.tsdf import ObjectBox

    if pkg_local.get("pose_source") == "object_tracked" or not pkg_local.get("crop_box") or not stills_local:
        return None, "not a table-top scan with photos"
    box = ObjectBox.from_contract(pkg_local["crop_box"])
    same = {0} | {int(k.get("segment") or 0) for k in keyframes_in_frame(pkg_local)}
    frames = frames_of(pkg_local, same)
    if not frames:
        return None, "no depth keyframes"
    cloud, table = segment_object_cloud(frames, box, plane_height(pkg_local, frames, box), voxel_m=0.0025)
    if table.height_m is None:
        return None, "no table measured"
    thin, h = thin_object(cloud.points, table.height_m)
    if not thin:
        return None, f"not thin ({h * 1000:.0f} mm tall)" if h is not None else "not thin"
    try:
        m = flat_model(pkg_local, stills_local, box, table.height_m, cloud.points, size_prior_mm=size_prior_mm)
    except FlatUnavailable as e:
        return None, f"thin, but no photo slab: {e}"
    m.stats.update(lidar_points=len(cloud), lidar_height_mm=None if h is None else round(h * 1000, 1),
                   table_y_m=round(table.height_m, 4))
    return m, "thin object: a photo-textured slab on the measured table"


def slab_points(m: FlatModel, step: float = 0.0002) -> tuple[np.ndarray, np.ndarray]:
    """The slab's surface as coloured points (the top every `step` from its texture; sides and bottom sparser)."""
    atlas = np.asarray(m.atlas)
    h, w = atlas.shape[:2]
    rng = np.random.default_rng(0)
    pts, cols = [], []
    for f in m.faces:
        v, uv = m.vertices[f].astype(float), m.uv[f].astype(float)
        area = np.linalg.norm(np.cross(v[1] - v[0], v[2] - v[0])) / 2
        n = int(area / step ** 2) + 1
        r = rng.random((n, 2))
        flip = r.sum(1) > 1
        r[flip] = 1 - r[flip]
        wts = np.stack([1 - r.sum(1), r[:, 0], r[:, 1]], 1)
        q = wts @ uv
        pts.append(wts @ v)
        cols.append(atlas[np.clip((q[:, 1] * h).astype(int), 0, h - 1), np.clip((q[:, 0] * w).astype(int), 0, w - 1)])
    return np.vstack(pts), np.vstack(cols).astype(np.uint8)


def _nearest_on_outline(q: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Each point's nearest point on the polygon's boundary."""
    best = np.full(len(q), np.inf)
    out = q.copy()
    for k in range(len(poly)):
        a, b = poly[k], poly[(k + 1) % len(poly)]
        ab = b - a
        t = np.clip(((q - a) @ ab) / max(float(ab @ ab), 1e-18), 0.0, 1.0)
        c = a + t[:, None] * ab
        d = np.einsum("ij,ij->i", q - c, q - c)
        closer = d < best
        best[closer], out[closer] = d[closer], c[closer]
    return out


def slab_mesh(m: FlatModel, cell: float = 0.0005, max_triangles: int | None = None
              ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The slab as a vertex-coloured mesh (the report's PLY): its top a `cell` grid over the outline (any convex
    polygon: a rectangle, or a hull with three or more corners), coloured from the texture, plus the sides and bottom.
    Grid cells cut by the outline keep their inside corners and have the outside ones moved onto the outline, so the
    top covers exactly the outline. `max_triangles`: the cell grows until the mesh fits (a plane is never decimated:
    it would collapse to a few triangles and lose its colours). Returns (vertices, faces, colours)."""
    atlas = np.asarray(m.atlas)
    h, w = atlas.shape[:2]
    top_y = float(m.vertices[0, 1])
    k = len(m.outline)
    o = np.asarray(m.outline, float)
    rest = np.array([f for f in m.faces if not (f < k).all()])
    # The texture is an affine map of (x, z) on the top: fitted on the outline's corners (exact)
    A = np.c_[o, np.ones(k)]
    uv_map, *_ = np.linalg.lstsq(A, np.asarray(m.uv[:k], float), rcond=None)
    # the grid along the outline's first edge, over the outline's extent in that frame
    a = (o[1] - o[0]) / max(float(np.linalg.norm(o[1] - o[0])), 1e-12)
    b = np.array([-a[1], a[0]])
    sa, sb = (o - o[0]) @ a, (o - o[0]) @ b
    la, lb = float(sa.max() - sa.min()), float(sb.max() - sb.min())
    def nodes(c: float) -> tuple[int, int]:
        return max(2, int(np.ceil(la / c)) + 1), max(2, int(np.ceil(lb / c)) + 1)
    if max_triangles:
        budget = max(1000, int(max_triangles) - len(rest))
        cell = max(cell, float(np.sqrt(2.0 * la * lb / budget)))
        while 2 * (nodes(cell)[0] - 1) * (nodes(cell)[1] - 1) > budget:
            cell *= 1.01
    na, nb = nodes(cell)
    s_, t_ = np.meshgrid(np.linspace(sa.min(), sa.max(), na), np.linspace(sb.min(), sb.max(), nb))
    xz = o[0] + s_.reshape(-1, 1) * a + t_.reshape(-1, 1) * b
    inside = _inside(xz, o)
    on_edge = np.linalg.norm(_nearest_on_outline(xz, o) - xz, axis=1) < 1e-9
    inside |= on_edge
    idx = np.arange(na * nb).reshape(nb, na)
    q00, q01, q10, q11 = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
    up = _cross2(a, b) < 0     # (a, b) as seen from above: faces wind to face +Y
    tris = np.vstack([np.stack([q00, q01, q11], 1), np.stack([q00, q11, q10], 1)]) if up else \
        np.vstack([np.stack([q00, q11, q01], 1), np.stack([q00, q10, q11], 1)])
    tris = tris[inside[tris].any(1)]                        # cells the outline reaches
    moved = ~inside
    xz[moved] = _nearest_on_outline(xz[moved], o)           # their outside corners onto the outline
    # drop the slivers and folds this leaves (a corner of the outline cuts a cell)
    p0, p1, p2 = xz[tris[:, 0]], xz[tris[:, 1]], xz[tris[:, 2]]
    area = (p1[:, 0] - p0[:, 0]) * (p2[:, 1] - p0[:, 1]) - (p1[:, 1] - p0[:, 1]) * (p2[:, 0] - p0[:, 0])
    ref = area[inside[tris].all(1)]   # the grid's own winding, from cells wholly inside
    sign = float(np.sign(np.median(ref if len(ref) else area))) or 1.0
    tris = tris[area * sign > 1e-12]
    used = np.unique(tris)
    remap = np.full(len(xz), -1)
    remap[used] = np.arange(len(used))
    xz, tris = xz[used], remap[tris]
    uv = np.c_[xz, np.ones(len(xz))] @ uv_map
    col = atlas[np.clip((uv[:, 1] * h).astype(int), 0, h - 1), np.clip((uv[:, 0] * w).astype(int), 0, w - 1)]
    verts = np.stack([xz[:, 0], np.full(len(xz), top_y), xz[:, 1]], 1)
    # sides and bottom from the slab's own faces past its top fan (all grey)
    n0 = len(verts)
    verts = np.vstack([verts, m.vertices.astype(float)])
    faces = np.vstack([tris, rest + n0])
    col = np.vstack([col, np.full((len(m.vertices), 3), 70, np.uint8)])
    return verts, faces.astype(np.int32), col.astype(np.uint8)
