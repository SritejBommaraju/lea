"""repairos.recon: the pure parts on synthetic geometry (no Apple, no photos).

- sim(3) / rigid alignment (Umeyama, random-sample consensus) on synthetic camera centres;
- the top/bottom merge on a synthetic flipped object: ICP (Open3D and numpy), the SIFT-free part of the feature
  bridge (3D random-sample consensus on lifted matches), the decision gates;
- parsing what recon-cli prints and writes;
- the TSDF helpers (support plane, island removal, object box) and a fusion of synthetic depth maps.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from repairos.locate.camera import Pinhole, Pose, project
from repairos.recon import cli
from repairos.recon.align import Sim3, robust_umeyama, residuals, rotation_angle_deg, umeyama
from repairos.recon.merge import (AppleCheck, FeatureMatchResult, Features, IcpResult, _midpoints, apple_cross_check,
                                  compare, decide, feature_merge, icp_refine, segment_sim3, split_joint_poses)
from repairos.recon.tsdf import (DepthFrame, ObjectBox, Segment, find_support_plane, fuse_segments,
                                 keep_main_components, mask_segment, moved_pose)


def rot(axis: str, deg: float) -> np.ndarray:
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return {"x": np.array([[1, 0, 0], [0, c, -s], [0, s, c]]), "y": np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]]),
            "z": np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])}[axis]


def look_at(eye, target) -> Pose:
    eye, target = np.asarray(eye, float), np.asarray(target, float)
    back = eye - target
    back /= np.linalg.norm(back)
    right = np.cross([0.0, 1.0, 0.0], back)
    right /= np.linalg.norm(right)
    return Pose(np.stack([right, np.cross(back, right), back], axis=1), eye)


def dome(n: int, radius: float = 0.3, centre=(0.0, 0.0, 0.0), seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    az = rng.uniform(0, 2 * np.pi, n)
    el = rng.uniform(np.radians(20), np.radians(70), n)
    return np.asarray(centre) + radius * np.stack([np.cos(el) * np.cos(az), np.sin(el), np.cos(el) * np.sin(az)], 1)


# --- sim(3) -------------------------------------------------------------------------------------------------------

def test_umeyama_recovers_a_similarity_from_camera_centres():
    """Apple's frame -> ARKit: object-centred, yawed, slightly scaled. 30 cameras on a dome, 1 mm noise."""
    truth = Sim3(1.03, rot("y", 67.0) @ rot("x", 2.0), np.array([-0.04, -0.18, -0.11]))
    a = dome(30)
    b = truth.apply(a) + np.random.default_rng(1).normal(0, 0.001, a.shape)
    s = umeyama(a, b)
    assert s.scale == pytest.approx(1.03, abs=0.01)
    assert rotation_angle_deg(s.r @ truth.r.T) < 0.5
    r = residuals(s, a, b)
    assert r["n"] == 30 and r["rmse_m"] < 0.002


def test_umeyama_exact_and_rigid():
    truth = Sim3(1.0, rot("z", 30) @ rot("x", 180), np.array([0.1, 0.2, 0.3]))
    a = dome(10)
    s = umeyama(a, truth.apply(a), with_scale=False)
    assert s.scale == 1.0
    np.testing.assert_allclose(s.matrix, truth.matrix, atol=1e-9)


def test_umeyama_rejects_degenerate_input():
    with pytest.raises(ValueError):
        umeyama(np.zeros((2, 3)), np.zeros((2, 3)))
    line = np.outer(np.arange(5.0), [1.0, 0, 0])
    with pytest.raises(ValueError):
        umeyama(line, line)


def test_sim3_algebra():
    a = Sim3(2.0, rot("y", 40), np.array([1.0, 0, 0]))
    b = Sim3(0.5, rot("x", 10), np.array([0, 2.0, 0]))
    x = dome(5)
    np.testing.assert_allclose((a @ b).apply(x), a.apply(b.apply(x)), atol=1e-12)
    np.testing.assert_allclose(a.inverse().apply(a.apply(x)), x, atol=1e-12)
    c = Sim3.from_matrix(a.matrix)
    assert c.scale == pytest.approx(2.0)
    np.testing.assert_allclose(c.r, a.r, atol=1e-12)


