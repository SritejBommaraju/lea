"""Bundle adjustment of a scan's stills (B13's core): every still's pose and the track points, by Levenberg-Marquardt
on the reprojection error with a Huber loss, with LiDAR as a loose baseline inside the same fit: each observation's
depth along the view axis is pulled toward the still's LiDAR depth (sigma `lidar_sigma_m`, about a centimetre), with
one offset `b` for the whole scan (lidar = depth + b), so a constant LiDAR bias is not taken for size. Pixels fix the
shape; thousands of loose LiDAR depths fix the size; ARKit's poses are a weak prior that also fixes the gauge.
Without LiDAR (`lidar_sigma_m=None`) the size is ARKit's motion only.

Optionally (`free_intrinsics`) one focal scale and one radial distortion term shared by the stills. Off by default:
ARKit reports factory-calibrated intrinsics per frame, and on a scan whose points sit on one surface in the middle of
the frame the two terms trade against depth and pull the scene out of shape (tests/unit/test_measure.py). Turn them on
to compare on a real capture, never blind.

Camera model (repairos.locate.camera conventions, ARKit camera axes): a camera point P = R^T (X - t) projects to
x = P.x / -P.z, y = P.y / -P.z, d = 1 + k1 (x^2 + y^2), u = cx + s fx x d, v = cy - s fy y d.
Updates: R <- Exp(w) R (world-frame rotation), t <- t + dt. Points are eliminated with the Schur complement; the
reduced system is 6 per still + 3 (s, k1, b).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from repairos.locate.camera import Pinhole, Pose
from repairos.measure.tracks import Tracks
from repairos.measure.views import View

SIGMA_PX = 0.5            # matched-pixel noise (working pixels)
HUBER = 2.0               # in units of SIGMA_PX
PRIOR_T_M = 0.005         # ARKit drift over a scan: a few millimetres
PRIOR_R_RAD = np.radians(0.5)
PRIOR_SCALE = 0.01        # focal scale prior around 1
PRIOR_K1 = 0.05
LIDAR_SIGMA_M = 0.01      # one LiDAR depth: about a centimetre
LIDAR_HUBER = 3.0         # in units of LIDAR_SIGMA_M (edges and dropouts are outliers)
PRIOR_OFFSET_M = 0.02
MAX_ITERS = 40


def skew(v: np.ndarray) -> np.ndarray:
    """Batch of skew matrices [v]x (N x 3 x 3) with [v]x w = v x w."""
    z = np.zeros(len(v))
    return np.stack([np.stack([z, -v[:, 2], v[:, 1]], -1),
                     np.stack([v[:, 2], z, -v[:, 0]], -1),
                     np.stack([-v[:, 1], v[:, 0], z], -1)], 1)


def expm(w: np.ndarray) -> np.ndarray:
    """Rotation matrix of an axis-angle vector (Rodrigues)."""
    th = float(np.linalg.norm(w))
    if th < 1e-12:
        return np.eye(3)
    k = skew((w / th)[None])[0]
    return np.eye(3) + np.sin(th) * k + (1 - np.cos(th)) * k @ k


def logm(r: np.ndarray) -> np.ndarray:
    """Axis-angle vector of a rotation matrix."""
    c = np.clip((np.trace(r) - 1) / 2, -1.0, 1.0)
    th = np.arccos(c)
    if th < 1e-9:
        return np.zeros(3)
    v = np.array([r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]])
    return v * th / (2 * np.sin(th))


@dataclass(frozen=True)
class Intrinsics:
    """The adjusted camera: focal scale `s` on every still's fx, fy and radial `k1` on normalised coordinates."""
    s: float = 1.0
    k1: float = 0.0

    def project(self, p_cam: np.ndarray, cam: Pinhole) -> np.ndarray:
        x, y = p_cam[:, 0] / -p_cam[:, 2], p_cam[:, 1] / -p_cam[:, 2]
        d = 1 + self.k1 * (x * x + y * y)
        return np.stack([cam.cx + self.s * cam.fx * x * d, cam.cy - self.s * cam.fy * y * d], axis=1)

    def ray(self, uv: np.ndarray, cam: Pinhole) -> np.ndarray:
        """Camera-frame unit directions through pixels uv (distortion inverted by fixed-point iteration)."""
        uv = np.atleast_2d(uv)
        xd = (uv[:, 0] - cam.cx) / (self.s * cam.fx)
        yd = -(uv[:, 1] - cam.cy) / (self.s * cam.fy)
        x, y = xd.copy(), yd.copy()
        for _ in range(20):
            d = 1 + self.k1 * (x * x + y * y)
            x, y = xd / d, yd / d
        v = np.stack([x, y, -np.ones_like(x)], axis=1)
        return v / np.linalg.norm(v, axis=1, keepdims=True)


