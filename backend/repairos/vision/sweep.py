"""Close tiles over a large object box, and look-alike groups for an odd-one-out comparison. Geometry only: no
detector label meaning, no part lists, nothing about the product.

Why (the 2026-10-24 public-dataset eval, PLAN.md §10): most defects that were missed sat inside a
crop the vision model saw and called ok, because one crop showed a whole board (too much to scan) or because the model
judged only the main part of a part crop. Two generic remedies:
- tiles: the largest object box (a board, a panel, a housing) is cut into a few overlapping close tiles, each judged
  for any fault anywhere in it (vision/inspect.py, VLM `view="tile"`);
- look-alikes: detector boxes with the same label and nearly the same size (pins, legs, identical parts in a row) are
  shown side by side in one numbered strip and the model says which differ (VLM `compare_parts`).

Sizes are in millimetres when the still has a scale (vision/scale.PixelScale from depth + intrinsics), else relative to
the still, as before.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from repairos.vision.tiling import iou

Box = tuple[float, float, float, float]

LARGE_BOX_MM = 30.0       # a box this long (long side) or longer is swept with tiles (a hand-sized board or panel)
LARGE_BOX_SIDE = 0.3      # without a scale: box_side (sqrt of the area share of the still) from which a box is large
TILE_MM = 20.0            # target tile side with a scale: a tile shows about a 2 cm square
TILE_OVERLAP = 0.15       # neighbouring tiles share this share of their side (a fault on a seam is in both)
MAX_TILES = 4             # tiles per still at most (they come out of the still's VLM budget)
MIN_TILE_PX = 256         # never smaller than the smallest crop (vision/crops.DEFAULT_MIN_SIDE)

LOOKALIKE_SIZE_RATIO = 1.35   # each side of every member within this factor of the group's median side
LOOKALIKE_MAX_IOU = 0.3       # members are separate things, not two boxes on one thing
MIN_GROUP, MAX_GROUP = 2, 8   # a strip compares two to eight look-alikes
STRIP_PAD_FRAC = 0.35         # context around each member in its strip cell (share of its long side)
STRIP_CELL_MAX, STRIP_CELL_MIN = 384, 96   # cell side in strip pixels: the smallest member's own size, in this range
STRIP_COLS = 4


def long_side(box: Sequence[float]) -> float:
    return max(box[2] - box[0], box[3] - box[1])


def is_large(box: Sequence[float], width: int, height: int, px_per_mm: float | None) -> bool:
    """A box swept with tiles: at least LARGE_BOX_MM long with a scale, else at least LARGE_BOX_SIDE of the still."""
    if px_per_mm:
        return long_side(box) / px_per_mm >= LARGE_BOX_MM
    area = max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])
    return width * height > 0 and (area / (width * height)) ** 0.5 >= LARGE_BOX_SIDE


def _spans(lo: float, hi: float, n: int, side: float) -> list[tuple[float, float]]:
    """n windows of length `side` evenly spread over [lo, hi] (first starts at lo, last ends at hi)."""
    if n <= 1 or hi - lo <= side:
        c = (lo + hi) / 2
        return [(c - side / 2, c + side / 2)]
    step = (hi - lo - side) / (n - 1)
    return [(lo + k * step, lo + k * step + side) for k in range(n)]


def _count(length: float, side: float) -> int:
    if length <= side:
        return 1
    return math.ceil((length - TILE_OVERLAP * side) / ((1 - TILE_OVERLAP) * side))


def tile_boxes(box: Sequence[float], width: int, height: int, px_per_mm: float | None,
               max_tiles: int = MAX_TILES) -> list[Box]:
    """Square tiles covering `box` (still pixels), clipped to the still, row by row. The tile side is TILE_MM with a
    scale, else half the box's long side; never under MIN_TILE_PX; grown until at most `max_tiles` tiles cover it."""
    x0, y0, x1, y1 = (max(0.0, float(box[0])), max(0.0, float(box[1])), min(float(width), float(box[2])),
                      min(float(height), float(box[3])))
    bw, bh = x1 - x0, y1 - y0
    if bw <= 0 or bh <= 0 or max_tiles <= 0:
        return []
    side = TILE_MM * px_per_mm if px_per_mm else max(bw, bh) / 2
    side = max(side, float(MIN_TILE_PX))
    while _count(bw, side) * _count(bh, side) > max_tiles:
        side *= 1.1
    side_x, side_y = min(side, float(width)), min(side, float(height))
    out = []
    for ty0, ty1 in _spans(y0, y1, _count(bh, side), side_y):
        for tx0, tx1 in _spans(x0, x1, _count(bw, side), side_x):
            sx0 = min(max(tx0, 0.0), width - side_x)
            sy0 = min(max(ty0, 0.0), height - side_y)
            out.append((sx0, sy0, sx0 + side_x, sy0 + side_y))
    return out