def test_ransac_umeyama_ignores_outliers():
    truth = Sim3(1.0, rot("y", 120), np.array([0.05, 0.0, -0.02]))
    rng = np.random.default_rng(3)
    a = rng.uniform(-0.04, 0.04, (200, 3))
    b = truth.apply(a) + rng.normal(0, 0.0005, a.shape)
    bad = rng.choice(200, 80, replace=False)
    b[bad] = rng.uniform(-0.1, 0.1, (80, 3))
    s, inl = robust_umeyama(a, b, threshold_m=0.003, with_scale=False)
    assert inl.sum() >= 115 and not inl[bad].any()
    assert rotation_angle_deg(s.r @ truth.r.T) < 0.5
    assert np.linalg.norm(s.t - truth.t) < 0.002


def test_segment_sim3_drops_a_misplaced_camera():
    truth = Sim3(0.998, rot("y", 12), np.array([-0.03, -0.18, -0.11]))
    a = dome(20)
    b = truth.apply(a)
    b[4] += [0.06, 0, 0]   # one camera 6 cm off
    poses = [cli.ApplePose(i, f"img{i}.HEIC", _m4(np.eye(3), a[i]), None) for i in range(20)]
    s, info = segment_sim3(poses, {f"img{i}.HEIC": b[i] for i in range(20)})
    assert info["registered"] == 20 and info["inliers"] == 19
    assert info["residual"]["rmse_m"] < 1e-6 and info["residual_all"]["max_m"] > 0.05


def _m4(r, t):
    m = np.eye(4)
    m[:3, :3], m[:3, 3] = r, t
    return m


def test_apple_cross_check_composes_the_two_segment_fits():
    """Apple's joint frame J; ref world A = S_a(J), moving world B = S_b(J); T(B->A) = S_a S_b^-1."""
    s_a = Sim3(1.0, rot("y", 20), np.array([0.0, -0.2, 0.1]))
    s_b = Sim3(1.0, rot("y", -50) @ rot("x", 180), np.array([0.3, -0.25, -0.1]))
    ja, jb = dome(15, seed=1), dome(12, seed=2)
    ref = [cli.ApplePose(i, f"a{i}", _m4(np.eye(3), ja[i]), None) for i in range(15)]
    mov = [cli.ApplePose(i, f"b{i}", _m4(np.eye(3), jb[i]), None) for i in range(12)]
    chk = apple_cross_check(ref, mov, {f"a{i}": s_a.apply(ja[i:i + 1])[0] for i in range(15)},
                            {f"b{i}": s_b.apply(jb[i:i + 1])[0] for i in range(12)})
    want = s_a @ s_b.inverse()
    np.testing.assert_allclose(chk.transform.matrix, want.matrix, atol=1e-6)


def test_split_joint_poses_restores_names():
    poses = [cli.ApplePose(0, "top__x.HEIC", np.eye(4), None), cli.ApplePose(1, "bottom__y.HEIC", np.eye(4), None),
             cli.ApplePose(2, "stray.HEIC", np.eye(4), None)]
    out = split_joint_poses(poses, {"top__x.HEIC": "top", "bottom__y.HEIC": "bottom"})
    assert [p.file for p in out["top"]] == ["x.HEIC"] and [p.file for p in out["bottom"]] == ["y.HEIC"]


# --- the flipped object ---------------------------------------------------------------------------------------------

def object_cloud(step: float = 0.002) -> np.ndarray:
    """An asymmetric 7 x 2.4 x 5 cm block with a 2 cm boss on one end of its top: points on its surface."""
    pts = []
    lo, hi = np.array([-0.035, 0.0, -0.025]), np.array([0.035, 0.024, 0.025])
    for axis in range(3):
        for side in (lo[axis], hi[axis]):
            other = [i for i in range(3) if i != axis]
            g = np.meshgrid(*[np.arange(lo[i], hi[i] + 1e-9, step) for i in other], indexing="ij")
            p = np.zeros((g[0].size, 3))
            p[:, axis] = side
            p[:, other[0]], p[:, other[1]] = g[0].ravel(), g[1].ravel()
            pts.append(p)
    gx, gz = np.meshgrid(np.arange(0.01, 0.03, step), np.arange(-0.01, 0.01, step), indexing="ij")
    for h in np.arange(0.024, 0.044, step):
        pts.append(np.stack([gx.ravel(), np.full(gx.size, h), gz.ravel()], 1))
    return np.concatenate(pts)


FLIP = Sim3(1.0, rot("y", 35) @ rot("x", 180), np.array([0.12, 0.024, -0.05]))   # object frame -> bottom world


