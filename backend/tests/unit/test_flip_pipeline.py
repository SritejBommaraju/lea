"""A flipped bottom segment through the pipeline: the scan package arrives with segment 1 (object_moved, aligned
false, in its own ARKit world), the server registers it (ensure_segment_alignment), a finding on the underside is
placed on the same model as segment 0's (locate), and the mesh fuses the bottom in. Synthetic geometry: an L-shaped
object, depth rendered analytically, files stored as blobs like an upload."""
from __future__ import annotations

import asyncio
import io

import numpy as np
import pytest

pytest.importorskip("open3d")

from repairos.importers.ply import ply_bytes  # noqa: E402
from repairos.locate.camera import Pinhole, Pose, pixel_rays, project  # noqa: E402
from repairos.pipeline.run import (Context, ensure_mesh, ensure_segment_alignment, locate_findings,  # noqa: E402
                                   run_id_for)
from repairos.slots.blob_local import LocalBlobStore  # noqa: E402
from repairos.slots.state_sqlite import SqliteState  # noqa: E402

L_BOXES = [(np.array([-0.05, 0.0, -0.03]), np.array([0.05, 0.03, 0.03])),
           (np.array([0.01, 0.03, -0.03]), np.array([0.05, 0.06, 0.01]))]
CAM = Pinhole(240.0, 240.0, 128.0, 96.0, 256, 192)


def rot(axis: str, deg: float) -> np.ndarray:
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return {"x": np.array([[1, 0, 0], [0, c, -s], [0, s, c]]), "y": np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])}[axis]


def m4(r, t):
    m = np.eye(4)
    m[:3, :3], m[:3, 3] = r, t
    return m


FLIP = m4(rot("y", 90) @ rot("x", 180), [0.4, 0.06, -0.2])   # object frame -> segment 1's own world


def look_at(eye, target) -> Pose:
    eye, target = np.asarray(eye, float), np.asarray(target, float)
    back = eye - target
    back /= np.linalg.norm(back)
    right = np.cross([0.0, 1.0, 0.0], back)
    right /= np.linalg.norm(right)
    return Pose(np.stack([right, np.cross(back, right), back], axis=1), eye)


def boxes_in(t):
    out = []
    for lo, hi in L_BOXES:
        c = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        w = c @ t[:3, :3].T + t[:3, 3]
        out.append((w.min(0), w.max(0)))
    return out


def render(pose: Pose, boxes) -> np.ndarray:
    u, v = np.meshgrid(np.arange(CAM.width) + 0.5, np.arange(CAM.height) + 0.5)
    o, d = pixel_rays(np.stack([u.ravel(), v.ravel()], 1), pose, CAM)
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
    return z.reshape(CAM.height, CAM.width)


def png(a: np.ndarray, mode: str) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(a).save(buf, format="PNG" if mode != "jpg" else "JPEG")
    return buf.getvalue()


def box_contract(t, centre_obj=(0, 0.03, 0)):
    q = Pose(t[:3, :3], t[:3, 3]).to_contract()["quaternion_xyzw"]
    return {"frame": "arkit_world", "center_m": [float(x) for x in t[:3, :3] @ np.asarray(centre_obj) + t[:3, 3]],
            "extent_m": [0.14, 0.08, 0.1], "quaternion_xyzw": q}


