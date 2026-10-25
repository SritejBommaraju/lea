"""Thin objects as a photo-textured slab (repairos.recreation.flat), on synthetic stills: a dark 69 x 53 mm board
with markings on a wood-grain table, photographed from above at full resolution with known poses."""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from repairos.locate.camera import Pinhole, Pose, pixel_rays
from repairos.recon.tsdf import ObjectBox
from repairos.recreation import flat as F

TABLE_Y = 0.0
BOARD = (np.array([-0.0345, -0.0267]), np.array([0.0345, 0.0267]))     # x, z half extents of a 69 x 53.4 mm board
THICK = 0.0016


BUSY = {"on": False}   # a busy top: rows of parts the table's own colour cross the board (pins, a wooden-looking jack)


def colour(xz: np.ndarray) -> np.ndarray:
    """Wood grain (orange-brown stripes) on the table, a dark board with light markings."""
    x, z = xz[:, 0], xz[:, 1]
    wood = np.stack([170 + 30 * np.sin(z * 900), 95 + 20 * np.sin(z * 900 + 1), 40 + 10 * np.sin(z * 400)], 1)
    on = (x > BOARD[0][0]) & (x < BOARD[1][0]) & (z > BOARD[0][1]) & (z < BOARD[1][1])
    board = np.tile([30.0, 40.0, 50.0], (len(x), 1))
    mark = on & (np.abs(((x * 1000) % 6) - 3) < 0.6) & (z > 0.0)            # stripes of "text"
    board[mark] = [210, 210, 210]
    if BUSY["on"]:   # 2.5 mm rows every 12 mm, in the table's colours: they cut the board into strips
        rows = on & (np.abs(((x * 1000) % 12) - 6) < 1.25)
        board[rows] = wood[rows]
    return np.where(on[:, None], board, wood)


def still(name: str, eye, target=(0.0, 0.0, 0.0), w=1600, h=1200, f=1500.0) -> tuple[dict, np.ndarray]:
    eye, target = np.asarray(eye, float), np.asarray(target, float)
    back = eye - target
    back /= np.linalg.norm(back)
    right = np.cross([0.0, 0.0, -1.0], back)
    right /= np.linalg.norm(right)
    pose = Pose(np.stack([right, np.cross(back, right), back], axis=1), eye)
    cam = Pinhole(f, f, w / 2, h / 2, w, h)
    u, v = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    o, d = pixel_rays(np.stack([u.ravel(), v.ravel()], 1), pose, cam)
    t = (TABLE_Y - o[1]) / d[:, 1]
    hit = o + t[:, None] * d
    t2 = (TABLE_Y + THICK - o[1]) / d[:, 1]                                # the board's top, 1.6 mm up
    hit2 = o + t2[:, None] * d
    on2 = ((hit2[:, 0] > BOARD[0][0]) & (hit2[:, 0] < BOARD[1][0])
           & (hit2[:, 2] > BOARD[0][1]) & (hit2[:, 2] < BOARD[1][1]))
    xz = np.where(on2[:, None], hit2[:, [0, 2]], hit[:, [0, 2]])
    img = colour(xz).reshape(h, w, 3).clip(0, 255).astype(np.uint8)
    return {"still_id": name, "camera": pose.to_contract(),
            "intrinsics": {"fx": f, "fy": f, "cx": w / 2, "cy": h / 2}, "pose_reliable": True, "sharpness": 1.0,
            "width_px": w, "height_px": h}, img


@pytest.fixture
def scene(tmp_path):
    stills = []
    for k, eye in enumerate([(0.004, 0.25, 0.003), (-0.03, 0.24, 0.05)]):
        s, img = still(f"s{k}", eye)
        p = tmp_path / f"s{k}.jpg"
        Image.fromarray(img).save(p, quality=95)
        stills.append({**s, "image_file": str(p)})
    box = ObjectBox.from_contract({"center_m": [0.0, 0.02, 0.0], "extent_m": [0.2, 0.04, 0.24],
                                   "quaternion_xyzw": [0, 0, 0, 1]})
    # the LiDAR saw something over the board (its raised parts), sparsely
    rng = np.random.default_rng(0)
    lidar = np.stack([rng.uniform(-0.03, 0.03, 400), rng.uniform(0.002, 0.008, 400),
                      rng.uniform(-0.022, 0.022, 400)], 1)
    return stills, box, lidar