def test_icp_recovers_a_flip_from_a_rough_start():
    """Top and bottom share only the vertical sides: ICP fixes the rotation and the horizontal position; the height
    is unobservable from that overlap, so ICP leaves it where the start (the feature transform) put it and says so."""
    obj = object_cloud()
    top = obj[obj[:, 1] > 0.003]                         # the top segment never sees the underside
    bottom_world = FLIP.apply(obj[obj[:, 1] < 0.021])    # the flipped segment sees the underside and the sides
    truth = FLIP.inverse()                               # bottom world -> top world
    start = Sim3(1.0, rot("y", 4) @ truth.r, truth.t + [0.003, -0.002, 0.002])
    c = bottom_world.mean(0)
    for prefer in ("numpy", "auto"):
        res = icp_refine(bottom_world, top, start, max_corr_m=0.003, prefer=prefer, max_points=1500)
        assert isinstance(res, IcpResult)
        assert rotation_angle_deg(res.transform.r @ truth.r.T) < 0.5, prefer
        err = res.transform.apply(c[None])[0] - truth.apply(c[None])[0]
        assert np.hypot(err[0], err[2]) < 0.001, prefer
        start_err = start.apply(c[None])[0] - truth.apply(c[None])[0]
        if res.degenerate:
            assert abs(res.degenerate[0][1]) > 0.99, prefer                  # the free direction is the height
            assert abs(err[1] - start_err[1]) < 0.0005, prefer               # kept from the start
        assert res.fitness > 0.4


def _features(points_world_true, keypoint_ids, pose, cam, descs, noise_m, rng):
    uv, depth = project(points_world_true, pose, cam)
    vis = (depth > 0) & (uv[:, 0] >= 0) & (uv[:, 0] < cam.width) & (uv[:, 1] >= 0) & (uv[:, 1] < cam.height)
    ids = keypoint_ids[vis]
    lifted = points_world_true[vis] + rng.normal(0, noise_m, (int(vis.sum()), 3))
    frame = DepthFrame("f", pose, cam, np.zeros((2, 2)), image_cam=cam)
    return Features(frame, uv[vis], (descs[ids] + rng.normal(0, 0.02, (len(ids), descs.shape[1]))).astype(np.float32),
                    lifted)


def test_feature_bridge_finds_the_flip_from_matched_object_points():
    """Textured object points seen by both segments' cameras (the sides), lifted with 1 mm noise, plus wrong
    matches on the table texture: the rigid random-sample consensus must return T(bottom -> top)."""
    pytest.importorskip("cv2")
    rng = np.random.default_rng(7)
    obj = object_cloud(0.004)
    sides = obj[(obj[:, 1] > 0.004) & (obj[:, 1] < 0.020)]
    descs = rng.normal(0, 1, (len(sides), 64))
    ids = np.arange(len(sides))
    cam = Pinhole(1500, 1500, 800, 600, 1600, 1200)
    ref = [_features(sides, ids, look_at(e, [0, 0.012, 0]), cam, descs, 0.001, rng)
           for e in dome(4, 0.25, seed=11) * [1, 0.3, 1] + [0, 0.02, 0]]
    bottom_pts = FLIP.apply(sides)
    centre_b = FLIP.apply(np.array([[0, 0.012, 0]]))[0]
    mov = [_features(bottom_pts, ids, look_at(e, centre_b), cam, descs, 0.001, rng)
           for e in dome(4, 0.25, centre_b, seed=12) * [1, 0.3, 1] + [0, 0.02, 0]]
    res = feature_merge(ref, mov, threshold_m=0.004, min_inliers=12)
    assert isinstance(res, FeatureMatchResult) and res.transform is not None, res.reason
    truth = FLIP.inverse()
    assert rotation_angle_deg(res.transform.r @ truth.r.T) < 1.0
    assert np.linalg.norm(res.transform.apply(centre_b[None]) - truth.apply(centre_b[None])) < 0.002
    assert res.inliers >= 12


def test_feature_bridge_says_why_when_nothing_matches():
    pytest.importorskip("cv2")
    cam = Pinhole(1500, 1500, 800, 600, 1600, 1200)
    f = Features(DepthFrame("f", Pose(np.eye(3), np.zeros(3)), cam, np.zeros((2, 2)), image_cam=cam),
                 np.zeros((0, 2)), np.zeros((0, 64), np.float32), np.zeros((0, 3)))
    res = feature_merge([f], [f])
    assert res.transform is None and "too few" in res.reason