def largest_large_box(dets: Sequence[Any], indices: Sequence[int], width: int, height: int,
                      px_per_mm_of=None) -> int | None:
    """The index (among `indices`) of the largest box that is large (is_large), or None."""
    best, best_area = None, 0.0
    for i in indices:
        b = dets[i].box
        s = px_per_mm_of(b) if px_per_mm_of else None
        if not is_large(b, width, height, s):
            continue
        a = (b[2] - b[0]) * (b[3] - b[1])
        if a > best_area:
            best, best_area = i, a
    return best


@dataclass(frozen=True)
class LookalikeGroup:
    label: str
    members: tuple[int, ...]       # detection indices, left to right then top to bottom (the strip's numbering)


def lookalike_groups(dets: Sequence[Any], indices: Sequence[int]) -> list[LookalikeGroup]:
    """Groups of boxes with the same detector label and nearly the same width and height (LOOKALIKE_SIZE_RATIO),
    that are separate things (overlap under LOOKALIKE_MAX_IOU), largest group first. At most MAX_GROUP members (the
    detector's strongest), at least MIN_GROUP."""
    by_label: dict[str, list[int]] = {}
    for i in indices:
        by_label.setdefault(dets[i].label, []).append(i)
    groups = []
    for label, idx in by_label.items():
        idx = sorted(idx, key=lambda i: -dets[i].score)
        while len(idx) >= MIN_GROUP:
            ws = sorted(dets[i].box[2] - dets[i].box[0] for i in idx)
            hs = sorted(dets[i].box[3] - dets[i].box[1] for i in idx)
            mw, mh = ws[len(ws) // 2], hs[len(hs) // 2]
            fit = [i for i in idx if _within(dets[i].box[2] - dets[i].box[0], mw)
                   and _within(dets[i].box[3] - dets[i].box[1], mh)]
            kept: list[int] = []
            for i in fit:
                if all(iou(dets[i].box, dets[k].box) < LOOKALIKE_MAX_IOU for k in kept):
                    kept.append(i)
            kept = kept[:MAX_GROUP]
            if len(kept) >= MIN_GROUP:
                order = sorted(kept, key=lambda i: (round(dets[i].box[1] / max(1.0, mh)), dets[i].box[0]))
                groups.append(LookalikeGroup(label, tuple(order)))
            rest = [i for i in idx if i not in fit]
            if len(rest) == len(idx):
                break
            idx = rest
    groups.sort(key=lambda g: (-len(g.members), -sum(dets[i].score for i in g.members)))
    return groups


def _within(v: float, ref: float) -> bool:
    return ref > 0 and v > 0 and max(v, ref) / min(v, ref) <= LOOKALIKE_SIZE_RATIO


def strip_cell_box(box: Sequence[float], width: int, height: int) -> Box:
    """The square still region shown for one member: the box plus STRIP_PAD_FRAC of its long side around it."""
    side = long_side(box) * (1 + 2 * STRIP_PAD_FRAC)
    side = min(side, float(width), float(height))
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    x0 = min(max(cx - side / 2, 0.0), width - side)
    y0 = min(max(cy - side / 2, 0.0), height - side)
    return (x0, y0, x0 + side, y0 + side)


def strip_image(still: Any, boxes: Sequence[Sequence[float]]) -> tuple[Any, float]:
    """(one image with each member's region in a numbered cell, cell pixels per still pixel). All cells have one size:
    the smallest member region's own pixel size within [STRIP_CELL_MIN, STRIP_CELL_MAX] (cells are downsized to it;
    upscaled only up to STRIP_CELL_MIN), so the members are compared at the same scale."""
    from PIL import Image, ImageDraw, ImageFont

    from repairos.vision.crops import as_pil

    im = as_pil(still)
    regions = [strip_cell_box(b, im.width, im.height) for b in boxes]
    native = min(r[2] - r[0] for r in regions)
    cell = int(max(STRIP_CELL_MIN, min(STRIP_CELL_MAX, native)))
    label_h = max(18, cell // 8)
    cols = min(STRIP_COLS, len(boxes))
    rows = math.ceil(len(boxes) / cols)
    gap = 8
    out = Image.new("RGB", (cols * cell + (cols - 1) * gap, rows * (cell + label_h) + (rows - 1) * gap), (255, 255, 255))
    d = ImageDraw.Draw(out)
    try:
        font = ImageFont.load_default(size=max(12, label_h - 4))
    except TypeError:   # Pillow < 10.1: fixed-size bitmap font
        font = ImageFont.load_default()
    for k, r in enumerate(regions):
        piece = im.crop(tuple(int(round(v)) for v in r)).resize((cell, cell), Image.Resampling.LANCZOS)
        cx, cy = (k % cols) * (cell + gap), (k // cols) * (cell + label_h + gap)
        out.paste(piece, (cx, cy))
        d.text((cx + cell // 2, cy + cell + label_h // 2), str(k + 1), fill=(0, 0, 0), font=font, anchor="mm")
    scale = cell / max(1.0, native)
    return out, scale
