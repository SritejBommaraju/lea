"""A scan's point cloud in memory: read the binary PLY of a session and query it along rays with a uniform grid.

The grid is a hash of cells of side `cell_m`: points are sorted by packed cell key, so a lookup is a vectorised
`searchsorted`. It is exact for "points within `radius` of a ray" whenever `radius <= cell_m` (the 27-cell
neighbourhood of every sample covers the radius). numpy only (BSD-3); no KD-tree library is needed.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from repairos.sessions import files

_NP_TYPES = {"char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1", "short": "<i2", "int16": "<i2",
             "ushort": "<u2", "uint16": "<u2", "int": "<i4", "int32": "<i4", "uint": "<u4", "uint32": "<u4",
             "float": "<f4", "float32": "<f4", "double": "<f8", "float64": "<f8"}
_BIAS = 1 << 20   # cell indices in [-2^20, 2^20): +-3 km at 3 mm cells
_BITS = 21


def read_ply(path: str | Path) -> dict[str, np.ndarray]:
    """The vertex properties of a binary little-endian PLY (the session format) as arrays by name."""
    path = Path(path)
    with path.open("rb") as f:
        h = files.ply_header(f, path.stat().st_size)
        f.seek(0)
        head = f.read(h["header_bytes"]).decode("ascii", "replace")
        types = []
        in_vertex = False
        for line in head.splitlines():
            parts = line.split()
            if parts[:1] == ["element"]:
                in_vertex = parts[1] == "vertex"
            elif parts[:1] == ["property"] and in_vertex:
                types.append((parts[2], _NP_TYPES[parts[1]]))
        data = np.fromfile(f, dtype=np.dtype(types), count=h["vertex_count"])
    return {name: data[name] for name, _ in types}


def _keys(cells: np.ndarray) -> np.ndarray:
    c = cells.astype(np.int64) + _BIAS
    return (c[..., 0] << (2 * _BITS)) | (c[..., 1] << _BITS) | c[..., 2]


_NEIGHBOURS = np.array([(i, j, k) for i in (-1, 0, 1) for j in (-1, 0, 1) for k in (-1, 0, 1)], dtype=np.int64)


@dataclass
class ScanCloud:
    """A scan's points in arkit_world (metres, Y up), and its mesh file when the package has one. `scan_id` and
    `depth_source` come from the scan package."""
    points: np.ndarray                       # N x 3 float, arkit_world metres
    scan_id: str = ""
    depth_source: str = "lidar"
    cell_m: float = 0.006
    mesh_file: Path | None = None            # binary PLY triangle mesh, arkit_world metres (scan package mesh_file)
    _sorted_keys: np.ndarray | None = field(default=None, repr=False)
    _order: np.ndarray | None = field(default=None, repr=False)

    @classmethod
    def from_package(cls, pkg: Mapping, base_dir: str | Path | None = None, **kw) -> ScanCloud:
        """Load a ScanPackage's cloud; `points_file` is resolved against `base_dir` (the session folder). A package
        with depth_source none has no cloud: the result has zero points and `depth_source == "none"`."""
        source = pkg.get("depth_source") or "lidar"
        if source == "none" or not pkg.get("points_file"):
            return cls(np.zeros((0, 3)), pkg.get("scan_id", ""), "none", **kw)
        def path(rel: str) -> Path:
            p = Path(rel)
            return Path(base_dir) / p if base_dir is not None and not p.is_absolute() else p

        v = read_ply(path(pkg["points_file"]))
        pts = np.stack([v["x"], v["y"], v["z"]], axis=1).astype(float)
        mesh = path(pkg["mesh_file"]) if pkg.get("mesh_file") else None
        return cls(pts, pkg.get("scan_id", ""), source, mesh_file=mesh, **kw)

    def _index(self) -> None:
        if self._sorted_keys is None:
            keys = _keys(np.floor(self.points / self.cell_m))
            self._order = np.argsort(keys, kind="stable")
            self._sorted_keys = keys[self._order]

    def near_ray(self, origin: np.ndarray, direction: np.ndarray, radius: float, t_min: float,
                 t_max: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Points within `radius` of the ray segment origin + t * direction, t in [t_min, t_max] (direction is a
        unit vector). Returns (indices, t along the ray, distance from the ray), sorted by t."""
        empty = np.zeros(0, dtype=np.int64), np.zeros(0), np.zeros(0)
        if len(self.points) == 0 or t_max <= t_min:
            return empty
        if radius > self.cell_m:
            raise ValueError(f"radius {radius} m exceeds the grid cell {self.cell_m} m")
        self._index()
        step = self.cell_m * 0.5
        ts = np.arange(t_min, t_max + step, step)
        cells = np.unique(np.floor((origin + ts[:, None] * direction) / self.cell_m).astype(np.int64), axis=0)
        cand = np.unique(_keys((cells[:, None, :] + _NEIGHBOURS[None]).reshape(-1, 3)))
        lo = np.searchsorted(self._sorted_keys, cand, side="left")
        hi = np.searchsorted(self._sorted_keys, cand, side="right")
        hit = hi > lo
        if not hit.any():
            return empty
        idx = np.concatenate([self._order[a:b] for a, b in zip(lo[hit], hi[hit], strict=True)])
        rel = self.points[idx] - origin
        t = rel @ direction
        dist = np.linalg.norm(rel - t[:, None] * direction, axis=1)
        keep = (t >= t_min) & (t <= t_max) & (dist <= radius)
        idx, t, dist = idx[keep], t[keep], dist[keep]
        o = np.argsort(t, kind="stable")
        return idx[o], t[o], dist[o]