@pytest.fixture
def flipped_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("REPAIROS_DATA_DIR", str(tmp_path))
    state, blobs = SqliteState(tmp_path / "state.sqlite3"), LocalBlobStore(tmp_path / "blobs")
    kfs, poses1 = [], []
    grey = np.full((CAM.height, CAM.width, 3), 170, np.uint8)
    for seg, t in ((0, np.eye(4)), (1, FLIP)):
        centre = t[:3, :3] @ np.array([0.0, 0.03, 0.0]) + t[:3, 3]
        for k in range(10):
            a = 2 * np.pi * k / 10
            pose = look_at(centre + [0.25 * np.cos(a), 0.25, 0.25 * np.sin(a)], centre)
            depth = render(pose, boxes_in(t))
            kfs.append({"keyframe_id": f"kf{seg}_{k}", "segment": seg, "timestamp_s": float(k + 20 * seg),
                        "image_file": blobs.put(png(grey, "jpg"), "image/jpeg"),
                        "depth_file": blobs.put(png(np.clip(depth * 1000, 0, 65535).astype(np.uint16), "png"),
                                                "image/png"),
                        "depth_encoding": "uint16_mm",
                        "confidence_file": blobs.put(png(np.full(depth.shape, 2, np.uint8), "png"), "image/png"),
                        "camera": pose.to_contract(),
                        "intrinsics": {"fx": CAM.fx, "fy": CAM.fy, "cx": CAM.cx, "cy": CAM.cy, "width_px": CAM.width,
                                       "height_px": CAM.height}, "tracking_state": "normal"})
            if seg == 1:
                poses1.append(pose)
    # Segment 0's in-box cloud: the L's upper surfaces (what the phone's LiDAR accumulates above the table).
    g = np.stack(np.meshgrid(np.arange(-0.05, 0.05, 0.002), np.arange(-0.03, 0.03, 0.002)), -1).reshape(-1, 2)
    top = np.column_stack([g[:, 0], np.where((g[:, 0] > 0.01) & (g[:, 1] < 0.01), 0.06, 0.03), g[:, 1]])
    scan = {"scan_id": "scan_flip", "product_id": "p", "session_id": "sess_flip", "captured_at": "2026-10-25T01:00:00Z",
            "device": {"model": "iPhone17,2", "ios": "26.5"}, "frame": "arkit_world", "units": "m",
            "depth_source": "lidar", "pose_source": "arkit", "voxel_mm": 3.0, "gravity_arkit_world": [0, -1, 0],
            "points_file": blobs.put(ply_bytes(top, np.full((len(top), 3), 170, np.uint8)), "application/x-ply"),
            "point_count": len(top), "crop_box": box_contract(np.eye(4)),
            "support_plane": {"transform": list(np.eye(4).reshape(-1)), "extent_m": [1.0, 1.0]},
            "keyframes": kfs,
            "segments": [{"segment": 1, "reason": "flipped", "object_moved": True, "aligned": False,
                          "crop_box": box_contract(FLIP)}]}

    async def seed():
        await state.transition("session", "sess_flip", "session.create", {"session_id": "sess_flip"})
        await state.transition("scan-package", "scan_flip", "scan.create", scan)
    asyncio.run(seed())
    return state, blobs, scan, poses1


def test_the_flipped_bottom_is_registered_located_and_fused(flipped_scan):
    state, blobs, scan, poses1 = flipped_scan
    ctx = Context(state=state, blobs=blobs)
    if ctx.locator is None:
        pytest.skip("repairos.locate is not installed")
    rec = asyncio.run(ensure_segment_alignment(ctx, "scan_flip"))
    seg = rec["segments"][0]
    assert seg["aligned"], seg["why_not"]
    # the shared sides alone (normals agreeing), or both table contacts with free space judged (segment_align)
    assert (seg["fitness"] >= 0.3 or "both tables" in seg["method"]) and rec.get("cloud_ref")
    truth = np.linalg.inv(FLIP)
    t = np.asarray(seg["transform"]).reshape(4, 4)
    assert np.linalg.norm(t[:3, 3] - truth[:3, 3]) < 0.01
    again = asyncio.run(ensure_segment_alignment(ctx, "scan_flip"))
    assert again["finished_at"] == rec["finished_at"]           # once per scan-package revision

    # A finding on the underside (the flipped L's top face, never seen by segment 0) from a segment-1 photo.
    pose = poses1[0]
    under_w1 = FLIP[:3, :3] @ np.array([-0.03, 0.0, 0.0]) + FLIP[:3, 3]
    (u, v), = project(under_w1[None], pose, CAM)[0]
    still = {"still_id": "s_bottom", "scan_id": "scan_flip", "session_id": "sess_flip", "kind": "unguided",
             "captured_at": "2026-10-25T01:01:00Z", "image_file": scan["keyframes"][10]["image_file"],
             "width_px": CAM.width, "height_px": CAM.height, "camera": pose.to_contract(), "pose_reliable": False,
             "intrinsics": {"fx": CAM.fx, "fy": CAM.fy, "cx": CAM.cx, "cy": CAM.cy}, "targets": [],
             "tracking_state": "normal", "segment": 1}
    finding = {"finding_id": "f_under", "evidence_refs": {"still_ids": ["s_bottom"],
                                                            "crop_box_px": [u - 6, v - 6, u + 6, v + 6]}}
    locs, _ = asyncio.run(locate_findings(ctx, scan, [still], [finding], {}, rec))
    loc = locs["f_under"]
    assert loc["status"] == "computed", loc
    want = np.array([-0.03, 0.0, 0.0])                            # the same spot in segment 0's object frame
    assert np.linalg.norm(np.asarray(loc["point_m"]) - want) < 0.012

    # Without the server's registration the finding is listed, not placed.
    locs, _ = asyncio.run(locate_findings(ctx, scan, [still], [finding], {}, None))
    assert locs["f_under"]["status"] == "not_computed" and "flipped" in locs["f_under"]["reason"]

    mesh = asyncio.run(ensure_mesh(ctx, "scan_flip"))
    assert mesh["status"] == "done" and "flipped segment(s) fused in" in mesh["method"]
    row = asyncio.run(state.get("pipeline-run", run_id_for("scan_flip"))).data
    assert row["segments"]["segments"][0]["aligned"] is True


