"""Flipped-segment registration on the server (repairos.recon.segment_align, repairos.pipeline.segments), on
synthetic geometry: an L-shaped object (asymmetric) and a plain box (symmetric), seen from cameras above, flipped for
the second segment in its own ARKit world (another heading and origin, same gravity)."""
from __future__ import annotations

import io

import numpy as np
import pytest

pytest.importorskip("open3d")

from repairos.locate.camera import Pinhole, Pose, pixel_rays  # noqa: E402
from repairos.pipeline import segments as S  # noqa: E402
from repairos.recon.segment_align import ObjectCloud, align_clouds, free_space_violation, rotation_deg  # noqa: E402
from repairos.recon.tsdf import DepthFrame  # noqa: E402


def rot(axis: str, deg: float) -> np.ndarray:
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return {"x": np.array([[1, 0, 0], [0, c, -s], [0, s, c]]), "y": np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]]),
            "z": np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])}[axis]


def m4(r, t):
    m = np.eye(4)
    m[:3, :3], m[:3, 3] = r, t
    return m


def look_at(eye, target) -> Pose:
    eye, target = np.asarray(eye, float), np.asarray(target, float)
    back = eye - target
    back /= np.linalg.norm(back)
    right = np.cross([0.0, 1.0, 0.0], back)
    right /= np.linalg.norm(right)
    return Pose(np.stack([right, np.cross(back, right), back], axis=1), eye)


# An L: a 10 x 3 x 6 cm slab plus a 4 x 3 x 4 cm block on one end (object frame, resting on y = 0).
L_BOXES = [(np.array([-0.05, 0.0, -0.03]), np.array([0.05, 0.03, 0.03])),
           (np.array([0.01, 0.03, -0.03]), np.array([0.05, 0.06, 0.01]))]
PLAIN_BOX = [(np.array([-0.04, 0.0, -0.025]), np.array([0.04, 0.03, 0.025]))]


def surface_points(boxes, step=0.002, colour_end=True):
    """Points on the outer faces of axis-aligned boxes with colours (the +x end red when colour_end, else grey)."""
    pts = []
    for lo, hi in boxes:
        for axis in range(3):
            for side in (lo[axis], hi[axis]):
                o = [i for i in range(3) if i != axis]
                g = np.meshgrid(*[np.arange(lo[i], hi[i] + 1e-9, step) for i in o], indexing="ij")
                p = np.zeros((g[0].size, 3))
                p[:, axis], p[:, o[0]], p[:, o[1]] = side, g[0].ravel(), g[1].ravel()
                pts.append(p)
    p = np.unique(np.round(np.concatenate(pts), 5), axis=0)
    inside = np.zeros(len(p), bool)
    for lo, hi in boxes:   # drop points buried inside another box
        inside |= np.all((p > lo + 1e-4) & (p < hi - 1e-4), axis=1)
    p = p[~inside]
    col = np.full((len(p), 3), 200, np.uint8)
    if colour_end:
        col[p[:, 0] > 0.03] = (220, 30, 30)
    return p, col


def visible_from_above(p: np.ndarray, boxes_world, n_cams=8, seed=0) -> np.ndarray:
    """Points some camera above (on a 45 deg ring) sees first along its ray (not hidden by the object)."""
    import open3d as o3d

    c = p.mean(0)
    vis = np.zeros(len(p), bool)
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(p))
    for k in range(n_cams):
        a = 2 * np.pi * k / n_cams
        cam = c + 0.4 * np.array([np.cos(a) * 0.7, 0.7, np.sin(a) * 0.7])
        _, idx = pc.hidden_point_removal(cam, 100 * np.linalg.norm(np.ptp(p, 0)))
        vis[np.asarray(idx)] = True
    return vis & (p[:, 1] > p[:, 1].min() + 0.002)


def segment_world(to_world: np.ndarray, boxes):
    """Object surface points in a world where the object frame maps by `to_world`, visible from above."""
    p, col = surface_points(boxes)
    w = p @ to_world[:3, :3].T + to_world[:3, 3]
    vis = visible_from_above(w, None)
    return ObjectCloud(w[vis], col[vis])


ON_TABLE = {"ref_plane_y": 0.0, "mov_plane_y": 0.0, "expect_flip": True}   # both rest on y = 0; a flip
FLIP_WORLD = m4(rot("y", 130) @ rot("x", 180), [0.4, 0.06, -0.2])   # object frame -> segment 1's own world


def test_flipped_l_shape_is_registered_from_scratch():
    rng = np.random.default_rng(1)
    ref = segment_world(np.eye(4), L_BOXES)
    mov = segment_world(FLIP_WORLD, L_BOXES)
    mov.points = mov.points + rng.normal(0, 0.001, mov.points.shape)
    res = align_clouds(ref, mov, voxel_m=0.003, **ON_TABLE)
    truth = np.linalg.inv(FLIP_WORLD)   # segment 1 world -> segment 0 (= object frame here)
    assert res.aligned, res.reason
    t = res.matrix
    assert rotation_deg(t[:3, :3] @ truth[:3, :3].T) < 2.0
    c = mov.points.mean(0)
    assert np.linalg.norm((t[:3, :3] @ c + t[:3, 3]) - (truth[:3, :3] @ c + truth[:3, 3])) < 0.003
    assert res.fitness >= 0.3 and res.rmse_mm < 3.0
    assert res.seconds["total_s"] < 10


