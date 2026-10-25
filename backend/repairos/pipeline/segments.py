"""Object-moved capture segments (the flipped mini-scan of the bottom) on the server: register each to segment 0 by
its shape (repairos.recon.segment_align), then use the transform everywhere the pipeline places things.

    rec = align_scan_segments(pkg_local)        # the run row's `segments` record (+ the merged cloud for locate)
    T = aligned_transforms(rec)                 # {segment: 4x4}: that segment's world -> segment 0's object frame
    still = restate_still(still, T[k])          # camera moved into segment 0's frame; pose_reliable true
    box_k = segment_box(pkg, seg, T.get(k))     # the object box in the segment's own world (for masking its depth)

A moved segment is `object_moved: true`, or reason flipped / flip (older or newer apps). It is in its own ARKit
session: gravity is still -Y, its heading and origin are its own, and its support plane is found in its own world.
The phone's `transform_to_segment0` (with `transform_quality`) is a start and, for a symmetric object, a tie-breaker.
The server always verifies it against the shapes. A segment the server cannot register stays unplaced: its stills
are inspected and listed, but their findings get no 3D point.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

MOVED_REASONS = ("flip", "flipped")
TIME_BUDGET_S = 10.0          # all segments of one scan; a segment not started within it is left unaligned
GROWN_BOX = (2.5, 1.5, 2.5)   # no box for a moved segment and no phone estimate: segment 0's box grown this much


def is_moved(seg: Mapping[str, Any]) -> bool:
    return seg.get("object_moved") is True or seg.get("reason") in MOVED_REASONS


def moved_segments(pkg: Mapping[str, Any]) -> list[dict]:
    return [dict(s) for s in pkg.get("segments") or [] if is_moved(s)]


def _matrix(rows: Any) -> np.ndarray | None:
    if not rows or len(rows) != 16:
        return None
    return np.asarray(rows, float).reshape(4, 4)


def aligned_transforms(rec: Mapping[str, Any] | None) -> dict[int, np.ndarray]:
    """{segment: 4x4} for every segment the server registered (the run row's `segments` record)."""
    out = {}
    for s in (rec or {}).get("segments") or []:
        m = _matrix(s.get("transform")) if s.get("aligned") else None
        if m is not None:
            out[int(s["segment"])] = m
    return out


def unplaced_segments(pkg: Mapping[str, Any], rec: Mapping[str, Any] | None) -> set[int]:
    """Segments whose stills are not in segment 0's frame after the server's alignment: marked `aligned: false` by
    the phone (and not moved, so nothing re-registers them), or moved and not registered by the server."""
    done = set(aligned_transforms(rec))
    out = set()
    for s in pkg.get("segments") or []:
        k = int(s["segment"])
        if is_moved(s):
            if k not in done:
                out.add(k)
        elif s.get("aligned") is False:
            out.add(k)
    return out


def segment_box(pkg: Mapping[str, Any], seg: Mapping[str, Any], to0: np.ndarray | None):
    """The object box in the segment's own world: its own crop_box; else segment 0's box carried by the inverse of
    a transform (the server's or the phone's); else segment 0's box grown (GROWN_BOX), which the object-piece filter
    then narrows. None when segment 0 has no box."""
    from repairos.recon.tsdf import ObjectBox

    if seg.get("crop_box"):
        return ObjectBox.from_contract(seg["crop_box"]), "segment's own box"
    if not pkg.get("crop_box"):
        return None, "no box"
    box0 = ObjectBox.from_contract(pkg["crop_box"])
    if to0 is not None:
        return box0.transformed(np.linalg.inv(to0)), "segment 0's box carried by the transform"
    m = box0.matrix.copy()
    m[:3, :3] = m[:3, :3] @ np.diag(GROWN_BOX)
    return ObjectBox(m), "segment 0's box, grown"


def own_plane_height(frames: list[Any], box: Any) -> float | None:
    """The support plane's height in the segment's own world, from its depth around the box (pipeline.mesh)."""
    from repairos.pipeline.mesh import measured_plane_height

    return measured_plane_height(frames, box)


def _object_only(cloud: Any, box: Any, voxel: float) -> Any:
    """Keep the object's pieces of a cloud (pipeline.mesh.object_pieces on DBSCAN clusters at two voxels)."""
    import open3d as o3d

    from repairos.pipeline.mesh import object_pieces
    from repairos.recon.segment_align import ObjectCloud

    if len(cloud) < 50:
        return cloud
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(cloud.points))
    labels = np.asarray(pc.cluster_dbscan(eps=2.0 * voxel, min_points=4))
    keep = object_pieces(labels, cloud.points, box, max(20, int(0.02 * len(cloud))))
    return ObjectCloud(cloud.points[keep], None if cloud.colours is None else cloud.colours[keep])


