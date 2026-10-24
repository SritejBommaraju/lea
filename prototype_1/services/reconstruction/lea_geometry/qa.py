"""Open3D geometry QA for a photo-reconstructed mesh against a saved reference.

Object Capture (or any image engine) produces the mesh; this module never trusts its units. It:
  1. drops small loose fragments and measures its minimal oriented bounding box;
  2. sets scale from ONE owner-measured box dimension (the anchor) and reports a DIFFERENT measured
     dimension (the holdout) so scale error is tested, not assumed;
  3. registers the scaled scan to the reference (mm) from 24 axis-aligned starts plus ICP;
  4. measures coverage (share of reference surface the scan reaches) and fit (RMS, inlier share);
  5. clusters reference regions the scan does not reach and scan regions far from the reference into
     candidate regions. A candidate is "material differs here", never "this part is broken": an
     unseen side and a snapped tab look the same to geometry alone.
The pass/fail verdict is NOT decided here; the SpacetimeDB module computes it from these numbers and
the owner's frozen policy.
"""
from __future__ import annotations

import itertools
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import open3d as o3d

BOX_AXES = ("longest", "middle", "shortest")


@dataclass
class Region:
    kind: str                 # "missing_material" (reference not reached) | "extra_material" (scan off reference)
    centroid_mm: list[float]  # reference frame
    extent_mm: list[float]
    points: int
    share_of_reference: float
    max_deviation_mm: float


@dataclass
class QAResult:
    engine: str
    scan_units_per_mm_before_scale: float
    scale_factor: float                 # mm per scan unit
    anchor_axis: str
    anchor_measured_scan_units: float
    holdout_axis: str
    measured_holdout_mm: float
    box_extents_mm: list[float]
    coverage: float
    fit_rms_mm: float
    fit_inlier_share: float
    correspondence_mm: float
    transform_scan_to_reference_mm: list[list[float]]
    regions: list[Region] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reference_present: bool = True
    scale_trusted: bool = True      # False: the anchor side is within 5% of another side, so it may be the wrong one
    holdout_trusted: bool = True    # likewise for the holdout side

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def load_mesh(path: str | Path) -> o3d.geometry.TriangleMesh:
    mesh = o3d.io.read_triangle_mesh(str(path), enable_post_processing=False)
    if mesh.is_empty() or len(mesh.triangles) == 0:
        raise ValueError(f"{path}: no triangles could be read")
    mesh.remove_duplicated_vertices()
    mesh.remove_degenerate_triangles()
    mesh.remove_unreferenced_vertices()
    return mesh


def drop_fragments(mesh: o3d.geometry.TriangleMesh, min_share: float = 0.02) -> tuple[o3d.geometry.TriangleMesh, float]:
    """Remove floating fragments under `min_share` of the surface (background specks, stray triangles).
    Larger disconnected pieces stay: a real object can be several shells."""
    clusters, _, areas = mesh.cluster_connected_triangles()
    clusters, areas = np.asarray(clusters), np.asarray(areas)
    keep = areas >= min_share * areas.sum()
    out = o3d.geometry.TriangleMesh(mesh)
    out.remove_triangles_by_mask(~keep[clusters])
    out.remove_unreferenced_vertices()
    return out, float(areas[keep].sum() / areas.sum())


def box_extents(mesh: o3d.geometry.TriangleMesh) -> np.ndarray:
    """Minimal oriented bounding box extents, sorted longest first."""
    pts = o3d.utility.Vector3dVector(np.asarray(mesh.vertices))
    try:
        box = o3d.geometry.OrientedBoundingBox.create_from_points_minimal(pts, robust=True)
    except Exception:
        box = o3d.geometry.OrientedBoundingBox.create_from_points(pts, robust=True)
    return np.sort(np.asarray(box.extent))[::-1]


def _sample(mesh: o3d.geometry.TriangleMesh, spacing_mm: float) -> o3d.geometry.PointCloud:
    n = int(min(max(mesh.get_surface_area() / (spacing_mm ** 2), 5_000), 400_000))
    pcd = mesh.sample_points_uniformly(number_of_points=n, use_triangle_normal=True)
    return pcd


