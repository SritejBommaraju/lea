"""What Lea looks at on a report turn (repairos.voice.views): the z-buffer, point maps, upright photos, renders, the
photo choice, the USDZ surface, the ringed photo of a spot and the part index's placement. A small synthetic scene
(a textured plane and cameras with known poses) so every number can be checked; the real-scan check is
eval/live_vision (report items)."""
from __future__ import annotations

import io

import numpy as np
from PIL import Image

from repairos.locate.camera import Pinhole, Pose, matrix_to_quat, project
from repairos.voice import views as V
from repairos.voice.evidence import INDEX_RADIUS_M, index_entries


def plane(n: int = 160, z: float = -0.5, size: float = 0.2) -> V.Surface:
    """A size x size square at depth z (facing +Z), coloured by x (red) and y (green)."""
    g = (np.arange(n) + 0.5) / n * size - size / 2
    xx, yy = np.meshgrid(g, g)
    pts = np.stack([xx.ravel(), yy.ravel(), np.full(xx.size, z)], axis=1).astype(np.float32)
    rgb = np.stack([(xx.ravel() / size + 0.5) * 255, (yy.ravel() / size + 0.5) * 255, np.full(xx.size, 80)], axis=1)
    return V.Surface(pts, rgb.astype(np.uint8), size / n, "cloud")


def still(eye=(0.0, 0.0, 0.0), look=(0.0, 0.0, -0.5), w=800, h=600, sid="scan_t_s3", sharp=100.0) -> dict:
    pose = V.look_pose(eye, look)
    return {"still_id": sid, "scan_id": "scan_t", "width_px": w, "height_px": h, "sharpness": sharp,
            "camera": {"frame": "arkit_world", "position_m": list(map(float, pose.t)),
                       "quaternion_xyzw": matrix_to_quat(pose.r)},
            "intrinsics": {"fx": 700.0, "fy": 700.0, "cx": w / 2, "cy": h / 2}, "image_file": "blob://sha256:" + "a" * 64}


def jpeg(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 120, 120)).save(buf, "JPEG")
    return buf.getvalue()


def test_look_pose_looks_at_the_target():
    pose = V.look_pose((0.3, 0.2, 0.4), (0, 0, 0))
    cam = Pinhole(500, 500, 320, 240, 640, 480)
    (u, v), d = (a[0] for a in project(np.zeros((1, 3)), pose, cam))
    assert abs(u - 320) < 1e-6 and abs(v - 240) < 1e-6 and d > 0
    top = V.look_pose((0, 1, 0), (0, 0, 0))        # straight above still has a valid frame
    assert np.allclose(top.r @ top.r.T, np.eye(3), atol=1e-9)


def test_zbuffer_keeps_the_nearest_surface():
    front, back = plane(z=-0.4, size=0.1), plane(z=-0.6)
    both = V.Surface(np.vstack([back.points, front.points]), np.vstack([back.colors, front.colors]), 0.00125, "cloud")
    pose, cam = V.look_pose((0, 0, 0), (0, 0, -1)), Pinhole(400, 400, 200, 200, 400, 400)
    index = V.zbuffer(both, pose, cam, 400, 400)
    centre = both.points[index[200, 200]]
    assert abs(centre[2] + 0.4) < 1e-6                 # the near square hides the far one at the centre
    assert abs(both.points[index[5, 200]][2] + 0.6) < 1e-6 if index[5, 200] >= 0 else True
    assert (index[160:240, 160:240] >= 0).mean() > 0.97   # points cover their pixels: the surface reads as solid


def test_photo_view_point_map_matches_the_turned_photo():
    s = plane()
    st = still()
    v = V.photo_view(s, st, jpeg(800, 600), "s3")
    im = Image.open(io.BytesIO(v.jpeg))
    assert im.size == (576, 768)                       # upright: the landscape still turned clockwise, 768 long
    target = np.array([0.05, -0.03, -0.5])
    (u, vv), _ = (a[0] for a in project(target[None], Pose.from_contract(st["camera"]),
                                        Pinhole.from_contract(st["intrinsics"], 800, 600)))
    x, y = 1 - vv / 600, u / 800                       # the same pixel once turned clockwise
    got = V.point_at(v, x, y)
    assert got is not None and np.linalg.norm(got - target) < 0.004


def test_point_at_snaps_to_the_nearest_surface_and_refuses_empty_space():
    v = V.render_view(plane(size=0.1), (0, 0, 0), (0, 0, -0.5), "v1", "caption")
    assert V.point_at(v, 0.5, 0.5) is not None
    assert V.point_at(v, 0.01, 0.01) is None           # off the model, and nothing within reach
    assert V.point_at(v, 1.5, 0.5) is None