def test_a_thin_board_becomes_a_measured_textured_slab(scene):
    stills, box, lidar = scene
    m = F.flat_model({}, stills, box, TABLE_Y, lidar, size_prior_mm=(68.6, 53.4))
    a, b = m.stats["measured_mm"]
    assert abs(a - 53.4) < 2 and abs(b - 69.0) < 2                   # measured, not the prior
    assert m.stats["shape"] == "rectangle" and m.stats["second_still_shift_mm"] < 1.5
    assert np.isclose(m.vertices[:, 1].min(), TABLE_Y) and m.vertices[:, 1].max() > TABLE_Y + 0.0014
    top = np.asarray(m.atlas)[:-8]
    assert top.mean() < 120                                           # the board's dark top, not the wood
    assert (top.max(2) > 180).mean() > 0.02                           # with its markings


def test_photos_that_disagree_are_refused(scene):
    stills, box, lidar = scene
    bad = dict(stills[1])
    cam = dict(bad["camera"])
    cam["position_m"] = [cam["position_m"][0] + 0.025, cam["position_m"][1], cam["position_m"][2]]   # 2.5 cm off
    bad["camera"] = cam
    with pytest.raises(F.FlatUnavailable, match="apart"):
        F.flat_model({}, [stills[0], bad], box, TABLE_Y, lidar)


def test_thin_means_close_to_the_table():
    pts = np.c_[np.zeros(500), np.linspace(0.0, 0.012, 500), np.zeros(500)]
    assert F.thin_object(pts, 0.0)[0]
    assert not F.thin_object(pts * [1, 5, 1], 0.0)[0]                 # 6 cm tall: the LiDAR mesh as usual
    assert F.thin_object(pts[:50], 0.0)[0]                            # almost nothing above the table
    tall = np.c_[np.zeros(100), np.linspace(0.03, 0.035, 100), np.zeros(100)]   # a cable, a tall part: a fifth
    assert F.thin_object(np.vstack([pts[:400] * [1, 0.5, 1], tall]), 0.0)[0]


def _stills(tmp_path, eyes, offset=None):
    out = []
    for k, eye in enumerate(eyes):
        s, img = still(f"s{k}", eye)
        p = tmp_path / f"s{k}.jpg"
        Image.fromarray(img).save(p, quality=95)
        s = {**s, "image_file": str(p)}
        if offset is not None and k == offset:   # this photo's recorded pose is 2.5 cm off, along z: the board's
            # markings repeat every 6 mm along x, so a shift along x is ambiguous (phase correlation finds 7 mm)
            cam = dict(s["camera"])
            cam["position_m"] = [*cam["position_m"][:2], cam["position_m"][2] + 0.025]
            s["camera"] = cam
        out.append(s)
    return out


EYES = [(0.004, 0.25, 0.003), (-0.03, 0.24, 0.05), (0.03, 0.24, -0.04), (0.02, 0.25, 0.04), (-0.02, 0.25, -0.03)]


def test_one_photo_with_a_bad_pose_does_not_sink_the_slab(scene, tmp_path):
    """One photo's pose can be off on its own (a real board scan: one of five 6-9 mm off, the others within 2 mm).
    Most photos agree: the slab is built, and the photo that disagrees fills none of its texture."""
    _, box, lidar = scene
    stills = _stills(tmp_path, EYES, offset=4)   # the second best photo: the one the old single check used
    m = F.flat_model({}, stills, box, TABLE_Y, lidar)
    shifts = m.stats["check_shifts_mm"]
    assert shifts["s4"] > 10 and sum(x <= 2 for x in shifts.values()) >= 3
    assert "s4" not in m.stats["texture_from"] and m.stats["second_still"] != "s4"


