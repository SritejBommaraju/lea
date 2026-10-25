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
# The merge gate (merge_gate): a registered bottom joins the mesh and recreation only when the merged object still
# makes sense; else the top scan alone is built and the bottom stays photos.
MERGE_FLOAT_M = 0.015         # the merged bottom's lowest points (2nd percentile) within this of segment 0's table
MERGE_MIN_IOU = 0.5           # its outline from above overlaps segment 0's this much (5 mm cells, one cell of slack)
MERGE_MAX_EMPTY = 0.10        # at most this much more of either cloud in the other segment's empty space than its own
MERGE_CELL_M = 0.005


def is_moved(seg: Mapping[str, Any]) -> bool:
    return seg.get("object_moved") is True or seg.get("reason") in MOVED_REASONS


def in_scan_frame(seg: Mapping[str, Any]) -> bool:
    """A later segment whose poses share segment 0's frame as recorded: more photos `continued` from the report,
    relocalised by ARKit (`aligned` not false), the object not moved. Used by every place that reads poses
    (mesh, recreation, registration, coverage)."""
    return seg.get("reason") == "continued" and seg.get("aligned") is not False and not is_moved(seg)


def same_frame_segments(pkg: Mapping[str, Any]) -> set[int]:
    """Segment 0 and every later segment in its frame (`in_scan_frame`)."""
    return {0} | {int(s["segment"]) for s in pkg.get("segments") or [] if in_scan_frame(s)}


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


def merged_transforms(rec: Mapping[str, Any] | None) -> dict[int, np.ndarray]:
    """The registered segments whose merge passed the gate (`merge_gate`): the ones the mesh and the recreation
    fuse in. A record from before the gate (no `merge`) counts as passed."""
    ok = {int(s["segment"]) for s in (rec or {}).get("segments") or [] if (s.get("merge") or {}).get("ok", True)}
    return {k: t for k, t in aligned_transforms(rec).items() if k in ok}


def merge_left_out(rec: Mapping[str, Any] | None) -> list[str]:
    """Why each registered segment was left out of the model (the gate's words), for the mesh's hero_reason."""
    return [str(s["merge"].get("why")) for s in (rec or {}).get("segments") or []
            if s.get("aligned") and s.get("merge") and not s["merge"].get("ok", True)]