@dataclass
class Adjusted:
    poses: list[Pose]
    intrinsics: Intrinsics
    points: np.ndarray            # P x 3 arkit_world metres
    point_sigma_m: np.ndarray     # P, sqrt of the covariance trace, scaled by the a-posteriori variance factor
    inlier: np.ndarray            # M bool (observation's reprojection under the Huber threshold)
    rms_px_before: float
    rms_px_after: float
    iterations: int
    variance_factor: float
    shift: dict = field(default_factory=dict)   # how far the adjustment moved the ARKit poses
    lidar_offset_m: float | None = None         # b in lidar = depth + b; None when LiDAR was not in the fit
    lidar_observations: int = 0


def _project_all(poses_r, poses_t, cams, intr, x, tracks):
    """Pixels of every observation; `intr` is (s, k1)."""
    v, p = tracks.view, tracks.track
    q = x[p] - poses_t[v]
    pc = np.einsum("mji,mj->mi", poses_r[v], q)   # R^T q
    fx, fy, cx, cy = cams[v, 0], cams[v, 1], cams[v, 2], cams[v, 3]
    xn, yn = pc[:, 0] / -pc[:, 2], pc[:, 1] / -pc[:, 2]
    r2 = xn * xn + yn * yn
    d = 1 + intr[1] * r2
    uv = np.stack([cx + intr[0] * fx * xn * d, cy - intr[0] * fy * yn * d], axis=1)
    return uv, pc, q, (xn, yn, r2, d, fx, fy)


def _jacobians(r, intr, pc, q, aux, view):
    """Per observation: d(u,v)/dP (2 x 3), dP/dX, dP/dw, dP/dt (3 x 3) and d(u,v)/d(s, k1) (2 x 2). `intr` is (s, k1)."""
    xn, yn, r2, d, fx, fy = aux
    fs, k1 = intr
    m = len(xn)
    dux, duy = fs * fx * (d + 2 * k1 * xn * xn), fs * fx * 2 * k1 * xn * yn
    dvx, dvy = -fs * fy * 2 * k1 * xn * yn, -fs * fy * (d + 2 * k1 * yn * yn)
    z = pc[:, 2]
    dxdp = np.stack([-1 / z, np.zeros(m), pc[:, 0] / z ** 2], 1)
    dydp = np.stack([np.zeros(m), -1 / z, pc[:, 1] / z ** 2], 1)
    duv_dp = np.stack([dux[:, None] * dxdp + duy[:, None] * dydp, dvx[:, None] * dxdp + dvy[:, None] * dydp], 1)
    rt = np.transpose(r[view], (0, 2, 1))                    # R^T
    duv_dg = np.stack([np.stack([fx * xn * d, fs * fx * xn * r2], 1), np.stack([-fy * yn * d, -fs * fy * yn * r2], 1)], 1)
    return duv_dp, rt, rt @ skew(q), -rt, duv_dg


def _huber_w(norm_sigma: np.ndarray) -> np.ndarray:
    return np.where(norm_sigma <= HUBER, 1.0, HUBER / np.maximum(norm_sigma, 1e-12))