def _fm(t, inliers=50):
    return FeatureMatchResult(t, 10, 5, 100, 80, inliers, {"rmse_m": 0.002})


def test_decide_keeps_features_when_icp_runs_away_and_refuses_on_disagreement():
    t = Sim3(1.0, rot("y", 30), np.zeros(3))
    icp_far = IcpResult(Sim3(1.0, rot("y", 45), np.zeros(3)), 0.9, 0.002, 0.02, 15.0, "x")
    r = decide(_fm(t), icp_far, None, np.zeros(3))
    assert r.merged and r.source == "features" and "kept the feature transform" in r.reasons[0]
    icp_ok = IcpResult(Sim3(1.0, rot("y", 31), np.zeros(3)), 0.9, 0.002, 0.001, 1.0, "x")
    assert decide(_fm(t), icp_ok, None, np.zeros(3)).source == "features+icp"
    fits = {"reference": {"residual": {"rmse_m": 0.002}}, "moving": {"residual": {"rmse_m": 0.002}}}
    apple_far = AppleCheck(Sim3(1.0, rot("y", 30), np.array([0.05, 0, 0])), fits)
    r = decide(_fm(t), None, apple_far, np.zeros(3))
    assert not r.merged and r.transform is None and "disagree" in r.reasons[-1]
    apple_near = AppleCheck(Sim3(1.0, rot("y", 31), np.array([0.002, 0, 0])), fits)
    r = decide(_fm(t), None, apple_near, np.zeros(3))
    assert r.merged and r.agreement["displacement_mm"] < 10


def test_decide_falls_back_to_apple_only_when_it_fits_both_segments():
    fits = {"reference": {"residual": {"rmse_m": 0.003}}, "moving": {"residual": {"rmse_m": 0.004}}}
    a = AppleCheck(Sim3(1.0, rot("y", 30), np.zeros(3)), fits)
    r = decide(_fm(None, 0), None, a, np.zeros(3))
    assert r.merged and r.source == "apple_joint"
    bad = AppleCheck(a.transform, {"reference": {"residual": {"rmse_m": 0.003}}, "moving": {"residual": {}}})
    assert not decide(_fm(None, 0), None, bad, np.zeros(3)).merged


def test_compare_reports_rotation_and_displacement():
    a = Sim3(1.0, np.eye(3), np.zeros(3))
    b = Sim3(1.0, rot("y", 10), np.array([0.003, 0, 0]))
    c = compare(a, b, np.zeros(3))
    assert c["rotation_deg"] == pytest.approx(10, abs=1e-6) and c["displacement_mm"] == pytest.approx(3.0)


def test_midpoints_triangulate_two_rays():
    p = np.array([[0.01, 0.02, -0.3]])
    o1, o2 = np.zeros((1, 3)), np.array([[0.1, 0, 0]])
    d1 = (p - o1) / np.linalg.norm(p - o1)
    d2 = (p - o2) / np.linalg.norm(p - o2)
    m, st = _midpoints(o1, d1, o2, d2)
    np.testing.assert_allclose(m, p, atol=1e-12)
    assert (st > 0).all()


# --- recon-cli output parsing ---------------------------------------------------------------------------------------

def test_parse_events_and_summary():
    err = "\n".join([
        '{"event":"start","images":34,"t_s":0.0}',
        "Warning (secondary thread): in _EnqueueDependency at line 105 of usdUtils/assetLocalization.cpp",
        '{"event":"stage","request":"modelFile","stage":"imageAlignment","eta_s":369.5,"t_s":6.7}',
        '{"not_an_event": 1}',
        "{broken json",
        "",
        '{"event":"poses","count":34,"t_s":38.2}'])
    events, other = cli.parse_events(err)
    assert [e["event"] for e in events] == ["start", "stage", "poses"]
    assert len(other) == 3
    out = 'noise\n{"ok":true,"command":"reconstruct","poses":34,"points":401,"wall_s":39.0}\n'
    s = cli.parse_summary(out)
    assert s["ok"] and s["poses"] == 34
    assert cli.parse_summary("nothing") == {}


def test_parse_free_percent():
    text = "The system has 17179869184 (1048576 pages ...)\nSystem-wide memory free percentage: 57%\n"
    assert cli.parse_free_percent(text) == 57.0
    assert cli.parse_free_percent("no numbers") is None


