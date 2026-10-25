"""A flipped thin board registered by its through-holes (repairos.recreation.flat_flip), on synthetic faces: an
asymmetric hole pattern seen from above and, mirrored and turned, from below."""
from __future__ import annotations

import json

import numpy as np

from repairos.recreation import flat_flip as FF

RNG = np.random.default_rng(4)
HALF = np.array([0.0343, 0.0267])
OUTLINE = np.array([[-HALF[0], -HALF[1]], [HALF[0], -HALF[1]], [HALF[0], HALF[1]], [-HALF[0], HALF[1]]])
# header rows (2.54 mm pitch, offset), four irregular mounting holes, a socket's two rows
HOLES = np.vstack([np.c_[np.arange(-0.025, 0.012, 0.00254), np.full(15, 0.024)],
                   np.c_[np.arange(-0.018, 0.020, 0.00254), np.full(15, -0.024)],
                   [[-0.031, 0.020], [0.029, 0.024], [0.030, -0.012], [-0.020, -0.021]],
                   np.c_[np.arange(-0.010, 0.025, 0.00254), np.full(14, -0.004)],
                   np.c_[np.arange(-0.010, 0.025, 0.00254), np.full(14, 0.0036)]])


def faces(turn_deg: float, shift=(0.12, -0.05), mirror=True):
    t = np.radians(turn_deg)
    r = np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])
    m = np.diag([1.0, -1.0]) if mirror else np.eye(2)
    a1 = r @ m                                  # board frame -> flipped segment's world
    b1 = np.asarray(shift)
    sil = np.stack(np.meshgrid(np.linspace(-HALF[0], HALF[0], 60), np.linspace(-HALF[1], HALF[1], 46)), -1).reshape(-1, 2)
    noise = lambda n: RNG.normal(0, 0.0002, (n, 2))
    top = FF.Face("top", None, None, OUTLINE, np.vstack([HOLES + noise(len(HOLES)), RNG.uniform(-0.03, 0.03, (15, 2))]),
                  0.0, sil)
    bot = FF.Face("bottom", None, None, OUTLINE @ a1.T + b1,
                  np.vstack([(HOLES + noise(len(HOLES))) @ a1.T + b1, RNG.uniform(-0.03, 0.03, (15, 2)) @ a1.T + b1]),
                  0.3, sil @ a1.T + b1)
    truth_a = np.linalg.inv(a1)                 # flipped world -> board frame (= segment 0's world here)
    return top, bot, truth_a, -truth_a @ b1


def test_the_hole_pattern_registers_a_flipped_board():
    top, bot, ta, tb = faces(37.0)
    m = FF.match_faces(top, bot)
    res = FF.decide(top, bot, m, 0.0, 0.3, 0.0016)
    assert res.aligned, res.reason
    assert np.allclose(res.a2, ta, atol=0.01) and np.allclose(res.b2, tb, atol=0.0008)
    assert res.stats["inliers"] >= 60 and res.stats["rms_mm"] < 0.5
    t = res.transform
    assert np.isclose(np.linalg.det(t[:3, :3]), 1.0) and t[1, 1] == -1.0      # a proper turn, upside down
    assert np.isclose(t[1, 3], 0.0 + 0.0016 + 0.3)                           # the bottom lands on the table


def test_wrong_headings_and_an_unmirrored_board_are_refused():
    top, bot, ta, tb = faces(37.0)
    n_true = FF._score(ta, tb, bot.features, top.features)[0]
    c = top.outline.mean(0)
    for deg in (90, 180):        # header rows line up at a half turn too, so it is the margin that refuses it
        r, rt = FF._turn(deg, c)
        a, b = FF._refine(r @ ta, r @ tb + rt, bot.features, top.features, True)[:2]
        n = FF._score(a, b, bot.features, top.features)[0]
        assert n_true - n >= FF.MARGIN and n_true >= FF.MARGIN_RATIO * n, deg
    m = FF.match_faces(top, bot, mirror=False)                 # treat it as not turned over
    res = FF.decide(top, bot, {"best": m["best"], "rival": m["rival"]}, 0.0, 0.3, 0.0016)
    assert not res.aligned


