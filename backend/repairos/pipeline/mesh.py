"""The scan's 3D mesh for the app (the 3D hero view): LiDAR TSDF (repairos.recon.tsdf) of the keyframes inside the
object box, decimated, vertex-coloured from the keyframe JPEGs, in the ARKit world frame of the scan, the frame
`locate` puts findings in, so a finding's `location.point_m` is a pin on this mesh as it is.

Keyframes of a capture segment whose poses are not in the scan's frame are left out: a flipped segment (the object
moved) that the server has not registered, and any segment marked `aligned: false`. A flipped segment the server
registered (pipeline/segments.py) is fused in with its poses restated in segment 0's frame. Segments marked
`continued` (the same, unmoved object, photographed later from the report) are fused in, so a continuation re-post
rebuilds a fuller mesh.

Output: binary little-endian PLY (float x y z, uchar red green blue per vertex, int triangle lists), which Model I/O
(MDLAsset) loads directly. Open3D (MIT) does the TSDF and the quadric decimation.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

GREY = 128
DEPTH_AGREE_M = 0.02      # a vertex takes a keyframe's colour only where that keyframe's depth sees it (not occluded)
MAX_COLOUR_FRAMES = 48    # keyframes used for colour, evenly spread over the capture


class MeshUnavailable(RuntimeError):
    """No mesh can be built from this scan (the reason is the message): no depth keyframes, nothing in the box."""


HERO_MIN_SIZE_M = 0.08       # an object smaller than this is below what the phone's LiDAR resolves: show photos
HERO_POINTS_MIN = 20_000      # the phone cloud is the 3D view when it has at least this many object points
HERO_MESH_MIN = 2_000         # else the mesh, when it has at least this many triangles
CLOUD_MAX_POINTS = 200_000    # the served cloud is thinned (voxel) to at most about this many points


@dataclass
class ScanMesh:
    ply: bytes
    vertices: int
    triangles: int
    keyframes: int
    seconds: dict[str, float] = field(default_factory=dict)
    method: str = ""
    cloud_ply: bytes | None = None   # the object's part of the phone's LiDAR cloud (points with colour)
    cloud_points: int = 0
    hero: str = "photos"             # mesh | points | photos: what the app should show as the 3D view
    hero_reason: str = ""
    size_m: float = 0.0


def keyframes_in_frame(pkg: Mapping[str, Any]) -> list[dict]:
    """The keyframes whose poses are in the scan's frame (segments.same_frame_segments)."""
    from repairos.pipeline.segments import same_frame_segments

    same = same_frame_segments(pkg)
    return [kf for kf in pkg.get("keyframes") or [] if int(kf.get("segment") or 0) in same]