def _outline(points: np.ndarray, lo: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    from repairos.recon.segment_align import _window_min

    m = np.zeros(shape, bool)
    ij = np.floor((points[:, [0, 2]] - lo) / MERGE_CELL_M).astype(int)
    ok = (ij[:, 0] >= 0) & (ij[:, 0] < shape[0]) & (ij[:, 1] >= 0) & (ij[:, 1] < shape[1])
    m[ij[ok, 0], ij[ok, 1]] = True
    return _window_min((~m).astype(np.int8), 1) == 0   # one cell of slack


def merge_gate(top: Any, bottom: Any, frames0: list[Any], frames_k: list[Any], to0: np.ndarray,
               plane0: float | None) -> dict:
    """Does the merged object make sense? `top`: segment 0's object cloud; `bottom`: the registered segment's object
    cloud already in segment 0's frame (by `to0`); the frames are each segment's own. Fails ({ok: false, why}) when
    the bottom floats over or sinks under segment 0's table, its outline from above does not match the top's, or
    either cloud lies where the other segment's cameras saw empty space (the shells clip through each other). Cheap:
    two occupancy grids and two free-space passes. Generic: measured shapes only."""
    from dataclasses import replace

    from repairos.recon.segment_align import FREE_SPACE_MARGIN_M, FreeSpace
    from repairos.recon.tsdf import moved_pose

    a, b = np.asarray(top.points), np.asarray(bottom.points)
    out: dict[str, Any] = {"ok": True}
    if len(a) < 50 or len(b) < 50:
        return {"ok": True, "why": "too few points to judge"}
    why = []
    if plane0 is not None:
        lo = float(np.percentile(b[:, 1], 2)) - plane0
        out["bottom_to_table_mm"] = round(lo * 1000, 1)
        if lo > MERGE_FLOAT_M:
            why.append(f"the bottom floats {lo * 1000:.0f} mm over the table")
        elif lo < -MERGE_FLOAT_M:
            why.append(f"the bottom sinks {-lo * 1000:.0f} mm under the table")
    both = np.vstack([a, b])
    lo2 = both[:, [0, 2]].min(0) - 2 * MERGE_CELL_M
    shape = tuple(int(x) for x in np.ceil((both[:, [0, 2]].max(0) + 2 * MERGE_CELL_M - lo2) / MERGE_CELL_M))
    oa, ob = _outline(a, lo2, shape), _outline(b, lo2, shape)
    iou = float((oa & ob).sum() / max(1, (oa | ob).sum()))
    out["outline_iou"] = round(iou, 3)
    if iou < MERGE_MIN_IOU:
        why.append(f"its outline from above matches the top scan's by only {iou:.0%}")

    def empty(frames: list[Any], pts: np.ndarray, own: np.ndarray) -> float | None:
        if not frames:
            return None
        fs = FreeSpace(frames)
        x, y = fs.violation(pts, margin_m=FREE_SPACE_MARGIN_M), fs.violation(own, margin_m=FREE_SPACE_MARGIN_M)
        return None if x is None else x - (y or 0.0)

    moved = [replace(f, pose=moved_pose(f.pose, to0)) for f in frames_k]
    e0, ek = empty(frames0, b, a), empty(moved, a, b)
    out["empty_share"] = [None if e is None else round(e, 3) for e in (e0, ek)]
    if e0 is not None and e0 > MERGE_MAX_EMPTY:
        why.append(f"{e0:.0%} of the bottom lies where the top scan saw empty space")
    if ek is not None and ek > MERGE_MAX_EMPTY:
        why.append(f"{ek:.0%} of the top lies where the bottom scan saw empty space")
    if why:
        out.update(ok=False, why="; ".join(why))
    return out


def unplaced_segments(pkg: Mapping[str, Any], rec: Mapping[str, Any] | None) -> set[int]:
    """Segments whose stills are not in segment 0's frame after the server's alignment: marked `aligned: false` by
    the phone (and not moved, so nothing re-registers them), or moved and not registered by the server."""
    done = set(merged_transforms(rec))   # registered and through the merge gate
    out = set()
    for s in pkg.get("segments") or []:
        k = int(s["segment"])
        if is_moved(s):
            if k not in done:
                out.add(k)
        elif s.get("aligned") is False:
            out.add(k)
    return out


def segment_box(pkg: Mapping[str, Any], seg: Mapping[str, Any], to0: np.ndarray | None, *, registered: bool = False):
    """The object box in the segment's own world: its own crop_box; else segment 0's box carried by the inverse of
    a transform (the server's or the phone's); else segment 0's box grown (GROWN_BOX), which the object-piece filter
    then narrows. None when segment 0 has no box. `registered` (`to0` is the server's verified registration):
    segment 0's carried box comes first: the phone's own box round a flipped object is generous (40 x 57 cm round a
    controller) and lets pieces of its table into the merged model."""
    from repairos.recon.tsdf import ObjectBox

    if registered and to0 is not None and pkg.get("crop_box"):
        return ObjectBox.from_contract(pkg["crop_box"]).transformed(np.linalg.inv(to0)), \
            "segment 0's box carried by the registration"
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
    from repairos.pipeline.mesh import keyframes_in_frame, plane_height, voxel_for
    from repairos.recon.segment_align import (ObjectCloud, align_clouds, segment_object_cloud,
                                              table_height_from_depth)
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
        cloud0, sup0 = segment_object_cloud(frames0, box0, plane0, voxel_m=voxel / 2)
        ref = _object_only(cloud0, box0, voxel / 2)
        plane0 = sup0.height_m          # the table as the frames measured it under the object
        rec["segment0_source"] = "keyframe depth"
        rec["segment0_table"] = sup0.to_dict()
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
        table_k = None
        if frames_k:
            plane_k = None
            if not tracked:   # no table seen around the box (a stool smaller than the box): the lowest layer in it
                plane_k = own_plane_height(frames_k, box_k)
                if plane_k is None:
                    plane_k = table_height_from_depth(frames_k, box_k)
            cloud_k, sup_k = segment_object_cloud(frames_k, box_k, plane_k, voxel_m=voxel / 2)
            mov = _object_only(cloud_k, box_k, voxel / 2)
            plane_k, table_k = sup_k.height_m, sup_k.to_dict()
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
                 plane_height_m=None if plane_k is None else round(plane_k, 4), table=table_k, why_not=d.pop("reason"))
        if res.aligned:
            t = res.matrix
            clouds[k] = ObjectCloud(mov.points @ t[:3, :3].T + t[:3, 3], mov.colours)
            d["merge"] = merge_gate(ref, clouds[k], frames0, frames_k, t, plane0)
            if not d["merge"]["ok"]:   # a merge that makes no sense: not in the model, its photos stay photos
                del clouds[k]
        rec["segments"].append(d)
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
        gate = r.get("merge") or {}
        e = {"segment": k, "reason": str(seg.get("reason") or "flipped"), "object_moved": True,
             "aligned": bool(r.get("aligned")) and gate.get("ok", True)}
        if r.get("fitness") is not None:
            e["fitness"] = float(r["fitness"])
        if r.get("rmse_mm") is not None:
            e["rmse_mm"] = float(r["rmse_mm"])
        if r.get("method"):
            e["method"] = str(r["method"])
        if not e["aligned"]:
            e["why_not"] = (f"its best fit does not make sense ({gate.get('why')}), so the model is the top scan alone"
                            if r.get("aligned") else str(r.get("why_not") or "the segment was not registered to the "
                                                                              "main scan"))
        e["findings_not_placed"] = sorted(unplaced_findings.get(k, []))
        out.append(e)
    return out