def cloud_from_points(points_file: str | None, box: Any, plane_y: float | None, voxel: float) -> Any:
    """An in-box phone cloud (points_file) cut to the object (pipeline.mesh.object_cloud), as an ObjectCloud."""
    from repairos.pipeline.mesh import object_cloud
    from repairos.recon.segment_align import ObjectCloud

    if not points_file:
        return ObjectCloud(np.zeros((0, 3)), None)
    p, c = object_cloud(points_file, box, plane_y, voxel_m=voxel / 2)
    return ObjectCloud(p, c)


def cloud_plane_height(points_file: str, box: Any) -> float | None:
    from repairos.locate.cloud import read_ply
    from repairos.recon.tsdf import find_support_plane

    v = read_ply(points_file)
    p = np.stack([v["x"], v["y"], v["z"]], axis=1).astype(float)
    p = p[box.contains(p, 0.3)]
    plane = find_support_plane(p) if len(p) >= 200 else None
    return plane.height_m if plane else None


def frames_of(pkg_local: Mapping[str, Any], segments: set[int]) -> list[Any]:
    from repairos.recon.tsdf import frames_from_scan_package

    kfs = [kf for kf in pkg_local.get("keyframes") or [] if int(kf.get("segment") or 0) in segments]
    return frames_from_scan_package({**pkg_local, "keyframes": kfs}, Path("/"))


def align_scan_segments(pkg_local: Mapping[str, Any], *, time_budget_s: float = TIME_BUDGET_S
                        ) -> tuple[dict, dict[int, Any]]:
    """Register every moved segment of a scan package (file fields are absolute local paths) to segment 0.
    Returns (the run row record, {segment: its object cloud in segment 0's frame} for the aligned ones)."""
    from repairos.pipeline.mesh import keyframes_in_frame, object_depths, plane_height, voxel_for
    from repairos.recon.segment_align import ObjectCloud, align_clouds, cloud_from_frames
    from repairos.recon.tsdf import ObjectBox

    t0 = time.perf_counter()
    moved = moved_segments(pkg_local)
    rec: dict[str, Any] = {"status": "none", "segments": []}
    clouds: dict[int, Any] = {}
    if not moved:
        return rec, clouds
    rec["status"] = "done"

    def fail(seg: Mapping[str, Any], reason: str) -> None:
        rec["segments"].append({"segment": int(seg["segment"]), "reason": seg.get("reason"), "object_moved": True,
                                "aligned": False, "fitness": None, "rmse_mm": None, "why_not": reason})

    if not pkg_local.get("crop_box"):
        for s in moved:
            fail(s, "segment 0 has no object box, so there is no object shape to register to")
        return rec, clouds
    box0 = ObjectBox.from_contract(pkg_local["crop_box"])
    same = {0} | {int(k.get("segment") or 0) for k in keyframes_in_frame(pkg_local)}
    frames0 = frames_of(pkg_local, same)
    voxel = max(voxel_for(box0), 0.003)
    plane0 = plane_height(pkg_local, frames0, box0)
    if frames0:
        depths0, _ = object_depths(frames0, box0, plane0)
        ref = _object_only(cloud_from_frames(frames0, depths0, voxel_m=voxel / 2), box0, voxel / 2)
        rec["segment0_source"] = "keyframe depth"
    else:   # no depth keyframes: the phone's in-box cloud
        ref = cloud_from_points(pkg_local.get("points_file"), box0, plane0, voxel)
        rec["segment0_source"] = "points_file"
    rec["segment0_points"] = len(ref)
    rec["segment0_plane_m"] = None if plane0 is None else round(plane0, 4)
    rec["voxel_mm"] = round(voxel * 1000, 2)
    for seg in moved:
        k = int(seg["segment"])
        if time.perf_counter() - t0 > time_budget_s:
            fail(seg, f"the {time_budget_s:.0f} s alignment budget was used up by earlier segments")
            continue
        est = _matrix(seg.get("transform_to_segment0"))
        frames_k = frames_of(pkg_local, {k})
        box_k, box_how = segment_box(pkg_local, seg, est)
        tracked = (seg.get("pose_source") or pkg_local.get("pose_source")) == "object_tracked"
        if frames_k:
            plane_k = None if tracked else own_plane_height(frames_k, box_k)
            depths_k, _ = object_depths(frames_k, box_k, plane_k)
            mov = _object_only(cloud_from_frames(frames_k, depths_k, voxel_m=voxel / 2), box_k, voxel / 2)
        elif seg.get("points_file"):   # no depth keyframes: the segment's own in-box cloud
            plane_k = None if tracked else cloud_plane_height(seg["points_file"], box_k)
            mov = cloud_from_points(seg["points_file"], box_k, plane_k, voxel)
        else:
            fail(seg, "the segment has neither depth keyframes nor its own cloud")
            continue
        res = align_clouds(ref, mov, voxel_m=voxel, init=est, init_quality=seg.get("transform_quality"),
                           segment=k, ref_frames=frames0 or None, mov_frames=frames_k or None, ref_plane_y=plane0,
                           mov_plane_y=plane_k, expect_flip=seg.get("reason") in MOVED_REASONS)
        d = res.to_dict()
        d.update(reason=seg.get("reason"), object_moved=True, box=box_how,
                 plane_height_m=None if plane_k is None else round(plane_k, 4), why_not=d.pop("reason"))
        rec["segments"].append(d)
        if res.aligned:
            t = res.matrix
            clouds[k] = ObjectCloud(mov.points @ t[:3, :3].T + t[:3, 3], mov.colours)
    rec["seconds"] = round(time.perf_counter() - t0, 3)
    return rec, clouds