def vertex_colours(v: np.ndarray, normals: np.ndarray, frames: list[Any]) -> np.ndarray:
    """N x 3 uint8: per vertex, the colour of the keyframes that see it (in front, inside the image, depth agrees),
    weighted by how squarely and how closely each one sees it. Unseen vertices are grey."""
    from PIL import Image

    from repairos.locate.camera import project

    acc = np.zeros((len(v), 3))
    wsum = np.zeros(len(v))
    step = max(1, len(frames) // MAX_COLOUR_FRAMES)
    for f in frames[::step]:
        if f.image_path is None or f.image_cam is None:
            continue
        with Image.open(f.image_path) as im:
            rgb = np.asarray(im.convert("RGB"))
        h, w = rgb.shape[:2]
        cam = f.image_cam if (f.image_cam.width, f.image_cam.height) in ((w, h), (None, None)) \
            else f.image_cam.scaled(w, h)
        uv, z = project(v, f.pose, cam)
        ok = (z > 0.05) & (uv[:, 0] >= 0) & (uv[:, 0] < w) & (uv[:, 1] >= 0) & (uv[:, 1] < h)
        dh, dw = f.depth_m.shape
        du = np.clip((uv[:, 0] * dw / w).astype(int), 0, dw - 1)
        dv = np.clip((uv[:, 1] * dh / h).astype(int), 0, dh - 1)
        d = f.depth_m[dv, du]
        ok &= np.isfinite(d) & (d > 0) & (np.abs(d - z) < DEPTH_AGREE_M)
        if not ok.any():
            continue
        view = v - f.pose.t
        dist = np.linalg.norm(view, axis=1)
        facing = np.clip(-(normals * view).sum(axis=1) / np.maximum(dist, 1e-6), 0.0, 1.0)
        wt = np.where(ok, facing / np.maximum(dist, 0.05) ** 2, 0.0)
        px = rgb[np.clip(uv[ok, 1].astype(int), 0, h - 1), np.clip(uv[ok, 0].astype(int), 0, w - 1)]
        acc[ok] += px * wt[ok, None]
        wsum += wt
    out = np.full((len(v), 3), GREY, dtype=np.uint8)
    seen = wsum > 0
    out[seen] = np.clip(acc[seen] / wsum[seen, None], 0, 255).astype(np.uint8)
    return out


def decimate(v: np.ndarray, f: np.ndarray, max_triangles: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(vertices, faces, vertex normals), at most `max_triangles` triangles (Open3D quadric decimation)."""
    import open3d as o3d

    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(np.asarray(v, float)),
                                  o3d.utility.Vector3iVector(np.asarray(f, np.int32)))
    if len(f) > max_triangles:
        m = m.simplify_quadric_decimation(target_number_of_triangles=int(max_triangles))
    m.remove_unreferenced_vertices()
    m.remove_degenerate_triangles()
    m.compute_vertex_normals()
    return np.asarray(m.vertices), np.asarray(m.triangles), np.asarray(m.vertex_normals)


PLANE_SEARCH_M = 0.02       # depth points this close to the support plane are table, for measuring its noise
PLANE_MIN_MARGIN_M = 0.002  # never cut closer to the plane than this
PLANE_AGREE_M = 0.03        # the anchored plane is the object's support when the depth around the box finds a plane
                            # this close to it (ARKit drifts 1-2 cm vertically; a floor under a stool is tens of cm)
SUPPORT_GROW = 0.3          # the support is measured in the box grown by this share of each side (a ring of table)
SUPPORT_CLEAR = 0.05        # ... and confirmed outside the box footprint grown by this share (never the object's top)
SUPPORT_AGREE_M = 0.01      # the box's layer is the support when the ring around the object finds one this close
CENTRAL = 0.3               # a kept mesh piece reaches within this share of the box's half-width of its centre
MIN_PIECE_SHARE = 0.02      # pieces smaller than this share of the triangles are dropped (specks)


def plane_height(pkg: Mapping[str, Any], frames: list[Any] | None = None, box: Any = None) -> float | None:
    """The support plane's world height (Y up): the scan package's anchored plane (an ARKit horizontal plane
    anchor). None for a hand-held scan (pose_source object_tracked): its points and poses are in the object frame,
    which includes the underside the person flipped into view, so nothing may be cut at the table's height.
    With `frames` and `box`, the anchor is checked against the depth around the box: ARKit anchors the largest plane
    it found, which under a small table, a stool or a cushion is the floor. When the depth's own support plane
    (measured_plane_height, confirmed outside the object's footprint) is more than PLANE_AGREE_M away from the anchor
    (or there is no anchor), the measured height is used; no table seen around the object keeps the anchor."""
    if pkg.get("pose_source") == "object_tracked":
        return None
    sp = pkg.get("support_plane")
    anchor = float(sp["transform"][7]) if sp and len(sp.get("transform") or []) == 16 else None   # row-major: t_y
    if not frames or box is None:
        return anchor
    measured = measured_plane_height(frames, box)
    if measured is None or (anchor is not None and abs(measured - anchor) <= PLANE_AGREE_M):
        return anchor
    return measured


def measured_plane_height(frames: list[Any], box: Any) -> float | None:
    """The support plane's height measured from high-confidence depth in the box grown by SUPPORT_GROW (tsdf.
    find_support_plane: the lowest well-supported horizontal layer), confirmed by the ring around the object (outside
    its footprint grown by SUPPORT_CLEAR): seen mostly from above, a flat object's top gives more points than the
    table and is the box's layer, so a layer the ring does not find within SUPPORT_AGREE_M gives way to the ring's.
    None when the ring finds no layer (no table seen around the object)."""
    from repairos.locate.camera import backproject
    from repairos.recon.tsdf import find_support_plane

    pts, ring = [], []
    for f in frames[:: max(1, len(frames) // 12)]:
        p = f.pose.to_world(backproject(f.depth_m, f.depth_cam))
        a = np.abs(box.local(p))
        ok = np.isfinite(f.depth_m) & (f.depth_m > 0) & np.all(a <= 0.5 + SUPPORT_GROW, axis=-1)
        if f.confidence is not None:
            ok &= f.confidence >= 2
        pts.append(p[ok])
        ring.append((a[..., 0] > 0.5 + SUPPORT_CLEAR)[ok] | (a[..., 2] > 0.5 + SUPPORT_CLEAR)[ok])
    pts = np.concatenate(pts) if pts else np.zeros((0, 3))
    ring = np.concatenate(ring) if ring else np.zeros(0, bool)
    around = find_support_plane(pts[ring]) if ring.sum() >= 200 else None
    if around is None:
        return None
    whole = find_support_plane(pts)
    if whole is not None and abs(whole.height_m - around.height_m) <= SUPPORT_AGREE_M:
        return whole.height_m      # the support under the object itself (a cushion sags a few mm under it)
    return around.height_m


def voxel_for(box: Any) -> float:
    """TSDF voxel scaled to the object: its box diagonal / 120, between 3 and 5 mm."""
    return float(np.clip(np.linalg.norm(box.extent) / 120.0, 0.003, 0.005))


def object_depths(frames: list[Any], box: Any, plane_y: float | None) -> tuple[list[np.ndarray], dict]:
    """Per frame, the depth kept for the object: high confidence only, inside the object box, and above the support
    plane by twice the plane's measured noise in that frame (at least PLANE_MIN_MARGIN_M; ARKit drifts vertically,
    so the plane is re-measured near its anchored height in each frame)."""
    from repairos.locate.camera import backproject

    out, margins = [], []
    for f in frames:
        pts = f.pose.to_world(backproject(f.depth_m, f.depth_cam))
        ok = np.isfinite(f.depth_m) & (f.depth_m > 0)
        if f.confidence is not None:
            ok &= f.confidence >= 2
        inbox = ok & box.contains(pts, 0.02)
        if plane_y is not None:
            near = pts[inbox & (np.abs(pts[..., 1] - plane_y) < PLANE_SEARCH_M)][:, 1]
            h, noise = plane_y, 0.0
            if len(near) >= 100:
                h = float(np.median(near))
                noise = float(1.4826 * np.median(np.abs(near - h)))
            margin = max(2.0 * noise, PLANE_MIN_MARGIN_M)
            margins.append(margin)
            inbox &= pts[..., 1] > h + margin
        out.append(np.where(inbox, f.depth_m, 0.0))
    info = {"plane_margin_mm": round(1000 * float(np.median(margins)), 2) if margins else None}
    return out, info


JOIN_GAP_M = 0.015         # pieces this close to the object piece are part of it (a mesh split by a thin gap)


def _piece_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Gap between two pieces' axis-aligned bounds (0 when they overlap)."""
    gap = np.maximum(0.0, np.maximum(a.min(0) - b.max(0), b.min(0) - a.max(0)))
    return float(np.linalg.norm(gap))


def object_pieces(labels: np.ndarray, pts: np.ndarray, box: Any, min_size: int) -> np.ndarray:
    """Mask of the points (or triangle centres) that belong to the object: the piece that comes nearest the box's
    vertical centre line, and the pieces within JOIN_GAP_M of it; walls, table edges and neighbours poking into the
    box are other pieces. `labels` gives each point its piece (-1 = none)."""
    ids, counts = np.unique(labels[labels >= 0], return_counts=True)
    ids = ids[counts >= min_size]
    if not len(ids):
        return np.zeros(len(pts), bool)
    local = box.local(pts)
    scale = box.extent[[0, 2]]
    r = np.linalg.norm(local[:, [0, 2]] * scale, axis=1)          # metres from the centre line
    near = {int(c): float(r[labels == c].min()) for c in ids}
    core = min(near, key=near.get)
    core_pts = pts[labels == core]
    keep = {core} | {int(c) for c in ids if c != core and _piece_distance(pts[labels == c], core_pts) <= JOIN_GAP_M}
    return np.isin(labels, list(keep))


def central_pieces(v: np.ndarray, f: np.ndarray, box: Any) -> tuple[np.ndarray, np.ndarray, int]:
    """The mesh pieces of the object (object_pieces on triangle centres). Returns (vertices, faces, dropped)."""
    import open3d as o3d

    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(v), o3d.utility.Vector3iVector(f.astype(np.int32)))
    clusters = np.asarray(m.cluster_connected_triangles()[0])
    keep = object_pieces(clusters, v[f].mean(axis=1), box, max(10, int(MIN_PIECE_SHARE * len(f))))
    m.remove_triangles_by_mask(~keep)
    m.remove_unreferenced_vertices()
    return np.asarray(m.vertices), np.asarray(m.triangles), int((~keep).sum())


