"""Which stills of a scan the pipeline inspects (speed, for any object): near-duplicate views are covered by one.

A phone scan of ~20 stills often has runs of nearly the same view (auto-fired from one spot). Inspecting each costs a
detector pass and several Claude calls, and the same parts are judged again. This module picks the stills worth
inspecting from geometry and sharpness only (ARKit pose, intrinsics, the still's own depth grid), never from what is in
the photo:

- Each posed still gets sample points: a grid over its depth map, back-projected to the world.
- Two stills of the same capture segment are near-duplicates when one sees most of the other's points (OVERLAP_MIN)
  and the other much of the first's (OVERLAP_BOTH), at about the same scale (SCALE_MAX) and from about the same
  direction (ANGLE_MAX_DEG).
- Deliberate close-ups (guided or macro stills) come first, then the sharpest still of each group of near-duplicates
  is kept; the others are covered by the kept view.
- Close-ups (those kinds, or stills much nearer than the scan's typical distance) are never dropped by the cap.
- When more than `max_stills` views are kept, close-ups and new viewpoints (no earlier kept view sees half of
  them, NEW_VIEW_SHARE) stay, and the rest are filled by farthest-view sampling (the view least like those kept,
  each time); the stills left over are covered by the kept view that sees most of them.
- A still without a usable pose or depth cannot be compared and is always kept (nothing is assumed about it).
- Stills that arrive one by one during the scan are decided against those kept before them (`already`): the same
  rules, in arrival order (a near-duplicate much sharper than the kept view is kept as well, SHARPER_BY).

A covered still is `not_inspected` with the reason (I3): never `ok`, and the report names the photo that covers it.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

OVERLAP_MIN = 0.75        # share of a still's sample points the other still sees (one way) for a near-duplicate
OVERLAP_BOTH = 0.6        # ... and at least this share the other way
NEW_VIEW_SHARE = 0.5      # a view that no earlier kept view sees half of is a new viewpoint: the cap never drops it
SCALE_MAX = 1.4           # distance ratio to the shared points at most this (either way) for a near-duplicate
ANGLE_MAX_DEG = 25.0      # angle between the two view axes at most this for a near-duplicate
CLOSEUP_FACTOR = 0.6      # a still nearer than this share of the scan's median subject distance is a close-up
GRID = (12, 9)            # sample points per depth map (columns, rows)
MIN_DEPTH_M, MAX_DEPTH_M = 0.05, 5.0
CLOSEUP_KINDS = ("guided", "macro_detail")
SHARPER_BY = 1.5          # a near-duplicate this many times sharper than the kept view is kept as well


@dataclass
class View:
    still_id: str
    segment: int
    sharpness: float
    kind: str
    pose: Any = None              # repairos.locate.camera.Pose
    cam: Any = None               # Pinhole of the full-resolution still
    points: np.ndarray | None = None  # N x 3 world sample points
    distance_m: float | None = None   # median distance to the sample points
    why_unposed: str | None = None

    @property
    def posed(self) -> bool:
        return self.points is not None and len(self.points) > 0


@dataclass
class Selection:
    kept: list[str]                               # stills to inspect (new ones only, not `already`), priority order
    covered: dict[str, dict[str, str]]            # still_id -> {"by": still_id, "why": words}
    closeups: list[str] = field(default_factory=list)
    unposed: dict[str, str] = field(default_factory=dict)  # kept without comparison: still_id -> why
    seconds: float = 0.0

    def as_record(self) -> dict:
        return {"kept": list(self.kept), "covered": {k: dict(v) for k, v in self.covered.items()},
                "closeups": list(self.closeups), "kept_without_pose": dict(self.unposed),
                "seconds": round(self.seconds, 3)}


def build_view(still: Mapping[str, Any], depth_m: np.ndarray | None) -> View:
    """A still's view: pose, camera and world sample points from its depth grid (metres, ARKit sceneDepth)."""
    from repairos.locate.camera import Pinhole, Pose, backproject

    v = View(still_id=still["still_id"], segment=int(still.get("segment") or 0),
             sharpness=float(still.get("sharpness") or 0.0), kind=str(still.get("kind") or ""))
    if not still.get("pose_reliable", True) or "camera" not in still or "intrinsics" not in still:
        v.why_unposed = "no reliable pose"
        return v
    if depth_m is None or depth_m.ndim != 2 or depth_m.size == 0:
        v.why_unposed = "no depth map"
        return v
    v.pose = Pose.from_contract(still["camera"])
    v.cam = Pinhole.from_contract(still["intrinsics"], still.get("width_px"), still.get("height_px"))
    h, w = depth_m.shape
    grid = v.cam.scaled(w, h)
    pc = backproject(depth_m, grid)
    cols = np.linspace(0, w - 1, GRID[0]).round().astype(int)
    rows = np.linspace(0, h - 1, GRID[1]).round().astype(int)
    sample = pc[np.ix_(rows, cols)].reshape(-1, 3)
    z = depth_m[np.ix_(rows, cols)].reshape(-1)
    ok = np.isfinite(z) & (z > MIN_DEPTH_M) & (z < MAX_DEPTH_M)
    if ok.sum() < 6:
        v.why_unposed = "too little valid depth"
        return v
    v.points = v.pose.to_world(sample[ok])
    v.distance_m = float(np.median(np.linalg.norm(v.points - v.pose.t, axis=1)))
    return v


