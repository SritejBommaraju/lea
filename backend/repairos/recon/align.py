"""Similarity (sim(3)) and rigid alignment of point sets: Umeyama's closed form, a random-sample consensus
wrapper for outliers, and
the residuals that say how good an alignment is.

    s = umeyama(src, dst)                  # Sim3 with dst ~= s.apply(src), least squares
    s, inliers = robust_umeyama(src, dst, threshold_m=0.01)
    r = residuals(s, src, dst)             # {rmse_m, median_m, max_m, n}

Used to put Apple's PhotogrammetrySession frame into the ARKit frame (camera centres of the images both have, as
COLMAP's model_aligner does) and, rigidly (`with_scale=False`), to register a flipped segment from 3D feature
matches. Reference: S. Umeyama, "Least-squares estimation of transformation parameters between two point
patterns", IEEE TPAMI 13(4), 1991. Pure numpy.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Sim3:
    """x' = scale * R @ x + t."""
    scale: float
    r: np.ndarray
    t: np.ndarray

    @classmethod
    def identity(cls) -> Sim3:
        return cls(1.0, np.eye(3), np.zeros(3))

    @classmethod
    def from_matrix(cls, m: np.ndarray) -> Sim3:
        m = np.asarray(m, dtype=float)
        a = m[:3, :3]
        s = float(np.cbrt(np.linalg.det(a)))
        if s <= 0:
            raise ValueError("not a proper similarity (det <= 0)")
        u, _, vt = np.linalg.svd(a / s)
        return cls(s, u @ vt, m[:3, 3].copy())

    @property
    def matrix(self) -> np.ndarray:
        m = np.eye(4)
        m[:3, :3] = self.scale * self.r
        m[:3, 3] = self.t
        return m

    def apply(self, x: np.ndarray) -> np.ndarray:
        return self.scale * (np.asarray(x, dtype=float) @ self.r.T) + self.t

    def apply_rotation(self, r: np.ndarray) -> np.ndarray:
        return self.r @ np.asarray(r, dtype=float)

    def inverse(self) -> Sim3:
        ri = self.r.T
        return Sim3(1.0 / self.scale, ri, -(ri @ self.t) / self.scale)

    def __matmul__(self, other: Sim3) -> Sim3:
        """(self @ other).apply(x) == self.apply(other.apply(x))."""
        return Sim3(self.scale * other.scale, self.r @ other.r, self.scale * (self.r @ other.t) + self.t)

    def rotation_deg(self) -> float:
        return rotation_angle_deg(self.r)

    def to_dict(self) -> dict:
        return {"scale": float(self.scale), "matrix_row_major": [float(v) for v in self.matrix.reshape(-1)],
                "rotation_deg": round(self.rotation_deg(), 4)}


def rotation_angle_deg(r: np.ndarray) -> float:
    c = (np.trace(np.asarray(r, dtype=float)) - 1.0) / 2.0
    return float(np.degrees(np.arccos(np.clip(c, -1.0, 1.0))))


def umeyama(src: np.ndarray, dst: np.ndarray, *, with_scale: bool = True,
            weights: np.ndarray | None = None) -> Sim3:
    """The similarity (or rigid motion) minimising sum w_i |dst_i - (s R src_i + t)|^2. Needs >= 3 non-collinear
    points."""
    x = np.asarray(src, dtype=float).reshape(-1, 3)
    y = np.asarray(dst, dtype=float).reshape(-1, 3)
    if len(x) != len(y):
        raise ValueError(f"{len(x)} source points but {len(y)} destination points")
    if len(x) < 3:
        raise ValueError("at least 3 point pairs are needed")
    w = np.ones(len(x)) if weights is None else np.asarray(weights, dtype=float).reshape(-1)
    w = w / w.sum()
    mx, my = w @ x, w @ y
    xc, yc = x - mx, y - my
    cov = (yc * w[:, None]).T @ xc
    u, d, vt = np.linalg.svd(cov)
    if d[1] < 1e-12 * max(d[0], 1e-300):
        raise ValueError("the points are collinear (or coincident): the rotation is not determined")
    sgn = np.ones(3)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        sgn[2] = -1.0
    r = u @ np.diag(sgn) @ vt
    var_x = float(w @ np.sum(xc * xc, axis=1))
    s = float(np.sum(d * sgn) / var_x) if with_scale else 1.0
    t = my - s * (r @ mx)
    return Sim3(s, r, t)


def residuals(s: Sim3, src: np.ndarray, dst: np.ndarray) -> dict:
    e = np.linalg.norm(s.apply(src) - np.asarray(dst, dtype=float), axis=1)
    if len(e) == 0:
        return {"n": 0, "rmse_m": None, "median_m": None, "max_m": None}
    return {"n": int(len(e)), "rmse_m": float(np.sqrt(np.mean(e ** 2))), "median_m": float(np.median(e)),
            "max_m": float(e.max())}


def robust_umeyama(src: np.ndarray, dst: np.ndarray, *, threshold_m: float, with_scale: bool = True,
                   iterations: int = 2000, min_inliers: int = 3, seed: int = 0) -> tuple[Sim3, np.ndarray]:
    """Umeyama on 3-point samples, keep the model with the most pairs within `threshold_m` (ties: lower inlier
    error), refit on its inliers. Returns (model, inlier mask). Raises ValueError when no model reaches
    `min_inliers`."""
    x = np.asarray(src, dtype=float).reshape(-1, 3)
    y = np.asarray(dst, dtype=float).reshape(-1, 3)
    n = len(x)
    if n < 3:
        raise ValueError("at least 3 point pairs are needed")
    rng = np.random.default_rng(seed)
    best, best_count, best_err = None, -1, np.inf
    for _ in range(iterations):
        idx = rng.choice(n, 3, replace=False)
        try:
            m = umeyama(x[idx], y[idx], with_scale=with_scale)
        except ValueError:
            continue
        if with_scale and not (0.2 < m.scale < 5.0):
            continue
        e = np.linalg.norm(m.apply(x) - y, axis=1)
        ok = e < threshold_m
        c = int(ok.sum())
        err = float(e[ok].mean()) if c else np.inf
        if c > best_count or (c == best_count and err < best_err):
            best, best_count, best_err = ok, c, err
    if best is None or best_count < min_inliers:
        raise ValueError(f"no model with at least {min_inliers} inliers within {threshold_m} m "
                         f"(best {max(best_count, 0)} of {n})")
    inl = best
    for _ in range(3):   # refit and re-score until the inlier set settles
        m = umeyama(x[inl], y[inl], with_scale=with_scale)
        new = np.linalg.norm(m.apply(x) - y, axis=1) < threshold_m
        if new.sum() < min_inliers or np.array_equal(new, inl):
            break
        inl = new
    return umeyama(x[inl], y[inl], with_scale=with_scale), inl