def object_cloud(points_ply: str | Path, box: Any, plane_y: float | None, voxel_m: float = 0.0025
                 ) -> tuple[np.ndarray, np.ndarray]:
    """The phone's in-box LiDAR voxel cloud (points_file) cut to the object: inside the box, above the support plane
    by twice the plane's noise measured in the cloud itself, and the object's pieces (object_pieces on DBSCAN
    clusters at two voxels). Returns (points N x 3, colours N x 3 uint8)."""
    import open3d as o3d

    pc = o3d.io.read_point_cloud(str(points_ply))
    p = np.asarray(pc.points)
    c = (np.asarray(pc.colors) * 255).astype(np.uint8) if pc.has_colors() else np.full((len(p), 3), GREY, np.uint8)
    keep = box.contains(p, 0.02)
    if plane_y is not None:
        near = p[keep & (np.abs(p[:, 1] - plane_y) < PLANE_SEARCH_M)][:, 1]
        h, noise = plane_y, 0.0
        if len(near) >= 50:
            h = float(np.median(near))
            noise = float(1.4826 * np.median(np.abs(near - h)))
        keep &= p[:, 1] > h + max(2.0 * noise, PLANE_MIN_MARGIN_M)
    p, c = p[keep], c[keep]
    if len(p) < 20:
        return p, c
    sub = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(p))
    labels = np.asarray(sub.cluster_dbscan(eps=2.0 * voxel_m, min_points=4))
    mask = object_pieces(labels, p, box, max(20, int(0.02 * len(p))))
    return p[mask], c[mask]


