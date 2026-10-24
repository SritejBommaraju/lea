"""Pick keyframes from a posed frame sequence: a new keyframe after `travel_m` of camera travel or `angle_deg` of
rotation since the last one (scan-package.schema.json: ~10 cm or 15 deg), the sharpest frame within the next
`window` frames."""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from repairos.locate.camera import Pose


def rotation_deg(a: Pose, b: Pose) -> float:
    c = (np.trace(a.r.T @ b.r) - 1.0) / 2.0
    return float(np.degrees(np.arccos(np.clip(c, -1.0, 1.0))))


def moved(a: Pose, b: Pose, travel_m: float, angle_deg: float) -> bool:
    return float(np.linalg.norm(a.t - b.t)) >= travel_m or rotation_deg(a, b) >= angle_deg


def select_keyframes(poses: Sequence[Pose], sharpness: Sequence[float], *, travel_m: float = 0.10,
                     angle_deg: float = 15.0, window: int = 1) -> list[int]:
    n = len(poses)
    if n == 0:
        return []
    sharp = np.asarray(sharpness, dtype=float)
    kept: list[int] = []
    i = 0
    while i < n:
        if not kept or moved(poses[kept[-1]], poses[i], travel_m, angle_deg):
            j = i + int(np.argmax(sharp[i:min(n, i + max(1, window))]))
            kept.append(j)
            i = j + 1
        else:
            i += 1
    return kept