def _cost(res_px, depth_res, priors) -> float:
    s = np.linalg.norm(res_px, axis=1) / SIGMA_PX
    rho = np.where(s <= HUBER, s * s, 2 * HUBER * s - HUBER * HUBER)
    return float(rho.sum() + (depth_res ** 2).sum() + priors)


def adjust(views: list[View], tracks: Tracks, *, lidar_sigma_m: float | None = LIDAR_SIGMA_M,
           free_intrinsics: bool = False, max_iters: int = MAX_ITERS, start: list[Pose] | None = None) -> Adjusted:
    """Adjust `views` and `tracks` (see module doc). Needs at least two views and six tracks. The views' poses are
    the ARKit prior; `start` (an earlier pass's poses) only changes where the search begins."""
    nv, npnt, m = len(views), tracks.n_points, len(tracks.track)
    if nv < 2 or npnt < 6:
        raise ValueError(f"adjustment needs two stills and six tracks, got {nv} and {npnt}")
    r0 = np.stack([v.pose.r for v in views])
    t0 = np.stack([v.pose.t for v in views])
    cams = np.array([[v.cam.fx, v.cam.fy, v.cam.cx, v.cam.cy] for v in views])
    lidar = np.full(m, np.nan)
    if lidar_sigma_m:
        for i, v in enumerate(views):
            sel = np.flatnonzero(tracks.view == i)
            z = v.depth_at(tracks.uv[sel])
            z[v.depth_edge(tracks.uv[sel])] = np.nan
            lidar[sel] = z
    has_lidar = np.isfinite(lidar)
    use_lidar = bool(lidar_sigma_m) and has_lidar.sum() >= 3 * nv
    r, t, x = r0.copy(), t0.copy(), tracks.seed.copy()
    if start is not None:
        r, t = np.stack([p.r for p in start]), np.stack([p.t for p in start])
    g = np.array([1.0, 0.0, 0.0])                       # s, k1, b
    g0 = g.copy()
    wg = np.array([1 / PRIOR_SCALE if free_intrinsics else 1e6, 1 / PRIOR_K1 if free_intrinsics else 1e6,
                   1 / PRIOR_OFFSET_M if use_lidar else 1e6])
    nc, gi = 6 * nv + 3, 6 * nv

    def priors_cost(r, t, g):
        rot = np.array([logm(r[i] @ r0[i].T) for i in range(nv)]) / PRIOR_R_RAD
        tr = (t - t0) / PRIOR_T_M
        gp = (g - g0) * wg
        return float((rot ** 2).sum() + (tr ** 2).sum() + (gp ** 2).sum()), rot, tr, gp

    def residuals(r, t, g, x):
        uv, pc, q, aux = _project_all(r, t, cams, g[:2], x, tracks)
        dres = np.zeros(m)
        if use_lidar:
            dres[has_lidar] = (-pc[has_lidar, 2] + g[2] - lidar[has_lidar]) / lidar_sigma_m
        return uv - tracks.uv, dres, pc, q, aux

    res, dres, pc, q, aux = residuals(r, t, g, x)
    rms_before = float(np.sqrt((res ** 2).sum(1).mean()))
    cost = _cost(res, dres, priors_cost(r, t, g)[0])
    lam, it = 1e-3, 0
    idx = np.concatenate([6 * tracks.view[:, None] + np.arange(6)[None], np.full((m, 3), gi) + np.arange(3)], axis=1)
    same = _pairs(tracks.track)
    s_mat = hinv = b_m = None
    for it in range(1, max_iters + 1):
        duv_dp, dp_dx, dp_dw, dp_dt, duv_dg = _jacobians(r, g[:2], pc, q, aux, tracks.view)
        # residual rows: u, v (pixels / SIGMA_PX) and depth (/ lidar sigma); camera-side columns: w, t, s, k1, b
        j_pt = np.zeros((m, 3, 3))
        j_ca = np.zeros((m, 3, 9))
        j_pt[:, :2] = duv_dp @ dp_dx / SIGMA_PX
        j_ca[:, :2, :3] = duv_dp @ dp_dw / SIGMA_PX
        j_ca[:, :2, 3:6] = duv_dp @ dp_dt / SIGMA_PX
        j_ca[:, :2, 6:8] = duv_dg / SIGMA_PX
        rr = np.zeros((m, 3))
        rr[:, :2] = res / SIGMA_PX
        wts = np.repeat(_huber_w(np.linalg.norm(res, axis=1) / SIGMA_PX)[:, None], 3, axis=1)
        wts[:, 2] = 0.0
        if use_lidar:
            ddp = np.array([0.0, 0.0, -1.0])
            j_pt[:, 2] = (ddp @ dp_dx) / lidar_sigma_m
            j_ca[:, 2, :3] = (ddp @ dp_dw) / lidar_sigma_m
            j_ca[:, 2, 3:6] = (ddp @ dp_dt) / lidar_sigma_m
            j_ca[:, 2, 8] = 1.0 / lidar_sigma_m
            rr[:, 2] = dres
            wts[:, 2] = np.where(has_lidar, _huber_w(np.abs(dres) / LIDAR_HUBER), 0.0)
        jw_ca, jw_pt = j_ca * wts[:, :, None], j_pt * wts[:, :, None]
        h_aa_m = np.einsum("mri,mrj->mij", jw_ca, j_ca)
        b_m = np.einsum("mri,mrj->mij", jw_ca, j_pt)              # 9 x 3 per observation
        h_pp_m = np.einsum("mri,mrj->mij", jw_pt, j_pt)
        h_aa = np.zeros((nc, nc))
        np.add.at(h_aa, (idx[:, :, None], idx[:, None, :]), h_aa_m)
        g_a = np.zeros(nc)
        np.add.at(g_a, idx, -np.einsum("mri,mr->mi", jw_ca, rr))
        h_pp = np.zeros((npnt, 3, 3))
        np.add.at(h_pp, tracks.track, h_pp_m)
        g_p = np.zeros((npnt, 3))
        np.add.at(g_p, tracks.track, -np.einsum("mri,mr->mi", jw_pt, rr))
        _, rot_r, tr_r, g_r = priors_cost(r, t, g)
        for i in range(nv):
            h_aa[6 * i:6 * i + 3, 6 * i:6 * i + 3] += np.eye(3) / PRIOR_R_RAD ** 2
            g_a[6 * i:6 * i + 3] -= rot_r[i] / PRIOR_R_RAD
            h_aa[6 * i + 3:6 * i + 6, 6 * i + 3:6 * i + 6] += np.eye(3) / PRIOR_T_M ** 2
            g_a[6 * i + 3:6 * i + 6] -= tr_r[i] / PRIOR_T_M
        h_aa[gi:, gi:] += np.diag(wg ** 2)
        g_a[gi:] -= g_r * wg
        while True:
            hinv = np.linalg.inv(h_pp + lam * (h_pp * np.eye(3)) + 1e-12 * np.eye(3))
            s_mat = h_aa + lam * np.diag(np.diag(h_aa))
            bh = np.einsum("mij,mjk->mik", b_m, hinv[tracks.track])     # B_m Hinv_p
            for a_i, b_i in same:
                np.add.at(s_mat, (idx[a_i][:, :, None], idx[b_i][:, None, :]),
                          -np.einsum("mij,mkj->mik", bh[a_i], b_m[b_i]))
            rhs = g_a.copy()
            np.add.at(rhs, idx, -np.einsum("mij,mj->mi", bh, g_p[tracks.track]))
            try:
                da = np.linalg.solve(s_mat, rhs)
            except np.linalg.LinAlgError:
                lam *= 10
                continue
            back = np.zeros((npnt, 3))
            np.add.at(back, tracks.track, np.einsum("mij,mi->mj", b_m, da[idx]))
            dx = np.einsum("pij,pj->pi", hinv, g_p - back)
            r_new = np.stack([expm(da[6 * i:6 * i + 3]) @ r[i] for i in range(nv)])
            t_new = t + da[:gi].reshape(nv, 6)[:, 3:]
            g_new, x_new = g + da[gi:], x + dx
            res_n, dres_n, pc_n, q_n, aux_n = residuals(r_new, t_new, g_new, x_new)
            cost_n = _cost(res_n, dres_n, priors_cost(r_new, t_new, g_new)[0]) if np.all(pc_n[:, 2] < 0) else np.inf
            if cost_n < cost:
                step = float(np.abs(da).max(initial=0)) + float(np.abs(dx).max(initial=0))
                r, t, g, x = r_new, t_new, g_new, x_new
                res, dres, pc, q, aux = res_n, dres_n, pc_n, q_n, aux_n
                improved, cost = (cost - cost_n) / max(cost, 1e-12), cost_n
                lam = max(lam / 3, 1e-9)
                break
            lam *= 4
            if lam > 1e8:
                improved, step = 0.0, 0.0
                break
        if improved < 1e-7 or step < 1e-9:
            break

    # a-posteriori variance factor: cost over the redundancy (the priors add as many rows as camera unknowns)
    vf = cost / max(1, int(2 * m + (has_lidar.sum() if use_lidar else 0)) - 3 * npnt)
    sigma = _point_sigma(hinv, b_m, tracks, idx, s_mat, vf)
    moved = np.linalg.norm(t - t0, axis=1)
    turned = np.degrees([np.linalg.norm(logm(r[i] @ r0[i].T)) for i in range(nv)])
    return Adjusted([Pose(r[i], t[i]) for i in range(nv)], Intrinsics(float(g[0]), float(g[1])), x, sigma,
                    np.linalg.norm(res, axis=1) / SIGMA_PX <= HUBER, rms_before, float(np.sqrt((res ** 2).sum(1).mean())),
                    it, float(vf), {"median_mm": float(np.median(moved) * 1000), "max_mm": float(moved.max() * 1000),
                                    "median_deg": float(np.median(turned)), "max_deg": float(np.max(turned))},
                    float(g[2]) if use_lidar else None, int(has_lidar.sum()) if use_lidar else 0)


