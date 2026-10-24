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
Outputs (out_dir): mesh_lidar_tsdf.ply, points_lidar_tsdf.ply, mesh_photogrammetry.ply (when Apple ran), recon.json.
`package["mesh_file"]` is the photogrammetry mesh when Apple's cameras fit ours within `max_apple_rmse_m` (it is the
sharper surface on small objects), else the TSDF mesh; recon.json says which and why.
"""
from __future__ import annotations

import json
import os
import resource
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from repairos.importers.ply import mesh_ply_bytes, ply_bytes
from repairos.recon import merge as M
from repairos.recon.align import Sim3, residuals, umeyama
from repairos.recon.cli import (ApplePose, MemoryGuardError, check_free_memory, load_poses, load_samples, run_cli,
                                tree_rss_mb)
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
    with sw("read_samples"):
        for s in segments:
            seg, samples = read_segment(s, work, max_memory_gb=max_memory_gb, min_free_pct=min_free_pct)
            segs.append(seg)
            report["segments"].append({"name": s.name, "images": len(samples), "with_pose_and_depth": len(seg.frames),
                                       "box_extent_m": seg.box.extent.round(4).tolist() if seg.box else None})
    guard_self(max_memory_gb, "reading the segments")

    with sw("tsdf_per_segment"):
        per_seg: list[FusedGeometry] = [fuse_frames(s.frames, box=s.box, prefer=prefer) for s in segs]
    guard_self(max_memory_gb, "per-segment fusion")

    # Apple: one joint run over every image (with >= 2 segments it is also the merge cross-check).
    apple_poses: dict[str, list[ApplePose]] = {}
    apple_summary = None
    apple_dir = work / "apple"
    if apple:
        names = M.link_images({s.name: [f.image_path for f in seg.frames] for s, seg in zip(segments, segs,
                                                                                            strict=True)},
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
    report["apple"] = apple_summary

    # Merge every further segment into the reference.
    transforms: list[Sim3] = [Sim3.identity()]
    merges = []
    for k in range(1, len(segs)):
        ref, mov = segs[0], segs[k]
        with sw(f"merge_{mov.name}"):
            mr = M.merge_segments(ref, mov, ref_points=per_seg[0].points, mov_points=per_seg[k].points,
                                  apple_poses={"reference": apple_poses.get(ref.name, []),
                                               "moving": apple_poses.get(mov.name, [])} if apple_poses else None,
                                  cache_dir=work / "cache")
        merges.append({"segment": mov.name, **mr.to_dict()})
        transforms.append(mr.transform if mr.merged else None)
    report["merges"] = merges
    guard_self(max_memory_gb, "merging")

    # Merged LiDAR mesh in the reference frame (unmerged segments are left out, never faked).
    fused_segs = []
    for seg, t in zip(segs, transforms, strict=True):
        if t is not None:
            fused_segs.append(Segment(seg.frames, seg.box, t.matrix, seg.name))
    with sw("tsdf_merged"):
        merged = fuse_segments(fused_segs, prefer=prefer)
    guard_self(max_memory_gb, "merged fusion")
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
    if apple_poses.get(segs[0].name) and (apple_dir / "mesh.ply").exists():
        ref_centres = {f.image_path.name: f.pose.t for f in segs[0].frames}
        s_ref, info = M.segment_sim3(apple_poses[segs[0].name], ref_centres)
        apple_fit = info
        if s_ref is not None:
            import open3d as o3d

            m = o3d.io.read_triangle_mesh(str(apple_dir / "mesh.ply"))
            v = s_ref.apply(np.asarray(m.vertices))
            (out / "mesh_photogrammetry.ply").write_bytes(mesh_ply_bytes(v, np.asarray(m.triangles)))
            files["mesh_photogrammetry"] = "mesh_photogrammetry.ply"
            report["photogrammetry_mesh"] = {"triangles": int(len(m.triangles)), "sim3_to_reference": info}
    use_apple = (apple_fit is not None and "mesh_photogrammetry" in files
                 and (apple_fit.get("residual", {}).get("rmse_m") or 1e9) <= max_apple_rmse_m)
    files["mesh_file"] = files["mesh_photogrammetry"] if use_apple else files.get("mesh_lidar_tsdf")
    report["mesh_choice"] = {
        "mesh_file": files["mesh_file"],
        "why": ("Apple's cameras fit the reference ARKit cameras within "
                f"{max_apple_rmse_m * 1000:.0f} mm: the photogrammetry surface is the sharper one" if use_apple else
                "no photogrammetry mesh that fits the reference cameras: the LiDAR TSDF mesh")}

    # Every still's pose in the reference frame.
    stills = []
    for seg, t in zip(segs, transforms, strict=True):
        for f in seg.frames:
            st = {"still_id": Path(f.name).stem, "scan_id": scan_id, "segment": seg.name,
                  "image_file": str(f.image_path), "width_px": f.image_cam.width, "height_px": f.image_cam.height,
                  "intrinsics": {"fx": f.image_cam.fx, "fy": f.image_cam.fy, "cx": f.image_cam.cx,
                                 "cy": f.image_cam.cy},
                  "tracking_state": "unknown"}
            if t is None:
                st.update(pose_reliable=False, pose_note="segment not merged: pose is in its own segment's world")
                st["camera_own_segment"] = pose_dict(f.pose)
            else:
                st["camera"] = pose_dict(moved_pose(f.pose, t.matrix))
                src = "arkit" if seg is segs[0] else f"arkit+merge({merges[segs.index(seg) - 1]['source']})"
                st["pose_source"] = src
            stills.append(st)

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