def test_a_wrong_phone_estimate_is_corrected_and_a_right_one_confirmed():
    ref = segment_world(np.eye(4), L_BOXES)
    mov = segment_world(FLIP_WORLD, L_BOXES)
    truth = np.linalg.inv(FLIP_WORLD)
    bad = m4(rot("y", 35), [0.03, 0, -0.02]) @ truth
    res = align_clouds(ref, mov, voxel_m=0.003, **ON_TABLE, init=bad, init_quality=0.9)
    assert res.aligned and res.phone_estimate["verdict"] == "corrected"
    assert res.phone_estimate["rotation_error_deg"] > 30
    good = align_clouds(ref, mov, voxel_m=0.003, **ON_TABLE, init=truth, init_quality=0.9)
    assert good.aligned and good.phone_estimate["verdict"] == "confirmed"


def test_symmetric_box_is_refused_without_colour_and_decided_by_colour_or_the_phone():
    """A plain box flipped fits equally well turned 180 deg: without telling colour the server refuses rather than
    guess; a coloured end decides; a confident phone estimate picks among the poses the shape allows."""
    flip = m4(FLIP_WORLD[:3, :3], [0.4, 0.03, -0.2])   # the 3 cm box rests on the table after the flip
    p, col = surface_points(PLAIN_BOX, colour_end=False)
    ref_w, mov_w = p, p @ flip[:3, :3].T + flip[:3, 3]
    vr, vm = visible_from_above(ref_w, None), visible_from_above(mov_w, None)
    ref, mov = ObjectCloud(ref_w[vr], col[vr]), ObjectCloud(mov_w[vm], col[vm])
    truth = np.linalg.inv(flip)
    res = align_clouds(ref, mov, voxel_m=0.003, **ON_TABLE)
    assert not res.aligned and "symmetric" in res.reason
    near = m4(rot("y", 6), [0.004, 0, 0]) @ truth
    by_phone = align_clouds(ref, mov, voxel_m=0.003, **ON_TABLE, init=near, init_quality=0.8)
    assert by_phone.aligned and rotation_deg(by_phone.matrix[:3, :3] @ truth[:3, :3].T) < 3
    weak_phone = align_clouds(ref, mov, voxel_m=0.003, **ON_TABLE, init=truth, init_quality=0.2)
    assert not weak_phone.aligned
    p2, col2 = surface_points(PLAIN_BOX, colour_end=True)
    mov2_w = p2 @ flip[:3, :3].T + flip[:3, 3]
    vr2, vm2 = visible_from_above(p2, None), visible_from_above(mov2_w, None)
    coloured = align_clouds(ObjectCloud(p2[vr2], col2[vr2]), ObjectCloud(mov2_w[vm2], col2[vm2]), voxel_m=0.003,
                           **ON_TABLE)
    assert coloured.aligned, coloured.reason
    assert rotation_deg(coloured.matrix[:3, :3] @ truth[:3, :3].T) < 3
    assert "colour decided" in (coloured.tie_break or "")


def test_too_little_overlap_is_rejected_with_the_reason():
    ref = segment_world(np.eye(4), L_BOXES)
    rng = np.random.default_rng(0)
    noise = ObjectCloud(rng.uniform(-0.05, 0.05, (3000, 3)) + [0.4, 0.03, 0], None)
    res = align_clouds(ref, noise, voxel_m=0.003, gravity_sweep=False)
    assert not res.aligned and res.reason and res.transform is None


def _render(pose: Pose, cam: Pinhole, boxes, table_half=None) -> np.ndarray:
    """Analytic depth along the view axis of axis-aligned boxes on the plane y = 0 (the support; with `table_half`
    (x, z) only |x|, |z| up to it: a small stand)."""
    u, v = np.meshgrid(np.arange(cam.width) + 0.5, np.arange(cam.height) + 0.5)
    o, d = pixel_rays(np.stack([u.ravel(), v.ravel()], 1), pose, cam)
    t = np.full(len(d), np.inf)
    with np.errstate(divide="ignore", invalid="ignore"):
        tp = -o[1] / d[:, 1]
        on = tp > 0
        if table_half is not None:
            hit = o + tp[:, None] * d
            on &= (np.abs(hit[:, 0]) <= table_half[0]) & (np.abs(hit[:, 2]) <= table_half[1])
        t = np.where(on, tp, t)
        for lo, hi in boxes:
            t0, t1 = (lo - o) / d, (hi - o) / d
            tn, tf = np.minimum(t0, t1).max(1), np.maximum(t0, t1).min(1)
            t = np.where((tn <= tf) & (tn > 0) & (tn < t), tn, t)
    z = t * -(d @ pose.r[:, 2])
    z[~np.isfinite(z)] = 0
    return z.reshape(cam.height, cam.width)


def test_free_space_check_flags_points_floating_in_front_of_measured_depth():
    cam = Pinhole(120.0, 120.0, 64.0, 48.0, 128, 96)
    frames = []
    for k in range(6):
        a = 2 * np.pi * k / 6
        pose = look_at([0.3 * np.cos(a), 0.3, 0.3 * np.sin(a)], [0, 0.015, 0])
        frames.append(DepthFrame(f"f{k}", pose, cam, _render(pose, cam, L_BOXES), np.full((96, 128), 2, np.uint8)))
    on, _ = surface_points(L_BOXES)
    own = free_space_violation(on, frames, margin_m=0.01)   # silhouette pixels at this coarse depth give a baseline
    floating = on + [0.0, 0.05, 0.0]
    assert free_space_violation(floating, frames, margin_m=0.01) > own + 0.4