def _axis_starts():
    """24 proper rotations that map principal axes onto each other (sign/permutation, det = +1)."""
    for perm in itertools.permutations(range(3)):
        for signs in itertools.product((1, -1), repeat=3):
            r = np.zeros((3, 3))
            for i, (p, s) in enumerate(zip(perm, signs)):
                r[i, p] = s
            if np.linalg.det(r) > 0:
                yield r


def _principal_frame(pcd: o3d.geometry.PointCloud) -> tuple[np.ndarray, np.ndarray]:
    pts = np.asarray(pcd.points)
    center = pts.mean(axis=0)
    _, vecs = np.linalg.eigh(np.cov((pts - center).T))
    vecs = vecs[:, ::-1]
    if np.linalg.det(vecs) < 0:
        vecs[:, 2] *= -1
    return center, vecs


def register(scan: o3d.geometry.PointCloud, ref: o3d.geometry.PointCloud, corr_mm: float):
    """Best of 24 principal-axis starts refined by point-to-plane ICP. Returns (transform, fitness, rmse)."""
    sc, sv = _principal_frame(scan)
    rc, rv = _principal_frame(ref)
    best = None
    crit = o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=60)
    for r in _axis_starts():
        rot = rv @ r @ sv.T
        init = np.eye(4)
        init[:3, :3] = rot
        init[:3, 3] = rc - rot @ sc
        coarse = o3d.pipelines.registration.registration_icp(
            scan, ref, corr_mm * 4, init, o3d.pipelines.registration.TransformationEstimationPointToPoint(), crit)
        fine = o3d.pipelines.registration.registration_icp(
            scan, ref, corr_mm, coarse.transformation, o3d.pipelines.registration.TransformationEstimationPointToPlane(), crit)
        key = (fine.fitness, -fine.inlier_rmse)
        if best is None or key > best[0]:
            best = (key, fine)
    fine = best[1]
    return np.asarray(fine.transformation), float(fine.fitness), float(fine.inlier_rmse)


def _regions(points: np.ndarray, deviations: np.ndarray, kind: str, total_ref: int, eps_mm: float, min_points: int) -> list[Region]:
    if len(points) < min_points:
        return []
    pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
    labels = np.asarray(pcd.cluster_dbscan(eps=eps_mm, min_points=min_points))
    out = []
    for label in sorted(set(labels.tolist()) - {-1}):
        mask = labels == label
        p = points[mask]
        out.append(Region(
            kind=kind, centroid_mm=p.mean(axis=0).round(2).tolist(), extent_mm=(p.max(axis=0) - p.min(axis=0)).round(2).tolist(),
            points=int(mask.sum()), share_of_reference=round(float(mask.sum()) / max(total_ref, 1), 4),
            max_deviation_mm=round(float(deviations[mask].max()), 2)))
    out.sort(key=lambda r: r.points, reverse=True)
    return out[:20]


