"""One judgment per place on the scan: a region two stills both show is sent to the vision model once.

Each detector box is put in the world with its still's own depth grid (ARKit pose + intrinsics + depth, no detector
label, nothing about the object): its centre point and its size in metres. Within one pipeline run, a box whose centre
lies within `same_region_m` (or half the smaller box's size, whichever is larger) of a box another still already
registered, at a similar size (SIZE_RATIO_MAX), reuses that still's answer instead of a new Claude call
(vision.inspect.finding_from_other_still). The first still to register a place judges it; the pipeline inspects
close-ups and the sharpest views first, so that is usually the better view.

A box that cannot be placed (no pose, no depth there) is never matched: it is judged on its own. Boxes of different
capture segments are never matched (the object may have moved, or the segment may not be aligned).
"""
from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any

import numpy as np

SIZE_RATIO_MAX = 2.0
MIN_DEPTH_M, MAX_DEPTH_M = 0.05, 5.0


@dataclass
class Entry:
    still_id: str
    index: int
    point: np.ndarray
    size_m: float
    segment: int = 0
    future: Future = field(default_factory=Future)

    def resolve(self, finding: dict) -> None:
        if not self.future.done():
            self.future.set_result(finding)

    def fail(self, e: BaseException) -> None:
        if not self.future.done():
            self.future.set_exception(e)

    def result(self, timeout: float | None = None) -> dict:
        return self.future.result(timeout)


def box_in_world(still: Mapping[str, Any], depth_m: np.ndarray, box: Sequence[float]) -> tuple[np.ndarray, float] | None:
    """(world centre point, size in metres) of a still-pixel box from the still's depth grid, or None."""
    from repairos.locate.camera import Pinhole, Pose

    if not still.get("pose_reliable", True) or "camera" not in still or "intrinsics" not in still:
        return None
    w_px, h_px = still.get("width_px"), still.get("height_px")
    if not w_px or not h_px or depth_m is None or depth_m.ndim != 2:
        return None
    h, w = depth_m.shape
    sx, sy = w / w_px, h / h_px
    x0, y0, x1, y1 = (float(v) for v in box)
    cx, cy, bw, bh = (x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0
    # the central half of the box on the depth grid (at least the cell under the centre)
    gx0, gx1 = int(np.floor((cx - bw / 4) * sx)), int(np.ceil((cx + bw / 4) * sx))
    gy0, gy1 = int(np.floor((cy - bh / 4) * sy)), int(np.ceil((cy + bh / 4) * sy))
    gx0, gy0 = max(0, min(gx0, w - 1)), max(0, min(gy0, h - 1))
    gx1, gy1 = max(gx0 + 1, min(gx1, w)), max(gy0 + 1, min(gy1, h))
    z = depth_m[gy0:gy1, gx0:gx1].reshape(-1)
    z = z[np.isfinite(z) & (z > MIN_DEPTH_M) & (z < MAX_DEPTH_M)]
    if z.size == 0:
        return None
    d = float(np.median(z))
    cam = Pinhole.from_contract(still["intrinsics"], w_px, h_px)
    p_cam = np.array([(cx - cam.cx) / cam.fx * d, -(cy - cam.cy) / cam.fy * d, -d])
    point = Pose.from_contract(still["camera"]).to_world(p_cam)
    size = float(np.sqrt(max(bw, 1.0) * max(bh, 1.0)) / cam.fx * d)
    return point, size


class SameRegions:
    """The register of one pipeline run (one scan). Thread-safe."""

    def __init__(self, same_region_m: float):
        self.same_region_m = same_region_m
        self._lock = threading.Lock()
        self._entries: list[Entry] = []

    def for_still(self, still: Mapping[str, Any], depth_m: np.ndarray | None) -> StillShare:
        return StillShare(self, still, depth_m)

    def claim(self, still_id: str, index: int, placed: tuple[np.ndarray, float] | None,
              register: bool, segment: int = 0) -> tuple[Entry | None, bool]:
        """(entry, mine): another still's entry for the same place (mine False); else a new entry of this still when
        `register` (mine True); else (None, False). An unplaced box is never matched or registered."""
        if placed is None:
            return None, False
        point, size = placed
        with self._lock:
            for e in self._entries:
                if e.still_id == still_id or e.segment != segment:   # other segments: poses not comparable
                    continue
                near = max(self.same_region_m, 0.5 * min(size, e.size_m))
                ratio = max(size, e.size_m) / max(min(size, e.size_m), 1e-6)
                if ratio <= SIZE_RATIO_MAX and float(np.linalg.norm(point - e.point)) <= near:
                    return e, False
            if not register:
                return None, False
            e = Entry(still_id, index, point, size, segment)
            self._entries.append(e)
            return e, True

    def __len__(self) -> int:
        return len(self._entries)


class StillShare:
    """The register as one still sees it (vision.inspect's `share`): claim(still_id, index, box, register)."""

    def __init__(self, regions: SameRegions, still: Mapping[str, Any], depth_m: np.ndarray | None):
        self.regions, self.still, self.depth_m = regions, still, depth_m

    def claim(self, still_id: str, index: int, box: Sequence[float], register: bool) -> tuple[Entry | None, bool]:
        placed = box_in_world(self.still, self.depth_m, box) if self.depth_m is not None else None
        return self.regions.claim(still_id, index, placed, register, int(self.still.get("segment") or 0))