# --- the pipeline seam ----------------------------------------------------------------------------------------------

def test_unplaced_segments_follow_the_server_registration():
    pkg = {"segments": [
        {"segment": 1, "reason": "continued"},
        {"segment": 2, "reason": "continued", "aligned": False},
        {"segment": 3, "reason": "flipped", "object_moved": True, "aligned": False},
        {"segment": 4, "reason": "flipped", "object_moved": True, "aligned": True,
         "transform_to_segment0": list(np.eye(4).reshape(-1)), "transform_quality": 0.9},
        {"segment": 5, "reason": "flip"}]}
    assert S.unplaced_segments(pkg, None) == {2, 3, 4, 5}          # the phone's transform alone is not trusted
    rec = {"segments": [{"segment": 3, "aligned": True, "transform": list(np.eye(4).reshape(-1))},
                        {"segment": 4, "aligned": False, "transform": None}]}
    assert S.unplaced_segments(pkg, rec) == {2, 4, 5}
    assert set(S.aligned_transforms(rec)) == {3}


def test_restate_still_moves_the_camera_into_segment_0():
    pose = look_at([0.2, 0.3, 0.1], [0, 0, 0])
    still = {"still_id": "s1", "segment": 1, "pose_reliable": False, "camera": pose.to_contract()}
    t = m4(rot("y", 90) @ rot("x", 180), [0.1, 0.05, 0])
    out = S.restate_still(still, t)
    moved = Pose.from_contract(out["camera"])
    np.testing.assert_allclose(moved.t, t[:3, :3] @ pose.t + t[:3, 3], atol=1e-9)
    np.testing.assert_allclose(moved.r, t[:3, :3] @ pose.r, atol=1e-6)
    assert out["pose_reliable"] is True and still["pose_reliable"] is False


def test_segment_box_prefers_its_own_then_the_transform_then_a_grown_box():
    box0 = {"frame": "arkit_world", "center_m": [0, 0.02, 0], "extent_m": [0.1, 0.04, 0.06],
            "quaternion_xyzw": [0, 0, 0, 1]}
    own = {**box0, "center_m": [1.0, 0.02, 1.0]}
    pkg = {"crop_box": box0}
    b, how = S.segment_box(pkg, {"segment": 1, "crop_box": own}, None)
    assert how == "segment's own box" and np.allclose(b.centre, [1.0, 0.02, 1.0])
    t = m4(np.eye(3), [0.5, 0, 0])
    b, how = S.segment_box(pkg, {"segment": 1}, t)
    assert "transform" in how and np.allclose(b.centre, [-0.5, 0.02, 0])
    b, how = S.segment_box(pkg, {"segment": 1}, None)
    assert "grown" in how and np.allclose(b.extent, [0.25, 0.06, 0.15])


def test_report_segments_list_unplaced_findings():
    pkg = {"segments": [{"segment": 1, "reason": "flipped", "object_moved": True},
                        {"segment": 2, "reason": "continued"}]}
    rec = {"segments": [{"segment": 1, "aligned": False, "fitness": 0.21, "rmse_mm": 2.4,
                         "why_not": "best fit too weak"}]}
    out = S.report_segments(pkg, rec, {1: ["f2", "f1"]})
    assert out == [{"segment": 1, "reason": "flipped", "object_moved": True, "aligned": False, "fitness": 0.21,
                    "rmse_mm": 2.4, "why_not": "best fit too weak", "findings_not_placed": ["f1", "f2"]}]
    ok = S.report_segments(pkg, {"segments": [{"segment": 1, "aligned": True, "fitness": 0.6, "rmse_mm": 1.9,
                                               "method": "x"}]}, {})
    assert ok[0]["aligned"] and ok[0]["findings_not_placed"] == [] and "why_not" not in ok[0]


def test_findings_by_segment_and_reason_words():
    from repairos.pipeline.run import findings_by_segment, not_aligned_reason

    stills = [{"still_id": "a", "segment": 0}, {"still_id": "b", "segment": 1}]
    findings = [{"finding_id": "f1", "evidence_refs": {"still_ids": ["a"]}},
                {"finding_id": "f2", "evidence_refs": {"still_ids": ["b"]}},
                {"finding_id": "f3", "evidence_refs": {"still_ids": ["b"]}}]
    locs = {"f1": {"status": "not_computed"}, "f2": {"status": "not_computed"}, "f3": {"status": "computed"}}
    assert findings_by_segment(stills, findings, locs) == {1: ["f2"]}
    scan = {"segments": [{"segment": 1, "reason": "flipped", "object_moved": True}]}
    rec = {"segments": [{"segment": 1, "aligned": False, "why_not": "symmetric object: colours do not tell"}]}
    assert "symmetric object" in not_aligned_reason(scan, rec, 1)


def _png16(a: np.ndarray) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(np.clip(a * 1000, 0, 65535).astype(np.uint16)).save(buf, format="PNG")
    return buf.getvalue()


