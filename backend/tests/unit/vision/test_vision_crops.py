"""Full-resolution crop rules (M.SEG.0 crops, V3.5): cut from the original still, transforms back to still pixels.

The test images are coordinate gradients (pixel value encodes its position), not product photos."""
from __future__ import annotations

import pytest

from repairos.vision.crops import crop_region

np = pytest.importorskip("numpy")
PIL = pytest.importorskip("PIL")


def gradient(w: int, h: int):
    y, x = np.mgrid[0:h, 0:w]
    return np.stack([x % 256, y % 256, (x // 256 + 16 * (y // 256)) % 256], axis=-1).astype(np.uint8)


def test_region_is_padded_square_and_inside():
    x0, y0, x1, y1 = crop_region((2000, 1500, 2040, 1520), 4032, 3024)
    side = x1 - x0
    assert side == y1 - y0 == 256                                   # min side for a tiny part
    assert x0 <= 2000 and x1 >= 2040 and y0 <= 1500 and y1 >= 1520
    big = crop_region((1000, 1000, 1400, 1200), 4032, 3024)
    assert big[2] - big[0] == big[3] - big[1] == round(400 + 2 * 40 + 2 * 0.15 * 400)


def test_region_shifts_into_the_still_not_truncated():
    r = crop_region((0, 0, 30, 30), 4032, 3024)
    assert r[0] == 0 and r[1] == 0 and r[2] - r[0] == r[3] - r[1] == 256
    r = crop_region((4000, 3000, 4032, 3024), 4032, 3024)
    assert r[2] == 4032 and r[3] == 3024 and r[2] - r[0] == 256
    r = crop_region((0, 0, 4032, 3024), 4032, 3024)                 # bigger than the still: clamped
    assert r == (0, 0, 4032, 3024)


def test_pad_scales_with_still_width():
    small = crop_region((100, 100, 300, 300), 1008, 756, min_side=1)
    assert small[2] - small[0] == round(200 + 2 * 10 + 2 * 0.15 * 200)


def test_empty_box_rejected():
    with pytest.raises(ValueError):
        crop_region((10, 10, 10, 20), 100, 100)


def test_cut_is_full_resolution_and_maps_back_exactly():
    from repairos.vision.crops import cut

    still = gradient(4032, 3024)
    c = cut(still, (3100, 2200, 3160, 2240), still_id="s1")
    assert c.scale == 1.0 and c.image.size == (256, 256)            # small crop: original pixels, never resized
    px = np.asarray(c.image)
    for u, v in [(0, 0), (17, 200), (255, 255)]:
        x, y = c.to_still(u, v)
        assert (px[v, u] == still[int(y), int(x)]).all()
    assert c.from_still(*c.to_still(12.5, 40.0)) == pytest.approx((12.5, 40.0))
    bx = c.box_in_crop()
    assert c.box_to_still(bx) == pytest.approx(c.box_px)


def test_large_crop_downsized_only_the_crop():
    from repairos.vision.crops import cut

    still = gradient(4032, 3024)
    c = cut(still, (500, 500, 2500, 2500), max_side=1024)
    side = c.region_px[2] - c.region_px[0]
    assert side > 1024 and c.scale == pytest.approx(1024 / side) and max(c.image.size) == 1024
    assert c.to_still(0, 0) == (c.region_px[0], c.region_px[1])
    assert c.to_still(1024, 1024) == pytest.approx((c.region_px[2], c.region_px[3]), abs=1)


def test_context_view_is_context_only():
    from repairos.vision.crops import context_view

    v = context_view(gradient(4032, 3024), (100, 100, 400, 400), max_side=1024)
    assert max(v.size) == 1024
