"""How well the capture saw the object, from where the camera was (generic: no part or product knowledge).

Every keyframe and still with a camera position is placed on a view dome around the object box: 3 elevation rings
(0-27.5, 27.5-55, 55-90 degrees above the support plane; below the plane counts as the lowest ring) x 12 sectors of
30 degrees, the same dome the app fills while scanning (ios/RepairOS/Capture/CoverageDome.swift). The verdict's
coverage gate (config/verdict.yaml) reads the share of cells seen and the number of distinct sectors.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

RINGS, SECTORS = (27.5, 55.0), 12
CELLS = (len(RINGS) + 1) * SECTORS


def _unit(v: Sequence[float]) -> list[float] | None:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n > 1e-9 else None


def _same_frame_segments(scan: Mapping[str, Any]) -> set[int]:
    """Segments whose camera poses are comparable with segment 0's box: segment 0, and continued segments ARKit
    relocalised (aligned not false). A flipped segment's object moved, and an unaligned one has its own world origin,
    so their poses would add viewing directions nobody had. A hand-held scan's poses are all object-relative."""
    segments = scan.get("segments") or []
    if scan.get("pose_source") == "object_tracked":
        return {0} | {g.get("segment") for g in segments}
    return {0} | {g.get("segment") for g in segments if g.get("reason") == "continued" and g.get("aligned") is not False}


def view_coverage(scan: Mapping[str, Any] | None, stills: Sequence[Mapping[str, Any]]) -> dict | None:
    """{cells, cells_total, sectors, sectors_total, share} or None when the object box or camera poses are unknown
    (a loose photo, a no-LiDAR capture without a box)."""
    box = (scan or {}).get("crop_box") or {}
    centre = box.get("center_m")
    if not centre or len(centre) != 3:
        return None
    g = (scan or {}).get("gravity_arkit_world")
    up = _unit([-x for x in g]) if g and len(g) == 3 else [0.0, 1.0, 0.0]
    if up is None:
        up = [0.0, 1.0, 0.0]
    # two horizontal axes (any pair perpendicular to up)
    ref = [1.0, 0.0, 0.0] if abs(up[0]) < 0.9 else [0.0, 0.0, 1.0]
    e1 = _unit([ref[i] - up[i] * sum(ref[j] * up[j] for j in range(3)) for i in range(3)])
    e2 = [up[1] * e1[2] - up[2] * e1[1], up[2] * e1[0] - up[0] * e1[2], up[0] * e1[1] - up[1] * e1[0]]
    cells, sectors = set(), set()
    same = _same_frame_segments(scan or {})
    poses = [k.get("camera") for k in (scan or {}).get("keyframes") or [] if (k.get("segment") or 0) in same] + [
        s.get("camera") for s in stills if (s.get("segment") or 0) in same]
    for cam in poses:
        p = (cam or {}).get("position_m")
        if not p or len(p) != 3:
            continue
        d = _unit([p[i] - centre[i] for i in range(3)])
        if d is None:
            continue
        el = math.degrees(math.asin(max(-1.0, min(1.0, sum(d[i] * up[i] for i in range(3))))))
        az = math.degrees(math.atan2(sum(d[i] * e2[i] for i in range(3)), sum(d[i] * e1[i] for i in range(3)))) % 360
        ring = sum(el >= b for b in RINGS)
        sector = int(az // (360 / SECTORS)) % SECTORS
        cells.add((ring, sector))
        sectors.add(sector)
    if not cells:
        return None
    return {"cells": len(cells), "cells_total": CELLS, "sectors": len(sectors), "sectors_total": SECTORS,
            "share": round(len(cells) / CELLS, 3)}