def test_align_scan_segments_end_to_end_from_keyframes(tmp_path):
    """A scan package with depth keyframes for segment 0 (the L upright) and segment 1 (flipped, in its own world):
    the server registers segment 1 and returns its cloud in segment 0's frame."""
    from PIL import Image

    cam = Pinhole(240.0, 240.0, 128.0, 96.0, 256, 192)
    flip = m4(rot("y", 90) @ rot("x", 180), [0.4, 0.06, -0.2])   # a quarter turn keeps the boxes axis-aligned

    def boxes_in(t):
        out = []
        for lo, hi in L_BOXES:
            c = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
            w = c @ t[:3, :3].T + t[:3, 3]
            out.append((w.min(0), w.max(0)))
        return out

    kfs = []
    for seg, t in ((0, np.eye(4)), (1, flip)):
        centre = t[:3, :3] @ np.array([0.0, 0.03, 0.0]) + t[:3, 3]
        for k in range(10):
            a = 2 * np.pi * k / 10
            pose = look_at(centre + [0.25 * np.cos(a), 0.25, 0.25 * np.sin(a)], centre)
            depth = _render(pose, cam, boxes_in(t))
            stem = tmp_path / f"s{seg}_{k}"
            (stem.with_suffix(".png")).write_bytes(_png16(depth))
            Image.fromarray(np.full((192, 256), 2, np.uint8)).save(f"{stem}_conf.png")
            Image.fromarray(np.full((192, 256, 3), 180, np.uint8)).save(f"{stem}.jpg")
            kfs.append({"keyframe_id": f"kf{seg}_{k}", "segment": seg, "camera": pose.to_contract(),
                        "intrinsics": {"fx": cam.fx, "fy": cam.fy, "cx": cam.cx, "cy": cam.cy, "width_px": 256,
                                       "height_px": 192},
                        "image_file": f"{stem}.jpg", "depth_file": str(stem.with_suffix(".png")),
                        "depth_encoding": "uint16_mm", "confidence_file": f"{stem}_conf.png",
                        "timestamp_s": float(k), "tracking_state": "normal"})
    box0 = {"frame": "arkit_world", "center_m": [0, 0.03, 0], "extent_m": [0.14, 0.08, 0.1],
            "quaternion_xyzw": [0, 0, 0, 1]}
    q = Pose(flip[:3, :3], flip[:3, 3]).to_contract()["quaternion_xyzw"]
    box1 = {"frame": "arkit_world", "center_m": list(flip[:3, :3] @ np.array([0, 0.03, 0]) + flip[:3, 3]),
            "extent_m": [0.14, 0.08, 0.1], "quaternion_xyzw": q}
    pkg = {"scan_id": "scan_t", "crop_box": box0, "keyframes": kfs,
           "support_plane": {"transform": list(np.eye(4).reshape(-1))},
           "segments": [{"segment": 1, "reason": "flipped", "object_moved": True, "aligned": False,
                         "crop_box": box1}]}
    rec, clouds = S.align_scan_segments(pkg)
    seg = rec["segments"][0]
    assert seg["aligned"], seg["why_not"]
    t = np.asarray(seg["transform"]).reshape(4, 4)
    truth = np.linalg.inv(flip)
    assert rotation_deg(t[:3, :3] @ truth[:3, :3].T) < 3
    assert np.linalg.norm(t[:3, 3] - truth[:3, 3]) < 0.01
    assert 1 in clouds and len(clouds[1].points) > 100
    assert rec["seconds"] < 10
    assert S.unplaced_segments(pkg, rec) == set()


def test_bottom_not_placed_lists_unaligned_findings_with_their_crops():
    from repairos.report.build import bottom_not_placed

    report = {"findings": [{"finding_id": "r1", "finding_ids": ["f1"], "subject": {"kind": "part", "ref": "U1"},
                            "part_class": "chip", "issue": "damaged",
                            "crop": {"still_id": "s9", "box_px": [1, 2, 30, 40], "crop_ref": "blob://sha256:ab"}},
                           {"finding_id": "r2", "crop": {"still_id": "s1", "box_px": [0, 0, 5, 5]}}],
              "insufficient_evidence": [{"finding_id": "f3", "photo": {"still_id": "s9"}, "reason": "blurry"}]}
    segs = [{"segment": 1, "aligned": False, "findings_not_placed": ["f1", "f3"]}]
    locs = {"f1": {"status": "not_computed", "reason": "the bottom could not be lined up"}}
    out = bottom_not_placed(report, segs, locs)
    assert [e["finding_id"] for e in out] == ["r1", "f3"]
    assert out[0]["photo"] == {"still_id": "s9", "box_px": [1, 2, 30, 40], "crop_ref": "blob://sha256:ab"}
    assert out[0]["reason"] == "the bottom could not be lined up" and out[0]["issue"] == "damaged"
    assert "issue" not in out[1] and out[1]["segment"] == 1
    assert bottom_not_placed(report, [{"segment": 1, "aligned": True, "findings_not_placed": []}], locs) == []


# --- what the first real flip taught (a controller on a padded stool, 2026-10-24) ------------------------------------

def _boxes_in(t, boxes=L_BOXES):
    out = []
    for lo, hi in boxes:
        c = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        w = c @ t[:3, :3].T + t[:3, 3]
        out.append((w.min(0), w.max(0)))
    return out


def _frames(t, *, drift_m=0.0, n=8):
    """Depth frames around the object placed by `t` (axis-aligned boxes on y = 0). `drift_m`: each frame's recorded
    pose is off vertically by up to this much (ARKit drift: the depth is right, the pose is not)."""
    cam = Pinhole(120.0, 120.0, 64.0, 48.0, 128, 96)
    centre = t[:3, :3] @ np.array([0.0, 0.03, 0.0]) + t[:3, 3]
    out = []
    for k in range(n):
        a = 2 * np.pi * k / n
        pose = look_at(centre + [0.3 * np.cos(a), 0.3, 0.3 * np.sin(a)], centre)
        depth = _render(pose, cam, _boxes_in(t))
        off = drift_m * (2 * (k % 2) - 1)
        out.append(DepthFrame(f"f{k}", Pose(pose.r, pose.t + [0.0, off, 0.0]), cam, depth,
                              np.full((96, 128), 2, np.uint8)))
    return out