def _axis(v: View) -> np.ndarray:
    return v.pose.r @ np.array([0.0, 0.0, -1.0])


def seen_share(a: View, b: View) -> tuple[float, float]:
    """(share of a's sample points inside b's image and in front of it, median distance ratio b/a to those points)."""
    from repairos.locate.camera import project

    uv, depth = project(a.points, b.pose, b.cam)
    w, h = b.cam.width or 2 * b.cam.cx, b.cam.height or 2 * b.cam.cy
    inside = (depth > MIN_DEPTH_M) & (uv[:, 0] >= 0) & (uv[:, 0] < w) & (uv[:, 1] >= 0) & (uv[:, 1] < h)
    if not inside.any():
        return 0.0, float("inf")
    pts = a.points[inside]
    ratio = float(np.median(np.linalg.norm(pts - b.pose.t, axis=1) / np.maximum(
        np.linalg.norm(pts - a.pose.t, axis=1), 1e-6)))
    return float(inside.mean()), ratio


def angle_deg(a: View, b: View) -> float:
    c = float(np.clip(np.dot(_axis(a), _axis(b)), -1.0, 1.0))
    return float(np.degrees(np.arccos(c)))


def near_duplicate(a: View, b: View) -> bool:
    if not (a.posed and b.posed) or a.segment != b.segment:
        return False
    ab, rab = seen_share(a, b)
    ba, _ = seen_share(b, a)
    return (max(ab, ba) >= OVERLAP_MIN and min(ab, ba) >= OVERLAP_BOTH and 1 / SCALE_MAX <= rab <= SCALE_MAX
            and angle_deg(a, b) <= ANGLE_MAX_DEG)


def is_new_view(v: View, others: Sequence[View]) -> bool:
    """A new viewpoint: no other view sees as much as NEW_VIEW_SHARE of what this one sees. Never dropped by the cap."""
    if not v.posed:
        return True
    return all(o.segment != v.segment or not o.posed or seen_share(v, o)[0] < NEW_VIEW_SHARE for o in others)


def view_distance(a: View, b: View) -> float:
    """How unlike two views are (0 = the same view): little shared content, or a different direction."""
    if not (a.posed and b.posed) or a.segment != b.segment:
        return 2.0
    shared = (seen_share(a, b)[0] + seen_share(b, a)[0]) / 2
    return (1.0 - shared) + angle_deg(a, b) / 180.0


def _share_word(share: float) -> str:
    return "most" if share >= OVERLAP_MIN else "much" if share >= 0.4 else "some" if share > 0 else "little"