def restate_still(still: Mapping[str, Any], to0: np.ndarray) -> dict:
    """The still with its camera pose in segment 0's object frame (the server's registration)."""
    from repairos.locate.camera import Pose
    from repairos.recon.tsdf import moved_pose

    out = dict(still)
    if "camera" in still:
        out["camera"] = moved_pose(Pose.from_contract(still["camera"]), to0).to_contract()
        out["pose_reliable"] = True
    return out


def merged_cloud_ply(points_file: str | None, clouds: Mapping[int, Any]) -> bytes | None:
    """Segment 0's in-box cloud (points_file) plus every aligned segment's object cloud, in segment 0's frame: the
    cloud `locate` ray-casts, so a finding on the underside lands on the bottom's points."""
    from repairos.importers.ply import ply_bytes
    from repairos.locate.cloud import read_ply

    if not clouds:
        return None
    pts, cols = [], []
    if points_file:
        v = read_ply(points_file)
        p = np.stack([v["x"], v["y"], v["z"]], axis=1).astype(float)
        c = np.stack([v[k] for k in ("red", "green", "blue")], axis=1) if "red" in v else np.full((len(p), 3), 128)
        pts.append(p)
        cols.append(np.asarray(c, np.uint8))
    for cl in clouds.values():
        pts.append(cl.points)
        cols.append(cl.colours if cl.colours is not None else np.full((len(cl.points), 3), 128, np.uint8))
    return ply_bytes(np.concatenate(pts), np.concatenate(cols))


def report_segments(pkg: Mapping[str, Any], rec: Mapping[str, Any] | None, unplaced_findings: Mapping[int, list[str]]
                    ) -> list[dict]:
    """The report's `capture.segments`: one entry per moved segment (aligned or not, fitness, RMSE, findings placed
    on no model)."""
    by = {int(s["segment"]): s for s in (rec or {}).get("segments") or []}
    out = []
    for seg in moved_segments(pkg):
        k = int(seg["segment"])
        r = by.get(k, {})
        e = {"segment": k, "reason": str(seg.get("reason") or "flipped"), "object_moved": True,
             "aligned": bool(r.get("aligned"))}
        if r.get("fitness") is not None:
            e["fitness"] = float(r["fitness"])
        if r.get("rmse_mm") is not None:
            e["rmse_mm"] = float(r["rmse_mm"])
        if r.get("method"):
            e["method"] = str(r["method"])
        if not e["aligned"]:
            e["why_not"] = str(r.get("why_not") or "the segment was not registered to the main scan")
        e["findings_not_placed"] = sorted(unplaced_findings.get(k, []))
        out.append(e)
    return out