def test_a_drifting_segment_does_not_hide_an_underside_laid_on_the_top_face():
    """Segment 0's cameras drift (its own cloud sits in their free space a lot), segment 1's do not. A pose that lays
    the underside on the top face must still be dropped: each segment's free space is judged against that segment's
    own baseline, never against the worse of the two (which let the drift excuse anything)."""
    flip = m4(rot("x", 180), [0.4, 0.06, -0.2])           # object -> segment 1's world, the L upside down on y = 0
    truth = np.linalg.inv(flip)
    p, col = surface_points(L_BOXES)
    vr = visible_from_above(p, None)
    mw = p @ flip[:3, :3].T + flip[:3, 3]
    vm = visible_from_above(mw, None)
    ref, mov = ObjectCloud(p[vr], col[vr]), ObjectCloud(mw[vm], col[vm])
    frames0, frames1 = _frames(np.eye(4), drift_m=0.04), _frames(flip)
    wrong = m4(np.eye(3), [0.0, 0.03, 0.0]) @ truth       # the underside lifted onto the slab's top face
    kw = {"voxel_m": 0.003, "ref_frames": frames0, "mov_frames": frames1, "gravity_sweep": False, "seeds": (),
          "ref_plane_y": 0.0, "mov_plane_y": 0.0, "expect_flip": True}
    res = align_clouds(ref, mov, init=wrong, init_quality=0.9, **kw)
    assert free_space_violation(ref.points, frames0, margin_m=0.025) > 0.2   # the drift is real in this scene
    if res.aligned:   # only the true pose (found from a flip of the wrong one) may come out
        assert rotation_deg(res.matrix[:3, :3] @ truth[:3, :3].T) < 3
        assert np.linalg.norm(res.matrix[:3, 3] - truth[:3, 3]) < 0.01
    else:
        assert "segment 0" in res.reason
    right = align_clouds(ref, mov, init=truth, init_quality=0.9, **kw)
    assert right.aligned, right.reason


def test_gravity_starts_put_a_flipped_segment_back_on_the_table():
    """Both segments rest on their tables: an upside-down start puts the segment's table contact at the object's top,
    so a flat object's underside starts under its top face rather than on it."""
    from repairos.recon.segment_align import _gravity_starts

    flip = m4(rot("x", 180), [0.4, 0.06 + 0.5, -0.2])     # segment 1's table is 0.5 m higher in its own world
    p, _ = surface_points(L_BOXES)
    mov = p @ flip[:3, :3].T + flip[:3, 3]
    starts = dict(_gravity_starts(mov, p, src_plane_y=0.5, tgt_plane_y=0.0))
    for name, m in starts.items():
        if "_side_" in name:   # a side start is centre on centre: the table alone cannot give its height
            continue
        y = (mov @ m[:3, :3].T + m[:3, 3])[:, 1]
        assert abs(y.min()) < 0.004 and abs(y.max() - 0.06) < 0.004, name
    assert sum("_side_" in n for n in starts) == 24


def test_an_anchored_floor_under_a_stool_is_replaced_by_the_measured_support():
    """ARKit anchors the largest plane it finds; under a stool or a small table that is the floor. The depth around
    the box finds the real support, and an anchor that agrees with it (within ARKit's drift) is kept."""
    from repairos.pipeline.mesh import plane_height
    from repairos.recon.tsdf import ObjectBox

    frames = _frames(np.eye(4))
    box = ObjectBox.from_contract({"center_m": [0, 0.03, 0], "extent_m": [0.14, 0.07, 0.1],
                                   "quaternion_xyzw": [0, 0, 0, 1]})
    floor = {"support_plane": {"transform": [1, 0, 0, 0, 0, 1, 0, -0.62, 0, 0, 1, 0, 0, 0, 0, 1]}}
    assert plane_height(floor) == -0.62                       # without depth: the anchor as before
    assert abs(plane_height(floor, frames, box)) < 0.003
    near = {"support_plane": {"transform": [1, 0, 0, 0, 0, 1, 0, 0.012, 0, 0, 1, 0, 0, 0, 0, 1]}}
    assert plane_height(near, frames, box) == 0.012
    assert abs(plane_height({}, frames, box)) < 0.003         # no anchor: the measured one
    assert plane_height({**floor, "pose_source": "object_tracked"}, frames, box) is None


