"""The seam to the 3D localization module (task B10): a finding's crop box -> its still's pose -> a place on the
scan, in arkit_world metres. The module is `repairos.locate` (built in its own task); this file imports it lazily,
so the pipeline runs before it is merged and uses it automatically after.

The module's API, used exactly:

    from repairos.locate import locate, try_locate, triangulate
    try_locate(scan, still, box, base_dir=...) -> Location | NotLocated   # NotLocated.reason says why
    locate(scan, still, box, **options)       -> Location | None
    triangulate([(still, box), ...])          -> Location | NotLocated   # no-LiDAR: two or more stills of a part

- `scan`:  ScanPackage dict (or a ScanCloud). Its `points_file` / `mesh_file` are resolved against `base_dir`; the
           pipeline passes them as absolute local paths. `depth_source` is `lidar` (also when absent) or `none`
           (no points_file: a no-LiDAR capture).
- `still`: Still dict. `box`: (x0, y0, x1, y1) in the still's full-resolution pixels.
- `Location.to_dict()`: point_m, region_min_m, region_max_m, normal (mesh only), distance_m, confidence (low |
           medium | high), method (ray_cast_mesh | ray_cast_point_cloud | triangulation), rays_cast, rays_hit,
           support_points, reasons, frame (arkit_world, metres).

Which call: `depth_source` lidar -> try_locate per box. `depth_source` none -> triangulate, only across stills the
app took for the same target (a shared `targets[].target_id`) that each show exactly one region of the same part
class; triangulate itself checks the rays meet inside every box. Anything else is "location not computed" with the
reason. A location is never made up (GEN3); an exception from the module is a stage failure.
"""
from __future__ import annotations

import importlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MODULE = "repairos.locate"
NOT_INSTALLED = "location not computed: the 3D localization module (repairos.locate) is not present"
NO_BOX = "the finding has no region in a still"
NO_SECOND_VIEW = ("no-LiDAR scan: locating needs a second still of the same target that shows exactly one region of "
                  "the same part to triangulate with")

Box = tuple[float, float, float, float]


class LocatorError(ValueError):
    """The module answered something that is not a Location or a NotLocated."""


def depth_source(scan: Mapping[str, Any]) -> str:
    """lidar | none; absent means lidar (scan-package contract)."""
    return scan.get("depth_source") or "lidar"


def not_computed(reason: str) -> dict[str, Any]:
    return {"status": "not_computed", "reason": reason}


@dataclass(frozen=True)
class Locator:
    module: Any
    version: str

    def locate(self, scan: Mapping[str, Any], still: Mapping[str, Any], box: Box,
               base_dir: str | None = None) -> dict[str, Any]:
        """One box on a LiDAR scan: the report `location` (computed, or not_computed with the module's reason)."""
        return self._report(self.module.try_locate(scan, still, tuple(float(v) for v in box), base_dir=base_dir))

    def triangulate(self, views: Sequence[tuple[Mapping[str, Any], Box]]) -> dict[str, Any]:
        return self._report(self.module.triangulate([(s, tuple(float(v) for v in b)) for s, b in views]))

    def _report(self, res: Any) -> dict[str, Any]:
        if hasattr(res, "reason") and not hasattr(res, "point_m"):
            reason = str(res.reason).strip()
            if not reason:
                raise LocatorError("NotLocated without a reason")
            return not_computed(reason)
        if not hasattr(res, "to_dict"):
            raise LocatorError(f"{MODULE} returned {type(res).__name__}, not a Location or NotLocated")
        return as_report_location(res.to_dict())


def load_locator(module: str = MODULE) -> Locator | None:
    """The module when it is present, else None. A module that is present but broken raises (a failure, not an
    absence)."""
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as e:
        if e.name is not None and module.startswith(e.name):
            return None
        raise
    for name in ("try_locate", "triangulate"):
        if not callable(getattr(mod, name, None)):
            raise LocatorError(f"{module} has no callable {name}()")
    return Locator(mod, f"{module} {getattr(mod, '__version__', None) or ''}".strip())


def _vec(v: Any, what: str) -> list[float]:
    try:
        out = [float(x) for x in v]
    except (TypeError, ValueError):
        raise LocatorError(f"{what} is not a 3-vector: {v!r}") from None
    if len(out) != 3 or not all(math.isfinite(x) for x in out):
        raise LocatorError(f"{what} is not a finite 3-vector: {v!r}")
    return out


def as_report_location(d: Mapping[str, Any]) -> dict[str, Any]:
    """`Location.to_dict()` -> the report's computed location (report.schema.json $defs/location)."""
    if d.get("frame", "arkit_world") != "arkit_world":
        raise LocatorError(f"location frame {d.get('frame')!r}: the report places regions in arkit_world")
    if d.get("confidence") not in ("low", "medium", "high"):
        raise LocatorError(f"location confidence {d.get('confidence')!r} is not a word (I6)")
    method = str(d.get("method") or "").strip()
    if not method:
        raise LocatorError("location without a method")
    out: dict[str, Any] = {"status": "computed", "frame": "arkit_world", "point_m": _vec(d.get("point_m"), "point_m"),
                           "region_min_m": _vec(d.get("region_min_m"), "region_min_m"),
                           "region_max_m": _vec(d.get("region_max_m"), "region_max_m"),
                           "confidence_word": d["confidence"], "method": method}
    if any(lo > hi for lo, hi in zip(out["region_min_m"], out["region_max_m"])):
        raise LocatorError("location region_min_m is above region_max_m")
    if d.get("normal") is not None:
        out["normal"] = _vec(d["normal"], "normal")
    if d.get("distance_m") is not None:
        dist = float(d["distance_m"])
        if not math.isfinite(dist) or dist < 0:
            raise LocatorError(f"location distance_m {dist!r}")
        out["distance_m"] = dist
    notes = [str(r) for r in d.get("reasons") or [] if str(r).strip()]
    if notes:
        out["notes"] = notes
    return out


def triangulation_views(finding: Mapping[str, Any], findings: Sequence[Mapping[str, Any]],
                        stills: Mapping[str, Mapping[str, Any]], part_class: Mapping[str, str]
                        ) -> list[tuple[Mapping[str, Any], Box]]:
    """(still, box) views for triangulating `finding` on a no-LiDAR scan: its own, plus one from each other still
    that shares a target with its still and holds exactly one boxed region of the same part class. Empty when there
    is no such second view (the pairing is never guessed between candidates)."""
    def view(f):
        refs = f.get("evidence_refs") or {}
        sid = (refs.get("still_ids") or [None])[0]
        return (sid, refs.get("crop_box_px")) if sid in stills and refs.get("crop_box_px") else (None, None)

    sid, box = view(finding)
    want = part_class.get(finding["finding_id"])
    if sid is None or want is None:
        return []
    targets = {t["target_id"] for t in stills[sid].get("targets") or []}
    if not targets:
        return []
    by_still: dict[str, list] = {}
    for f in findings:
        osid, obox = view(f)
        if osid and osid != sid and part_class.get(f["finding_id"]) == want:
            by_still.setdefault(osid, []).append(obox)
    views = [(stills[sid], tuple(box))]
    for osid, boxes in sorted(by_still.items()):
        shared = targets & {t["target_id"] for t in stills[osid].get("targets") or []}
        if shared and len(boxes) == 1:
            views.append((stills[osid], tuple(boxes[0])))
    return views if len(views) >= 2 else []
