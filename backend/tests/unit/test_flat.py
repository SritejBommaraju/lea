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


def colour(xz: np.ndarray) -> np.ndarray:
    """Wood grain (orange-brown stripes) on the table, a dark board with light markings."""
    x, z = xz[:, 0], xz[:, 1]
    wood = np.stack([170 + 30 * np.sin(z * 900), 95 + 20 * np.sin(z * 900 + 1), 40 + 10 * np.sin(z * 400)], 1)
    on = (x > BOARD[0][0]) & (x < BOARD[1][0]) & (z > BOARD[0][1]) & (z < BOARD[1][1])
    board = np.tile([30.0, 40.0, 50.0], (len(x), 1))
    mark = on & (np.abs(((x * 1000) % 6) - 3) < 0.6) & (z > 0.0)            # stripes of "text"
    board[mark] = [210, 210, 210]
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
    cam["position_m"] = [cam["position_m"][0] + 0.01, cam["position_m"][1], cam["position_m"][2]]   # 1 cm off
    bad["camera"] = cam
    with pytest.raises(F.FlatUnavailable, match="apart"):
        F.flat_model({}, [stills[0], bad], box, TABLE_Y, lidar)


def test_thin_means_close_to_the_table():
    pts = np.c_[np.zeros(500), np.linspace(0.0, 0.012, 500), np.zeros(500)]
    assert F.thin_object(pts, 0.0)[0]
    assert not F.thin_object(pts * [1, 5, 1], 0.0)[0]                 # 6 cm tall: the LiDAR mesh as usual
    assert F.thin_object(pts[:50], 0.0)[0]                            # almost nothing above the table
