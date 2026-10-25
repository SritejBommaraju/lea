"""One object, one frame: build the merged mesh and every still's pose in the reference segment's ARKit world, ready
for `repairos.locate`.

    res = build([seg_top, seg_bottom], out_dir)       # SegmentInput per capture segment, the first is the reference
    res.package     # {"scan_id", "frame", "depth_source", "points_file", "mesh_file", ...} -> locate's `scan`
    res.stills      # Still-shaped dicts (camera, intrinsics, width/height, segment) in that frame

Per segment (an Object Capture image folder: each HEIC carries its ARKit pose, intrinsics and LiDAR depth, read by
`recon-cli samples`):
1. LiDAR TSDF of the object (tsdf.py), seconds.
2. With >= 2 segments: the merge (merge.py): SIFT bridge -> ICP -> Apple joint-reconstruction cross-check.
3. Apple PhotogrammetrySession (recon-cli reconstruct) on all images, unless `apple=False`: its mesh is put into the
   reference frame by sim(3) on camera centres (align.py).
Every image with an ARKit pose is a still, with or without depth (only the depth frames go into the LiDAR fusion).
The Python steps run under `memory_watch` (stopped once over `max_memory_gb`); recon-cli runs under its own guard.
Outputs (out_dir): mesh_lidar_tsdf.ply, points_lidar_tsdf.ply, mesh_photogrammetry.ply (when Apple ran), recon.json.
`package["mesh_file"]` is the photogrammetry mesh when Apple's cameras fit ours within `max_apple_rmse_m` (it is the
sharper surface on small objects) and every further segment was merged where Apple's joint reconstruction put it
(`apple_placed_every_segment`), else the TSDF mesh; recon.json says which and why.
"""
from __future__ import annotations

import _thread
import json
import os
import resource
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from repairos.importers.ply import mesh_ply_bytes, ply_bytes
from repairos.recon import merge as M
from repairos.recon.align import Sim3, residuals, umeyama
from repairos.recon.cli import (ApplePose, CliResult, MemoryGuardError, SampleInfo, check_free_memory, load_poses,
                                load_samples, run_cli, tree_rss_mb)
from repairos.recon.tsdf import (DepthFrame, FusedGeometry, ObjectBox, Segment, frames_from_samples, fuse_frames,
                                 fuse_segments, moved_pose)

FRAME = "arkit_world"


@dataclass
class SegmentInput:
    images_dir: Path
    name: str


@dataclass
class BuildResult:
    out_dir: Path
    package: dict
    stills: list[dict]
    report: dict
    timings: dict = field(default_factory=dict)


class Stopwatch:
    def __init__(self) -> None:
        self.t: dict[str, float] = {}

    def __call__(self, name: str):
        sw = self

        class _C:
            def __enter__(self):
                self.t0 = time.monotonic()

            def __exit__(self, *exc):
                sw.t[name] = round(sw.t.get(name, 0.0) + time.monotonic() - self.t0, 3)

        return _C()


def self_rss_gb() -> float:
    return tree_rss_mb(os.getpid()) / 1024.0


def guard_self(max_memory_gb: float, where: str) -> None:
    rss = self_rss_gb()
    if rss > max_memory_gb:
        raise MemoryGuardError(f"the reconstruction process uses {rss:.2f} GB after {where}, over the "
                               f"{max_memory_gb:.1f} GB limit: stopped")


@contextmanager
def memory_watch(max_memory_gb: float, where: str, poll_s: float = 0.5) -> Iterator[None]:
    """Runs a Python step under the memory limit: this process is sampled every `poll_s` while the step runs, and
    once it is over `max_memory_gb` the step is stopped (interrupted on the main thread; elsewhere it stops at its
    end) with MemoryGuardError, so a large temporary allocation cannot pass unseen between checks."""
    over: list[float] = []
    lock, done = threading.Lock(), threading.Event()
    interrupt = threading.current_thread() is threading.main_thread()

    def watch() -> None:
        while not done.wait(poll_s):
            rss = self_rss_gb()
            if rss > max_memory_gb:
                with lock:
                    if not done.is_set():
                        over.append(rss)
                        if interrupt:
                            _thread.interrupt_main()
                return

    th = threading.Thread(target=watch, daemon=True)
    th.start()
    try:
        try:
            yield
        finally:
            with lock:
                done.set()
            th.join()
    except KeyboardInterrupt:
        if not over:
            raise
    if over:
        raise MemoryGuardError(f"the reconstruction process used {over[0]:.2f} GB during {where}, over the "
                               f"{max_memory_gb:.1f} GB limit: stopped")
    guard_self(max_memory_gb, where)


