"""measure(): a scan's stills and a measuring request -> the measurement record (schema `lea-measurement/1`).

Request (marks in full-resolution still pixels):
    {"points": {"<name>": [{"still_id": "...", "uv": [u, v]}, ...], ...},
     "dimensions": [{"name": "...", "kind": "distance", "between": ["<point>", "<point>"]},
                    {"name": "...", "kind": "diameter", "rim": ["<point>", "<point>", "<point>", ...]}],
     "reference": {"dimension": "...", "true_mm": 68.6, "uncertainty_mm": 0.05},     optional
     "holdout":   {"dimension": "...", "true_mm": 53.3, "uncertainty_mm": 0.05},     optional
     "tolerance_mm": 0.5}
Extra keys on a dimension (e.g. `fit`) are passed through untouched.

Steps: measuring views (one capture segment: poses from different segments are different frames) -> tracks ->
adjustment -> LiDAR scale -> points -> dimensions -> reference and holdout. A step that cannot run is recorded with
its reason and the next falls back (no tracks: ARKit poses and LiDAR planes), never silently.
"""
from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np

from repairos.measure import dimension, reference, size
from repairos.measure.adjust import PRIOR_R_RAD, PRIOR_T_M, Adjusted, Intrinsics, adjust
from repairos.measure.tracks import Tracks, find_tracks, neighbours, surface_depth
from repairos.measure.views import View, make_view, usable

log = logging.getLogger(__name__)

SCHEMA = "lea-measurement/1"
MAX_STILLS = 40
MIN_TRACKS = 30
DEFAULT_TOLERANCE_MM = 0.5
LIDAR_ONLY_SIGMA_S = 0.02    # size from LiDAR planes alone: a couple of percent (no adjustment to check it)


def _segment(s: Mapping) -> tuple[int, str]:
    return int(s.get("segment") or 0), str(s.get("pose_source") or "arkit")


def choose_stills(stills: Sequence[Mapping], marked: set[str], max_stills: int = MAX_STILLS) -> tuple[list, list]:
    """(stills to measure with, [(still_id, reason)] left out). The marked stills' segment only; marked first."""
    ok, out = [], []
    for s in stills:
        why = usable(s)
        (out.append((s.get("still_id"), why)) if why else ok.append(s))
    seg = {_segment(s) for s in ok if s["still_id"] in marked}
    if len(seg) > 1:
        raise ValueError("the marks are on stills from different capture segments (the object moved between them)")
    if seg:
        keep = [s for s in ok if _segment(s) in seg]
        out += [(s["still_id"], "another capture segment") for s in ok if _segment(s) not in seg]
        ok = keep
    ok.sort(key=lambda s: (s["still_id"] not in marked, str(s.get("captured_at") or "")))
    out += [(s["still_id"], "over the still cap") for s in ok[max_stills:]]
    return ok[:max_stills], out


def track_and_adjust(views: list[View]) -> tuple[Tracks, Adjusted | None, Tracks]:
    """Two passes. ARKit pose error and LiDAR noise put a prediction 10-20 px off in a still far from the corner's
    own, past what tracking can close; between the nearest stills it is a few pixels. So: track between nearest
    stills and adjust; then predict from the adjusted poses and surface into every still that looks the same way,
    track again and adjust again (the ARKit poses stay the prior). Returns (final tracks, adjustment, first tracks);
    no adjustment when either pass has too few tracks."""
    first = find_tracks(views, neighbours(views, k=2))
    if first.n_points < MIN_TRACKS:
        return first, None, first
    a1 = adjust(views, first)
    refined = [dataclasses.replace(v, pose=a1.poses[i]) for i, v in enumerate(views)]
    depth_fns = [surface_depth(v, a1.points) for v in refined]
    second = find_tracks(refined, neighbours(refined), depth_fns)
    if second.n_points < MIN_TRACKS:
        return first, a1, first
    return second, adjust(views, second, start=a1.poses), first


