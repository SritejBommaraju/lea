"""Pixels per millimetre on a still, from its own depth grid and intrinsics (no detector label, nothing about the object).

A still that carries a depth grid (ARKit, 256x192, metres) and pinhole intrinsics gives, for any box, the depth of the
surface it shows (median over the box's central half, like pipeline/regions.box_in_world) and so its scale:
px/mm = fx / depth_mm (a surface roughly facing the camera; an oblique surface is coarser than this, never finer).
Without depth or intrinsics there is no scale and callers fall back to sizes relative to the still.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

MIN_DEPTH_M, MAX_DEPTH_M = 0.03, 5.0


@dataclass(frozen=True)
class PixelScale:
    """Scale of one still: `px_per_mm(box)` for still-pixel boxes, None where the depth grid has no valid value."""
    depth_m: np.ndarray            # H x W metres (the still's depth grid, any resolution)
    fx_px: float                   # focal length in pixels of the still (width_px x height_px)
    width_px: int
    height_px: int

    @classmethod
    def from_still(cls, still: Mapping[str, Any], depth_m: np.ndarray | None) -> PixelScale | None:
        """The still's scale, or None without a usable depth grid, intrinsics or image size. A still whose pose is not
        reliable (macro_detail: a different camera, the depth frame of another moment) gets none."""
        if depth_m is None or getattr(depth_m, "ndim", 0) != 2 or still.get("pose_reliable", True) is False:
            return None
        intr = still.get("intrinsics") or {}
        w, h = still.get("width_px"), still.get("height_px")
        if not intr.get("fx") or not w or not h:
            return None
        return cls(np.asarray(depth_m, dtype=float), float(intr["fx"]), int(w), int(h))

    @classmethod
    def constant(cls, px_per_mm: float, width_px: int, height_px: int, depth_m: float = 0.2) -> PixelScale:
        """A still of a flat surface at one depth (the same arithmetic as a real grid; used for tests and replays of
        known geometry)."""
        fx = px_per_mm * depth_m * 1000.0
        return cls(np.full((192, 256), depth_m), fx, width_px, height_px)

    def depth_at(self, box: Sequence[float]) -> float | None:
        h, w = self.depth_m.shape
        sx, sy = w / self.width_px, h / self.height_px
        x0, y0, x1, y1 = (float(v) for v in box)
        cx, cy, bw, bh = (x0 + x1) / 2, (y0 + y1) / 2, max(1.0, x1 - x0), max(1.0, y1 - y0)
        gx0, gx1 = int(np.floor((cx - bw / 4) * sx)), int(np.ceil((cx + bw / 4) * sx))
        gy0, gy1 = int(np.floor((cy - bh / 4) * sy)), int(np.ceil((cy + bh / 4) * sy))
        gx0, gy0 = max(0, min(gx0, w - 1)), max(0, min(gy0, h - 1))
        gx1, gy1 = max(gx0 + 1, min(gx1, w)), max(gy0 + 1, min(gy1, h))
        z = self.depth_m[gy0:gy1, gx0:gx1].reshape(-1)
        z = z[np.isfinite(z) & (z > MIN_DEPTH_M) & (z < MAX_DEPTH_M)]
        return float(np.median(z)) if z.size else None

    def px_per_mm(self, box: Sequence[float]) -> float | None:
        d = self.depth_at(box)
        return self.fx_px / (d * 1000.0) if d else None

    def mm(self, px: float, box: Sequence[float]) -> float | None:
        """A length in still pixels at `box`, in millimetres."""
        s = self.px_per_mm(box)
        return px / s if s else None
