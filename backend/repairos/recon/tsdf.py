"""LiDAR depth + ARKit poses -> a mesh of the object (the fast path, seconds on the Mac).

    frames = frames_from_samples(load_samples("samples.json"))        # Object Capture HEICs (recon-cli samples)
    frames = frames_from_scan_package(pkg, session_dir)               # app keyframes (kf_*.jpg + depth + conf)
    out = fuse_frames(frames, box=box)                                # FusedGeometry: points, mesh, params

Fusion is `repairos.importers.fuse` (Open3D tensor VoxelBlockGrid TSDF when Open3D is installed; the numpy voxel
average otherwise, which gives points and no mesh: locate then ray-casts the points). Before fusing, depth pixels
whose 3D point falls outside the object box (grown by `box_margin`) are dropped, so the room is not fused; after
fusing, the support plane is found (the lowest well-supported horizontal plane: ARKit world is gravity-aligned,
Y up) and depth pixels within `plane_clearance_m` of it or below it are dropped too, per segment in its own world,
so a flipped segment keeps its own table out and the object bottom it adds survives.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from repairos.importers.fuse import make_fuser, measure_spacing
from repairos.locate.camera import Pinhole, Pose, backproject
from repairos.recon.cli import SampleInfo, read_f32


@dataclass(frozen=True)
class ObjectBox:
    """Oriented box in the world: points p with |inv(matrix) p|_inf <= 0.5 are inside (a unit cube centred at 0,
    as Object Capture's `boundingBox` and the contract's crop_box with extent = side lengths)."""
    matrix: np.ndarray    # 4x4, columns 0-2 are the scaled box axes, column 3 the centre

    @classmethod
    def from_contract(cls, box: Mapping) -> ObjectBox:
        from repairos.locate.camera import quat_to_matrix

        m = np.eye(4)
        m[:3, :3] = quat_to_matrix(box["quaternion_xyzw"]) @ np.diag(np.asarray(box["extent_m"], float))
        m[:3, 3] = np.asarray(box["center_m"], float)
        return cls(m)

    @property
    def centre(self) -> np.ndarray:
        return self.matrix[:3, 3].copy()

    @property
    def extent(self) -> np.ndarray:
        return np.linalg.norm(self.matrix[:3, :3], axis=0)

    def local(self, p: np.ndarray) -> np.ndarray:
        inv = np.linalg.inv(self.matrix)
        return np.asarray(p, float) @ inv[:3, :3].T + inv[:3, 3]

    def contains(self, p: np.ndarray, margin: float = 0.0) -> np.ndarray:
        """`margin` grows the box by that fraction of each side."""
        return np.all(np.abs(self.local(p)) <= 0.5 + margin, axis=-1)

    def corners(self) -> np.ndarray:
        c = np.array([[x, y, z] for x in (-0.5, 0.5) for y in (-0.5, 0.5) for z in (-0.5, 0.5)])
        return c @ self.matrix[:3, :3].T + self.matrix[:3, 3]

    def transformed(self, m4: np.ndarray) -> ObjectBox:
        return ObjectBox(np.asarray(m4, float) @ self.matrix)


@dataclass
class DepthFrame:
    name: str
    pose: Pose                 # camera-to-world, ARKit camera axes
    depth_cam: Pinhole         # intrinsics of the depth grid
    depth_m: np.ndarray        # H x W, metres along the view axis
    confidence: np.ndarray | None = None   # H x W, ARKit 0/1/2
    image_path: Path | None = None
    image_cam: Pinhole | None = None       # intrinsics of the full image (stored pixel orientation)


def frames_from_samples(samples: Sequence[SampleInfo], images_dir: str | Path | None = None) -> list[DepthFrame]:
    """Object Capture images that carry an ARKit pose and depth (recon-cli samples --depth)."""
    out = []
    for s in samples:
        if s.arkit_pose is None or s.cam is None or s.depth_file is None or s.depth_shape is None:
            continue
        h, w = s.depth_shape
        depth = read_f32(s.depth_file, s.depth_shape)
        conf = np.rint(read_f32(s.conf_file, s.depth_shape)).astype(np.uint8) if s.conf_file else None
        out.append(DepthFrame(s.file, s.arkit_pose, s.cam.scaled(w, h), depth, conf,
                              Path(images_dir) / s.file if images_dir else None, s.cam))
    return out


def frames_from_scan_package(pkg: Mapping, base_dir: str | Path) -> list[DepthFrame]:
    """Keyframes of a ScanPackage that have a depth map (uint16 mm PNG or float32 m) and a pose."""
    from PIL import Image

    base = Path(base_dir)
    out = []
    for kf in pkg.get("keyframes", []):
        if not kf.get("depth_file"):
            continue
        dpath = base / kf["depth_file"]
        if kf.get("depth_encoding", "uint16_mm") == "uint16_mm" or dpath.suffix.lower() == ".png":
            depth = np.asarray(Image.open(dpath), dtype=np.float64) / 1000.0
        else:
            raw = np.fromfile(dpath, dtype="<f4")
            depth = raw.reshape(192, 256)
        conf = (np.asarray(Image.open(base / kf["confidence_file"]), dtype=np.uint8)
                if kf.get("confidence_file") else None)
        h, w = depth.shape
        cam = Pinhole.from_contract(kf["intrinsics"])
        out.append(DepthFrame(kf["keyframe_id"], Pose.from_contract(kf["camera"]), cam.scaled(w, h), depth, conf,
                              base / kf["image_file"], cam))
    return out


def box_mask(frame: DepthFrame, box: ObjectBox, margin: float) -> np.ndarray:
    pts = frame.pose.to_world(backproject(frame.depth_m, frame.depth_cam))
    return box.contains(pts, margin)


@dataclass
class SupportPlane:
    height_m: float          # world Y of the plane
    support: int             # points within the band
    band_m: float

    def to_dict(self) -> dict:
        return {"height_m": round(self.height_m, 5), "support_points": self.support, "band_m": self.band_m,
                "normal": [0.0, 1.0, 0.0]}


def find_support_plane(points: np.ndarray, *, band_m: float = 0.002, min_fraction: float = 0.05,
                       lowest_share: float = 0.5) -> SupportPlane | None:
    """The supporting surface under the object: among horizontal planes y = h (world Y up) with at least
    `min_fraction` of the points within `band_m`, the lowest whose support is >= `lowest_share` of the best one.
    None when no plane is well supported."""
    y = np.sort(np.asarray(points, float)[:, 1])
    if len(y) < 50:
        return None
    lo = np.searchsorted(y, y - band_m, side="left")
    hi = np.searchsorted(y, y + band_m, side="right")
    count = hi - lo
    best = int(count.max())
    if best < min_fraction * len(y):
        return None
    ok = np.nonzero(count >= lowest_share * best)[0]
    i = int(ok[0])
    # Refine: the densest centre within this cluster (the run of candidates near the lowest strong one).
    near = np.nonzero((np.abs(y - y[i]) <= band_m) & (count >= lowest_share * best))[0]
    j = int(near[np.argmax(count[near])])
    sel = y[lo[j]:hi[j]]
    return SupportPlane(float(np.median(sel)), int(len(sel)), band_m)


@dataclass
class FusedGeometry:
    points: np.ndarray                     # N x 3, world metres
    rgb: np.ndarray                        # N x 3 uint8
    mesh_vertices: np.ndarray | None
    mesh_faces: np.ndarray | None
    method: str
    params: dict = field(default_factory=dict)

    @property
    def triangles(self) -> int:
        return 0 if self.mesh_faces is None else int(len(self.mesh_faces))


@dataclass
class Segment:
    """One capture segment: frames whose poses share one world (one ARKit session, the object not moved), the
    object box in that world, and the rigid transform from that world into the output frame (identity for the
    reference segment)."""
    frames: list[DepthFrame]
    box: ObjectBox | None = None
    to_output: np.ndarray = field(default_factory=lambda: np.eye(4))
    name: str = "segment"


def mask_segment(seg: Segment, *, box_margin: float = 0.15, remove_plane: bool = True,
                 plane_clearance_m: float = 0.003, min_confidence: int = 1, per_frame_plane: bool = True,
                 per_frame_search_m: float = 0.02) -> tuple[list[np.ndarray], dict]:
    """Per frame, the depth with every pixel outside the object box (grown by `box_margin`) or on / below the
    support plane (+ `plane_clearance_m`) set to 0 (no measurement), all in the segment's own world, where the
    plane is horizontal (ARKit is gravity-aligned). Returns (masked depths, info)."""
    worlds, oks = [], []
    for f in seg.frames:
        pts = f.pose.to_world(backproject(f.depth_m, f.depth_cam))
        ok = np.isfinite(f.depth_m) & (f.depth_m > 0)
        if f.confidence is not None:
            ok &= f.confidence >= min_confidence
        if seg.box is not None:
            ok &= seg.box.contains(pts, box_margin)
        worlds.append(pts)
        oks.append(ok)
    info: dict = {"frames": len(seg.frames), "box_margin": box_margin if seg.box is not None else None,
                  "support_plane": None}
    if remove_plane:
        sample = np.concatenate([w[o] for w, o in zip(worlds, oks, strict=True)]) if worlds else np.zeros((0, 3))
        plane = find_support_plane(sample)
        if plane is not None:
            # ARKit drifts vertically over a capture (we measured up to ~1.5 cm over two minutes), so the table is
            # found again in each frame, near the segment's plane; a frame that sees too little table uses the
            # segment's plane.
            heights = []
            for i, (w, o) in enumerate(zip(worlds, oks, strict=True)):
                h = plane.height_m
                if per_frame_plane:
                    near = w[o & (np.abs(w[..., 1] - plane.height_m) < per_frame_search_m)]
                    fp = find_support_plane(near, min_fraction=0.2) if len(near) >= 200 else None
                    if fp is not None:
                        h = fp.height_m
                heights.append(h)
                oks[i] = o & (w[..., 1] > h + plane_clearance_m)
            info["support_plane"] = {**plane.to_dict(), "clearance_m": plane_clearance_m,
                                     "per_frame": per_frame_plane,
                                     "frame_height_spread_m": round(float(np.ptp(heights)), 5)}
    depths = [np.where(o, f.depth_m, 0.0) for f, o in zip(seg.frames, oks, strict=True)]
    return depths, info


def moved_pose(pose: Pose, m4: np.ndarray) -> Pose:
    """`pose` (camera-to-world) after the rigid world transform m4 (4x4)."""
    m4 = np.asarray(m4, float)
    return Pose(m4[:3, :3] @ pose.r, m4[:3, :3] @ pose.t + m4[:3, 3])


def fuse_segments(segments: Sequence[Segment], *, box_margin: float = 0.15, prefer: str = "auto",
                  spacing_m: float | None = None, min_confidence: int = 2, min_depth_m: float = 0.05,
                  max_depth_m: float = 1.5, remove_plane: bool = True,
                  plane_clearance_m: float = 0.003, per_frame_plane: bool = True,
                  min_component_share: float = 0.1) -> FusedGeometry:
    """Mask each segment in its own world, then fuse every frame in the output frame (segment.to_output)."""
    if not segments or not any(s.frames for s in segments):
        raise ValueError("no depth frames")
    items: list[tuple[DepthFrame, np.ndarray, Pose]] = []
    seg_info = []
    for seg in segments:
        depths, info = mask_segment(seg, box_margin=box_margin, remove_plane=remove_plane,
                                    per_frame_plane=per_frame_plane,
                                    plane_clearance_m=plane_clearance_m)
        seg_info.append({"name": seg.name, **info})
        items += [(f, d, moved_pose(f.pose, seg.to_output)) for f, d in zip(seg.frames, depths, strict=True)]
    sp = measure_spacing(((d, f.confidence, f.depth_cam) for f, d, _ in items), min_confidence=min_confidence,
                         min_depth_m=min_depth_m, max_depth_m=max_depth_m)
    if sp is None:
        raise ValueError("no depth pixel passed the confidence / range / box / plane mask")
    spacing = spacing_m or sp.spacing_m
    fuser = make_fuser(spacing, prefer=prefer, min_confidence=min_confidence, min_depth_m=min_depth_m,
                       max_depth_m=max_depth_m)
    for f, d, pose in items:
        fuser.add_depth(d, f.confidence, None, pose, f.depth_cam)
    res = fuser.result()
    mesh = res["mesh"]
    params = {**res["params"], "spacing": sp.to_dict(), "segments": seg_info}
    mv, mf = (mesh["vertices"], mesh["faces"]) if mesh else (None, None)
    if mv is not None and len(mf):
        mv, mf, dropped = keep_main_components(mv, mf, min_share=min_component_share)
        params["islands_dropped_triangles"] = dropped
    return FusedGeometry(res["xyz"], res["rgb"], mv, mf, res["method"], params)


def keep_main_components(v: np.ndarray, f: np.ndarray, *, min_share: float = 0.1):
    """Drop connected pieces smaller than `min_share` of the largest (LiDAR specks on the table that survived the
    plane cut). Union-find over shared vertices, numpy only."""
    parent = np.arange(len(v))

    def find(x: np.ndarray) -> np.ndarray:
        while True:
            p = parent[x]
            if np.array_equal(p, x):
                return x
            parent[x] = parent[p]
            x = parent[x]

    edges = np.concatenate([f[:, [0, 1]], f[:, [1, 2]]])
    for _ in range(64):   # label propagation: min root over edges until stable
        ra, rb = find(edges[:, 0]), find(edges[:, 1])
        lo = np.minimum(ra, rb)
        changed = (ra != lo) | (rb != lo)
        if not changed.any():
            break
        np.minimum.at(parent, ra, lo)
        np.minimum.at(parent, rb, lo)
    root = find(np.arange(len(v)))
    tri_root = root[f[:, 0]]
    labels, counts = np.unique(tri_root, return_counts=True)
    keep_labels = labels[counts >= min_share * counts.max()]
    keep = np.isin(tri_root, keep_labels)
    v2, f2, _ = _compact(v, f[keep])
    return v2, f2, int((~keep).sum())


def fuse_frames(frames: Iterable[DepthFrame], *, box: ObjectBox | None = None, **kw) -> FusedGeometry:
    """One segment in its own world."""
    return fuse_segments([Segment(list(frames), box)], **kw)


def _compact(v: np.ndarray, f: np.ndarray):
    used = np.zeros(len(v), bool)
    used[f.reshape(-1)] = True
    remap = -np.ones(len(v), np.int64)
    remap[used] = np.arange(int(used.sum()))
    return v[used], remap[f], used
