"""Tiling and merging used by every DET implementation (pure functions, no model)."""
from __future__ import annotations

import pytest

from repairos.vision.tiling import iou, nms, prompt_spans, tile_grid, tiled_detect, touches_inner_edge


def test_tiles_cover_a_12mp_still_with_overlap():
    W, H = 4032, 3024
    tiles = tile_grid(W, H, 1600, 0.2)
    assert len(tiles) == 3 * 3
    assert all(t[2] - t[0] == 1600 and t[3] - t[1] == 1600 for t in tiles)
    assert {t[0] for t in tiles} == {0, 1216, 2432} and max(t[2] for t in tiles) == W and max(t[3] for t in tiles) == H
    xs = sorted({t[0] for t in tiles})
    assert all(b - a <= 1600 * 0.8 + 1 for a, b in zip(xs, xs[1:]))  # overlap at least 20 %


def test_small_image_is_one_tile():
    assert tile_grid(800, 600, 1600, 0.2) == [(0, 0, 800, 600)]
    with pytest.raises(ValueError):
        tile_grid(10, 10, 5, 1.0)


def test_iou_and_nms():
    a, b, c = (0, 0, 10, 10), (1, 1, 11, 11), (50, 50, 60, 60)
    assert iou(a, a) == 1 and iou(a, c) == 0 and 0.6 < iou(a, b) < 0.7
    kept = nms([(a, "x", 0.9), (b, "x", 0.8), (b, "y", 0.7), (c, "x", 0.5)], 0.5, per_label=True)
    assert [(k[1], k[2]) for k in kept] == [("x", 0.9), ("y", 0.7), ("x", 0.5)]
    kept = nms(kept, 0.5, per_label=False)
    assert [(k[1], k[2]) for k in kept] == [("x", 0.9), ("x", 0.5)]


def test_inner_edge():
    tile = (1000, 0, 2000, 1000)
    assert touches_inner_edge((1000, 10, 1100, 100), tile, 3000, 2000)       # left edge is inside the image
    assert not touches_inner_edge((1100, 0, 1200, 100), tile, 3000, 2000)    # top edge is the image edge
    assert touches_inner_edge((1100, 900, 1200, 999), tile, 3000, 2000)


def test_prompt_spans():
    # [CLS] wire . cap ##ac ##itor . [SEP]
    assert prompt_spans([101, 7, 9, 3, 4, 5, 9, 102], sep_id=9, n_prompts=2) == [[1], [3, 4, 5]]
    with pytest.raises(ValueError):
        prompt_spans([101, 7, 9, 102], sep_id=9, n_prompts=2)


def test_tiled_detect_maps_tile_boxes_to_still_pixels_and_merges():
    W, H = 3000, 2000
    calls = []

    def run(images):
        calls.append([im for im in images])
        out = []
        for tile in images:
            if tile == (0, 0, W, H):          # whole-still view: one big part
                out.append([((100, 100, 1400, 1400), "big", 0.6)])
            else:                              # every tile sees a small part at its own (10..40) and a tile-filler
                out.append([((10, 10, 40, 40), "small", 0.5), ((0, 0, tile[2] - tile[0], tile[3] - tile[1]), "blur", 0.9)])
        return out

    dets, views = tiled_detect((W, H), run, tile_px=1600, overlap=0.2, use_tiles=True, batch_size=2, nms_iou=0.5,
                               cross_label_iou=0.6, max_detections=50, crop=lambda t: t)
    tiles = tile_grid(W, H, 1600, 0.2)
    assert views == 1 + len(tiles)
    labels = [d[1] for d in dets]
    assert "blur" not in labels                                   # tile-filling boxes are dropped
    assert labels.count("big") == 1
    assert {(d[0][0], d[0][1]) for d in dets if d[1] == "small"} == {(t[0] + 10, t[1] + 10) for t in tiles}