def _faces_down(v, f):
    tri = v[f]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    return tri.mean(1), n


def test_the_recreation_shows_the_object_alone_and_closes_with_the_registered_bottom(flipped_scan):
    """The report's 3D model: no table (segment 0's is cut from the mesh), and once the server registered the flipped
    segment, its depth (cut to its own box and above its own table) closes the underside, textured from its own
    views in segment 0's frame. An unregistered segment stays out."""
    from repairos.pipeline import segments
    from repairos.pipeline.run import _local_scan
    from repairos.recreation.service import ensure_recreation
    from repairos.recreation.surface import scan_surface
    from repairos.recreation.texture import scan_views

    state, blobs, scan, _ = flipped_scan
    ctx = Context(state=state, blobs=blobs)
    rec = asyncio.run(ensure_segment_alignment(ctx, "scan_flip"))
    moved = segments.aligned_transforms(rec)
    assert set(moved) == {1}
    local = _local_scan(ctx, scan)

    def check_no_table(v):
        assert (np.abs(v[:, 0]) <= 0.065).all() and (np.abs(v[:, 2]) <= 0.045).all()   # the L's footprint + 1.5 cm
        assert (v[:, 1] >= -0.006).all()

    top = scan_surface(local)
    check_no_table(top.vertices)
    c, n = _faces_down(top.vertices, top.faces)
    assert ((n[:, 1] < -0.7) & (c[:, 1] < 0.01)).sum() < 20          # open underneath
    assert top.params["bottom_segments"] == []

    closed = scan_surface(local, segment_transforms=moved)
    check_no_table(closed.vertices)
    c, n = _faces_down(closed.vertices, closed.faces)
    under = (n[:, 1] < -0.7) & (c[:, 1] < 0.012)
    assert under.sum() > 200                                           # the bottom face is there now
    assert np.ptp(c[under, 0]) > 0.07 and np.ptp(c[under, 2]) > 0.04   # across the whole underside
    assert closed.params["bottom_segments"] == [1]

    views0 = scan_views(local, [], max_keyframes=80)
    views1 = scan_views(local, [], max_keyframes=80, segment_transforms=moved)
    assert len(views1) == len(views0) + 10
    v1 = [v for v in views1 if v.name.startswith("kf1_")]
    assert v1 and all(v.pose.t[1] < -0.1 for v in v1)                # above the flipped L: under it in segment 0
    t = moved[1]
    raw = Pose.from_contract(scan["keyframes"][10]["camera"])
    np.testing.assert_allclose(v1[0].pose.t, t[:3, :3] @ raw.t + t[:3, 3], atol=1e-9)

    out = asyncio.run(ensure_recreation(ctx, "scan_flip"))
    assert out["status"] == "done", out
    assert out["bottom_segments"] == [1] and out["input_key"].endswith("|bottom:1")