def run_qa(scan_path: str | Path, reference_path: str | Path | None, reference_units_per_mm: float,
           anchor_axis: str, anchor_mm: float, holdout_axis: str, max_fit_rms_mm: float,
           engine: str, spacing_mm: float = 0.75) -> tuple[QAResult, o3d.geometry.TriangleMesh]:
    if anchor_axis not in BOX_AXES or holdout_axis not in BOX_AXES:
        raise ValueError(f"anchor and holdout must be one of {BOX_AXES}")
    if anchor_axis == holdout_axis:
        raise ValueError("the holdout must be a different dimension from the anchor")
    if not (anchor_mm > 0 and math.isfinite(anchor_mm)):
        raise ValueError("anchor_mm must be a positive measurement")

    warnings: list[str] = []
    mesh, kept_share = drop_fragments(load_mesh(scan_path))
    if kept_share < 0.95:
        warnings.append(f"{1 - kept_share:.0%} of the surface was small loose fragments and was dropped")
    extents_units = box_extents(mesh)
    anchor_units = float(extents_units[BOX_AXES.index(anchor_axis)])
    if anchor_units <= 0:
        raise ValueError("the scan has no extent along the anchor axis")
    scale = anchor_mm / anchor_units
    mesh.scale(scale, center=np.zeros(3))
    extents_mm = extents_units * scale
    holdout_mm = float(extents_mm[BOX_AXES.index(holdout_axis)])
    def ambiguous(axis: str) -> bool:
        i = BOX_AXES.index(axis)
        return any(abs(extents_mm[i] - extents_mm[j]) < 0.05 * max(extents_mm[i], extents_mm[j]) for j in (i - 1, i + 1) if 0 <= j < 3)
    scale_trusted, holdout_trusted = not ambiguous(anchor_axis), not ambiguous(holdout_axis)
    for name, ok in (("anchor", scale_trusted), ("holdout", holdout_trusted)):
        if not ok:
            warnings.append(f"the {name} side is within 5% of another side, so the scan cannot tell which side was measured")

    if reference_path is None:
        return QAResult(engine, 1 / scale, scale, anchor_axis, anchor_units, holdout_axis, holdout_mm,
                        extents_mm.round(3).tolist(), 0.0, math.inf, 0.0, 0.0, np.eye(4).tolist(),
                        [], warnings + ["no reference mesh: coverage and fit are not measured"], False, scale_trusted, holdout_trusted), mesh

    ref_mesh = load_mesh(reference_path)
    ref_mesh.scale(1.0 / reference_units_per_mm, center=np.zeros(3))
    corr = max(2.0 * max_fit_rms_mm, 2.5 * spacing_mm)
    scan_pcd, ref_pcd = _sample(mesh, spacing_mm), _sample(ref_mesh, spacing_mm)
    for p in (scan_pcd, ref_pcd):
        p.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=corr * 2, max_nn=30))
    transform, fitness, rmse = register(scan_pcd, ref_pcd, corr)
    mesh.transform(transform)
    scan_pcd.transform(transform)

    ref_to_scan = np.asarray(ref_pcd.compute_point_cloud_distance(scan_pcd))
    scan_to_ref = np.asarray(scan_pcd.compute_point_cloud_distance(ref_pcd))
    coverage = float((ref_to_scan <= corr).mean())
    ref_pts, scan_pts = np.asarray(ref_pcd.points), np.asarray(scan_pcd.points)
    min_pts = max(30, int(0.002 * len(ref_pts)))
    regions = (_regions(ref_pts[ref_to_scan > corr], ref_to_scan[ref_to_scan > corr], "missing_material", len(ref_pts), corr * 2, min_pts)
               + _regions(scan_pts[scan_to_ref > corr], scan_to_ref[scan_to_ref > corr], "extra_material", len(ref_pts), corr * 2, min_pts))
    if fitness < 0.5:
        warnings.append("under half of the scan lies on the reference; check it is the same object and side")

    t_total = transform @ np.diag([scale, scale, scale, 1.0])
    return QAResult(engine, 1 / scale, scale, anchor_axis, anchor_units, holdout_axis, holdout_mm, extents_mm.round(3).tolist(),
                    round(coverage, 4), round(rmse, 4), round(fitness, 4), corr, t_total.round(9).tolist(), regions, warnings,
                    True, scale_trusted, holdout_trusted), mesh


def write_grain_ply(mesh: o3d.geometry.TriangleMesh, path: str | Path, spacing_mm: float = 1.0) -> int:
    """Binary little-endian PLY of surface points in METRES, design axes (upload with units=m, align=none)."""
    pcd = _sample(mesh, spacing_mm)
    pcd.points = o3d.utility.Vector3dVector(np.asarray(pcd.points) / 1000.0)
    pcd.normals = o3d.utility.Vector3dVector()
    if not o3d.io.write_point_cloud(str(path), pcd, write_ascii=False):
        raise IOError(f"could not write {path}")
    return len(pcd.points)