def choose_hero(size_m: float, cloud_points: int, triangles: int) -> tuple[str, str]:
    """What the 3D view shows, decided from measurements on renders of real scans (2026-10-24): the phone's coloured
    LiDAR cloud reads as the object better than the TSDF mesh on a large object (wires and panels keep their
    colours); a small object (a few centimetres) is a blob in both, so the photos are the better view."""
    if size_m < HERO_MIN_SIZE_M:
        return "photos", "the object is smaller than the LiDAR resolves well; photos show it better"
    if cloud_points >= HERO_POINTS_MIN:
        return "points", "the coloured LiDAR cloud shows the object best"
    if triangles >= HERO_MESH_MIN:
        return "mesh", "the LiDAR mesh shows the object best"
    return "photos", "too little 3D surface of the object was captured; photos show it better"


def thin_cloud(p: np.ndarray, c: np.ndarray, max_points: int) -> tuple[np.ndarray, np.ndarray]:
    if len(p) <= max_points:
        return p, c
    step = int(np.ceil(len(p) / max_points))
    order = np.random.default_rng(0).permutation(len(p))[::step]
    return p[np.sort(order)], c[np.sort(order)]


def points_ply(p: np.ndarray, c: np.ndarray) -> bytes:
    from repairos.importers.ply import ply_bytes

    try:
        return ply_bytes(p, c)
    except TypeError:   # an older writer signature: points only
        return ply_bytes(p)


def moved_segment_frames(pkg_local: Mapping[str, Any], transforms: Mapping[int, np.ndarray]
                         ) -> list[tuple[list[Any], list[np.ndarray], Any, float | None, Mapping[str, Any]]]:
    """Per object-moved segment the server registered (pipeline/segments.py): its frames with poses moved into
    segment 0's frame, their object depths (masked in the segment's own world: its box, its own support plane),
    the box, the plane height and the segment record."""
    from dataclasses import replace

    from repairos.pipeline.segments import frames_of, moved_segments, own_plane_height, segment_box
    from repairos.recon.tsdf import moved_pose

    out = []
    for seg in moved_segments(pkg_local):
        k = int(seg["segment"])
        t = transforms.get(k)
        if t is None:
            continue
        frames = frames_of(pkg_local, {k})
        box, _ = segment_box(pkg_local, seg, t)
        if not frames or box is None:
            continue
        plane = None if (seg.get("pose_source") or pkg_local.get("pose_source")) == "object_tracked" \
            else own_plane_height(frames, box)
        depths, _ = object_depths(frames, box, plane)
        moved = [replace(f, pose=moved_pose(f.pose, t)) for f in frames]
        out.append((moved, depths, box, plane, seg))
    return out


