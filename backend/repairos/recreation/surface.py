"""The recreation's surface: the object AND the patch of table it stands on, from the keyframes' LiDAR depth.

    s = scan_surface(pkg_local)      # Surface(vertices, faces, ground_y, region, ...)

Why the table patch: the report's earlier mesh (pipeline.mesh) cuts everything below the support plane + its noise,
which on the phone's LiDAR (about 1 cm of noise at arm's length) removes the lower centimetre of every object and
leaves flat objects (a controller, a charger) as a few hundred triangles floating in the air. Keeping a disc of the
table makes the object sit on its own real surface, textured from the same photos, and costs nothing on tall objects.
The viewer fades the disc's rim into the background.

Method: a dense TSDF over the object box grown sideways by GROW (each keyframe's depth, high confidence weighted 1,
medium 0.4), the zero level extracted with naive Surface Nets (one vertex per sign-changing cell at the mean of its
edge crossings, one quad per sign-changing edge: more even triangles than a cube-table isosurface, which suits texturing),
the piece under the box centre kept, Taubin-smoothed. The same steps run on the phone (Report/Recreation/
ShellBuilder.swift), so the instant shell and the server's mesh agree.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

GROW = 0.35                 # the region reaches this share of the box's larger side beyond each side of the box
GROW_MIN_M = 0.04
BELOW_M = 0.015             # the region starts this far below the table
ABOVE_M = 0.03              # and ends this far above the box top
TRUNC_VOXELS = 3.0
TRUNC_MIN_M = 0.012         # never truncate tighter than the LiDAR noise
MIN_WEIGHT = 1.5            # a voxel takes part in the surface once its summed weight reaches this
VOXEL_LIMITS_M = (0.0025, 0.006)
FOOTPRINT_MARGIN = 0.1     # share of the box's half side beyond it that still counts as the object
TABLE_BAND_M = 0.012       # outside the footprint, geometry up to this far above the table is the table
MAX_VOXELS = 3_000_000


@dataclass
class Region:
    """The volume meshed: a grid in the box's own axes (yaw), `origin` the corner voxel centre in box-local metres."""
    rot: np.ndarray            # 3x3 box axes (columns) in the world, unscaled
    centre: np.ndarray         # world point the local frame is centred on (the box centre)
    lo: np.ndarray             # local min corner (metres)
    dims: tuple[int, int, int]
    voxel: float

    def world(self, idx_f: np.ndarray) -> np.ndarray:
        local = self.lo + (idx_f + 0.5) * self.voxel
        return local @ self.rot.T + self.centre

    def footprint_radius(self, p_world: np.ndarray) -> np.ndarray:
        """Normalised distance in the table plane: 1 at the region's edge (an ellipse fitted in the rectangle)."""
        local = (p_world - self.centre) @ self.rot
        half = (self.lo * -1.0)
        return np.sqrt((local[:, 0] / half[0]) ** 2 + (local[:, 2] / half[2]) ** 2)


@dataclass
class Surface:
    vertices: np.ndarray
    faces: np.ndarray
    ground_y: float | None
    region: Region
    seconds: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)