def select_stills(stills: Sequence[Mapping[str, Any]], depth_of: Callable[[Mapping[str, Any]], np.ndarray | None],
                  *, max_stills: int, words: Mapping[str, str] | None = None,
                  already: Sequence[Mapping[str, Any]] = ()) -> Selection:
    """The stills to inspect (priority order) and the ones covered by another, with the reason in words.

    `stills` are photo stills not decided yet (labels and skipped stills are handled by the caller); `already` are
    stills of the same scan already kept (being or done inspecting): they stay kept, count towards `max_stills`, and
    new stills are compared with them (a still that arrives during the scan is decided against the ones before it).
    `depth_of(still)` is its depth grid in metres or None; `words` names stills in the reasons (default: the id)."""
    import time

    t0 = time.perf_counter()
    words = words or {}
    name = lambda sid: words.get(sid, f"still {sid}")  # noqa: E731
    fixed = [build_view(s, depth_of(s)) for s in already]
    views = [build_view(s, depth_of(s)) for s in stills]
    posed = [v for v in fixed + views if v.posed]
    median_d = float(np.median([v.distance_m for v in posed])) if posed else 0.0
    closeup = {v.still_id for v in fixed + views if v.kind in CLOSEUP_KINDS or
               (v.posed and median_d > 0 and v.distance_m <= CLOSEUP_FACTOR * median_d)}
    # Deliberate close-ups first, then the sharpest: within a group of near-duplicates (same scale by definition) the
    # sharpest still is the one inspected. A still much sharper than the kept near-duplicate (SHARPER_BY) is kept too:
    # that happens only against a still kept earlier (`already`), which is inspected by then.
    order = sorted(views, key=lambda v: (v.kind not in CLOSEUP_KINDS, -v.sharpness, v.still_id))
    kept: list[View] = list(fixed)
    covered: dict[str, dict[str, str]] = {}
    for v in order:
        dup = max((k for k in kept if near_duplicate(k, v)), key=lambda k: k.sharpness, default=None)
        if dup is None or v.sharpness >= SHARPER_BY * max(dup.sharpness, 1e-9):
            kept.append(v)
        else:
            covered[v.still_id] = {"by": dup.still_id, "why": (
                f"covered by {name(dup.still_id)}, a near-identical view (same place, distance and direction) that "
                f"is {'as sharp or sharper' if dup.sharpness >= v.sharpness else 'nearly as sharp'}")}
    if len(kept) > max_stills:
        must = [v for n, v in enumerate(kept)   # kept is in decision order: `already` first, then by priority
                if v in fixed or v.still_id in closeup or is_new_view(v, kept[:n])]
        rest = [v for v in kept if v not in must]
        picked = list(must)
        room = max(0, max_stills - len(picked))
        while rest and room:
            best = max(rest, key=lambda v: (min((view_distance(v, p) for p in picked), default=2.0), v.sharpness))
            picked.append(best)
            rest.remove(best)
            room -= 1
        picked_ids = {p.still_id for p in picked}

        def nearest(v: View) -> tuple[View | None, float]:
            comparable = [p for p in picked if p.posed and p.segment == v.segment]
            if not comparable:
                return (picked[0] if picked else None), 0.0
            by = max(comparable, key=lambda p: seen_share(v, p)[0])
            return by, seen_share(v, by)[0]

        for v in rest:
            by, share = nearest(v)
            covered[v.still_id] = {"by": by.still_id if by else "", "why": (
                "not inspected to keep the report fast: this scan has more different views than the per-scan limit"
                + (f"; {name(by.still_id)} shows {_share_word(share)} of this view" if by else ""))}
        by_id = {v.still_id: v for v in views}
        for sid, c in list(covered.items()):   # a near-duplicate of a view the cap dropped: name an inspected view
            if c["by"] in picked_ids or sid not in by_id or c["by"] not in by_id:
                continue
            by, share = nearest(by_id[sid])
            covered[sid] = {"by": by.still_id if by else "", "why": (
                f"not inspected to keep the report fast: it nearly repeats {name(c['by'])}, which was over the "
                "per-scan limit of views too" + (f"; {name(by.still_id)} shows {_share_word(share)} of this view"
                                                 if by else ""))}
        kept = [v for v in kept if v.still_id in picked_ids]
    new = [v for v in kept if v not in fixed]
    return Selection(kept=[v.still_id for v in new], covered=covered,
                     closeups=[v.still_id for v in new if v.still_id in closeup],
                     unposed={v.still_id: v.why_unposed for v in views if not v.posed},
                     seconds=time.perf_counter() - t0)