def _pairs(track: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    """Observation index pairs (a, b) that see the same point, grouped by offset so each group is vectorised."""
    order = np.argsort(track, kind="stable")
    tr = track[order]
    starts = np.flatnonzero(np.r_[True, tr[1:] != tr[:-1]])
    counts = np.diff(np.r_[starts, len(tr)])
    out = []
    kmax = int(counts.max(initial=0))
    for da in range(kmax):
        for db in range(kmax):
            ok = counts > max(da, db)
            if ok.any():
                out.append((order[starts[ok] + da], order[starts[ok] + db]))
    return out


def _point_sigma(hinv, b_m, tracks, idx, s_mat, vf) -> np.ndarray:
    """sqrt(trace) of each point's marginal covariance: Hinv + Y^T S^-1 Y with Y the point's camera coupling."""
    npnt, nc = tracks.n_points, s_mat.shape[0]
    try:
        s_inv = np.linalg.inv(s_mat)
    except np.linalg.LinAlgError:
        return np.full(npnt, np.nan)
    y = np.zeros((npnt, nc, 3))
    np.add.at(y, (tracks.track[:, None], idx), np.einsum("mij,mjk->mik", b_m, hinv[tracks.track]))
    cov = hinv + np.einsum("pci,cd,pdj->pij", y, s_inv, y)
    return np.sqrt(np.clip(np.trace(cov, axis1=1, axis2=2), 0, None) * max(vf, 1e-12))
