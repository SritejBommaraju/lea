"""repairos.measure on a rendered scene whose geometry is known exactly: a textured board on the table plane (y = 0)
seen from twelve stills by a calibrated camera. The phone's errors are put in on purpose: LiDAR depth with
about 8 mm of noise and a 3 mm offset, ARKit poses off by about 2 mm and 0.2 degrees, and ARKit's whole scale 1% too big.
The adjusted shape must match the truth to a tenth of a millimetre; without a known length the lengths carry the size's
uncertainty (LiDAR alone pins size to about a percent here), and with one known length and a holdout they come back
within the half-millimetre tolerance and are verified."""
from __future__ import annotations

import numpy as np
import pytest

from repairos.locate.camera import Pinhole, Pose, matrix_to_quat
from repairos.measure import dimension, reference
from repairos.measure.adjust import Intrinsics, _jacobians, _project_all, expm
from repairos.measure.size import fit_scale
from repairos.measure.run import measure, track_and_adjust
from repairos.measure.tracks import Tracks
from repairos.recon.align import umeyama
from repairos.measure.views import make_view

W, H = 960, 720
CAM = Pinhole(760.0, 760.0, 481.0, 358.0, W, H)
TRUE_INTR = Intrinsics()          # a calibrated lens (see adjust.py: distortion must come from calibration)
HALF_X, HALF_Z = 0.065, 0.05          # the textured board: 130 x 100 mm
TEX_PX_PER_M = 8000