def test_parse_poses_shape():
    m = _m4(rot("y", 30), [0.1, 0.2, 0.3])
    doc = {"poses": [{"id": 3, "file": "a.HEIC", "transform_row_major": m.reshape(-1).tolist(),
                      "translation": [0.1, 0.2, 0.3], "quaternion_xyzw": [0, 0.259, 0, 0.966],
                      "intrinsics_row_major": [3001.1, 0, 1999.3, 0, 3001.1, 1515.3, 0, 0, 1]},
                     {"id": 4, "transform_row_major": np.eye(4).reshape(-1).tolist()}]}
    poses = cli.parse_poses(json.loads(json.dumps(doc)))
    assert poses[0].file == "a.HEIC" and poses[0].intrinsics[0, 2] == pytest.approx(1999.3)
    np.testing.assert_allclose(poses[0].centre, [0.1, 0.2, 0.3])
    assert poses[1].file is None and poses[1].intrinsics is None
    flipped = poses[0].pose(opencv_axes=True)
    np.testing.assert_allclose(flipped.r[:, 2], -poses[0].pose().r[:, 2])


def test_parse_samples_shape(tmp_path):
    depth = np.full((192, 256), 0.25, np.float32)
    depth.tofile(tmp_path / "x_depth.f32")
    np.full((192, 256), 2, np.float32).tofile(tmp_path / "x_conf.f32")
    t = _m4(rot("x", -40), [0.0, 0.2, 0.25])
    doc = {"images": [
        {"file": "x.HEIC", "width_px": 4032, "height_px": 3024, "orientation": 6, "scan_pass_id": 0,
         "session_uuid": "S", "gravity": [0.04, -0.55, -0.84],
         "bounding_box_row_major": _m4(np.diag([0.08, 0.07, 0.08]), [0, -0.18, -0.1]).reshape(-1).tolist(),
         "camera": {"intrinsics_row_major": [2922.5, 0, 2013.3, 0, 2922.5, 1508.7, 0, 0, 1],
                    "transform_row_major": t.reshape(-1).tolist(), "camera_id": 2},
         "depth": {"width": 256, "height": 192, "format": "depth_f32", "file": "x_depth.f32"},
         "confidence": {"width": 256, "height": 192, "format": "u8", "file": "x_conf.f32"}},
        {"file": "broken.HEIC", "error": "could not read"}]}
    (tmp_path / "samples.json").write_text(json.dumps(doc))
    s = cli.load_samples(tmp_path / "samples.json")
    assert len(s) == 1 and s[0].cam.width == 4032 and s[0].cam.fx == pytest.approx(2922.5)
    np.testing.assert_allclose(s[0].arkit_pose.t, [0, 0.2, 0.25])
    assert s[0].depth_shape == (192, 256)
    assert cli.read_f32(s[0].depth_file, s[0].depth_shape)[0, 0] == pytest.approx(0.25)
    box = ObjectBox(s[0].bounding_box)
    np.testing.assert_allclose(box.extent, [0.08, 0.07, 0.08])
    assert box.contains(np.array([[0.039, -0.18, -0.1]])) and not box.contains(np.array([[0.041, -0.18, -0.1]]))


def test_run_cli_reports_a_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("REPAIROS_RECON_CLI", str(tmp_path / "nope"))
    with pytest.raises(cli.ReconCliError):
        cli.run_cli(["samples"], min_free_pct=None)


def test_memory_guard_refuses_to_start_when_memory_is_low(monkeypatch):
    monkeypatch.setattr(cli, "memory_free_percent", lambda: 12.0)
    with pytest.raises(cli.MemoryGuardError):
        cli.check_free_memory(25)


# --- TSDF helpers ---------------------------------------------------------------------------------------------------

def test_find_support_plane_picks_the_table_under_the_object():
    rng = np.random.default_rng(0)
    table = np.stack([rng.uniform(-0.1, 0.1, 4000), rng.normal(-0.19, 0.0005, 4000), rng.uniform(-0.1, 0.1, 4000)], 1)
    top = np.stack([rng.uniform(-0.03, 0.03, 3000), np.full(3000, -0.166), rng.uniform(-0.03, 0.03, 3000)], 1)
    p = find_support_plane(np.concatenate([table, top]))
    assert p is not None and p.height_m == pytest.approx(-0.19, abs=0.001)


def test_keep_main_components_drops_specks():
    big_v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0]], float)
    big_f = np.array([[0, 1, 2], [1, 3, 2]] * 10)
    speck_v = big_v + 5
    v = np.concatenate([big_v, speck_v])
    f = np.concatenate([big_f, np.array([[4, 5, 6]])])
    v2, f2, dropped = keep_main_components(v, f, min_share=0.2)
    assert dropped == 1 and len(v2) == 4 and f2.max() == 3