def test_a_symmetric_pattern_is_refused_as_not_unique():
    global HOLES
    saved = HOLES
    HOLES = np.vstack([np.c_[np.arange(-0.025, 0.026, 0.00254), np.full(21, y)] for y in (-0.024, 0.024)])
    try:
        top, bot, _, _ = faces(10.0)
        top.silhouette = bot.silhouette[:0]
        res = FF.decide(top, bot, FF.match_faces(top, bot), 0.0, 0.3, 0.0016)
        assert not res.aligned and "unique" in res.reason
    finally:
        HOLES = saved


def _replay():
    import json
    from pathlib import Path

    fx = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "flat_flip_sides_52315bea.json").read_text())
    poses = FF.rect_poses(np.array(fx["outline1"]), np.array(fx["outline0"]))
    return fx, poses


def test_the_recorded_side_cues_of_a_real_flipped_board_are_refused_as_too_unsure():
    """The real flipped board (scan_52315bea): the model put the USB shell on the same physical end of both faces
    (confidence 0.6), but the barrel jack and the gapped header only at 0.45-0.55, under CUE_MIN_CONFIDENCE. One
    agreeing cue cannot pick among four mirrored poses: refused, never a guess."""
    fx, poses = _replay()
    assert len(poses) == 4
    d = FF.choose_by_cues(fx["answers"], fx["face0"], fx["face1"], poses, fx["ious"])
    assert not d["accepted"] and "1 side cues" in d["why"]
    assert d["agree"][d["chosen"]] == ["usb"]


def test_confident_cues_pick_one_pose_and_contradicting_silhouettes_veto_it():
    fx, poses = _replay()
    sure = {face: {k: {**v, "confidence": 0.9} for k, v in fx["answers"][face].items()} for face in ("photo_a", "photo_b")}
    d = FF.choose_by_cues(sure, fx["face0"], fx["face1"], poses, fx["ious"])
    assert d["accepted"] and d["chosen"] == int(np.argmax(fx["ious"])) and {"usb", "jack"} <= set(d["agree"][d["chosen"]])
    worse = list(fx["ious"])
    worse[d["chosen"]] = max(worse) - 0.1                 # the silhouettes favour another pose by far
    assert not FF.choose_by_cues(sure, fx["face0"], fx["face1"], poses, worse)["accepted"]


def _fixture(name):
    import json
    from pathlib import Path

    fx = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / name).read_text())
    return fx, FF.rect_poses(np.array(fx["outline1"]), np.array(fx["outline0"]))


def test_the_rule_accepts_the_pose_verified_by_eye_on_a_real_flipped_board():
    """scan_477a: pose 0 (verified by eye) fits the silhouettes best (0.928 vs 0.912 turned half round) and the model
    is sure (0.9) on both faces where the USB shell is; nothing sure points elsewhere."""
    fx, poses = _fixture("flat_flip_sides_477a.json")
    d = FF.decide_pose(fx["answers"], fx["face0"], fx["face1"], poses, fx["ious"])
    assert d["accepted"], d["why"]
    assert d["chosen"] == fx["verified_pose"] == 0 and d["sure_cues"] == {"usb": [0]}


def test_the_rule_refuses_when_the_model_is_not_sure():
    """scan_52315: the USB shell at 0.6, the jack and header lower: nothing sure, refused."""
    fx, poses = _fixture("flat_flip_sides_52315bea.json")
    d = FF.decide_pose(fx["answers"], fx["face0"], fx["face1"], poses, fx["ious"])
    assert not d["accepted"] and "sure" in d["why"]