def _texture(rng) -> np.ndarray:
    h, w = int(2 * HALF_Z * TEX_PX_PER_M), int(2 * HALF_X * TEX_PX_PER_M)
    t = rng.random((h // 8, w // 8))
    t = np.kron(t, np.ones((8, 8)))
    k = np.ones(9) / 9
    for _ in range(2):
        t = np.apply_along_axis(lambda r: np.convolve(r, k, mode="same"), 1, t)
        t = np.apply_along_axis(lambda c: np.convolve(c, k, mode="same"), 0, t)
    return (t - t.min()) / (t.max() - t.min())


def _look_at(eye: np.ndarray, target: np.ndarray) -> np.ndarray:
    back = (eye - target) / np.linalg.norm(eye - target)          # camera +z points back (it looks along -z)
    right = np.cross([0.0, 1.0, 0.0], back)
    right /= np.linalg.norm(right)
    up = np.cross(back, right)
    return np.stack([right, up, back], axis=1)


def _render(tex: np.ndarray, pose: Pose) -> tuple[np.ndarray, np.ndarray]:
    """(grey image H x W, true view-axis depth on a 256 x 192 grid) of the board on the plane y = 0."""
    def hit(uv, cam):
        d = TRUE_INTR.ray(uv, cam) @ pose.r.T
        lam = -pose.t[1] / d[:, 1]
        return pose.t + lam[:, None] * d, lam * -(TRUE_INTR.ray(uv, cam)[:, 2])

    img = np.zeros(W * H)
    for oy in (0.25, 0.75):                       # 2 x 2 samples per pixel, bilinear texels: no aliasing
        for ox in (0.25, 0.75):
            uu, vv = np.meshgrid(np.arange(W) + ox, np.arange(H) + oy)
            x, _ = hit(np.stack([uu.ravel(), vv.ravel()], 1), CAM)
            tx = (x[:, 0] + HALF_X) * TEX_PX_PER_M
            tz = (x[:, 2] + HALF_Z) * TEX_PX_PER_M
            inside = (tx >= 0) & (tx < tex.shape[1] - 1) & (tz >= 0) & (tz < tex.shape[0] - 1)
            val = np.full(len(tx), 0.5)
            x0, z0 = tx[inside].astype(int), tz[inside].astype(int)
            fx, fz = tx[inside] - x0, tz[inside] - z0
            val[inside] = (tex[z0, x0] * (1 - fx) * (1 - fz) + tex[z0, x0 + 1] * fx * (1 - fz)
                           + tex[z0 + 1, x0] * (1 - fx) * fz + tex[z0 + 1, x0 + 1] * fx * fz)
            img += val / 4
    dc = CAM.scaled(256, 192)
    gu, gv = np.meshgrid(np.arange(256) + 0.5, np.arange(192) + 0.5)
    _, depth = hit(np.stack([gu.ravel(), gv.ravel()], 1), dc)
    return img.reshape(H, W).astype(np.float32), depth.reshape(192, 256)


def _lidar_noise(rng, shape) -> np.ndarray:
    """About 8 mm in all: a smooth field (6 mm, tens of cells wide, as the phone's fused depth wanders) plus 2 mm
    cell noise."""
    coarse = rng.normal(0, 1, (shape[0] // 24 + 2, shape[1] // 24 + 2))
    yy, xx = np.meshgrid(np.arange(shape[0]) / 24, np.arange(shape[1]) / 24, indexing="ij")
    y0, x0 = yy.astype(int), xx.astype(int)
    fy, fx = yy - y0, xx - x0
    smooth = (coarse[y0, x0] * (1 - fx) * (1 - fy) + coarse[y0, x0 + 1] * fx * (1 - fy)
              + coarse[y0 + 1, x0] * (1 - fx) * fy + coarse[y0 + 1, x0 + 1] * fx * fy)
    return 0.006 * smooth / smooth.std() + rng.normal(0, 0.002, shape)


def _scene(seed=0, n=12):
    rng = np.random.default_rng(seed)
    tex = _texture(rng)
    stills, images, depths, truth = [], {}, {}, []
    for k in range(n):
        az = np.radians(-60 + 120 * k / (n - 1))
        el = np.radians(55 + 10 * np.sin(k))
        dist = 0.24 + 0.06 * (k % 3) / 2
        eye = np.array([np.sin(az) * np.cos(el), np.sin(el), np.cos(az) * np.cos(el)]) * dist
        true = Pose(_look_at(eye, np.array([0.0, 0.0, 0.0])), eye)
        truth.append(true)
        img, depth = _render(tex, true)
        img = np.clip(img + rng.normal(0, 0.01, img.shape), 0, 1)
        depth = depth + 0.003 + _lidar_noise(rng, depth.shape)             # LiDAR: offset + noise
        r_err = expm(rng.normal(0, np.radians(0.2), 3))
        arkit = Pose(r_err @ true.r, true.t * 1.01 + rng.normal(0, 0.002, 3))  # ARKit: 1% scale, drift
        sid = f"s{k:02d}"
        stills.append({"still_id": sid, "scan_id": "scan", "kind": "guided", "width_px": W, "height_px": H,
                       "pose_reliable": True, "captured_at": f"2026-10-24T00:00:{k:02d}Z",
                       "intrinsics": {"fx": CAM.fx, "fy": CAM.fy, "cx": CAM.cx, "cy": CAM.cy},
                       "camera": {"frame": "arkit_world", "position_m": list(arkit.t),
                                  "quaternion_xyzw": matrix_to_quat(arkit.r)}})
        images[sid], depths[sid] = (img * 255).astype(np.uint8), depth.astype(np.float32)
    return stills, images, depths, truth


@pytest.fixture(scope="module")
def scene():
    return _scene()


def _marks(truth, still_ids, world, rng, noise_px=0.3):
    """Where a world point is in each named still (true camera and lens), with a tap's noise."""
    out = []
    for k in still_ids:
        uv = TRUE_INTR.project(truth[k].to_camera(world[None]), CAM)[0]
        out.append({"still_id": f"s{k:02d}", "uv": list(uv + rng.normal(0, noise_px, 2)), "sigma_px": noise_px})
    return out


def test_the_lens_model_inverts():
    uv = np.array([[30.0, 40.0], [480.0, 360.0], [900.0, 700.0]])
    p = Intrinsics(1.01, -0.04).ray(uv, CAM)
    back = Intrinsics(1.01, -0.04).project(p, CAM)
    assert np.allclose(back, uv, atol=1e-6)


def test_the_adjustment_derivatives_match_finite_differences():
    rng = np.random.default_rng(1)
    nv, npnt = 3, 5
    r = np.stack([expm(rng.normal(0, 0.3, 3)) for _ in range(nv)])
    x = rng.normal(0, 0.03, (npnt, 3))
    t = np.stack([x.mean(0) + r[i] @ np.array([0, 0, 0.3]) for i in range(nv)])    # each camera looks at the points
    tr = Tracks(np.repeat(np.arange(npnt), nv), np.tile(np.arange(nv), npnt), np.zeros((npnt * nv, 2)), x)
    cams = np.array([[700.0, 700.0, 480.0, 360.0]] * nv)
    intr = np.array([1.02, -0.05])
    uv, pc, q, aux = _project_all(r, t, cams, intr, x, tr)
    duv_dp, dp_dx, dp_dw, dp_dt, duv_dg = _jacobians(r, intr, pc, q, aux, tr.view)
    e = 1e-6

    def fd(**kw):
        return (_project_all(kw.get("r", r), kw.get("t", t), cams, kw.get("g", intr), kw.get("x", x), tr)[0] - uv) / e
    for k in range(3):
        step = np.zeros(3)
        step[k] = e
        scale = np.abs(duv_dp @ dp_dx).max()
        assert np.abs(fd(x=x + step) - (duv_dp @ dp_dx)[:, :, k]).max() < 1e-5 * scale
        assert np.abs(fd(t=t + step) - (duv_dp @ dp_dt)[:, :, k]).max() < 1e-5 * scale
        assert np.abs(fd(r=np.stack([expm(step) @ ri for ri in r])) - (duv_dp @ dp_dw)[:, :, k]).max() < 1e-3
    for k in range(2):
        step = np.zeros(2)
        step[k] = e
        assert np.abs(fd(g=intr + step) - duv_dg[:, :, k]).max() < 1e-4


def test_tracks_land_on_the_true_surface(scene):
    stills, images, depths, truth = scene
    views = [make_view(s, images[s["still_id"]], depths[s["still_id"]]) for s in stills]
    tr, _, first = track_and_adjust(views)
    assert first.n_points > 300 and tr.n_points > 300 and len(tr.track) > 6 * tr.n_points
    # each match must be where the true camera sees the reference corner's true surface point
    errs = []
    for t in range(0, tr.n_points, 7):
        obs = np.flatnonzero(tr.track == t)
        ref, others = obs[0], obs[1:]
        o, d = truth[tr.view[ref]].t, TRUE_INTR.ray(tr.uv[ref][None], CAM)[0] @ truth[tr.view[ref]].r.T
        x = o + (-o[1] / d[1]) * d
        for m in others:
            uv = TRUE_INTR.project(truth[tr.view[m]].to_camera(x[None]), CAM)[0]
            errs.append(np.linalg.norm(uv - tr.uv[m]))
    assert np.median(errs) < 0.2 and np.percentile(errs, 95) < 0.6     # the adjustment assumes 0.5 px


def test_adjustment_and_lidar_recover_the_true_shape_and_size(scene):
    stills, images, depths, truth = scene
    views = [make_view(s, images[s["still_id"]], depths[s["still_id"]]) for s in stills]
    tr, adj, first = track_and_adjust(views)
    assert np.median(np.bincount(tr.track)) >= 6               # the second pass sees each point from many stills
    assert adj.rms_px_after < 0.2 * adj.rms_px_before and adj.rms_px_after < 0.2
    # the adjusted cameras are the true ones up to a similarity, to about a millimetre
    sim = umeyama(np.stack([p.t for p in adj.poses]), np.stack([p.t for p in truth]))
    moved = sim.scale * np.stack([p.t for p in adj.poses]) @ sim.r.T + sim.t
    assert np.abs(moved - np.stack([p.t for p in truth])).max() < 0.0015
    # LiDAR in the fit sets the size (its offset trades against it over a short depth range, and the sigmas say so)
    sc = fit_scale(views, adj, tr)
    assert sc["status"] == "fitted" and abs(sc["s"] - 1) < 0.002
    assert abs(sim.scale - 1) < 2.5 * sc["sigma_s"] and sc["sigma_s"] < 0.03
    assert abs(adj.lidar_offset_m - 0.003) < 2.5 * sc["sigma_b_m"]
    assert sc["motion_scale"] > 1.0                            # ARKit was made 1% too big


CORNERS = {"a": np.array([-0.045, 0.0, -0.03]), "b": np.array([0.04, 0.0, -0.03]),
           "c": np.array([0.04, 0.0, 0.035]), "d": np.array([-0.045, 0.0, 0.035]), "e": np.array([0.0, 0.0, 0.0])}


def _request(truth, seed=5):
    rng = np.random.default_rng(seed)
    return {"points": {"a": _marks(truth, [0, 4], CORNERS["a"], rng), "b": _marks(truth, [0, 4], CORNERS["b"], rng),
                       "c": _marks(truth, [5, 9], CORNERS["c"], rng), "d": _marks(truth, [6], CORNERS["d"], rng),
                       "e": _marks(truth, [2], CORNERS["e"], rng)},
            "dimensions": [{"name": "ab", "kind": "distance", "between": ["a", "b"]},
                           {"name": "bc", "kind": "distance", "between": ["b", "c"]},
                           {"name": "ac", "kind": "distance", "between": ["a", "c"]},
                           {"name": "ad", "kind": "distance", "between": ["a", "d"], "fit": {"role": "spacing"}},
                           {"name": "ce", "kind": "distance", "between": ["c", "e"]}],
            "tolerance_mm": 0.5}


def _true(name):
    return float(np.linalg.norm(CORNERS[name[0]] - CORNERS[name[1]]) * 1000)


def test_lengths_carry_an_honest_uncertainty_without_a_known_length(scene):
    stills, images, depths, truth = scene
    req = _request(truth)
    rng = np.random.default_rng(11)          # one point marked on two different corners: its rays do not meet
    req["points"]["bad"] = _marks(truth, [0], CORNERS["a"], rng) + _marks(truth, [6], CORNERS["c"], rng)
    req["dimensions"].append({"name": "bad_ab", "kind": "distance", "between": ["bad", "b"]})
    rec = measure(stills, lambda s: images[s["still_id"]], lambda s: depths[s["still_id"]], req)
    assert "do not meet" in rec["points"]["bad"]["error"]
    assert [f["name"] for f in rec["failed"]] == ["bad_ab"]
    for d in rec["dimensions"]:
        err = d["value_mm"] - _true(d["name"])
        assert abs(err) < 1.0 and abs(err) < 2.5 * d["uncertainty_mm"], (d, _true(d["name"]))
        assert d["uncertainty_mm"] < 2.0 and not d["verified"]
    assert rec["points"]["c"]["method"] == "parallax" and rec["points"]["d"]["method"] == "surface"
    assert next(d for d in rec["dimensions"] if d["name"] == "ad")["fit"] == {"role": "spacing"}
    assert "no holdout" in " ".join(rec["check"]["notes"])


def test_a_known_length_brings_the_rest_within_the_tolerance(scene):
    stills, images, depths, truth = scene
    req = _request(truth) | {"reference": {"dimension": "ab", "true_mm": _true("ab"), "uncertainty_mm": 0.05},
                             "holdout": {"dimension": "ad", "true_mm": _true("ad"), "uncertainty_mm": 0.05}}
    rec = measure(stills, lambda s: images[s["still_id"]], lambda s: depths[s["still_id"]], req)
    assert rec["check"]["passed"] and abs(rec["check"]["holdout_error_mm"]) <= 0.5, rec["check"]
    for d in rec["dimensions"]:      # the size error is gone; what is left is where the taps landed
        assert abs(d["value_mm"] - _true(d["name"])) <= 0.5, d
        assert d["verified"] and (d["method"] == "given") == (d["name"] in ("ab", "ad"))


def test_a_hole_diameter_from_rim_points(scene):
    stills, images, depths, truth = scene
    rng = np.random.default_rng(9)
    centre, rad = np.array([0.01, 0.0, 0.0]), 0.0016
    rim = {f"r{k}": centre + rad * np.array([np.cos(a), 0.0, np.sin(a)])
           for k, a in enumerate(np.radians([0, 72, 144, 216, 288]))}
    req = {"points": {k: _marks(truth, [3, 8], x, rng, 0.3) for k, x in rim.items()},
           "dimensions": [{"name": "hole", "kind": "diameter", "rim": list(rim), "value_mm": 99.0}]}   # not taken
    rec = measure(stills, lambda s: images[s["still_id"]], lambda s: depths[s["still_id"]], req)
    hole = rec["dimensions"][0]
    assert abs(hole["value_mm"] - 3.2) < 0.3 and hole["uncertainty_mm"] < 0.5


def test_reference_corrects_and_holdout_verifies():
    dims = [{"name": "ref", "value_mm": 70.0, "uncertainty_mm": 0.2},
            {"name": "hold", "value_mm": 51.0, "uncertainty_mm": 0.2},
            {"name": "spacing", "value_mm": 40.0, "uncertainty_mm": 0.2},
            {"name": "loose", "value_mm": 12.0, "uncertainty_mm": 0.9}]
    out, check = reference.apply(dims, {"dimension": "ref", "true_mm": 68.6}, {"dimension": "hold", "true_mm": 50.0},
                                 0.5)
    by = {d["name"]: d for d in out}
    assert check["factor"] == pytest.approx(0.98) and check["passed"]
    assert by["spacing"]["value_mm"] == pytest.approx(39.2) and by["spacing"]["verified"]
    assert by["ref"]["method"] == "given" and by["ref"]["value_mm"] == 68.6
    assert not by["loose"]["verified"]                  # its own sigma is over the tolerance
    out, check = reference.apply(dims, {"dimension": "ref", "true_mm": 68.6}, {"dimension": "hold", "true_mm": 52.0},
                                 0.5)
    assert not check["passed"] and not any(d["verified"] for d in out if d["name"] in ("spacing", "loose"))
    with pytest.raises(ValueError):
        reference.apply(dims, {"dimension": "ref", "true_mm": 68.6}, {"dimension": "ref", "true_mm": 68.6}, 0.5)
    same = [dict(dims[0], between=["a", "b"]), dict(dims[1], name="check", between=["b", "a"])]
    with pytest.raises(ValueError, match="different points"):     # one length under two names is no holdout
        reference.apply(same, {"dimension": "ref", "true_mm": 68.6}, {"dimension": "check", "true_mm": 68.6}, 0.5)


def test_a_still_without_a_reliable_pose_does_not_measure(scene):
    stills, images, depths, _ = scene
    bad = {**stills[0], "pose_reliable": False}
    with pytest.raises(ValueError, match="no reliable pose"):
        make_view(bad, images[bad["still_id"]], depths[bad["still_id"]])
    with pytest.raises(dimension.Unmeasurable):
        sc = dimension.Scene([make_view(stills[1], images["s01"], None)], [Pose(np.eye(3), np.zeros(3))],
                             Intrinsics(), None, None)
        dimension.locate_point(sc, "x", [{"still_id": "s01", "uv": [100.0, 100.0]}])



def test_without_an_adjustment_a_point_carries_the_poses_own_error(scene):
    stills, images, depths, truth = scene
    view = make_view(stills[0], images["s00"], depths["s00"])
    plain = dimension.Scene([view], [view.pose], Intrinsics(), None, None)
    honest = dimension.Scene([view], [view.pose], Intrinsics(), None, None, sigma_s=0.02, pose_sigma_m=0.005,
                             pose_sigma_rad=np.radians(0.5))
    mark = [{"still_id": "s00", "uv": [480.0, 360.0]}]
    a, b = dimension.locate_point(plain, "p", mark), dimension.locate_point(honest, "p", mark)
    assert a.method == b.method == "lidar"
    assert np.sqrt(np.trace(b.cov)) > np.sqrt(np.trace(a.cov)) + 0.005      # millimetres of pose error, said
