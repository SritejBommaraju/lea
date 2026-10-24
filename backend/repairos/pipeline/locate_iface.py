"""The 3D localization seam (task B10): a finding's crop box -> its still's pose -> a point or region on the scan.

The localizer itself is built separately in `repairos.locate` (its own task and worktree). The pipeline only needs:

    from repairos.locate import locate             # optional module
    locate(scan, still, box) -> Location | None

- `scan`:  ScanPackage dict (spec/contracts/scan-package.schema.json) whose file fields (`points_file`, keyframe
           `image_file` / `depth_file` / `confidence_file`) are LOCAL FILE PATHS the locator can open.
- `still`: Still dict (spec/contracts/still.schema.json), file fields as local paths, `camera` in arkit_world.
- `box`:   (x0, y0, x1, y1) in the still's full-resolution pixels (Finding.evidence_refs.crop_box_px).
- returns a `Location` (or any object / dict with the same fields) in the scan's frame, or None when the region
           cannot be placed (no depth there, box off the scan, ...). It never guesses; raising is a stage failure.
- module attributes read: `locate` (required), `VERSION` or `__version__` (recorded in the report's provenance).

When the module is absent, or a still has no reliable pose, the report says "location not computed" with the
reason. Nothing stands in for the locator (GEN3).
"""
from __future__ import annotations

import importlib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

MODULE = "repairos.locate"
NOT_INSTALLED = "the 3D localization module is not installed yet"
NO_POSE = "the still has no reliable camera pose"
NOT_PLACED = "the locator could not place this region on the scan"
NO_BOX = "the finding has no region in a still"

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class Location:
    point_m: tuple[float, float, float]                  # region centre on the scan, arkit_world metres (Y up)
    method: str = ""                                    # how it was found; the pipeline adds the module version
    extent_m: tuple[float, float, float] | None = None  # axis-aligned size of the region in arkit_world, if known
    frame: str = "arkit_world"


LocateFn = Callable[[dict[str, Any], dict[str, Any], Box], Any]


class LocatorError(ValueError):
    """The locator answered something that is not a Location."""


@dataclass(frozen=True)
class Locator:
    fn: LocateFn
    version: str

    def __call__(self, scan: dict[str, Any], still: dict[str, Any], box: Box) -> dict[str, Any]:
        """The report's `location` object: computed, or not_computed with the reason."""
        out = self.fn(scan, still, tuple(float(v) for v in box))
        if out is None:
            return not_computed(NOT_PLACED)
        return as_report_location(out, self.version)


def load_locator(module: str = MODULE) -> Locator | None:
    """The installed locator, or None when `repairos.locate` does not exist yet. Any other import error is raised:
    a broken locator is a failure, not an absent one."""
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as e:
        if e.name is not None and module.startswith(e.name):
            return None
        raise
    fn = getattr(mod, "locate", None)
    if not callable(fn):
        raise LocatorError(f"{module} has no callable `locate(scan, still, box)`")
    version = str(getattr(mod, "VERSION", None) or getattr(mod, "__version__", None) or "unversioned")
    return Locator(fn, f"{module} {version}")


def not_computed(reason: str) -> dict[str, Any]:
    return {"status": "not_computed", "reason": reason}


def pose_reliable(still: Mapping[str, Any]) -> bool:
    return (still.get("kind") != "macro_detail" and still.get("pose_reliable", True) is not False
            and still.get("tracking_state") == "normal")


def _vec(v: Any, what: str, nonneg: bool = False) -> list[float]:
    try:
        out = [float(x) for x in v]
    except (TypeError, ValueError):
        raise LocatorError(f"{what} is not a 3-vector: {v!r}") from None
    if len(out) != 3 or not all(math.isfinite(x) for x in out) or (nonneg and any(x < 0 for x in out)):
        raise LocatorError(f"{what} is not a finite 3-vector: {v!r}")
    return out


def as_report_location(loc: Any, version: str) -> dict[str, Any]:
    get = loc.get if isinstance(loc, Mapping) else lambda k, d=None: getattr(loc, k, d)
    frame = get("frame", "arkit_world")
    if frame != "arkit_world":
        raise LocatorError(f"location frame {frame!r}: the report places regions in arkit_world")
    out: dict[str, Any] = {"status": "computed", "frame": "arkit_world", "point_m": _vec(get("point_m"), "point_m")}
    if get("extent_m") is not None:
        out["extent_m"] = _vec(get("extent_m"), "extent_m", nonneg=True)
    method = str(get("method", "") or "").strip()
    out["method"] = f"{version}: {method}" if method else version
    return out
