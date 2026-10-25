"""Raised parts on a thin object's slab (repairos.recreation.relief), on synthetic posed renders: a textured 50 x 40 mm
board with three boxes of known height, seen by one near top-down and four oblique cameras (ray-marched), must come
back with those heights within 1.5 mm, a flat board round them, and blocks in the mesh."""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("torch")

from repairos.locate.camera import Pinhole, Pose  # noqa: E402
from repairos.recreation import flat as F  # noqa: E402
from repairos.recreation import relief as R  # noqa: E402

TOP_Y = 0.0016
BOXES = [  # x0, x1, z0, z1 (m), height above the board top (m)
    (-0.018, -0.008, -0.012, -0.004, 0.004),
    (-0.002, 0.012, 0.004, 0.008, 0.0085),
    (0.006, 0.020, -0.014, -0.004, 0.011),
]


def _height(x: np.ndarray, z: np.ndarray) -> np.ndarray:
    h = np.where((np.abs(x) <= 0.025) & (np.abs(z) <= 0.02), TOP_Y, 0.0)
    for x0, x1, z0, z1, bh in BOXES:
        h = np.where((x >= x0) & (x <= x1) & (z >= z0) & (z <= z1), TOP_Y + bh, h)
    return h


def _texture(x: np.ndarray, y: np.ndarray, z: np.ndarray) -> np.ndarray:
    rng = np.random.default_rng(7)
    lut = rng.integers(20, 235, size=(4096,))
    i = np.floor(x / 0.0004).astype(np.int64) * 73856093 ^ np.floor(z / 0.0004).astype(np.int64) * 19349663
    j = np.floor(y / 0.0004).astype(np.int64) * 83492791
    return lut[(i ^ j) % 4096].astype(np.uint8)


def _look(eye: np.ndarray, target: np.ndarray, up: np.ndarray) -> Pose:
    f = target - eye
    f /= np.linalg.norm(f)
    x = np.cross(f, up)
    x /= np.linalg.norm(x)
    y = np.cross(x, f)
    return Pose(np.stack([x, y, -f], 1), eye)


def _render(pose: Pose, cam: Pinhole, w: int, h: int) -> np.ndarray:
    u, v = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    d = np.stack([(u - cam.cx) / cam.fx, -(v - cam.cy) / cam.fy, -np.ones_like(u)], -1).reshape(-1, 3) @ pose.r.T
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    top = TOP_Y + max(b[4] for b in BOXES) + 0.001
    t = (top - pose.t[1]) / d[:, 1]
    hit = np.full(len(d), np.nan)
    step = 0.0001
    for _ in range(int((top + 0.002) / step / 0.5) + 400):
        p = pose.t + t[:, None] * d
        below = (p[:, 1] <= _height(p[:, 0], p[:, 2])) & np.isnan(hit)
        hit[below] = t[below]
        t = np.where(np.isnan(hit), t + step / np.maximum(-d[:, 1], 0.3), t)
        if not np.isnan(hit).any() or (p[:, 1] < -0.001).all():
            break
    t = np.where(np.isnan(hit), t, hit)
    p = pose.t + t[:, None] * d
    g = _texture(p[:, 0], p[:, 1], p[:, 2]).reshape(h, w)
    return np.repeat(g[..., None], 3, -1)


@pytest.fixture(scope="module")
def views() -> list:
    w, h = 640, 480
    cam = Pinhole(1800.0, 1800.0, w / 2, h / 2, w, h)
    target = np.array([0.0, TOP_Y, 0.0])
    eyes = [np.array([0.0, 0.2, 0.02])]
    for az in (20, 110, 200, 290):
        a = np.radians(az)
        eyes.append(np.array([0.12 * np.cos(a), 0.17, 0.12 * np.sin(a)]))
    out = []
    for k, e in enumerate(eyes):
        pose = _look(e, target, np.array([0.0, 0.0, -1.0]) if k == 0 else np.array([0.0, 1.0, 0.0]))
        img = _render(pose, cam, w, h)
        out.append(F.Still(f"s{k}", img, pose, cam, float(-(-pose.r[:, 2])[1]), 1000.0))
    return out


def test_box_heights_recovered(views: list) -> None:
    grid = F.Grid(np.array([-0.025, -0.02]), np.array([1.0, 0.0]), np.array([0.0, 1.0]), R.RES_M, (160, 200))
    rel = R.relief_heights(views[0], views[1:], grid, TOP_Y)
    assert len(rel.stats["views"]) >= 4
    h = R.clean(rel, np.ones(grid.shape, bool))
    # the heights are on the top still's rays: compare each box's middle, away from its edges
    c = views[0].pose.t
    for x0, x1, z0, z1, bh in BOXES:
        top = TOP_Y + bh
        s = (TOP_Y - c[1]) / (top - c[1])          # a point on the box top seen at this grid point
        pts = np.array([[x, z] for x in np.linspace(x0 + 0.0015, x1 - 0.0015, 5)
                        for z in np.linspace(z0 + 0.0012, z1 - 0.0012, 5)])
        grid_xz = c[[0, 2]] + s * (pts - c[[0, 2]])
        rc = np.floor(grid.to_pixel(grid_xz)).astype(int)
        got = float(np.median(h[rc[:, 0], rc[:, 1]]))
        assert abs(got - bh) <= 0.0015, (bh, got)
    # the board round the boxes stays flat
    flat = np.ones(grid.shape, bool)
    for x0, x1, z0, z1, _ in BOXES:
        rc0 = np.floor(grid.to_pixel(np.array([[x0 - 0.006, z0 - 0.006]]))).astype(int)[0]
        rc1 = np.ceil(grid.to_pixel(np.array([[x1 + 0.006, z1 + 0.006]]))).astype(int)[0]
        flat[max(rc0[0], 0):rc1[0], max(rc0[1], 0):rc1[1]] = False
    assert np.mean(h[flat] < R.RAISED_M) > 0.97


def test_blocks_in_the_slab(views: list) -> None:
    outline = np.array([[-0.025, -0.02], [0.025, -0.02], [0.025, 0.02], [-0.025, 0.02]])
    tex = F.Grid(outline[0], np.array([1.0, 0.0]), np.array([0.0, 1.0]), 0.0001, (400, 500))
    k = len(outline)
    top = np.c_[outline[:, 0], np.full(k, TOP_Y), outline[:, 1]]
    uv = np.c_[(outline[:, 0] + 0.025) / 0.05, (outline[:, 1] + 0.02) / 0.0408]
    from PIL import Image

    m = F.FlatModel(top.astype(np.float32), uv.astype(np.float32), np.array([[0, 2, 1], [0, 3, 2]], np.int32),
                    Image.new("RGB", (500, 408)), outline, {})
    m = R.add_relief(m, views, tex, 0.0, None)
    assert m.relief is not None, m.stats.get("relief")
    assert len(m.faces) > 2 and len(m.faces) <= R.MAX_TRIANGLES
    assert float(m.vertices[:, 1].max()) == pytest.approx(TOP_Y + 0.011, abs=0.0015)
    assert m.vertices[:, 1].min() >= TOP_Y - 1e-6
    assert len(m.uv) == len(m.vertices)