def build_scan_mesh(pkg_local: Mapping[str, Any], *, max_triangles: int,
                    segment_transforms: Mapping[int, np.ndarray] | None = None) -> ScanMesh:
    """The object's coloured, decimated mesh from a scan package whose file fields are local paths: high-confidence
    LiDAR depth inside the object box and above the support plane, a TSDF at a voxel scaled to the box, the pieces
    that reach the box's middle. `segment_transforms` ({segment: 4x4}, pipeline/segments.py): object-moved
    segments the server registered are fused in too, in segment 0's frame (the flipped bottom). Raises
    MeshUnavailable."""
    from repairos.importers.fuse import make_fuser
    from repairos.importers.ply import mesh_ply_bytes
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
    plane = plane_height(pkg, frames, box)
    depths, info = object_depths(frames, box, plane)
    extra = moved_segment_frames(pkg_local, segment_transforms or {})
    for moved, mdepths, _, _, _ in extra:
        frames, depths = frames + moved, depths + mdepths
    t["load_s"] = time.perf_counter() - t0
    t1 = time.perf_counter()
    spacing = voxel_for(box)
    fuser = make_fuser(spacing, prefer="open3d", min_confidence=2, min_depth_m=0.05, max_depth_m=1.5)
    for f, d in zip(frames, depths, strict=True):
        if (d > 0).any():
            fuser.add_depth(d, f.confidence, None, f.pose, f.depth_cam)
    mesh = fuser.result()["mesh"]
    if not mesh or not len(mesh["faces"]):
        raise MeshUnavailable("no object surface above the support plane inside the box")
    t["fuse_s"] = time.perf_counter() - t1
    t2 = time.perf_counter()
    v, f, dropped = central_pieces(np.asarray(mesh["vertices"], float), np.asarray(mesh["faces"]), box)
    v, f, n = decimate(v, f, max_triangles)
    t["clean_s"] = time.perf_counter() - t2
    t3 = time.perf_counter()
    rgb = vertex_colours(v, n, frames)
    t["colour_s"] = time.perf_counter() - t3
    ply = mesh_ply_bytes(v, f, rgb, comments=("repairos object mesh, arkit_world metres",))
    t4 = time.perf_counter()
    cloud, cloud_n, size = None, 0, float(np.ptp(v, axis=0).max()) if len(v) else 0.0
    if pkg.get("points_file"):
        cp, cc = object_cloud(pkg["points_file"], box, plane)
        for moved, _, mbox, mplane, seg in extra:   # the registered bottom's own in-box cloud, carried over
            if seg.get("points_file") and len(cp):
                mp, mc = object_cloud(seg["points_file"], mbox, mplane)
                tr = (segment_transforms or {})[int(seg["segment"])]
                cp, cc = np.vstack([cp, mp @ tr[:3, :3].T + tr[:3, 3]]), np.vstack([cc, mc])
        if len(cp):
            size = float(np.ptp(cp, axis=0).max())
            cloud_n = len(cp)
            cp, cc = thin_cloud(cp, cc, CLOUD_MAX_POINTS)
            cloud = points_ply(cp, cc)
    t["cloud_s"] = time.perf_counter() - t4
    hero, why = choose_hero(size, cloud_n, len(f))
    t["total_s"] = time.perf_counter() - t0
    return ScanMesh(ply=ply, vertices=len(v), triangles=len(f), keyframes=len(frames), cloud_ply=cloud,
                    cloud_points=cloud_n, hero=hero, hero_reason=why, size_m=round(size, 4),
                    seconds={k: round(x, 3) for k, x in t.items()},
                    method=f"tsdf {spacing * 1000:.1f} mm, plane margin {info['plane_margin_mm']} mm, "
                           f"{dropped} off-object triangles dropped"
                           + (f", {len(extra)} flipped segment(s) fused in" if extra else ""))
