"""Tiling and box merging for any DET implementation on 12 MP stills. Pure Python (no torch), unit-tested anywhere.

A 4032 x 3024 still resized to the detector's 800 px input loses parts smaller than ~25 px. So detection runs on
one whole-still view (large parts) plus overlapping full-resolution tiles (small parts). A tile box that touches an
inner tile edge is a cut-off part and is dropped: the overlap or the whole-still view sees it whole. Boxes from all
views are merged with per-label NMS, then near-duplicate boxes across labels keep the higher score.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

Box = tuple[float, float, float, float]


def tile_grid(width: int, height: int, tile_px: int, overlap: float) -> list[tuple[int, int, int, int]]:
    """Overlapping tiles [x0, y0, x1, y1) covering the image; one tile when the image fits in `tile_px`."""
    if not 0 <= overlap < 1:
        raise ValueError("overlap must be in [0, 1)")

    def starts(n: int) -> list[int]:
        if n <= tile_px:
            return [0]
        step = tile_px * (1 - overlap)
        count = math.ceil((n - tile_px) / step) + 1
        return [round(i * (n - tile_px) / (count - 1)) for i in range(count)]

    tw, th = min(tile_px, width), min(tile_px, height)
    return [(x, y, x + tw, y + th) for y in starts(height) for x in starts(width)]


def iou(a: Box, b: Box) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def nms(items: Sequence[tuple[Box, str, float]], iou_max: float, per_label: bool = True) -> list[tuple[Box, str, float]]:
    """Greedy NMS by score. `per_label`: only boxes with the same label suppress each other."""
    kept: list[tuple[Box, str, float]] = []
    for it in sorted(items, key=lambda t: -t[2]):
        if all(not ((not per_label or k[1] == it[1]) and iou(k[0], it[0]) > iou_max) for k in kept):
            kept.append(it)
    return kept


def touches_inner_edge(box: Box, tile: tuple[int, int, int, int], width: int, height: int, margin: float = 2.0) -> bool:
    """True when the box reaches a tile edge that is not also an image edge (the part continues past the tile)."""
    x0, y0, x1, y1 = tile
    return ((x0 > 0 and box[0] <= x0 + margin) or (y0 > 0 and box[1] <= y0 + margin)
            or (x1 < width and box[2] >= x1 - margin) or (y1 < height and box[3] >= y1 - margin))


def clamp_box(box: Box, width: int, height: int) -> Box:
    return (min(max(box[0], 0.0), width), min(max(box[1], 0.0), height),
            min(max(box[2], 0.0), width), min(max(box[3], 0.0), height))


def prompt_spans(token_ids: Sequence[int], sep_id: int, n_prompts: int) -> list[list[int]]:
    """Token positions of each prompt in '[CLS] p1 . p2 . ... pn . [SEP]' (separator token `sep_id`)."""
    spans: list[list[int]] = []
    cur: list[int] = []
    for pos, tid in enumerate(token_ids[1:], start=1):
        if tid == sep_id:
            spans.append(cur)
            cur = []
            if len(spans) == n_prompts:
                break
        else:
            cur.append(pos)
    if len(spans) != n_prompts or any(not s for s in spans):
        raise ValueError(f"prompt/token mismatch: {len(spans)} spans for {n_prompts} prompts "
                         "(a prompt may contain '.' or the text was truncated)")
    return spans


def tiled_detect(image_size: tuple[int, int], run, *, tile_px: int, overlap: float, use_tiles: bool, batch_size: int,
                 nms_iou: float, cross_label_iou: float, max_detections: int, crop,
                 max_tile_fill: float = 0.5) -> tuple[list[tuple[Box, str, float]], int]:
    """Run a detector over the whole view plus overlapping tiles and merge.

    `run(images) -> per image [(box in that image's pixels, label, score)]`; `crop(tile) -> image` cuts a tile from
    the full-resolution still. A tile box filling more than `max_tile_fill` of its tile is dropped: large things are
    the whole-still view's job, and zero-shot detectors put tile-sized boxes on blur. Returns (merged detections in
    still pixels, number of views)."""
    W, H = image_size
    tiles = tile_grid(W, H, tile_px, overlap) if use_tiles else []
    if len(tiles) == 1:
        tiles = []  # the whole-still view already is the only tile
    found: list[tuple[Box, str, float]] = list(run([crop((0, 0, W, H))])[0])
    for i in range(0, len(tiles), batch_size):
        batch = tiles[i:i + batch_size]
        for tile, dets in zip(batch, run([crop(t) for t in batch])):
            for box, label, score in dets:
                full = (box[0] + tile[0], box[1] + tile[1], box[2] + tile[0], box[3] + tile[1])
                tile_area = (tile[2] - tile[0]) * (tile[3] - tile[1])
                fill = (box[2] - box[0]) * (box[3] - box[1]) / tile_area if tile_area else 1.0
                if fill <= max_tile_fill and not touches_inner_edge(full, tile, W, H):
                    found.append((full, label, score))
    found = [(clamp_box(b, W, H), lab, s) for b, lab, s in found]
    found = [f for f in found if f[0][2] - f[0][0] >= 1 and f[0][3] - f[0][1] >= 1]
    merged = nms(nms(found, nms_iou, per_label=True), cross_label_iou, per_label=False)
    return merged[:max_detections], 1 + len(tiles)