def measure(stills: Sequence[Mapping[str, Any]], image_of: Callable[[Mapping], np.ndarray],
            depth_of: Callable[[Mapping], np.ndarray | None], request: Mapping[str, Any],
            confidence_of: Callable[[Mapping], np.ndarray | None] | None = None) -> dict:
    points_req: Mapping[str, list] = request.get("points") or {}
    marked = {str(m["still_id"]) for marks in points_req.values() for m in marks}
    chosen, left_out = choose_stills(stills, marked)
    rec: dict[str, Any] = {"schema": SCHEMA, "left_out": [{"still_id": i, "reason": r} for i, r in left_out],
                           "notes": []}
    views: list[View] = []
    for s in chosen:
        try:
            views.append(make_view(s, image_of(s), depth_of(s), confidence_of(s) if confidence_of else None))
        except Exception as e:  # noqa: BLE001 - one unreadable still is left out and named
            rec["left_out"].append({"still_id": s.get("still_id"), "reason": f"unreadable: {e}"})
    rec["stills_used"] = [v.still_id for v in views]
    if not views:
        raise ValueError("no still can measure (each needs a reliable ARKit pose and intrinsics)")

    tracks, adj, first = track_and_adjust(views) if len(views) >= 2 else (None, None, None)
    rec["tracks"] = {"points": tracks.n_points if tracks else 0, "observations": len(tracks.track) if tracks else 0,
                     "first_pass_points": first.n_points if first else 0}
    # without an adjustment the rays use ARKit's poses as they are: their own error (the adjustment's prior) and
    # LiDAR's size error go into every point, so such a scan never looks surer than its poses are
    scene = dimension.Scene(views, [v.pose for v in views], Intrinsics(), None, None, sigma_s=LIDAR_ONLY_SIGMA_S,
                            pose_sigma_m=PRIOR_T_M, pose_sigma_rad=PRIOR_R_RAD)
    if adj is not None:
        sc = size.fit_scale(views, adj, tracks)
        rec["adjustment"] = {"rms_px_before": round(adj.rms_px_before, 3), "rms_px_after": round(adj.rms_px_after, 3),
                             "iterations": adj.iterations, "inlier_share": round(float(adj.inlier.mean()), 3),
                             "focal_scale": round(adj.intrinsics.s, 5), "k1": round(adj.intrinsics.k1, 5),
                             "pose_shift": {k: round(v, 3) for k, v in adj.shift.items()},
                             "lidar_offset_mm": None if adj.lidar_offset_m is None else round(adj.lidar_offset_m * 1000, 3),
                             "point_sigma_mm_median": round(float(np.nanmedian(adj.point_sigma_m)) * 1000, 3)}
        rec["scale"] = sc
        scene = dimension.Scene(views, adj.poses, adj.intrinsics, adj.points, adj.point_sigma_m,
                                sc.get("s", 1.0), sc.get("sigma_s") or 0.0, sc.get("b_m") or 0.0)
        if sc.get("status") == "fitted" and not sc["motion_agrees"]:
            rec["notes"].append(f"ARKit's motion and LiDAR disagree on size by {100 * (sc['motion_scale'] - 1):+.2f}%;"
                                " LiDAR's is used")
    else:
        rec["adjustment"] = {"status": "skipped", "reason": f"{rec['tracks']['points']} tracks (needs {MIN_TRACKS}):"
                             " ARKit poses and LiDAR planes only"}
        rec["scale"] = {"s": 1.0, "sigma_s": LIDAR_ONLY_SIGMA_S, "status": "lidar_only"}

    pts: dict[str, dimension.Point3] = {}
    rec["points"] = {}
    for name, marks in points_req.items():
        try:
            p = dimension.locate_point(scene, name, marks)
            pts[name] = p
            rec["points"][name] = {"method": p.method, "stills": p.stills,
                                   "sigma_mm": round(float(np.sqrt(np.trace(p.cov))) * 1000, 3), **p.detail}
        except dimension.Unmeasurable as e:
            rec["points"][name] = {"error": str(e)}

    dims, failed = [], []
    for d in request.get("dimensions") or []:
        extra = {k: d[k] for k in ("between", "rim", "fit") if k in d}   # what was measured; numbers are computed
        need = d.get("between") or d.get("rim") or []
        missing = [n for n in need if n not in pts]
        if missing:
            failed.append({"name": d["name"], "kind": d.get("kind"), "error": f"points not measured: {missing}"})
            continue
        try:
            if d.get("kind") == "diameter":
                v, u_shape, u = dimension.diameter(scene, [pts[n] for n in d["rim"]])
            else:
                v, u_shape, u = dimension.distance(scene, pts[d["between"][0]], pts[d["between"][1]])
        except dimension.Unmeasurable as e:
            failed.append({"name": d["name"], "kind": d.get("kind"), "error": str(e)})
            continue
        methods = sorted({pts[n].method for n in need})
        stills_ = sorted({s for n in need for s in pts[n].stills})
        dims.append({"name": d["name"], "kind": d.get("kind") or "distance", "value_mm": v, "uncertainty_mm": u,
                     "shape_uncertainty_mm": u_shape, "method": "+".join(methods), "evidence_still_ids": stills_,
                     **extra})
    tol = float(request.get("tolerance_mm") or DEFAULT_TOLERANCE_MM)
    dims, check = reference.apply(dims, request.get("reference"), request.get("holdout"), tol)
    for d in dims:
        for k in ("value_mm", "uncertainty_mm", "measured_mm", "shape_uncertainty_mm"):
            if d.get(k) is not None:
                d[k] = round(float(d[k]), 3)
    rec.update(dimensions=dims, failed=failed, check=check)
    return rec
