"""How sure the adjusted scene's size is, and whether camera motion agrees with LiDAR about it.

The adjustment already holds LiDAR inside the fit (repairos.measure.adjust) with its offset `b`; this checks the
result: one scale `s` with lidar_depth - b = s * depth, fitted robustly (Huber, IRLS) over every inlier observation
with a confident LiDAR depth away from a depth edge, and `s` should come out near 1. (An adjustment run without LiDAR
gets `s` and `b` fitted here, the offset only when the depths span enough range to separate it.) Its sigma is a jackknife over stills (fit
again without each still): one still's LiDAR errors are correlated (the phone fuses depth over time), so treating
thousands of depths as independent would claim a size known far better than it is.

Camera motion's own size is ARKit's: `motion_scale` is ARKit's camera centres over the LiDAR-scaled adjusted ones (a
similarity fit), 1.01 when ARKit's odometry made the scan 1% too big. Beyond three sigmas from 1 the two disagree about
size on this scan; LiDAR's is kept and the record says so.
"""
from __future__ import annotations

import numpy as np

from repairos.measure.adjust import Adjusted
from repairos.measure.tracks import Tracks
from repairos.measure.views import View
from repairos.recon.align import umeyama

HUBER_M = 0.015
MAX_SIGMA_S_WITH_OFFSET = 0.01   # above this the offset is not separable from the scale on this scan
MIN_SAMPLES = 50


def adjusted_depths(adj: Adjusted, tracks: Tracks) -> np.ndarray:
    """Depth along the view axis of each observation's track point in its adjusted still."""
    out = np.empty(len(tracks.track))
    for i, pose in enumerate(adj.poses):
        sel = tracks.view == i
        out[sel] = -pose.to_camera(adj.points[tracks.track[sel]])[:, 2]
    return out


def _irls(a: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, float]:
    coef = np.linalg.lstsq(a, y, rcond=None)[0]
    for _ in range(20):
        r = y - a @ coef
        w = np.sqrt(np.where(np.abs(r) <= HUBER_M, 1.0, HUBER_M / np.maximum(np.abs(r), 1e-12)))
        coef_n = np.linalg.lstsq(a * w[:, None], y * w, rcond=None)[0]
        done = np.allclose(coef_n, coef, atol=1e-9)
        coef = coef_n
        if done:
            break
    r = y - a @ coef
    return coef, 1.4826 * float(np.median(np.abs(r - np.median(r))))


def _jackknife(a: np.ndarray, y: np.ndarray, group: np.ndarray) -> np.ndarray | None:
    """Standard errors of the IRLS coefficients, leaving out one group (still) at a time."""
    gs = np.unique(group)
    if len(gs) < 3:
        return None
    est = np.array([_irls(a[group != k], y[group != k])[0] for k in gs])
    n = len(gs)
    return np.sqrt((n - 1) / n * ((est - est.mean(axis=0)) ** 2).sum(axis=0))


def fit_scale(views: list[View], adj: Adjusted, tracks: Tracks) -> dict:
    """{'status': 'fitted', 's', 'sigma_s', 'b_m', 'sigma_b_m', 'offset': 'fitted'|'not_separable', 'n',
    'residual_m', 'depth_range_m', 'motion_scale', 'motion_agrees'} or {'status': 'no_lidar', 's': 1.0, ...}."""
    z_adj = adjusted_depths(adj, tracks)
    z_l = np.full(len(z_adj), np.nan)
    for i, v in enumerate(views):
        sel = np.flatnonzero(tracks.view == i)
        if not len(sel):
            continue
        z = v.depth_at(tracks.uv[sel], min_confidence=2 if v.confidence is not None else 1)
        z[v.depth_edge(tracks.uv[sel])] = np.nan
        z_l[sel] = z
    ok = adj.inlier & np.isfinite(z_l) & (z_adj > 0)
    n = int(ok.sum())
    motion = float(umeyama(np.stack([p.t for p in adj.poses]), np.stack([v.pose.t for v in views])).scale)
    if n < MIN_SAMPLES:
        return {"s": 1.0, "status": "no_lidar", "n": n, "motion_scale": motion,
                "reason": f"only {n} matched points have a confident LiDAR depth (needs {MIN_SAMPLES})"}
    y, x, grp = z_l[ok], z_adj[ok], tracks.view[ok]
    out = {"status": "fitted", "n": n, "stills": int(len(np.unique(grp))),
           "depth_range_m": [round(float(x.min()), 4), round(float(x.max()), 4)]}
    if adj.lidar_offset_m is not None:     # the adjustment fitted the offset jointly: check the scale alone
        a1 = x[:, None]
        c1, resid = _irls(a1, y - adj.lidar_offset_m)
        se1 = _jackknife(a1, y - adj.lidar_offset_m, grp)
        # the offset is uncertain too, and over a short depth range it trades against size: the sigma is the larger
        # of the scale-alone and the scale-with-offset jackknifes
        se2 = _jackknife(np.stack([x, np.ones(n)], 1), y, grp)
        sig = max(float(se1[0]) if se1 is not None else 0.01, float(se2[0]) if se2 is not None else 0.01)
        out.update(s=float(c1[0]), sigma_s=min(sig, 0.05), b_m=adj.lidar_offset_m,
                   sigma_b_m=float(se2[1]) if se2 is not None else None, offset="in_adjustment", residual_m=resid)
    else:
        a2 = np.stack([x, np.ones(n)], 1)
        coef, resid = _irls(a2, y)
        se = _jackknife(a2, y, grp)
        if se is not None and se[0] <= MAX_SIGMA_S_WITH_OFFSET:
            out.update(s=float(coef[0]), sigma_s=float(se[0]), b_m=float(coef[1]), sigma_b_m=float(se[1]),
                       offset="fitted", residual_m=resid)
        else:
            a1 = x[:, None]
            c1, resid1 = _irls(a1, y)
            se1 = _jackknife(a1, y, grp)
            out.update(s=float(c1[0]), sigma_s=float(se1[0]) if se1 is not None else 0.01, b_m=0.0, sigma_b_m=None,
                       offset="not_separable", residual_m=resid1)
    out["motion_scale"] = motion / out["s"]
    out["motion_agrees"] = bool(abs(out["motion_scale"] - 1.0) <= max(3 * out["sigma_s"], 0.002))
    return out