def test_a_flat_top_seen_from_above_never_replaces_a_right_anchor():
    """Seen mostly from above, a flat enclosure's top gives more depth than the strip of table around it, so the
    lowest well-supported layer over the whole box is the top. A measured layer must be confirmed outside the
    object's footprint: the strip of table confirms the anchor instead, and with no table seen around the object the
    anchor stays."""
    from repairos.locate.camera import backproject
    from repairos.pipeline.mesh import plane_height
    from repairos.recon.tsdf import ObjectBox, find_support_plane

    cam = Pinhole(120.0, 120.0, 64.0, 48.0, 128, 96)
    lid = [(np.array([-0.08, 0.0, -0.06]), np.array([0.08, 0.04, 0.06]))]      # 16 x 4 x 12 cm, top at 4 cm
    box = ObjectBox.from_contract({"center_m": [0, 0.02, 0], "extent_m": [0.16, 0.04, 0.12],
                                   "quaternion_xyzw": [0, 0, 0, 1]})
    anchor = {"support_plane": {"transform": list(np.eye(4).reshape(-1)), "extent_m": [1.0, 1.0]}}

    def frames(table_half):
        out = []
        for k in range(8):
            a = 2 * np.pi * k / 8
            pose = look_at([0.08 * np.cos(a), 0.35, 0.08 * np.sin(a)], [0.0, 0.02, 0.0])
            out.append(DepthFrame(f"f{k}", pose, cam, _render(pose, cam, lid, table_half),
                                  np.full((96, 128), 2, np.uint8)))
        return out

    strip = frames((0.095, 0.075))                  # a 1.5 cm strip of stand around the lid
    pts = np.concatenate([f.pose.to_world(backproject(f.depth_m, f.depth_cam))[f.depth_m > 0] for f in strip])
    whole = find_support_plane(pts[box.contains(pts, 0.3)])
    assert whole is not None and abs(whole.height_m - 0.04) < 0.003   # over the whole box the top wins
    assert plane_height(anchor, strip, box) == 0.0
    assert plane_height(anchor, frames((0.08, 0.06)), box) == 0.0     # nothing around it: the anchor stays
    floor = {"support_plane": {"transform": [1, 0, 0, 0, 0, 1, 0, -0.62, 0, 0, 1, 0, 0, 0, 0, 1]}}
    assert abs(plane_height(floor, strip, box)) < 0.003               # a floor anchor is still replaced by the stand


def test_an_object_laid_on_its_side_is_registered():
    """Many things cannot rest upside down (a mouse rocks, a controller stands on its triggers): the person lays them on
    a side. The gravity sweep's side starts find that pose, which ICP then refines like the others."""
    rng = np.random.default_rng(3)
    r = rot("y", 70) @ rot("z", 90)
    p, _ = surface_points(L_BOXES)
    lift = -float((p @ r.T)[:, 1].min())   # resting on y = 0 in its own world
    side_world = m4(r, [-0.3, lift, 0.25])
    ref = segment_world(np.eye(4), L_BOXES)
    mov = segment_world(side_world, L_BOXES)
    mov.points = mov.points + rng.normal(0, 0.001, mov.points.shape)
    res = align_clouds(ref, mov, voxel_m=0.003, **ON_TABLE)
    truth = np.linalg.inv(side_world)
    assert res.aligned, res.reason
    assert rotation_deg(res.matrix[:3, :3] @ truth[:3, :3].T) < 3.0
    assert res.seconds["total_s"] < 10


# --- what four real flips taught (controller, board, mouse, controller on a stool; 2026-10-25) ------------------------

def _tilted(pose: Pose, deg: float, about: np.ndarray, axis: str = "x") -> Pose:
    """`pose` turned by `deg` about a horizontal axis through `about`: the recorded pose of a frame whose cloud leans
    (LiDAR and pose error tilt a frame's table 1-4 deg in the world on real captures)."""
    r = rot(axis, deg)
    return Pose(r @ pose.r, r @ (pose.t - about) + about)


def _faces(frame: DepthFrame) -> tuple[np.ndarray, np.ndarray]:
    """World points of a frame's depth (its true pose) and whether each lies on a vertical face (not the table, not a
    top)."""
    from repairos.locate.camera import backproject

    p = frame.pose.to_world(backproject(frame.depth_m, frame.depth_cam))
    tops = {round(hi[1], 4) for _, hi in L_BOXES}
    flat = (np.abs(p[..., 1]) < 1e-3) | np.any([np.abs(p[..., 1] - y) < 1e-3 for y in tops], axis=0)
    return p, (frame.depth_m > 0) & ~flat


def _dist_to_l(points: np.ndarray) -> np.ndarray:
    from repairos.recon.segment_align import _Nearest

    surf, _ = surface_points(L_BOXES, step=0.001)
    return _Nearest(surf)(points)[1]


BIG_BOX = {"center_m": [0, 0.04, 0], "extent_m": [0.3, 0.08, 0.3], "quaternion_xyzw": [0, 0, 0, 1]}  # 3x the object