def test_the_rule_refuses_conflicting_cues_a_worse_silhouette_and_contrary_holes():
    fx, poses = _fixture("flat_flip_sides_477a.json")
    # the jack, sure on both faces, placed on the bottom photo where it agrees with one other pose only (searched
    # over every edge and half, so the cues land on different poses by construction)
    conflicting = []
    for edge in ("top", "bottom", "left", "right"):
        for half in ("first", "second", "none"):
            ans = json.loads(json.dumps(fx["answers"]))
            ans["photo_a"]["jack"] = {**ans["photo_a"]["jack"], "confidence": 0.9}
            ans["photo_b"]["jack"] = {"edge": edge, "half": half, "confidence": 0.9}
            votes = FF.cue_votes(ans, fx["face0"], fx["face1"], poses)
            if votes["usb"] == [0] and votes.get("jack") and 0 not in votes["jack"]:
                conflicting.append(ans)
    assert conflicting
    for ans in conflicting:
        d = FF.decide_pose(ans, fx["face0"], fx["face1"], poses, fx["ious"])
        assert not d["accepted"] and "different poses" in d["why"], d["why"]
    worse = list(fx["ious"])
    worse[0] = max(worse) - 0.05
    worse[2] = 0.95
    d = FF.decide_pose(fx["answers"], fx["face0"], fx["face1"], poses, worse)
    assert not d["accepted"] and "silhouettes" in d["why"]
    d = FF.decide_pose(fx["answers"], fx["face0"], fx["face1"], poses, fx["ious"], holes=[5, 0, 40, 0])
    assert not d["accepted"] and "through-holes" in d["why"]


def test_one_sure_cue_agreeing_with_two_poses_is_refused():
    fx, poses = _fixture("flat_flip_sides_477a.json")
    d = FF.decide_pose(fx["answers"], fx["face0"], fx["face1"], poses + [poses[0]], fx["ious"] + [fx["ious"][0]])
    assert not d["accepted"]


def test_a_registered_underside_is_a_textured_grid_facing_down_in_the_report_mesh(tmp_path):
    """The report's mesh and cloud (flat.slab_mesh / slab_points) show the same two-faced slab as the recreation: the
    underside textured from the flipped segment's photo (here a synthetic top-down still of a dark board, the flip
    the identity), as a grid facing -Y; the sides keep their grey."""
    from PIL import Image
    from test_flat import _model, still

    from repairos.recon.tsdf import ObjectBox
    from repairos.recreation import flat as F

    outline = np.array([[-0.03, -0.02], [0.03, -0.02], [0.03, 0.02], [-0.03, 0.02]])
    m = _model(outline)
    k = len(outline)
    m.faces = np.vstack([m.faces, [[2 * k, 2 * k + t + 1, 2 * k + t] for t in range(1, k - 1)]]).astype(np.int32)
    s, img = still("s0", (0.004, 0.25, 0.003))
    Image.fromarray(img).save(tmp_path / "s0.jpg", quality=95)
    box = ObjectBox.from_contract({"center_m": [0.0, 0.02, 0.0], "extent_m": [0.2, 0.04, 0.24],
                                   "quaternion_xyzw": [0, 0, 0, 1]})
    view = F.load_stills([{**s, "image_file": str(tmp_path / "s0.jpg")}], box)[0]
    m = FF.add_underside(m, view, np.eye(4), 0.0)
    assert m.stats["underside"] == "textured from s0"
    side_uv = m.uv[2 * k:3 * k]
    assert np.allclose(side_uv[:, 1], side_uv[0, 1])                       # the sides' bottom edge stays grey
    v, f, c = F.slab_mesh(m)
    low = f[(v[f][:, :, 1] < 1e-6).all(1)]
    assert len(low) > 1000                                                 # a grid, not the four-corner fan
    p = v[low]
    normal_y = ((p[:, 1] - p[:, 0])[:, [2, 0]] * (p[:, 2] - p[:, 0])[:, [0, 2]] * [1, -1]).sum(1)  # (e1 x e2).y
    assert (normal_y < 0).mean() > 0.99                                     # facing down
    under = np.unique(low)
    assert abs(c[under].astype(float).mean(0) - [30, 40, 50]).max() < 40  # the board's colour from the photo
    pts, cols = F.slab_points(m)
    assert abs(cols[pts[:, 1] < 1e-6].astype(float).mean(0) - [30, 40, 50]).max() < 40
