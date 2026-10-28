"""Fuse posed depth maps (ARKit sceneDepth) into a clean point cloud, and a mesh when Open3D is installed.

Every depth pixel is used only when its ARKit confidence is >= `min_confidence` (2 = high) and its depth is within
[min_depth_m, max_depth_m]. Two fusers share one interface (`add_depth`, `result`); `make_fuser` picks one and the
result says which ran (`method`):

- `open3d_voxel_block_grid_tsdf` (preferred; Open3D, MIT, the `geometry` extra): TSDF integration in the tensor
  `VoxelBlockGrid` with the frame's pose and the depth grid's intrinsics; the cloud is extracted (voxels seen >= 3
  times), cleaned with `remove_statistical_outlier` and thinned with `voxel_down_sample`; the mesh is extracted,
  simplified by vertex clustering at the same spacing, and floating fragments are dropped.
- `numpy_voxel_average` (fallback when Open3D is missing): points averaged per voxel, a voxel kept when at least
  `min_views` frames saw it. No mesh.

The spacing is MEASURED, not guessed (`measure_spacing`): the lateral size of one depth pixel at the median distance
of the scan's high-confidence depth, `median_depth / fx_depth`. Finer voxels than that add no information from the
sensor; coarser ones throw detail away. It is clamped to [1, 20] mm and recorded in import.json.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from repairos.importers.camera import FLIP_YZ, Pinhole, Pose, backproject

SPACING_LIMITS_M = (0.001, 0.02)
TSDF_TRUNC_VOXELS = 5          # truncation distance in voxels (Open3D's examples use 3-5x the voxel)
WEIGHT_THRESHOLD = 3.0         # a TSDF voxel is surface only after >= 3 depth observations (Open3D's default)
OUTLIER_NEIGHBOURS, OUTLIER_STD = 20, 2.0
MIN_FRAGMENT_TRIANGLES = 50    # mesh pieces smaller than this float free of the scan: dropped

_BIAS = 1 << 20
_BITS = 21


def open3d_available() -> bool:
    try:
        import open3d  # noqa: F401
    except ImportError:
        return False
    return True


def depth_mask(depth_m: np.ndarray, confidence: np.ndarray | None, min_confidence: int, min_depth_m: float,
               max_depth_m: float) -> np.ndarray:
    ok = np.isfinite(depth_m) & (depth_m >= min_depth_m) & (depth_m <= max_depth_m)
    if confidence is not None:
        if confidence.shape != depth_m.shape:
            raise ValueError(f"confidence is {confidence.shape[::-1]}, depth is {depth_m.shape[::-1]}")
        ok &= confidence >= min_confidence
    return ok


@dataclass(frozen=True)
class Spacing:
    spacing_m: float
    median_depth_m: float
    fx_depth_px: float
    frames: int
    rule: str = "median high-confidence depth / depth-grid focal length, clamped to [1, 20] mm"

    def to_dict(self) -> dict:
        return {"spacing_mm": round(self.spacing_m * 1000, 3), "median_depth_m": round(self.median_depth_m, 4),
                "fx_depth_px": round(self.fx_depth_px, 3), "frames_measured": self.frames, "rule": self.rule}


def measure_spacing(frames: Iterable[tuple[np.ndarray, np.ndarray | None, Pinhole]], *, min_confidence: int = 2,
                    min_depth_m: float = 0.1, max_depth_m: float = 3.0) -> Spacing | None:
    """From (depth_m, confidence, depth-grid intrinsics) frames; None when no pixel passes the mask."""
    medians, fxs = [], []
    for depth, conf, cam in frames:
        ok = depth_mask(depth, conf, min_confidence, min_depth_m, max_depth_m)
        if ok.any():
            medians.append(float(np.median(depth[ok])))
            fxs.append(float(cam.fx))
    if not medians:
        return None
    d, fx = float(np.median(medians)), float(np.median(fxs))
    return Spacing(float(np.clip(d / fx, *SPACING_LIMITS_M)), d, fx, len(medians))


class _Base:
    method = ""

    def __init__(self, spacing_m: float, *, min_confidence: int = 2, min_depth_m: float = 0.1,
                 max_depth_m: float = 3.0):
        self.spacing_m, self.min_confidence = spacing_m, min_confidence
        self.min_depth_m, self.max_depth_m = min_depth_m, max_depth_m
        self.frames = 0

    def _prepare(self, depth_m, confidence, cam: Pinhole):
        h, w = depth_m.shape
        if (cam.width, cam.height) != (w, h):
            raise ValueError(f"intrinsics are for {cam.width}x{cam.height}, depth is {w}x{h}")
        return depth_mask(depth_m, confidence, self.min_confidence, self.min_depth_m, self.max_depth_m)

    def params(self) -> dict:
        return {"method": self.method, "spacing_mm": round(self.spacing_m * 1000, 3),
                "min_confidence": self.min_confidence, "depth_range_m": [self.min_depth_m, self.max_depth_m],
                "frames": self.frames}


def _keys(points: np.ndarray, voxel_m: float) -> np.ndarray:
    c = np.floor(points / voxel_m).astype(np.int64) + _BIAS
    if (c < 0).any() or (c >= 2 * _BIAS).any():
        raise ValueError("point outside the fusion range")
    return (c[:, 0] << (2 * _BITS)) | (c[:, 1] << _BITS) | c[:, 2]


class VoxelFuser(_Base):
    """numpy fallback: per-voxel averages, kept when >= min_views frames saw the voxel."""
    method = "numpy_voxel_average"

    def __init__(self, spacing_m: float, *, min_views: int = 2, **kw):
        super().__init__(spacing_m, **kw)
        self.min_views = min_views
        self._parts: list[tuple[np.ndarray, ...]] = []
        self._table: tuple[np.ndarray, ...] | None = None

    def add_depth(self, depth_m: np.ndarray, confidence: np.ndarray | None, rgb: np.ndarray | None, pose: Pose,
                  cam: Pinhole) -> int:
        ok = self._prepare(depth_m, confidence, cam)
        self.frames += 1
        if not ok.any():
            return 0
        pts = pose.to_world(backproject(depth_m, cam)[ok])
        col = np.full((len(pts), 3), 128, np.uint8) if rgb is None else rgb[ok].astype(np.uint8)
        conf = np.full(len(pts), 2, np.uint8) if confidence is None else confidence[ok].astype(np.uint8)
        self._add_points(pts, col, conf)
        return len(pts)

    def _add_points(self, points, rgb, conf) -> None:
        uk, inv = np.unique(_keys(points, self.spacing_m), return_inverse=True)
        m = len(uk)
        sums = np.stack([np.bincount(inv, points[:, i], m) for i in range(3)], axis=1)
        csum = np.stack([np.bincount(inv, rgb[:, i].astype(float), m) for i in range(3)], axis=1)
        cmin = np.full(m, 255, np.uint8)
        np.minimum.at(cmin, inv, conf)
        self._parts.append((uk, sums, csum, np.bincount(inv, minlength=m).astype(float), np.ones(m), cmin))
        if len(self._parts) >= 32:
            self._merge()

    def _merge(self) -> None:
        parts = self._parts + ([self._table] if self._table is not None else [])
        self._parts = []
        if not parts:
            return
        uk, inv = np.unique(np.concatenate([p[0] for p in parts]), return_inverse=True)
        m = len(uk)
        cat = [np.concatenate([p[i] for p in parts]) for i in range(1, 6)]
        cmin = np.full(m, 255, np.uint8)
        np.minimum.at(cmin, inv, cat[4])
        self._table = (uk, np.stack([np.bincount(inv, cat[0][:, i], m) for i in range(3)], axis=1),
                       np.stack([np.bincount(inv, cat[1][:, i], m) for i in range(3)], axis=1),
                       np.bincount(inv, cat[2], m), np.bincount(inv, cat[3], m), cmin)

    def params(self) -> dict:
        return {**super().params(), "min_views": self.min_views}

    def result(self) -> dict:
        """{xyz, rgb, views, confidence, mesh: None, method, params}."""
        self._merge()
        out = {"mesh": None, "method": self.method, "params": self.params()}
        if self._table is None:
            return {**out, "xyz": np.zeros((0, 3)), "rgb": np.zeros((0, 3), np.uint8),
                    "views": np.zeros(0, np.uint8), "confidence": np.zeros(0, np.uint8)}
        _, sums, csum, count, views, cmin = self._table
        keep = views >= self.min_views
        n = count[keep][:, None]
        return {**out, "xyz": sums[keep] / n, "rgb": np.clip(np.rint(csum[keep] / n), 0, 255).astype(np.uint8),
                "views": np.minimum(views[keep], 255).astype(np.uint8), "confidence": cmin[keep]}


def open3d_extrinsic(pose: Pose) -> np.ndarray:
    """Open3D's extrinsic for a pose: world-to-camera with OpenCV camera axes (Open3D's pinhole convention)."""
    t_wc = np.eye(4)
    t_wc[:3, :3] = pose.r @ FLIP_YZ
    t_wc[:3, 3] = pose.t
    return np.linalg.inv(t_wc)


def open3d_intrinsic_matrix(cam: Pinhole) -> np.ndarray:
    """3x3 K for Open3D, which puts pixel centres at integers; ours are at +0.5 (camera.py): half-pixel shift."""
    return np.array([[cam.fx, 0.0, cam.cx - 0.5], [0.0, cam.fy, cam.cy - 0.5], [0.0, 0.0, 1.0]])


class Open3DFuser(_Base):
    """TSDF fusion in Open3D's tensor `VoxelBlockGrid` (Open3D 0.20's legacy `ScalableTSDFVolume` integrated
    nothing in our tests on macOS arm64; the tensor grid is Open3D's current reconstruction API)."""
    method = "open3d_voxel_block_grid_tsdf"

    def __init__(self, spacing_m: float, *, block_resolution: int = 8, block_count: int = 20000, **kw):
        super().__init__(spacing_m, **kw)
        import open3d as o3d

        self._o3d = o3d
        self.sdf_trunc_m = TSDF_TRUNC_VOXELS * spacing_m
        f32 = o3d.core.float32
        self._vbg = o3d.t.geometry.VoxelBlockGrid(
            attr_names=("tsdf", "weight", "color"), attr_dtypes=(f32, f32, f32), attr_channels=((1), (1), (3)),
            voxel_size=spacing_m, block_resolution=block_resolution, block_count=block_count,
            device=o3d.core.Device("CPU:0"))

    def add_depth(self, depth_m: np.ndarray, confidence: np.ndarray | None, rgb: np.ndarray | None, pose: Pose,
                  cam: Pinhole) -> int:
        o3d = self._o3d
        ok = self._prepare(depth_m, confidence, cam)
        self.frames += 1
        if not ok.any():
            return 0
        d = np.ascontiguousarray(np.where(ok, depth_m, 0.0), dtype=np.float32)   # 0 = no measurement
        col = (np.full((*d.shape, 3), 0.5, np.float32) if rgb is None
               else np.ascontiguousarray(rgb, dtype=np.float32) / 255.0)
        depth = o3d.t.geometry.Image(o3d.core.Tensor(d))
        color = o3d.t.geometry.Image(o3d.core.Tensor(col))
        k = o3d.core.Tensor(open3d_intrinsic_matrix(cam), o3d.core.float64)
        ext = o3d.core.Tensor(open3d_extrinsic(pose), o3d.core.float64)
        far = self.max_depth_m + 0.01
        try:
            blocks = self._vbg.compute_unique_block_coordinates(depth, k, ext, 1.0, far, float(TSDF_TRUNC_VOXELS))
        except RuntimeError as e:   # Open3D: "No block is touched" (a few stray pixels): this frame adds nothing
            if "No block is touched" not in str(e):
                raise
            return 0
        self._vbg.integrate(blocks, depth, color, k, k, ext, 1.0, far, float(TSDF_TRUNC_VOXELS))
        return int(ok.sum())

    def params(self) -> dict:
        return {**super().params(), "sdf_trunc_mm": round(self.sdf_trunc_m * 1000, 3),
                "weight_threshold": WEIGHT_THRESHOLD,
                "outlier_removal": {"nb_neighbors": OUTLIER_NEIGHBOURS, "std_ratio": OUTLIER_STD},
                "min_fragment_triangles": MIN_FRAGMENT_TRIANGLES}

    def result(self) -> dict:
        """{xyz, rgb, views: None, confidence: None, mesh: {vertices, faces, rgb} | None, method, params}."""
        o3d = self._o3d
        pcd = self._vbg.extract_point_cloud(weight_threshold=WEIGHT_THRESHOLD).to_legacy()
        before = len(pcd.points)
        if before > OUTLIER_NEIGHBOURS:
            pcd, _ = pcd.remove_statistical_outlier(nb_neighbors=OUTLIER_NEIGHBOURS, std_ratio=OUTLIER_STD)
        after_outliers = len(pcd.points)
        pcd = pcd.voxel_down_sample(self.spacing_m)
        xyz = np.asarray(pcd.points, dtype=float)
        rgb = (np.clip(np.asarray(pcd.colors) * 255.0, 0, 255).round().astype(np.uint8) if pcd.has_colors()
               else np.full((len(xyz), 3), 128, np.uint8))
        mesh_out = None
        if before:
            mesh = self._vbg.extract_triangle_mesh(weight_threshold=WEIGHT_THRESHOLD).to_legacy()
            if len(mesh.triangles):
                mesh = mesh.simplify_vertex_clustering(voxel_size=self.spacing_m,
                                                       contraction=o3d.geometry.SimplificationContraction.Average)
                mesh.remove_degenerate_triangles()
                clusters, counts, _ = mesh.cluster_connected_triangles()
                clusters, counts = np.asarray(clusters), np.asarray(counts)
                if len(counts):
                    mesh.remove_triangles_by_mask(counts[clusters] < MIN_FRAGMENT_TRIANGLES)
                mesh.remove_unreferenced_vertices()
                if len(mesh.triangles):
                    mesh_out = {"vertices": np.asarray(mesh.vertices, dtype=float),
                                "faces": np.asarray(mesh.triangles, dtype=np.int64),
                                "rgb": (np.clip(np.asarray(mesh.vertex_colors) * 255.0, 0, 255).round()
                                        .astype(np.uint8) if mesh.has_vertex_colors() else None)}
        params = {**self.params(), "points_extracted": before, "points_after_outlier_removal": after_outliers,
                  "points_after_downsample": len(xyz),
                  "mesh_triangles": int(len(mesh_out["faces"])) if mesh_out else 0}
        return {"xyz": xyz, "rgb": rgb, "views": None, "confidence": None, "mesh": mesh_out,
                "method": self.method, "params": params}


def make_fuser(spacing_m: float, *, prefer: str = "auto", min_views: int = 2, **kw) -> _Base:
    """`prefer`: auto (Open3D when installed), open3d (required) or numpy."""
    if prefer == "numpy" or (prefer == "auto" and not open3d_available()):
        return VoxelFuser(spacing_m, min_views=min_views, **kw)
    if prefer in ("auto", "open3d"):
        return Open3DFuser(spacing_m, **kw)
    raise ValueError(f"unknown fuser {prefer!r}")
