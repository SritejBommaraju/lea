"""Measured features of a validated scan, for a design request Grain's agent can model natively.

Input: the QA's aligned mesh (reference frame, millimetres). Output: dominant planes (normal, offset, size), wall
thicknesses from parallel plane pairs, and holes found as enclosed gaps in a plane's points (centre, diameter).
Every number is measured from the scan; precision is bounded by the scan's fit (reported by QA), and anything not
seen (inside, underneath, smaller than ~2 mm) is absent rather than guessed.
"""
from __future__ import annotations

import json
import sys
from collections import deque

import numpy as np
import open3d as o3d

MAX_POINTS = 600_000


def planes(pcd: o3d.geometry.PointCloud, max_planes: int = 8, min_share: float = 0.04, dist_mm: float = 0.6):
    rest, out, total = pcd, [], len(pcd.points)
    for _ in range(max_planes):
        if len(rest.points) < total * min_share:
            break
        model, idx = rest.segment_plane(distance_threshold=dist_mm, ransac_n=3, num_iterations=1500)
        if len(idx) < total * min_share:
            break
        a, b, c, d = model
        n = np.array([a, b, c]) / np.linalg.norm([a, b, c])
        pts = np.asarray(rest.select_by_index(idx).points)
        out.append({"normal": n, "offset": -d / np.linalg.norm([a, b, c]), "points": pts})
        rest = rest.select_by_index(idx, invert=True)
    return out


def _basis(n):
    u = np.cross(n, [1, 0, 0] if abs(n[0]) < 0.9 else [0, 1, 0])
    u /= np.linalg.norm(u)
    return u, np.cross(n, u)


def holes_in_plane(pts: np.ndarray, n: np.ndarray, spacing: float, min_d_mm: float = 2.0):
    """Enclosed empty regions of the plane's point raster: through-holes or blind holes seen from this side."""
    u, v = _basis(n)
    uv = np.stack([pts @ u, pts @ v], axis=1)
    lo = uv.min(axis=0)
    ij = np.floor((uv - lo) / spacing).astype(int)
    w, h = ij.max(axis=0) + 1
    if w * h > 4_000_000:
        return []
    grid = np.zeros((w, h), bool)
    grid[ij[:, 0], ij[:, 1]] = True
    # close single-cell sampling gaps so only real voids remain
    filled = grid.copy()
    filled[1:-1, 1:-1] |= grid[2:, 1:-1] & grid[:-2, 1:-1] | grid[1:-1, 2:] & grid[1:-1, :-2]
    seen = np.zeros_like(filled)
    holes = []
    for i0 in range(w):
        for j0 in range(h):
            if filled[i0, j0] or seen[i0, j0]:
                continue
            cells, q, border = [], deque([(i0, j0)]), False
            seen[i0, j0] = True
            while q:
                i, j = q.popleft()
                cells.append((i, j))
                if i in (0, w - 1) or j in (0, h - 1):
                    border = True
                for a, b in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                    if 0 <= a < w and 0 <= b < h and not filled[a, b] and not seen[a, b]:
                        seen[a, b] = True
                        q.append((a, b))
            if border:
                continue  # outside the part's outline, not a hole
            c = np.array(cells, float)
            area = len(cells) * spacing ** 2
            d = 2 * np.sqrt(area / np.pi)
            ext = (c.max(axis=0) - c.min(axis=0) + 1) * spacing
            if d < min_d_mm:
                continue
            centre_uv = lo + (c.mean(axis=0) + 0.5) * spacing
            centre = centre_uv[0] * u + centre_uv[1] * v
            roundness = min(ext) / max(ext)
            holes.append({"diameter_mm": round(float(d), 2), "round": bool(roundness > 0.8), "extent_mm": [round(float(x), 2) for x in ext],
                          "centre_in_plane_mm": [round(float(x), 2) for x in centre_uv], "centre_mm": [round(float(x), 2) for x in centre]})
    return holes


def extract(mesh_path: str) -> dict:
    mesh = o3d.io.read_triangle_mesh(mesh_path)
    if mesh.is_empty():
        raise ValueError("no mesh")
    # About six samples per raster cell, so an empty cell is a real gap (chance of a sampling gap ~ e^-6),
    # with cells as fine as 0.5 mm when the point budget allows.
    area = mesh.get_surface_area()
    spacing = max(0.5, float(np.sqrt(6 * area / MAX_POINTS)))
    pcd = mesh.sample_points_uniformly(number_of_points=int(min(6 * area / spacing ** 2, MAX_POINTS)))
    found = planes(pcd)
    out_planes = []
    for i, p in enumerate(found):
        u, v = _basis(p["normal"])
        uv = np.stack([p["points"] @ u, p["points"] @ v], axis=1)
        size = uv.max(axis=0) - uv.min(axis=0)
        out_planes.append({"id": i, "normal": [round(float(x), 3) for x in p["normal"]], "offset_mm": round(float(p["offset"]), 2),
                           "size_mm": sorted([round(float(x), 1) for x in size], reverse=True),
                           "holes": holes_in_plane(p["points"], p["normal"], spacing)})
    walls = []
    for i in range(len(found)):
        for j in range(i + 1, len(found)):
            ni, nj = found[i]["normal"], found[j]["normal"]
            if abs(float(ni @ nj)) > 0.99:
                gap = abs(found[i]["offset"] - np.sign(ni @ nj) * found[j]["offset"])
                if 0.5 < gap < 50:
                    walls.append({"planes": [i, j], "thickness_mm": round(float(gap), 2)})
    # A through-hole shows on both faces of a wall: keep it once (same centre once the normal offset is removed).
    holes = []
    for p in out_planes:
        n_ = np.array(p["normal"])
        for h in p["holes"]:
            c = np.array(h["centre_mm"])
            if not any(abs(abs(float(n_ @ np.array(q["normal"]))) - 1) < 0.01 and np.linalg.norm((c - q["c"]) - ((c - q["c"]) @ n_) * n_) < 1.0 for q in holes):
                holes.append({**h, "plane": p["id"], "normal": p["normal"], "c": c})
        del p["holes"]
    for h in holes:
        del h["c"]
    return {"spacing_mm": round(spacing, 2), "planes": out_planes, "holes": holes, "walls": sorted(walls, key=lambda w: w["thickness_mm"])[:6]}


if __name__ == "__main__":
    try:
        print(json.dumps(extract(sys.argv[1])))
    except Exception as e:  # a result, not a crash
        print(json.dumps({"error": str(e)}))
