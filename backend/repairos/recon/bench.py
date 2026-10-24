"""Benchmark the reconstruction paths on real captures (Object Capture image folders: HEICs with ARKit pose + LiDAR
depth). One heavy job at a time; each Python path runs in its own process so its peak memory is its own.

    python -m repairos.recon.bench --ref <images dir> [--other <images dir>] --out <dir> [--paths a,b,...]

Paths: apple_reduced, apple_poses_only, apple_no_depth, apple_medium, tsdf_open3d, tsdf_numpy, merge.
Writes <out>/bench.json and prints a Markdown table. Quality: poses recovered / images, points, triangles, and for
meshes the distance to Apple's reduced mesh (both in the ARKit frame, Apple's moved there by sim(3)).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from repairos.recon.cli import check_free_memory, load_poses, load_samples, run_cli

APPLE = {"apple_reduced": ["--detail", "reduced"], "apple_poses_only": ["--detail", "none"],
         "apple_no_depth": ["--detail", "reduced", "--no-depth"], "apple_medium": ["--detail", "medium"]}


def apple_path(name: str, images: Path, out: Path, max_gb: float) -> dict:
    o = out / name
    res = run_cli(["reconstruct", "--input", images, "--out", o, *APPLE[name]], max_memory_gb=max_gb,
                  check=False)
    s = res.summary
    return {"path": name, "wall_s": round(res.wall_s, 1), "peak_mb": round(res.peak_rss_mb), "ok": res.ok,
            "poses": s.get("poses"), "images": s.get("images"), "points": s.get("points"),
            "triangles": s.get("mesh_triangles"), "stitching_incomplete": s.get("stitching_incomplete"),
            "skipped": len(s.get("skipped_samples") or []), "errors": s.get("errors")}


def sub(step: str, *args: str, max_gb: float = 6.0) -> dict:
    """Run one Python step in a fresh process (its own peak RSS), stopped once it is over `max_gb` (memory_watch)."""
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, "-m", "repairos.recon.bench", "--step", step, "--max-memory", str(max_gb),
                        *args], capture_output=True, text=True)
    if p.returncode != 0:
        return {"path": step, "ok": False, "error": p.stderr.strip().splitlines()[-1:] or ["?"]}
    d = json.loads(p.stdout.strip().splitlines()[-1])
    d["process_wall_s"] = round(time.monotonic() - t0, 1)
    return d


def _peak_mb() -> float:
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2 ** 20


def _apple_mesh_in_arkit(apple_dir: Path, samples_json: Path):
    import open3d as o3d

    from repairos.recon.merge import segment_sim3

    sm = {s.file: s.arkit_pose.t for s in load_samples(samples_json) if s.arkit_pose is not None}
    s, info = segment_sim3(load_poses(apple_dir / "poses.json"), sm)
    m = o3d.io.read_triangle_mesh(str(apple_dir / "mesh.ply"))
    m.vertices = o3d.utility.Vector3dVector(s.apply(np.asarray(m.vertices)))
    return m, info


def mesh_distance_mm(verts: np.ndarray, ref_mesh) -> dict:
    import open3d as o3d

    sc = o3d.t.geometry.RaycastingScene()
    sc.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(ref_mesh))
    d = sc.compute_distance(o3d.core.Tensor(np.asarray(verts, np.float32))).numpy() * 1000
    return {"median_mm": round(float(np.median(d)), 2), "p90_mm": round(float(np.percentile(d, 90)), 2)}


def step_tsdf(prefer: str, samples_json: str, apple_dir: str) -> dict:
    from repairos.recon.tsdf import ObjectBox, frames_from_samples, fuse_frames

    t0 = time.monotonic()
    sm = load_samples(samples_json)
    frames = frames_from_samples(sm)
    g = fuse_frames(frames, box=ObjectBox(sm[0].bounding_box), prefer=prefer)
    wall = time.monotonic() - t0
    out = {"path": f"tsdf_{prefer}", "wall_s": round(wall, 2), "peak_mb": round(_peak_mb()), "ok": True,
           "poses": len(frames), "images": len(sm), "points": int(len(g.points)), "triangles": g.triangles,
           "method": g.method, "spacing_mm": g.params["spacing"]["spacing_mm"]}
    if Path(apple_dir, "mesh.ply").exists():
        m, _ = _apple_mesh_in_arkit(Path(apple_dir), Path(samples_json))
        out["points_to_apple_mesh"] = mesh_distance_mm(g.points, m)
        if g.mesh_vertices is not None:
            out["mesh_to_apple_mesh"] = mesh_distance_mm(g.mesh_vertices, m)
    return out


def step_merge(ref_json: str, ref_imgs: str, mov_json: str, mov_imgs: str, joint_dir: str, cache: str) -> dict:
    from repairos.recon import merge as M
    from repairos.recon.tsdf import ObjectBox, Segment, frames_from_samples, fuse_frames

    t0 = time.monotonic()

    def seg(js, imgs, name):
        sm = load_samples(js)
        return Segment(frames_from_samples(sm, imgs), ObjectBox(sm[0].bounding_box), name=name)

    a, b = seg(ref_json, ref_imgs, "ref"), seg(mov_json, mov_imgs, "mov")
    ga, gb = fuse_frames(a.frames, box=a.box), fuse_frames(b.frames, box=b.box)
    t1 = time.monotonic()
    apple = None
    if Path(joint_dir, "poses.json").exists():
        poses = load_poses(Path(joint_dir) / "poses.json")
        apple = M.split_joint_poses(poses, {p.file: p.file.split("__")[0] for p in poses if p.file})
        apple = {"reference": apple.get("ref", []), "moving": apple.get("mov", [])}
    res = M.merge_segments(a, b, ref_points=ga.points, mov_points=gb.points, apple_poses=apple,
                           cache_dir=Path(cache))
    t2 = time.monotonic()
    d = res.to_dict()
    out = {"path": "merge_features_icp", "wall_s": round(t2 - t0, 2), "merge_s": round(t2 - t1, 2),
           "peak_mb": round(_peak_mb()), "ok": res.merged, "source": res.source, "agreement": res.agreement,
           "reasons": res.reasons, "features": d["features"], "icp": d["icp"],
           "apple_fit": (d["apple_joint"] or {}).get("per_segment")}
    # End to end: locate the moving segment's object (box centre) from its stills on the merged geometry.
    if res.merged:
        out["locate_check"] = locate_check(a, b, res.transform)
    return out


def locate_check(a, b, t) -> dict:
    """Each moving-segment still: a 200 px box around the projection of its own object-box centre, located by
    ray casting on the reference segment's TSDF cloud after the merge (the still's pose moved by T). A hit inside
    the reference object box (grown 25%) means the merged frame is consistent."""
    from repairos.locate import ScanCloud, try_locate
    from repairos.locate.camera import project
    from repairos.recon.tsdf import fuse_segments, moved_pose, Segment as Seg

    merged = fuse_segments([Seg(a.frames, a.box, name="ref"), Seg(b.frames, b.box, t.matrix, "mov")], prefer="numpy")
    cloud = ScanCloud(merged.points, "merged", "lidar", cell_m=0.004)
    hits, inside, n = 0, 0, 0
    for f in b.frames:
        uv, dep = project(b.box.centre[None], f.pose, f.image_cam)
        if dep[0] <= 0:
            continue
        n += 1
        p = moved_pose(f.pose, t.matrix)
        still = {"still_id": f.name, "scan_id": "merged", "camera": p.to_contract(), "width_px": f.image_cam.width,
                 "height_px": f.image_cam.height, "tracking_state": "normal",
                 "intrinsics": {"fx": f.image_cam.fx, "fy": f.image_cam.fy, "cx": f.image_cam.cx,
                                "cy": f.image_cam.cy}}
        u, v = uv[0]
        r = try_locate(cloud, still, (u - 100, v - 100, u + 100, v + 100), radius_m=0.004)
        if hasattr(r, "point_m"):
            hits += 1
            inside += bool(a.box.contains(np.array([r.point_m]), 0.25)[0])
    return {"stills": n, "located": hits, "inside_reference_object": inside}


def table(rows: list[dict]) -> str:
    head = "| path | wall s | peak MB | poses / images | points | triangles | vs Apple mesh (median / p90 mm) |"
    lines = [head, "|---|---|---|---|---|---|---|"]
    for r in rows:
        q = r.get("mesh_to_apple_mesh") or r.get("points_to_apple_mesh") or {}
        lines.append(f"| {r['path']} | {r.get('wall_s', '-')} | {r.get('peak_mb', '-')} | "
                     f"{r.get('poses', '-')} / {r.get('images', '-')} | {r.get('points', '-')} | "
                     f"{r.get('triangles', '-')} | {q.get('median_mm', '-')} / {q.get('p90_mm', '-')} |")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step")
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--ref", type=Path)
    ap.add_argument("--other", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--paths", default="apple_reduced,apple_poses_only,apple_no_depth,tsdf_open3d,tsdf_numpy,merge")
    ap.add_argument("--max-memory", type=float, default=6.0)
    ap.add_argument("--min-free", type=float, default=25.0)
    a = ap.parse_args(argv)
    if a.step in ("tsdf", "merge"):
        from repairos.recon.build import memory_watch

        with memory_watch(a.max_memory, f"the {a.step} step"):
            row = step_tsdf(*a.rest) if a.step == "tsdf" else step_merge(*a.rest)
        print(json.dumps(row, default=str))
        return 0
    out = a.out
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    run_cli(["samples", "--input", a.ref, "--out", out / "samples_ref", "--depth"], min_free_pct=a.min_free)
    for name in a.paths.split(","):
        check_free_memory(a.min_free)        # stop rather than squeeze the other jobs on the Mac
        if name in APPLE:
            rows.append(apple_path(name, a.ref, out, a.max_memory))
        elif name.startswith("tsdf_"):
            rows.append(sub("tsdf", name.split("_", 1)[1], str(out / "samples_ref" / "samples.json"),
                            str(out / "apple_reduced"), max_gb=a.max_memory))
        elif name == "merge" and a.other:
            from repairos.recon.merge import link_images

            run_cli(["samples", "--input", a.other, "--out", out / "samples_other", "--depth"],
                    min_free_pct=a.min_free)
            link_images({"ref": sorted(a.ref.glob("*.HEIC")), "mov": sorted(a.other.glob("*.HEIC"))},
                        out / "joint" / "images")
            j = run_cli(["reconstruct", "--input", out / "joint" / "images", "--out", out / "joint", "--detail",
                         "reduced"], max_memory_gb=a.max_memory, min_free_pct=a.min_free, check=False)
            rows.append({"path": "apple_joint_both_segments", "wall_s": round(j.wall_s, 1),
                         "peak_mb": round(j.peak_rss_mb), "ok": j.ok, "poses": j.summary.get("poses"),
                         "images": j.summary.get("images"), "points": j.summary.get("points"),
                         "triangles": j.summary.get("mesh_triangles")})
            check_free_memory(a.min_free)
            rows.append(sub("merge", str(out / "samples_ref" / "samples.json"), str(a.ref),
                            str(out / "samples_other" / "samples.json"), str(a.other), str(out / "joint"),
                            str(out / "cache"), max_gb=a.max_memory))
        (out / "bench.json").write_text(json.dumps(rows, indent=1, default=str) + "\n")
    print(table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
