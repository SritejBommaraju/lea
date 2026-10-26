"""A still as a measuring view: its grey image at a working size, its ARKit pose and pinhole for that size, its depth
grid (metres along the view axis, ARKit sceneDepth) and confidence. Conventions: repairos.locate.camera."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from repairos.locate.camera import Pinhole, Pose

MAX_SIDE = 2016          # stills are tracked at most this wide; marks arrive in full-resolution pixels and are scaled
MIN_DEPTH_M, MAX_DEPTH_M = 0.05, 3.0


@dataclass(frozen=True)
class View:
    still_id: str
    gray: np.ndarray               # H x W float32, 0..1
    pose: Pose                     # camera-to-world, arkit_world metres
    cam: Pinhole                   # intrinsics of `gray`
    scale: float                   # gray pixels per still pixel (marks are multiplied by it)
    depth: np.ndarray | None       # h x w metres, any grid size covering the same field of view
    confidence: np.ndarray | None  # h x w uint8 (0 low, 1 medium, 2 high) or None

    @property
    def size(self) -> tuple[int, int]:
        return self.gray.shape[1], self.gray.shape[0]

    def depth_at(self, uv: np.ndarray, min_confidence: int = 1) -> np.ndarray:
        """Depth (metres) at gray pixels uv (N x 2): the median of the valid cells in the 3 x 3 grid cells around
        each, NaN where none is valid or confident enough."""
        uv = np.atleast_2d(np.asarray(uv, dtype=float))
        out = np.full(len(uv), np.nan)
        if self.depth is None:
            return out
        h, w = self.depth.shape
        gw, gh = self.size
        gx = np.floor(uv[:, 0] * w / gw).astype(int)
        gy = np.floor(uv[:, 1] * h / gh).astype(int)
        cells = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                x, y = np.clip(gx + dx, 0, w - 1), np.clip(gy + dy, 0, h - 1)
                z = self.depth[y, x].astype(float)
                ok = np.isfinite(z) & (z > MIN_DEPTH_M) & (z < MAX_DEPTH_M)
                if self.confidence is not None:
                    ok &= self.confidence[y, x] >= min_confidence
                cells.append(np.where(ok, z, np.nan))
        stack = np.stack(cells, axis=1)
        good = (gx >= 0) & (gx < w) & (gy >= 0) & (gy < h) & ~np.isnan(stack).all(axis=1)
        if good.any():
            out[good] = np.nanmedian(stack[good], axis=1)
        return out

    def depth_edge(self, uv: np.ndarray, jump_m: float = 0.02, jump_rel: float = 0.06) -> np.ndarray:
        """True where the depth in the 3 x 3 cells around uv spans more than max(jump_m, jump_rel x depth): an object
        edge, where LiDAR smears foreground into background. Below that the span is the sensor's own noise."""
        uv = np.atleast_2d(np.asarray(uv, dtype=float))
        if self.depth is None:
            return np.ones(len(uv), dtype=bool)
        h, w = self.depth.shape
        gw, gh = self.size
        gx = np.clip(np.floor(uv[:, 0] * w / gw).astype(int), 1, w - 2)
        gy = np.clip(np.floor(uv[:, 1] * h / gh).astype(int), 1, h - 2)
        z = self.depth.astype(float)
        win = np.stack([z[gy + dy, gx + dx] for dy in (-1, 0, 1) for dx in (-1, 0, 1)], axis=1)
        with np.errstate(all="ignore"):
            span = np.nanmax(win, axis=1) - np.nanmin(win, axis=1)
            return ~np.isfinite(win).all(axis=1) | (span > np.maximum(jump_m, jump_rel * np.nanmedian(win, axis=1)))


def usable(still: Mapping[str, Any]) -> str | None:
    """Why a still cannot be a measuring view, or None. Only a reliable ARKit pose with intrinsics measures."""
    if still.get("pose_reliable") is False or not still.get("camera"):
        return "no reliable pose"
    if not (still.get("intrinsics") or {}).get("fx") or not still.get("width_px") or not still.get("height_px"):
        return "no intrinsics"
    if (still.get("camera") or {}).get("frame", "arkit_world") != "arkit_world":
        return "pose not in arkit_world"
    return None


def to_gray(rgb: np.ndarray) -> np.ndarray:
    a = np.asarray(rgb, dtype=np.float32)
    if a.ndim == 3:
        a = a[..., :3] @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    return a / 255.0 if a.max() > 1.5 else a


def shrink(gray: np.ndarray, factor: int) -> np.ndarray:
    """Box-filter downsample by an integer factor (crops the remainder)."""
    if factor <= 1:
        return gray
    h, w = (gray.shape[0] // factor) * factor, (gray.shape[1] // factor) * factor
    return gray[:h, :w].reshape(h // factor, factor, w // factor, factor).mean(axis=(1, 3))


def make_view(still: Mapping[str, Any], rgb: np.ndarray, depth: np.ndarray | None = None,
              confidence: np.ndarray | None = None, max_side: int = MAX_SIDE) -> View:
    """A View from a still contract row and its decoded image (stored pixel orientation, the intrinsics' grid)."""
    why = usable(still)
    if why:
        raise ValueError(f"still {still.get('still_id')}: {why}")
    gray = to_gray(rgb)
    W, H = int(still["width_px"]), int(still["height_px"])
    if gray.shape[:2] != (H, W):
        raise ValueError(f"still {still['still_id']}: image is {gray.shape[1]}x{gray.shape[0]}, contract {W}x{H}")
    factor = max(1, int(np.ceil(max(W, H) / max_side)))
    small = shrink(gray, factor)
    cam = Pinhole.from_contract(still["intrinsics"], W, H)
    # the box filter keeps pixel (i, j) of the small grid over still pixels [i*f, (i+1)*f): intrinsics scale by 1/f
    cam = Pinhole(cam.fx / factor, cam.fy / factor, cam.cx / factor, cam.cy / factor, small.shape[1], small.shape[0])
    return View(str(still["still_id"]), small.astype(np.float32), Pose.from_contract(still["camera"]), cam,
                1.0 / factor, None if depth is None else np.asarray(depth, dtype=np.float32), confidence)