def test_each_frame_cuts_its_own_leaning_table():
    """A frame's table is flat to a millimetre but leans a few degrees in the world, a different way per frame, and
    LiDAR noise has long tails. One horizontal cut at twice the noise leaves a layer of table in the object (each
    frame's tail above the cut, frame after frame); the cut along each frame's own table, at its tail mirrored from
    below, leaves the object alone, and the table height it reports is the one under the object."""
    from repairos.pipeline.mesh import object_depths
    from repairos.recon.segment_align import cloud_from_frames, segment_object_cloud
    from repairos.recon.tsdf import ObjectBox

    rng = np.random.default_rng(2)
    box = ObjectBox.from_contract(BIG_BOX)
    centre = np.array([0.0, 0.0, 0.0])
    frames = []
    for k, f in enumerate(_frames(np.eye(4), n=12)):
        d = f.depth_m.copy()
        on = d > 0
        d[on] += rng.normal(0, 0.0008, on.sum()) + np.where(rng.random(on.sum()) < 0.03,
                                                             rng.normal(0, 0.006, on.sum()), 0.0)
        frames.append(DepthFrame(f.name, _tilted(f.pose, 1.5 * (1 if k % 2 else -1), centre, "xz"[(k // 2) % 2]),
                                 f.depth_cam, d, f.confidence))
    cloud, table = segment_object_cloud(frames, box, 0.0, voxel_m=0.0015)
    assert table.frames == len(frames) and abs(table.tilt_deg - 1.5) < 0.5 and abs(table.height_m) < 0.002
    assert np.mean(_dist_to_l(cloud.points) > 0.008) < 0.005           # the object alone
    depths, _ = object_depths(frames, box, 0.0)                          # the one horizontal cut, as before
    old = cloud_from_frames(frames, depths, voxel_m=0.0015)
    assert np.mean(_dist_to_l(old.points) > 0.008) > 0.03                # table came along


def test_medium_confidence_sides_are_the_surface_a_flip_shares():
    """Dark plastic seen obliquely is never high confidence: the sides are confidence 1. They are kept, since a flipped
    object shares nothing with its first scan but its sides."""
    from repairos.recon.segment_align import cloud_from_frames, segment_object_cloud, segment_object_depths
    from repairos.recon.tsdf import ObjectBox

    box = ObjectBox.from_contract(BIG_BOX)
    frames = []
    for f in _frames(np.eye(4)):
        _, side = _faces(f)
        frames.append(DepthFrame(f.name, f.pose, f.depth_cam, f.depth_m, np.where(side, 1, 2).astype(np.uint8)))
    cloud, _ = segment_object_cloud(frames, box, 0.0, voxel_m=0.0015)
    on_side = (np.abs(np.abs(cloud.points[:, 0]) - 0.05) < 0.002) & (cloud.points[:, 1] > 0.008) & \
        (cloud.points[:, 1] < 0.025)
    assert on_side.sum() > 50
    high, _ = segment_object_depths(frames, box, 0.0, min_confidence=2)
    only_high = cloud_from_frames(frames, high, voxel_m=0.0015)
    assert ((np.abs(np.abs(only_high.points[:, 0]) - 0.05) < 0.002) & (only_high.points[:, 1] > 0.008)
            & (only_high.points[:, 1] < 0.025)).sum() == 0


def test_one_frames_noise_does_not_survive():
    """Pixels thrown a few centimetres off in one frame (mixed pixels, multipath) do not repeat in the others: a voxel
    needs several frames, and what most views see through is carved."""
    from repairos.recon.segment_align import cloud_from_frames, segment_object_cloud, segment_object_depths
    from repairos.recon.tsdf import ObjectBox

    rng = np.random.default_rng(5)
    box = ObjectBox.from_contract(BIG_BOX)
    frames = []
    for f in _frames(np.eye(4), n=12):
        d = f.depth_m.copy()
        hit = (rng.random(d.shape) < 0.01) & (d > 0)
        d[hit] -= rng.uniform(0.02, 0.05, hit.sum())
        frames.append(DepthFrame(f.name, f.pose, f.depth_cam, d, f.confidence))
    cloud, _ = segment_object_cloud(frames, box, 0.0, voxel_m=0.0015)
    assert np.mean(_dist_to_l(cloud.points) > 0.006) < 0.005
    raw, _ = segment_object_depths(frames, box, 0.0)
    assert np.mean(_dist_to_l(cloud_from_frames(frames, raw, voxel_m=0.0015).points) > 0.006) > 0.02


def test_free_space_ignores_drifted_silhouettes_but_not_a_floating_surface():
    """ARKit drift shifts each frame's silhouette by about a centimetre: judged by any one frame, a cloud's own points
    sit in empty space a lot (42% on a real controller). Judged against the nearest surface within the drift and by
    most of the views that can see a point, its own cloud is clean, and a surface floating over the object is not."""
    frames = []
    for k, f in enumerate(_frames(np.eye(4))):
        shift = np.array([0.01 * (1 if k % 2 else -1), 0.0, 0.006 * (1 if k % 4 < 2 else -1)])
        frames.append(DepthFrame(f.name, Pose(f.pose.r, f.pose.t + shift), f.depth_cam, f.depth_m, f.confidence))
    on, _ = surface_points(L_BOXES)
    raw = free_space_violation(on, frames, margin_m=0.005, drift_m=0.0, vote=0.0)
    robust = free_space_violation(on, frames, margin_m=0.005)
    assert raw > 0.05 and robust < 0.01
    assert free_space_violation(on + [0.0, 0.05, 0.0], frames, margin_m=0.005) > 0.4


# An L-shaped part (its outline from above tells every heading apart), 4 cm tall like a mouse.
FLAT_L = [(np.array([-0.05, 0.0, -0.03]), np.array([0.05, 0.04, 0.03])),
          (np.array([0.02, 0.0, 0.03]), np.array([0.05, 0.04, 0.06]))]


def _dark_sided_segment(t: np.ndarray, boxes, *, unseen_below=0.014, n=10):
    """Frames of `boxes` placed by `t` (on y = 0), whose lower sides return no confident depth (dark plastic near the
    table, as on the real flips): each segment sees its top and the upper part of its sides only."""
    from repairos.locate.camera import backproject

    cam = Pinhole(160.0, 160.0, 80.0, 60.0, 160, 120)
    placed = _boxes_in(t, boxes)
    centre = t[:3, :3] @ np.array([0.0, 0.02, 0.0]) + t[:3, 3]
    tops = {round(hi[1], 4) for _, hi in placed}
    frames = []
    for k in range(n):
        a = 2 * np.pi * k / n
        pose = look_at(centre + [0.28 * np.cos(a), 0.25, 0.28 * np.sin(a)], centre)
        depth = _render(pose, cam, placed)
        p = pose.to_world(backproject(depth, cam))
        flat = (np.abs(p[..., 1]) < 1e-3) | np.any([np.abs(p[..., 1] - y) < 1e-3 for y in tops], axis=0)
        conf = np.where(~flat & (p[..., 1] < unseen_below), 0, 2).astype(np.uint8)
        frames.append(DepthFrame(f"f{k}", pose, cam, depth, conf))
    return frames


def _flat_l_flip():
    from repairos.recon.segment_align import segment_object_cloud
    from repairos.recon.tsdf import ObjectBox

    flip = m4(rot("y", 90) @ rot("x", 180), [0.35, 0.04, -0.2])    # a quarter turn keeps the boxes axis-aligned
    out = {}
    for name, t in (("ref", np.eye(4)), ("mov", flip)):
        frames = _dark_sided_segment(t, FLAT_L)
        c = t[:3, :3] @ np.array([0.0, 0.02, 0.015]) + t[:3, 3]
        box = ObjectBox.from_contract({"center_m": list(c), "extent_m": [0.24, 0.07, 0.24],
                                       "quaternion_xyzw": [0, 0, 0, 1]})
        cloud, table = segment_object_cloud(frames, box, 0.0, voxel_m=0.0015)
        out[name] = (cloud, frames, table.height_m)
    return out, np.linalg.inv(flip)


def test_a_flip_sharing_only_a_band_of_side_is_registered_on_its_tables():
    """Each segment sees its top and the upper sides, so the two share only a band of side: too little overlap for the
    old 30% fitness. Footprint starts (the outline from above) kept on both tables find the pose, and it is accepted
    because it touches both tables and both segments' cameras find nothing of it in empty space."""
    seg, truth = _flat_l_flip()
    (ref, f0, p0), (mov, f1, p1) = seg["ref"], seg["mov"]
    res = align_clouds(ref, mov, voxel_m=0.003, ref_frames=f0, mov_frames=f1, ref_plane_y=p0, mov_plane_y=p1,
                       expect_flip=True)
    assert res.aligned, res.reason
    assert rotation_deg(res.matrix[:3, :3] @ truth[:3, :3].T) < 4
    c = mov.points.mean(0)
    assert np.linalg.norm(res.matrix[:3, :3] @ c + res.matrix[:3, 3] - (truth[:3, :3] @ c + truth[:3, 3])) < 0.006
    assert res.seconds["total_s"] < 10


def test_wrong_poses_of_a_low_overlap_flip_are_refused():
    """The poses a low-overlap flip invites: kept upright, the underside laid on the top face (floating over the
    table it rested on), and turned to the wrong heading. Each, offered as the phone's estimate with no other start,
    is refused, or only the true pose comes out of it."""
    seg, truth = _flat_l_flip()
    (ref, f0, p0), (mov, f1, p1) = seg["ref"], seg["mov"]
    c = ref.points.mean(0)
    upright = m4(rot("x", 180), c - rot("x", 180) @ c) @ truth
    lifted = m4(np.eye(3), [0, 0.04, 0]) @ truth
    turned = {deg: m4(rot("y", deg), c - rot("y", deg) @ c) @ truth for deg in (30, 90, 180)}
    kw = {"voxel_m": 0.003, "ref_frames": f0, "mov_frames": f1, "ref_plane_y": p0, "mov_plane_y": p1,
          "expect_flip": True, "gravity_sweep": False, "seeds": ()}
    wrongs = {"upright": upright, "on the top face": lifted, **{f"turned {d}": w for d, w in turned.items()}}
    for name, wrong in wrongs.items():
        res = align_clouds(ref, mov, init=wrong, init_quality=0.9, **kw)
        if res.aligned:
            assert rotation_deg(res.matrix[:3, :3] @ truth[:3, :3].T) < 4, name
        else:
            assert res.reason, name


def test_without_depth_frames_a_small_overlap_is_never_enough():
    """Without the segments' depth frames nothing judges free space: a turned-about pose fits a low-overlap flip's
    shape and both tables as well as the true one (it did on a real mouse). Only the 30% overlap may then accept."""
    seg, _ = _flat_l_flip()
    (ref, _, p0), (mov, _, p1) = seg["ref"], seg["mov"]
    res = align_clouds(ref, mov, voxel_m=0.003, ref_plane_y=p0, mov_plane_y=p1, expect_flip=True)
    assert not res.aligned or res.fitness >= 0.30


def test_mixed_pixels_at_the_silhouette_are_dropped_for_the_mesh():
    """For the mesh, medium-confidence depth within a couple of pixels of the object's silhouette is dropped (its depth
    blends the object and the table behind it: an orange skirt round the rim); high confidence and the rest stay."""
    from repairos.recon.segment_align import segment_object_depths
    from repairos.recon.tsdf import ObjectBox

    box = ObjectBox.from_contract(BIG_BOX)
    frames = []
    for f in _frames(np.eye(4)):
        _, side = _faces(f)
        frames.append(DepthFrame(f.name, f.pose, f.depth_cam, f.depth_m, np.where(side, 1, 2).astype(np.uint8)))
    plain, _ = segment_object_depths(frames, box, 0.0)
    edged, _ = segment_object_depths(frames, box, 0.0, edge_px=2)
    for f, a, b in zip(frames, plain, edged, strict=True):
        assert np.all(b[f.confidence == 2] == a[f.confidence == 2])          # high confidence untouched
        assert (b > 0).sum() < (a > 0).sum()                                  # some rim pixels gone
        assert ((b > 0) & (f.confidence == 1)).sum() > 0.3 * ((a > 0) & (f.confidence == 1)).sum()   # sides stay