def test_object_box_from_contract():
    b = ObjectBox.from_contract({"frame": "arkit_world", "center_m": [0, 0.1, 0], "extent_m": [0.1, 0.2, 0.3],
                                 "quaternion_xyzw": [0, 0, 0, 1]})
    np.testing.assert_allclose(b.extent, [0.1, 0.2, 0.3])
    assert len(b.corners()) == 8 and b.contains(np.array([[0.049, 0.199, 0.149]]))


def _render_depth(pose: Pose, cam: Pinhole, boxes) -> np.ndarray:
    """Analytic depth (metres along the view axis) of axis-aligned boxes and the plane y = 0."""
    from repairos.locate.camera import pixel_rays

    u, v = np.meshgrid(np.arange(cam.width) + 0.5, np.arange(cam.height) + 0.5)
    o, d = pixel_rays(np.stack([u.ravel(), v.ravel()], 1), pose, cam)
    t = np.full(len(d), np.inf)
    with np.errstate(divide="ignore", invalid="ignore"):
        tp = -o[1] / d[:, 1]
        t = np.where(tp > 0, tp, t)
        for lo, hi in boxes:
            t0, t1 = (lo - o) / d, (hi - o) / d
            tn, tf = np.minimum(t0, t1).max(1), np.maximum(t0, t1).min(1)
            hit = (tn <= tf) & (tn > 0)
            t = np.where(hit & (tn < t), tn, t)
    z = t * -(d @ pose.r[:, 2])   # distance along the view axis (-z)
    z[~np.isfinite(z)] = 0
    return z.reshape(cam.height, cam.width)


@pytest.mark.parametrize("prefer", ["numpy", "auto"])
def test_fuse_segments_merges_a_flipped_segment(prefer):
    """Top segment: a 6 x 2 x 4 cm block on a table. Bottom segment: the same block flipped onto its top in another
    world. Masking drops each segment's own table; the merged fusion spans the block's full height."""
    if prefer == "auto":
        pytest.importorskip("open3d")
    lo, hi = np.array([-0.03, 0.0, -0.02]), np.array([0.03, 0.02, 0.02])
    cam = Pinhole(96.0, 96.0, 64.0, 48.0, 128, 96)
    top_frames = [DepthFrame(f"t{i}", look_at(e, [0, 0.01, 0]), cam, None) for i, e in enumerate(dome(16, 0.25))]
    for f in top_frames:
        f.depth_m = _render_depth(f.pose, cam, [(lo, hi)])
        f.confidence = np.full(f.depth_m.shape, 2, np.uint8)
    # Bottom world: the block upside down (its old bottom now at y = 0.02), shifted; T(bottom -> top) = flip back.
    to_top = Sim3(1.0, rot("x", 180), np.array([0.0, 0.02, 0.0]))
    from_top = to_top.inverse()
    b_lo, b_hi = np.minimum(from_top.apply(lo[None]), from_top.apply(hi[None]))[0], \
        np.maximum(from_top.apply(lo[None]), from_top.apply(hi[None]))[0]
    bot_frames = [DepthFrame(f"b{i}", look_at(e, [0, 0.01, 0]), cam, None)
                  for i, e in enumerate(dome(16, 0.25, seed=5))]
    for f in bot_frames:
        f.depth_m = _render_depth(f.pose, cam, [(b_lo, b_hi)])
        f.confidence = np.full(f.depth_m.shape, 2, np.uint8)
    box_m = _m4(np.diag([0.09, 0.05, 0.07]), [0, 0.01, 0])
    top = Segment(top_frames, ObjectBox(box_m), name="top")
    bottom = Segment(bot_frames, ObjectBox(box_m), to_top.matrix, name="bottom")
    _, info = mask_segment(top)
    assert info["support_plane"]["height_m"] == pytest.approx(0.0, abs=0.002)
    alone = fuse_segments([top], prefer=prefer, spacing_m=0.002, min_component_share=0.0)
    both = fuse_segments([top, bottom], prefer=prefer, spacing_m=0.002, min_component_share=0.0)
    assert alone.points[:, 1].min() > 0.002           # the top segment cannot see the underside
    assert both.points[:, 1].min() < 0.004            # the flipped segment adds it, in the top frame
    inside = np.all((both.points > lo - 0.004) & (both.points < hi + 0.004), axis=1)
    assert inside.mean() > 0.95
    if prefer == "auto":
        assert both.triangles > 0