def test_a_busy_board_is_found_whole(scene, tmp_path):
    """Rows of parts in the table's own colours cross the board: the outline is the whole board, not its largest
    strip (on a real board the largest piece was a third of it)."""
    _, box, lidar = scene
    BUSY["on"] = True
    try:
        stills = _stills(tmp_path, EYES[:2])
    finally:
        BUSY["on"] = False
    m = F.flat_model({}, stills, box, TABLE_Y, lidar)
    a, b = m.stats["measured_mm"]
    assert abs(a - 53.4) < 3 and abs(b - 69.0) < 3


def _model(outline: np.ndarray) -> F.FlatModel:
    """A slab over `outline` with a texture whose colour encodes x (to check every top vertex's texture lookup)."""
    k = len(outline)
    lo, hi = outline.min(0), outline.max(0)
    uv_top = (outline - lo) / (hi - lo)
    atlas = np.zeros((108, 100, 3), np.uint8)
    atlas[:100, :, 0] = np.linspace(0, 255, 100)[None, :].astype(np.uint8)
    top = np.c_[outline[:, 0], np.full(k, 0.0016), outline[:, 1]]
    bot = np.c_[outline[:, 0], np.zeros(k), outline[:, 1]]
    verts = np.vstack([top, top, bot]).astype(np.float32)
    uv = np.vstack([uv_top * [1, 100 / 108], np.tile([0.5, 104 / 108], (2 * k, 1))]).astype(np.float32)
    faces = [[0, t, t + 1] for t in range(1, k - 1)]
    faces += [[k + t, k + (t + 1) % k, 2 * k + (t + 1) % k] for t in range(k)]
    return F.FlatModel(verts, uv, np.asarray(faces, np.int32), Image.fromarray(atlas), outline)


@pytest.mark.parametrize("outline", [
    np.array([[0.0, 0.0], [0.05, 0.0], [0.05, 0.03], [0.0, 0.03]]),                       # a rectangle
    np.array([[0.0, 0.0], [0.06, 0.01], [0.02, 0.05]]),                                   # a triangle
    np.array([[0.0, 0.0], [0.04, -0.01], [0.07, 0.02], [0.05, 0.05], [0.01, 0.04]]),     # a hull of five corners
])
def test_the_slab_top_covers_its_outline_exactly(outline):
    """Any convex outline (a rectangle, or the hull branch's triangle or polygon): the top grid covers the outline,
    nothing outside it, and every top vertex has its texture."""
    m = _model(outline)
    v, f, c = F.slab_mesh(m)
    top = f[(v[f][:, :, 1] > 0.001).all(1) & (f < len(v) - 3 * len(outline)).all(1)]
    xz = v[:, [0, 2]]
    p0, p1, p2 = xz[top[:, 0]], xz[top[:, 1]], xz[top[:, 2]]
    area = 0.5 * np.abs((p1[:, 0] - p0[:, 0]) * (p2[:, 1] - p0[:, 1]) - (p1[:, 1] - p0[:, 1]) * (p2[:, 0] - p0[:, 0]))
    poly = abs(F._polygon_area_signed(outline))
    assert abs(area.sum() - poly) < 0.002 * poly
    used = np.unique(top)
    inside = F._inside(xz[used], outline)
    on = np.linalg.norm(F._nearest_on_outline(xz[used], outline) - xz[used], axis=1) < 1e-7
    assert (inside | on).all()
    expect = (xz[used, 0] - outline[:, 0].min()) / np.ptp(outline[:, 0]) * 255
    assert np.abs(c[used, 0].astype(float) - expect).max() < 6             # every vertex coloured from its texture


def test_the_slab_mesh_keeps_to_the_triangle_limit():
    """A 20 x 24 cm thin object at 0.5 mm would be ~384,000 top triangles: the grid coarsens to the limit."""
    m = _model(np.array([[0.0, 0.0], [0.2, 0.0], [0.2, 0.24], [0.0, 0.24]]))
    _, f, _ = F.slab_mesh(m, max_triangles=80_000)
    assert 40_000 < len(f) <= 80_000