def read_segment(seg: SegmentInput, work: Path, *, max_memory_gb: float, min_free_pct: float | None):
    out = work / f"samples_{seg.name}"
    run_cli(["samples", "--input", seg.images_dir, "--out", out, "--depth"], max_memory_gb=max_memory_gb,
            min_free_pct=min_free_pct)
    samples = load_samples(out / "samples.json")
    frames = frames_from_samples(samples, seg.images_dir)
    box = next((ObjectBox(s.bounding_box) for s in samples if s.bounding_box is not None), None)
    return Segment(frames, box, name=seg.name), samples


def pose_dict(pose) -> dict:
    return pose.to_contract()


def posed_samples(samples: Sequence[SampleInfo]) -> list[SampleInfo]:
    """The images with an ARKit pose and intrinsics: each is a still, with or without LiDAR depth (only the depth
    frames go into the LiDAR fusion)."""
    return [s for s in samples if s.arkit_pose is not None and s.cam is not None]


def apple_mesh_of(res: CliResult) -> Path | None:
    """The mesh this Apple run reported writing; a mesh.ply an earlier run left in a reused out dir is not it."""
    mesh = res.summary.get("mesh_file") if res.ok else None
    return Path(mesh) if mesh else None


def read_mesh_ply(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """(vertices N x 3, triangles M x 3) of a binary little-endian mesh PLY as recon-cli and `mesh_ply_bytes` write
    it: float x, y, z vertices, then a `uchar int` face list of triangles. NumPy only (Open3D is an optional extra)."""
    data = Path(path).read_bytes()
    end = data.find(b"end_header\n")
    if not data.startswith(b"ply\n") or end < 0:
        raise ValueError(f"{path}: not a PLY file")
    head = data[:end].decode("ascii").splitlines()
    if "format binary_little_endian 1.0" not in head:
        raise ValueError(f"{path}: not a binary little-endian PLY")
    counts, vprops, element = {}, [], None
    for line in head:
        w = line.split()
        if w[:1] == ["element"]:
            element = w[1]
            counts[element] = int(w[2])
        elif w[:1] == ["property"] and element == "vertex":
            vprops.append((w[2], {"float": "<f4", "uchar": "u1", "double": "<f8"}[w[1]]))
        elif w[:1] == ["property"] and element == "face" and w[1:4] not in (["list", "uchar", "int"],
                                                                            ["list", "uint8", "int32"]):
            raise ValueError(f"{path}: face list {' '.join(w[1:4])} is not uchar int")
    vt = np.dtype(vprops)
    off = end + len(b"end_header\n")
    verts = np.frombuffer(data, vt, counts.get("vertex", 0), off)
    faces = np.frombuffer(data, np.dtype([("n", "u1"), ("i", "<i4", (3,))]), counts.get("face", 0),
                          off + vt.itemsize * counts.get("vertex", 0))
    if len(faces) and not np.all(faces["n"] == 3):
        raise ValueError(f"{path}: only triangle faces are read")
    v = np.stack([verts["x"], verts["y"], verts["z"]], axis=1).astype(float)
    return v, faces["i"].astype(np.int64)


def apple_placed_every_segment(merges: Sequence[dict]) -> bool:
    """Apple's joint mesh holds every segment where Apple's joint reconstruction put it: it matches the accepted
    LiDAR points and still poses only when each further segment was merged there (Apple's placement itself, or a
    placement that agreed with it)."""
    for m in merges:
        if not m["merged"]:
            return False
        if m["source"] != "apple_joint" and (not m["agreement"] or "cloud_fit_3mm" in m["agreement"]):
            return False   # never compared with Apple's placement, or chosen over it after a disagreement
    return True


def stills_of(segments: Sequence[SegmentInput], posed: Sequence[Sequence[SampleInfo]],
              transforms: Sequence[Sim3 | None], merges: Sequence[dict], scan_id: str) -> list[dict]:
    """Every posed image as a still, its pose in the reference frame (or in its own segment's world when that
    segment was not merged)."""
    stills = []
    for k, (s_in, samples, t) in enumerate(zip(segments, posed, transforms, strict=True)):
        for smp in samples:
            st = {"still_id": Path(smp.file).stem, "scan_id": scan_id, "segment": s_in.name,
                  "image_file": str(s_in.images_dir / smp.file), "width_px": smp.cam.width or smp.width,
                  "height_px": smp.cam.height or smp.height,
                  "intrinsics": {"fx": smp.cam.fx, "fy": smp.cam.fy, "cx": smp.cam.cx, "cy": smp.cam.cy},
                  "tracking_state": "unknown"}
            if t is None:
                st.update(pose_reliable=False, pose_note="segment not merged: pose is in its own segment's world")
                st["camera_own_segment"] = pose_dict(smp.arkit_pose)
            else:
                st["camera"] = pose_dict(moved_pose(smp.arkit_pose, t.matrix))
                st["pose_source"] = "arkit" if k == 0 else f"arkit+merge({merges[k - 1]['source']})"
            stills.append(st)
    return stills


def build(segments: Sequence[SegmentInput], out_dir: str | Path, *, apple: bool = True, detail: str = "reduced",
          max_memory_gb: float = 6.0, min_free_pct: float | None = 25.0, max_apple_rmse_m: float = 0.015,
          masking: bool = True, prefer: str = "auto", scan_id: str = "recon") -> BuildResult:
    if not segments:
        raise ValueError("no segments")
    out = Path(out_dir)
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    sw = Stopwatch()
    report: dict[str, Any] = {"frame": FRAME, "reference_segment": segments[0].name, "segments": []}
    if min_free_pct is not None:
        report["memory_free_pct_at_start"] = check_free_memory(min_free_pct)

    segs: list[Segment] = []
    all_images: list[list[Path]] = []
    posed: list[list[SampleInfo]] = []
    with sw("read_samples"):   # recon-cli runs under its own guard; the depth grids read here are small
        for s in segments:
            seg, samples = read_segment(s, work, max_memory_gb=max_memory_gb, min_free_pct=min_free_pct)
            segs.append(seg)
            all_images.append([s.images_dir / smp.file for smp in samples])
            posed.append(posed_samples(samples))
            report["segments"].append({"name": s.name, "images": len(samples), "with_pose": len(posed[-1]),
                                       "with_pose_and_depth": len(seg.frames),
                                       "box_extent_m": seg.box.extent.round(4).tolist() if seg.box else None})
    guard_self(max_memory_gb, "reading the segments")

    with sw("tsdf_per_segment"), memory_watch(max_memory_gb, "per-segment fusion"):
        per_seg: list[FusedGeometry] = [fuse_frames(s.frames, box=s.box, prefer=prefer) for s in segs]

    # Apple: one joint run over every image (with >= 2 segments it is also the merge cross-check).
    apple_poses: dict[str, list[ApplePose]] = {}
    apple_summary = None
    apple_mesh: Path | None = None
    apple_dir = work / "apple"
    if apple:
        # every image, depth or not (photogrammetry needs no LiDAR depth)
        names = M.link_images({s.name: imgs for s, imgs in zip(segments, all_images, strict=True)},
                              apple_dir / "images")
        args = ["reconstruct", "--input", apple_dir / "images", "--out", apple_dir, "--masking",
                "on" if masking else "off", "--detail", detail]
        with sw("apple_photogrammetry"):
            res = run_cli(args, max_memory_gb=max_memory_gb, min_free_pct=min_free_pct, check=False)
        apple_summary = {k: res.summary.get(k) for k in ("ok", "poses", "points", "mesh_triangles", "images",
                                                         "stitching_incomplete", "skipped_samples",
                                                         "invalid_samples", "errors", "request_done_s")}
        apple_summary.update(wall_s=round(res.wall_s, 2), peak_rss_mb=round(res.peak_rss_mb, 1))
        if res.ok:
            apple_poses = M.split_joint_poses(load_poses(apple_dir / "poses.json"), names)
        apple_mesh = apple_mesh_of(res)
    report["apple"] = apple_summary

    # Merge every further segment into the reference.
    transforms: list[Sim3] = [Sim3.identity()]
    merges = []
    for k in range(1, len(segs)):
        ref, mov = segs[0], segs[k]
        with sw(f"merge_{mov.name}"), memory_watch(max_memory_gb, "merging"):
            mr = M.merge_segments(ref, mov, ref_points=per_seg[0].points, mov_points=per_seg[k].points,
                                  apple_poses={"reference": apple_poses.get(ref.name, []),
                                               "moving": apple_poses.get(mov.name, [])} if apple_poses else None,
                                  cache_dir=work / "cache")
        merges.append({"segment": mov.name, **mr.to_dict()})
        transforms.append(mr.transform if mr.merged else None)
    report["merges"] = merges

    # Merged LiDAR mesh in the reference frame (unmerged segments are left out, never faked).
    fused_segs = []
    for seg, t in zip(segs, transforms, strict=True):
        if t is not None:
            fused_segs.append(Segment(seg.frames, seg.box, t.matrix, seg.name))
    with sw("tsdf_merged"), memory_watch(max_memory_gb, "merged fusion"):
        merged = fuse_segments(fused_segs, prefer=prefer)
    files: dict[str, str] = {}
    (out / "points_lidar_tsdf.ply").write_bytes(ply_bytes(merged.points, merged.rgb))
    files["points_file"] = "points_lidar_tsdf.ply"
    if merged.mesh_vertices is not None:
        (out / "mesh_lidar_tsdf.ply").write_bytes(mesh_ply_bytes(merged.mesh_vertices, merged.mesh_faces))
        files["mesh_lidar_tsdf"] = "mesh_lidar_tsdf.ply"
    report["lidar_tsdf"] = {"method": merged.method, "points": int(len(merged.points)),
                            "triangles": merged.triangles, "segments_fused": [s.name for s in fused_segs],
                            "params": merged.params}

    # Apple mesh into the reference frame.
    apple_fit = None
    if apple_poses.get(segs[0].name) and apple_mesh is not None and apple_mesh.exists():
        ref_centres = {f.image_path.name: f.pose.t for f in segs[0].frames}
        s_ref, info = M.segment_sim3(apple_poses[segs[0].name], ref_centres)
        apple_fit = info
        if s_ref is not None:
            verts, tris = read_mesh_ply(apple_mesh)
            (out / "mesh_photogrammetry.ply").write_bytes(mesh_ply_bytes(s_ref.apply(verts), tris))
            files["mesh_photogrammetry"] = "mesh_photogrammetry.ply"
            report["photogrammetry_mesh"] = {"triangles": int(len(tris)), "sim3_to_reference": info}
    fits_reference = (apple_fit is not None and "mesh_photogrammetry" in files
                      and (apple_fit.get("residual", {}).get("rmse_m") or 1e9) <= max_apple_rmse_m)
    use_apple = fits_reference and apple_placed_every_segment(merges)
    files["mesh_file"] = files["mesh_photogrammetry"] if use_apple else files.get("mesh_lidar_tsdf")
    report["mesh_choice"] = {
        "mesh_file": files["mesh_file"],
        "why": ("Apple's cameras fit the reference ARKit cameras within "
                f"{max_apple_rmse_m * 1000:.0f} mm: the photogrammetry surface is the sharper one" if use_apple else
                "the photogrammetry mesh places a segment where the merge did not: the LiDAR TSDF mesh"
                if fits_reference else "no photogrammetry mesh that fits the reference cameras: the LiDAR TSDF mesh")}

    # Every posed image's pose in the reference frame (images without depth too).
    stills = stills_of(segments, posed, transforms, merges, scan_id)

    voxel_mm = merged.params.get("spacing", {}).get("spacing_mm")
    package = {"scan_id": scan_id, "frame": FRAME, "units": "m", "depth_source": "lidar",
               "points_file": files["points_file"], "point_count": int(len(merged.points)),
               "voxel_mm": voxel_mm, "mesh_file": files["mesh_file"]}
    peak_self = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2 ** 20
    report.update(files=files, package=package, timings_s=sw.t, peak_python_rss_mb=round(peak_self, 1))
    (out / "recon.json").write_text(json.dumps({**report, "stills": stills}, indent=1, default=_json_default) + "\n")
    return BuildResult(out, package, stills, report, sw.t)


def _json_default(o):
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    return str(o)


def camera_residual(frames: Sequence[DepthFrame], poses: Sequence[ApplePose]) -> dict:
    """sim(3) fit of Apple's camera centres to the frames' ARKit centres (shared images only)."""
    c = {f.image_path.name: f.pose.t for f in frames if f.image_path}
    pairs = [(p.centre, c[p.file]) for p in poses if p.file in c]
    if len(pairs) < 3:
        return {"n": len(pairs)}
    a, b = np.array([p for p, _ in pairs]), np.array([q for _, q in pairs])
    s = umeyama(a, b)
    return {**residuals(s, a, b), "scale": s.scale}
