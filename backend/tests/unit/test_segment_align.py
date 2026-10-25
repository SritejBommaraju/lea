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


def _render(pose: Pose, cam: Pinhole, boxes) -> np.ndarray:
    """Analytic depth along the view axis of axis-aligned boxes on the plane y = 0 (the support)."""
    u, v = np.meshgrid(np.arange(cam.width) + 0.5, np.arange(cam.height) + 0.5)
    o, d = pixel_rays(np.stack([u.ravel(), v.ravel()], 1), pose, cam)
    t = np.full(len(d), np.inf)
    with np.errstate(divide="ignore", invalid="ignore"):
        tp = -o[1] / d[:, 1]
        t = np.where(tp > 0, tp, t)
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