def test_render_view_draws_the_surface_on_the_stage():
    v = V.render_view(plane(), (0, 0, 0), (0, 0, -0.5), "v1", "View v1")
    rgb = np.asarray(Image.open(io.BytesIO(v.jpeg)))
    assert rgb.shape == (768, 768, 3) and v.kind == "render" and v.points.shape == (384, 384, 3)
    assert abs(int(rgb[2, 2].mean()) - int(np.mean(V.BACKGROUND))) < 12    # the corner is the dark stage


def test_area_target_and_photo_choice_follow_the_question_words():
    parts = [{"id": "f_a", "part": "face button", "where": "red B button", "point": [0.05, 0.0, -0.5], "still_id": "s_far"},
             {"id": "f_b", "part": "cable", "where": "white cable", "point": [-0.05, 0.0, -0.5]}]
    assert np.allclose(V.area_target("where's the X button", parts), [0.05, 0.0, -0.5])
    assert V.area_target("hello there", parts) is None
    near = still(eye=(0.05, 0, -0.2), look=(0.05, 0, -0.5), sid="s_near", sharp=50)
    far = still(eye=(-0.3, 0, 0.3), look=(-0.3, 0, -0.5), sid="s_far", sharp=200)
    flip = {**still(sid="s_flip"), "segment": 1}       # a flipped bottom's photo: not in the model's frame yet
    picked = V.pick_photos([far, near, flip], "where's the X button", parts, np.zeros(3), k=2)
    assert [p["still_id"] for p in picked] == ["s_far", "s_near"]    # its own photo first, then the one that frames it
    assert V.short_key("scan_ab12_s14") == "s14"


def test_surface_from_our_usdz():
    from repairos.recreation.usdz import usdz_bytes

    v = np.array([[0, 0, -0.5], [0.1, 0, -0.5], [0, 0.1, -0.5]], float)
    uv = np.array([[0, 0], [1, 0], [0, 1]], float)
    atlas = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 30, 40)).save(atlas, "JPEG")
    s = V.surface_from_usdz(usdz_bytes(v, uv, np.array([[0, 1, 2]]), atlas.getvalue()), max_samples=5000)
    assert s is not None and s.kind == "recreation" and len(s.points) >= 1000
    assert np.all(s.points[:, 2] == np.float32(-0.5)) and s.colors[:, 0].mean() > 150
    assert V.surface_from_usdz(b"not a zip") is None


def test_spot_photo_rings_the_closest_photo_or_renders_the_model():
    s = plane()
    stills = [still(eye=(0, 0, 0.3), sid="s_far"), still(eye=(0.02, 0, -0.25), look=(0.02, 0, -0.5), sid="s_near")]
    img, sid = V.spot_photo(s, stills, lambda st: jpeg(800, 600), [0.02, 0.0, -0.5], 0.01)
    im = np.asarray(Image.open(io.BytesIO(img)))
    assert sid == "s_near" and min(im.shape[:2]) >= 400          # a full-resolution crop from the closer photo
    assert (np.abs(im.astype(int) - V.RING_RGB).sum(axis=-1) < 90).sum() > 50    # the ring is drawn
    img, sid = V.spot_photo(s, [still(eye=(0, 0, -1.0), look=(0, 0, -2.0))], lambda st: jpeg(800, 600),
                            [0.0, 0.0, -0.5], 0.01)
    assert sid is None and Image.open(io.BytesIO(img)).size == (V.CROP_PX, V.CROP_PX)   # no photo sees it: the model


def test_index_entries_place_listed_parts_on_the_model():
    s = plane()
    v = V.photo_view(s, still(), jpeg(800, 600), "s3")
    parts = [{"label": "A2", "kind": "pin", "photo": "S3", "x": 0.5, "y": 0.5, "note": "analog input"},
             {"label": "ghost", "kind": "other", "photo": "s9", "x": 0.5, "y": 0.5, "note": ""},     # no such photo
             {"label": "edge", "kind": "other", "photo": "s3", "x": 0.0, "y": 0.0, "note": ""}]      # off the model
    out = index_entries(parts, {"s3": v})
    assert [e["id"] for e in out] == ["ix_1"] and out[0]["label"] == "A2" and out[0]["radius"] == INDEX_RADIUS_M
    assert out[0]["still_id"] == "scan_t_s3" and abs(out[0]["point"][2] + 0.5) < 1e-3