def test_moved_pose_applies_a_world_transform():
    p = look_at([0.2, 0.2, 0.2], [0, 0, 0])
    t = Sim3(1.0, rot("y", 90), np.array([1.0, 0, 0]))
    q = moved_pose(p, t.matrix)
    np.testing.assert_allclose(q.t, t.apply(p.t[None])[0])
    np.testing.assert_allclose(q.r, t.r @ p.r)


def test_decide_breaks_a_disagreement_with_the_lidar_clouds():
    t = Sim3(1.0, rot("y", 30), np.zeros(3))
    a = Sim3(1.0, rot("y", 37), np.array([0.012, 0, 0]))
    fits = {"reference": {"residual": {"rmse_m": 0.003}}, "moving": {"residual": {"rmse_m": 0.003}}}
    chk = AppleCheck(a, fits)
    score = {id(t): 0.26, id(a): 0.69}
    r = decide(_fm(t), None, chk, np.zeros(3), cloud_fit=lambda x: score[id(x)])
    assert r.merged and r.source == "apple_joint" and r.agreement["cloud_fit_3mm"]["apple_joint"] == 0.69
    close = {id(t): 0.60, id(a): 0.65}
    r = decide(_fm(t), None, chk, np.zeros(3), cloud_fit=lambda x: close[id(x)])
    assert not r.merged and "do not pick one clearly" in r.reasons[-1]


# --- Greptile review of PR #8 ------------------------------------------------------------------------------------
def test_apple_cross_check_keeps_the_matched_centre_when_it_drops_the_scale():
    """The two camera fits disagree on scale by 2% with the object 0.5 m from the moving world's origin: dropping
    the scale must not keep the translation computed with it (that shifts the object by 10 mm)."""
    s_a = Sim3(1.0, rot("y", 20), np.array([0.0, -0.2, 0.1]))
    s_b = Sim3(1.02, rot("y", -50) @ rot("x", 180), np.array([0.3, -0.25, -0.1]))
    ja, jb = dome(15, seed=1), dome(12, seed=2)
    ref = [cli.ApplePose(i, f"a{i}", _m4(np.eye(3), ja[i]), None) for i in range(15)]
    mov = [cli.ApplePose(i, f"b{i}", _m4(np.eye(3), jb[i]), None) for i in range(12)]
    mov_centres = {f"b{i}": s_b.apply(jb[i:i + 1])[0] + np.array([0.5, 0.0, 0.0]) for i in range(12)}
    chk = apple_cross_check(ref, mov, {f"a{i}": s_a.apply(ja[i:i + 1])[0] for i in range(15)}, mov_centres)
    s_b_off = Sim3(1.02, s_b.r, s_b.t + np.array([0.5, 0.0, 0.0]))
    want = s_a @ s_b_off.inverse()                      # moving world -> Apple -> reference, with the scale
    centre = np.mean(list(mov_centres.values()), axis=0)
    assert chk.transform.scale == 1.0 and chk.per_segment["scale_moving_to_reference"] == pytest.approx(1 / 1.02)
    shift = np.linalg.norm(chk.transform.apply(centre[None])[0] - want.apply(centre[None])[0])
    assert shift < 0.001                                 # was (1 - scale) * |centre| = 10 mm


def test_the_heic_cache_tells_same_named_images_of_two_captures_apart(tmp_path):
    from repairos.recon.merge import gray_cache_path

    a, b = tmp_path / "top" / "IMG_0001.HEIC", tmp_path / "bottom" / "IMG_0001.HEIC"
    for p, data in ((a, b"top"), (b, b"bottom")):
        p.parent.mkdir()
        p.write_bytes(data)
    cache = tmp_path / "cache"
    assert gray_cache_path(a, 1600, cache) != gray_cache_path(b, 1600, cache)
    assert gray_cache_path(a, 1600, cache) == gray_cache_path(a, 1600, cache)
    before = gray_cache_path(a, 1600, cache)
    a.write_bytes(b"top, captured again")
    assert gray_cache_path(a, 1600, cache) != before     # a replaced file is decoded again