def ground_height(frames: list[Any], box: Any, grow: float) -> float | None:
    """The table under the object: the lowest well-supported horizontal layer of depth points in the box's grown
    footprint between 10 cm below the box and its middle (ARKit world is gravity-aligned, Y up)."""
    from repairos.locate.camera import backproject
    from repairos.recon.tsdf import find_support_plane

    lo_y = box.centre[1] - box.extent[1] / 2 - 0.10
    hi_y = box.centre[1]
    pts = []
    for f in frames[:: max(1, len(frames) // 30)]:
        p = f.pose.to_world(backproject(f.depth_m, f.depth_cam))
        ok = np.isfinite(f.depth_m) & (f.depth_m > 0)
        if f.confidence is not None:
            ok &= f.confidence >= 2
        local = box.local(p[ok])
        near = (np.abs(local[:, 0]) <= 0.5 + grow) & (np.abs(local[:, 2]) <= 0.5 + grow)
        q = p[ok][near]
        pts.append(q[(q[:, 1] > lo_y) & (q[:, 1] < hi_y)])
    allp = np.concatenate(pts) if pts else np.zeros((0, 3))
    plane = find_support_plane(allp, band_m=0.004, min_fraction=0.03)
    return plane.height_m if plane else None


def make_region(box: Any, ground_y: float | None, voxel: float | None = None) -> Region:
    ext = box.extent
    rot = box.matrix[:3, :3] / ext[None, :]
    side = max(ext[0], ext[2])
    grow = max(GROW * side, GROW_MIN_M)
    bottom = (ground_y if ground_y is not None else box.centre[1] - ext[1] / 2) - BELOW_M
    top = box.centre[1] + ext[1] / 2 + ABOVE_M
    # local y is world y (yaw-only boxes): the vertical range is measured from the box centre
    lo = np.array([-ext[0] / 2 - grow, bottom - box.centre[1], -ext[2] / 2 - grow])
    hi = np.array([ext[0] / 2 + grow, top - box.centre[1], ext[2] / 2 + grow])
    size = hi - lo
    if voxel is None:
        voxel = float(np.clip(np.linalg.norm(ext) / 160.0, *VOXEL_LIMITS_M))
    while np.prod(np.ceil(size / voxel)) > MAX_VOXELS:
        voxel *= 1.1
    dims = tuple(int(x) for x in np.ceil(size / voxel))
    return Region(rot, box.centre.copy(), lo, dims, voxel)


def fuse(frames: list[Any], region: Region) -> tuple[np.ndarray, np.ndarray]:
    """(tsdf, weight) on the region's grid from every frame's depth (projective TSDF, running weighted mean)."""
    nx, ny, nz = region.dims
    idx = np.stack(np.meshgrid(np.arange(nx), np.arange(ny), np.arange(nz), indexing="ij"), -1).reshape(-1, 3)
    pw = region.world(idx.astype(np.float64)).astype(np.float32)
    trunc = max(TRUNC_VOXELS * region.voxel, TRUNC_MIN_M)
    tsdf = np.ones(len(pw), np.float32)
    wsum = np.zeros(len(pw), np.float32)
    for f in frames:
        pc = (pw - f.pose.t.astype(np.float32)) @ f.pose.r.astype(np.float32)
        z = -pc[:, 2]
        cam = f.depth_cam
        h, w = f.depth_m.shape
        with np.errstate(divide="ignore", invalid="ignore"):
            u = (cam.cx + cam.fx * pc[:, 0] / z).astype(np.int32)
            v = (cam.cy - cam.fy * pc[:, 1] / z).astype(np.int32)
        ok = (z > 0.05) & (u >= 0) & (u < w) & (v >= 0) & (v < h)
        sel = np.nonzero(ok)[0]
        d = f.depth_m[v[sel], u[sel]].astype(np.float32)
        c = f.confidence[v[sel], u[sel]] if f.confidence is not None else np.full(len(sel), 2, np.uint8)
        sdf = d - z[sel]
        good = (d > 0) & (c >= 1) & (sdf > -trunc)
        sel, sdf, c = sel[good], sdf[good], c[good]
        wt = np.where(c >= 2, 1.0, 0.4).astype(np.float32)
        val = np.clip(sdf / trunc, -1.0, 1.0)
        nw = wsum[sel] + wt
        tsdf[sel] = (tsdf[sel] * wsum[sel] + val * wt) / nw
        wsum[sel] = nw
    return tsdf.reshape(region.dims), wsum.reshape(region.dims)


def clear_neighbours(weight: np.ndarray, region: Region, box: Any, ground_y: float | None) -> None:
    """Outside the box's footprint (+ FOOTPRINT_MARGIN) only the table is kept: voxels higher than the table by
    TABLE_BAND_M lose their weight (a mug or a cable beside the object is not part of it). In place."""
    if ground_y is None:
        return
    nx, ny, nz = region.dims
    lx = region.lo[0] + (np.arange(nx) + 0.5) * region.voxel
    ly = region.lo[1] + (np.arange(ny) + 0.5) * region.voxel + region.centre[1]
    lz = region.lo[2] + (np.arange(nz) + 0.5) * region.voxel
    half = box.extent / 2
    mx = max(FOOTPRINT_MARGIN * half[0], 0.01)
    mz = max(FOOTPRINT_MARGIN * half[2], 0.01)
    outside = (np.abs(lx)[:, None] > half[0] + mx) | (np.abs(lz)[None, :] > half[2] + mz)     # nx x nz
    high = ly > ground_y + TABLE_BAND_M                                                        # ny
    weight[outside[:, None, :] & high[None, :, None]] = 0.0


def surface_nets(tsdf: np.ndarray, weight: np.ndarray, min_weight: float = MIN_WEIGHT
                 ) -> tuple[np.ndarray, np.ndarray]:
    """Naive Surface Nets on the zero level of `tsdf` (cells whose 8 corners all have weight >= min_weight).
    Returns (vertices in fractional voxel indices, triangles) with outward faces (tsdf > 0 outside)."""
    nx, ny, nz = tsdf.shape
    known = weight >= min_weight
    inside = tsdf < 0
    # corners of every cell
    corners = [(i, j, k) for i in (0, 1) for j in (0, 1) for k in (0, 1)]
    cell_known = np.ones((nx - 1, ny - 1, nz - 1), bool)
    n_in = np.zeros((nx - 1, ny - 1, nz - 1), np.int8)
    for i, j, k in corners:
        sl = (slice(i, nx - 1 + i), slice(j, ny - 1 + j), slice(k, nz - 1 + k))
        cell_known &= known[sl]
        n_in += inside[sl]
    active = cell_known & (n_in > 0) & (n_in < 8)
    cells = np.argwhere(active)
    vid = np.full(active.shape, -1, np.int64)
    vid[active] = np.arange(len(cells))
    # vertex = mean of the edge crossings of the cell
    edges = [((0, 0, 0), (1, 0, 0)), ((0, 1, 0), (1, 1, 0)), ((0, 0, 1), (1, 0, 1)), ((0, 1, 1), (1, 1, 1)),
             ((0, 0, 0), (0, 1, 0)), ((1, 0, 0), (1, 1, 0)), ((0, 0, 1), (0, 1, 1)), ((1, 0, 1), (1, 1, 1)),
             ((0, 0, 0), (0, 0, 1)), ((1, 0, 0), (1, 0, 1)), ((0, 1, 0), (0, 1, 1)), ((1, 1, 0), (1, 1, 1))]
    acc = np.zeros((len(cells), 3))
    cnt = np.zeros(len(cells))
    cx, cy, cz = cells[:, 0], cells[:, 1], cells[:, 2]
    for a, b in edges:
        va = tsdf[cx + a[0], cy + a[1], cz + a[2]]
        vb = tsdf[cx + b[0], cy + b[1], cz + b[2]]
        cross = (va < 0) != (vb < 0)
        t = np.where(cross, va / np.where(cross, va - vb, 1.0), 0.0)
        p = np.array(a, float)[None] + t[:, None] * (np.array(b, float) - np.array(a, float))[None]
        acc[cross] += p[cross]
        cnt[cross] += 1
    verts = cells + acc / np.maximum(cnt, 1)[:, None]
    # one quad per sign-changing grid edge, between the 4 cells around it
    quads = []
    for axis in range(3):
        o1, o2 = [ax for ax in range(3) if ax != axis]
        sl_a = [slice(0, n) for n in tsdf.shape]
        sl_b = [slice(0, n) for n in tsdf.shape]
        sl_a[axis] = slice(0, tsdf.shape[axis] - 1)
        sl_b[axis] = slice(1, tsdf.shape[axis])
        ia, ib = inside[tuple(sl_a)], inside[tuple(sl_b)]
        ok = (ia != ib) & known[tuple(sl_a)] & known[tuple(sl_b)]
        e = np.argwhere(ok)
        e = e[(e[:, o1] >= 1) & (e[:, o2] >= 1) & (e[:, o1] < tsdf.shape[o1] - 1) & (e[:, o2] < tsdf.shape[o2] - 1)]
        if not len(e):
            continue
        flip = ia[tuple(e.T)]          # inside -> outside along +axis
        cellsq = []
        for d1, d2 in ((0, 0), (1, 0), (1, 1), (0, 1)):
            c = e.copy()
            c[:, o1] -= d1
            c[:, o2] -= d2
            c[:, axis] = np.minimum(c[:, axis], active.shape[axis] - 1)
            cellsq.append(vid[c[:, 0], c[:, 1], c[:, 2]])
        q = np.stack(cellsq, 1)
        good = (q >= 0).all(1)
        q, flip = q[good], flip[good]
        # orientation: (o1, o2, axis) is a right-handed cycle for axis 0 (y,z), 1 (z,x)... fix by parity
        rh = (o1, o2) in ((1, 2), (2, 0), (0, 1))
        rev = flip if rh else ~flip
        q[~rev] = q[~rev][:, ::-1]
        quads.append(q)
    if not quads:
        return verts, np.zeros((0, 3), np.int64)
    q = np.concatenate(quads)
    tris = np.concatenate([q[:, [0, 1, 2]], q[:, [0, 2, 3]]])
    return verts, tris


def keep_central(v: np.ndarray, f: np.ndarray, region: Region) -> tuple[np.ndarray, np.ndarray]:
    """The connected piece with the most triangles within the box footprint's middle (the object and its table)."""
    import open3d as o3d

    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(v), o3d.utility.Vector3iVector(f.astype(np.int32)))
    cl, counts, _ = m.cluster_connected_triangles()
    cl = np.asarray(cl)
    r = region.footprint_radius(v[f].mean(1))
    central = np.bincount(cl[r < 0.5], minlength=len(counts))
    if not central.any():
        return v, f
    keep = cl == int(central.argmax())
    m.remove_triangles_by_mask(~keep)
    m.remove_unreferenced_vertices()
    return np.asarray(m.vertices), np.asarray(m.triangles)


def object_focus(v: np.ndarray, ground_y: float | None, box: Any) -> tuple[np.ndarray, float]:
    """What the viewer frames: the surface above the table inside the box footprint (2nd-98th percentile bounds, from
    the table up), so the object fills the view even when the locked box was generous; the box when too little
    stands up. (centre, radius). The phone does the same (ShellBuilder.objectFocus)."""
    floor = (ground_y if ground_y is not None else box.centre[1] - box.extent[1] / 2) + max(0.01, 0.1 * box.extent[1])
    local = box.local(v)                      # the unit box: |x|, |z| <= 0.5 inside
    sel = v[(v[:, 1] > floor) & (np.abs(local[:, 0]) <= 0.55) & (np.abs(local[:, 2]) <= 0.55)]
    if len(sel) < 50:
        return box.centre.copy(), float(np.linalg.norm(box.extent) / 2)
    lo, hi = np.percentile(sel, 2, axis=0), np.percentile(sel, 98, axis=0)
    if ground_y is not None:
        lo[1] = min(lo[1], ground_y)
    return (lo + hi) / 2, max(0.02, float(np.linalg.norm(hi - lo) / 2))


def scan_surface(pkg_local: Mapping[str, Any], *, voxel_m: float | None = None, smooth_iters: int = 10,
                 max_triangles: int = 200_000) -> Surface:
    """The object and its table patch as a triangle mesh in the scan's ARKit world (see the module docstring)."""
    import open3d as o3d

    from repairos.pipeline.mesh import MeshUnavailable, keyframes_in_frame
    from repairos.recon.tsdf import ObjectBox, frames_from_scan_package

    t = {}
    t0 = time.perf_counter()
    pkg = {**pkg_local, "keyframes": keyframes_in_frame(pkg_local)}
    frames = frames_from_scan_package(pkg, Path("/"))
    if not frames:
        raise MeshUnavailable("the scan has no keyframes with depth in its frame")
    if not pkg.get("crop_box"):
        raise MeshUnavailable("the scan has no object box")
    box = ObjectBox.from_contract(pkg["crop_box"])
    ground = ground_height(frames, box, GROW)
    region = make_region(box, ground, voxel_m)
    t["load_s"] = time.perf_counter() - t0
    t1 = time.perf_counter()
    tsdf, wsum = fuse(frames, region)
    clear_neighbours(wsum, region, box, ground)
    t["fuse_s"] = time.perf_counter() - t1
    t2 = time.perf_counter()
    vi, f = surface_nets(tsdf, wsum)
    if not len(f):
        raise MeshUnavailable("no surface in the object's region")
    v = region.world(vi)  # fractional voxel index -> world (voxel centres at index + 0.5 of lo)
    v, f = keep_central(v, f, region)
    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(v), o3d.utility.Vector3iVector(f.astype(np.int32)))
    if smooth_iters:
        m = m.filter_smooth_taubin(number_of_iterations=smooth_iters)
    if len(m.triangles) > max_triangles:
        m = m.simplify_quadric_decimation(target_number_of_triangles=int(max_triangles))
    m.remove_degenerate_triangles()
    m.remove_unreferenced_vertices()
    t["extract_s"] = time.perf_counter() - t2
    return Surface(np.asarray(m.vertices), np.asarray(m.triangles), ground, region,
                   {k: round(x, 3) for k, x in t.items()},
                   {"voxel_mm": round(region.voxel * 1000, 2), "dims": list(region.dims), "frames": len(frames),
                    "ground_found": ground is not None})
