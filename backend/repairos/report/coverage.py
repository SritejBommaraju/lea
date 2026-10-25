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


def _comparable(item: Mapping[str, Any], scan: Mapping[str, Any]) -> bool:
    """Is this keyframe's or still's camera pose in segment 0's frame (comparable with its box)? Decided per segment
    and per pose, never for the whole package: segment 0; a continued segment ARKit relocalised (aligned not false,
    object not moved); or, in a hand-held scan, a pose the app tracked relative to the object (pose_source
    object_tracked) in a segment not marked unaligned. A hand-held scan's continuation records fresh ARKit poses with
    aligned false (no world map to relocalise against): those are not comparable, nor is a flip ARKit tracked."""
    seg = item.get("segment") or 0
    if seg == 0:
        return True
    info = next((g for g in scan.get("segments") or [] if g.get("segment") == seg), {})
    if info.get("aligned") is False:
        return False
    if item.get("pose_source") == "object_tracked" and scan.get("pose_source") == "object_tracked":
        return True
    return info.get("reason") == "continued" and not info.get("object_moved")


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
    pkg = scan or {}
    poses = [x.get("camera") for x in list(pkg.get("keyframes") or []) + list(stills) if _comparable(x, pkg)]
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