def test_the_apple_mesh_is_read_without_open3d(tmp_path, monkeypatch):
    import sys

    from repairos.importers.ply import mesh_ply_bytes
    from repairos.recon.build import read_mesh_ply

    v = np.array([[0, 0, 0], [0.1, 0, 0], [0, 0.1, 0], [0, 0, 0.1]], float)
    f = np.array([[0, 1, 2], [0, 1, 3], [1, 2, 3]])
    p = tmp_path / "mesh.ply"
    p.write_bytes(mesh_ply_bytes(v, f))                  # the layout recon-cli writes (writeMeshPLY)
    monkeypatch.setitem(sys.modules, "open3d", None)     # the optional geometry extra is not installed
    rv, rf = read_mesh_ply(p)
    np.testing.assert_allclose(rv, v, atol=1e-6)
    np.testing.assert_array_equal(rf, f)


def test_a_mesh_left_by_an_earlier_run_is_not_the_current_one(tmp_path):
    from repairos.recon.build import apple_mesh_of

    (tmp_path / "mesh.ply").write_bytes(b"ply\n")       # an earlier run's output in a reused out dir
    poses_only = cli.CliResult({"ok": True, "poses": 3}, [], 0, 1.0, 10.0)   # --detail none, or the model failed
    assert apple_mesh_of(poses_only) is None
    now = cli.CliResult({"ok": True, "poses": 3, "mesh_file": str(tmp_path / "mesh.ply")}, [], 0, 1.0, 10.0)
    assert apple_mesh_of(now) == tmp_path / "mesh.ply"
    failed = cli.CliResult({"ok": False, "mesh_file": str(tmp_path / "mesh.ply")}, [], 1, 1.0, 10.0)
    assert apple_mesh_of(failed) is None


def test_the_apple_mesh_is_used_only_where_every_segment_sits_where_apple_put_it():
    from repairos.recon.build import apple_placed_every_segment

    agreed = {"merged": True, "source": "features+icp", "agreement": {"displacement_mm": 2.0, "rotation_deg": 0.5}}
    apple = {"merged": True, "source": "apple_joint", "agreement": {}}
    refused = {"merged": False, "source": None, "agreement": {"displacement_mm": 40.0, "rotation_deg": 9.0}}
    overruled = {"merged": True, "source": "features",
                 "agreement": {"displacement_mm": 30.0, "rotation_deg": 6.0, "cloud_fit_3mm": {"features": 0.8}}}
    unchecked = {"merged": True, "source": "features", "agreement": {}}
    assert apple_placed_every_segment([]) and apple_placed_every_segment([agreed, apple])
    for bad in (refused, overruled, unchecked):
        assert not apple_placed_every_segment([agreed, bad])


def test_posed_photos_without_depth_stay_stills(tmp_path):
    from repairos.recon.build import SegmentInput, posed_samples, stills_of

    cam = Pinhole(1400.0, 1400.0, 960.0, 720.0, 1920, 1440)
    pose = look_at([0.2, 0.2, 0.2], [0, 0, 0])
    with_depth = cli.SampleInfo("IMG_1.HEIC", 1920, 1440, pose, cam, None, None, None, None,
                                tmp_path / "d.bin", None, (192, 256))
    no_depth = cli.SampleInfo("IMG_2.HEIC", 1920, 1440, pose, cam, None, None, None, None, None, None, None)
    no_pose = cli.SampleInfo("IMG_3.HEIC", 1920, 1440, None, cam, None, None, None, None, None, None, None)
    posed = posed_samples([with_depth, no_depth, no_pose])
    assert [s.file for s in posed] == ["IMG_1.HEIC", "IMG_2.HEIC"]
    stills = stills_of([SegmentInput(tmp_path, "top")], [posed], [Sim3.identity()], [], "scan_t")
    assert [s["still_id"] for s in stills] == ["IMG_1", "IMG_2"]
    assert stills[1]["camera"]["position_m"] == pytest.approx([0.2, 0.2, 0.2])
    assert stills[1]["image_file"] == str(tmp_path / "IMG_2.HEIC") and stills[1]["width_px"] == 1920


def test_a_python_step_over_the_memory_limit_is_stopped_while_it_runs(monkeypatch):
    import time

    from repairos.recon import build as B

    monkeypatch.setattr(B, "self_rss_gb", lambda: 9.0)  # the step's resident memory, over a 6 GB limit
    t0 = time.monotonic()
    with pytest.raises(cli.MemoryGuardError, match="fusion"):
        with B.memory_watch(6.0, "fusion", poll_s=0.02):
            while time.monotonic() - t0 < 5:             # a long step that never checks memory itself
                time.sleep(0.01)
    assert time.monotonic() - t0 < 2                      # stopped during the step, not after it
