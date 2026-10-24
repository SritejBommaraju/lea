"""repairos.recreation: the textured recreation end to end on a synthetic capture whose truth is known (a 10 cm cube
on a table, painted by world position, seen by posed cameras with LiDAR-like depth), the USDZ container, the splat
file format and the camera conventions the GPU trainer relies on."""
from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path

import numpy as np
import pytest

o3d = pytest.importorskip("open3d")
from PIL import Image  # noqa: E402

from repairos.locate.camera import Pose, matrix_to_quat, project  # noqa: E402
from test_api_gateway import gw  # noqa: E402, F401 (fixture)

CUBE = 0.10          # side, metres, standing on the table y = 0, centred on x = z = 0


def paint(p: np.ndarray) -> np.ndarray:
    """The scene's colour at world points: smooth ramps in x and z, height in blue (0..255)."""
    r = 128 + 400 * p[:, 0]
    g = 128 + 400 * p[:, 2]
    b = 60 + 1500 * np.clip(p[:, 1], 0, 0.12)
    return np.clip(np.stack([r, g, b], 1), 0, 255)


def scene() -> o3d.t.geometry.RaycastingScene:
    table = o3d.geometry.TriangleMesh.create_box(0.6, 0.002, 0.6).translate((-0.3, -0.002, -0.3))
    cube = o3d.geometry.TriangleMesh.create_box(CUBE, CUBE, CUBE).translate((-CUBE / 2, 0, -CUBE / 2))
    s = o3d.t.geometry.RaycastingScene()
    s.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(table))
    s.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(cube))
    return s


def look_at(eye: np.ndarray, target: np.ndarray) -> np.ndarray:
    """ARKit camera-to-world rotation looking from eye to target (camera -Z forward, +Y up)."""
    back = eye - target
    back /= np.linalg.norm(back)
    right = np.cross([0.0, 1.0, 0.0], back)
    right /= np.linalg.norm(right)
    up = np.cross(back, right)
    return np.stack([right, up, back], 1)


def render(s, pose: Pose, w: int, h: int, f: float) -> tuple[np.ndarray, np.ndarray]:
    """(depth along the view axis in metres, H x W; RGB uint8) by ray casting."""
    u, v = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    d_cam = np.stack([(u - w / 2) / f, -(v - h / 2) / f, -np.ones_like(u)], -1).reshape(-1, 3)
    d = d_cam @ pose.r.T
    rays = np.concatenate([np.repeat(pose.t[None], len(d), 0), d], 1).astype(np.float32)
    t = s.cast_rays(o3d.core.Tensor(rays))["t_hit"].numpy()
    hit = np.isfinite(t)
    depth = np.where(hit, t, 0.0).reshape(h, w)            # t along d_cam (unit z) = depth along the view axis
    p = pose.t + d * np.where(hit, t, 0)[:, None]
    rgb = np.where(hit[:, None], paint(p), 20).reshape(h, w, 3).astype(np.uint8)
    return depth, rgb


@pytest.fixture(scope="module")
def capture(tmp_path_factory) -> Path:
    """A session folder in the phone's layout: 24 keyframes (640 x 480 + 256 x 192 depth) and 8 stills (1280 x 960)
    on two rings around the cube."""
    root = tmp_path_factory.mktemp("cap")
    sid, scan = "sess_test", "scan_test"
    (root / "scans" / scan).mkdir(parents=True)
    (root / "stills").mkdir()
    s = scene()
    kfs = []
    for i in range(24):
        a = 2 * np.pi * i / 24
        eye = np.array([0.35 * np.cos(a), 0.18 + 0.12 * (i % 2), 0.35 * np.sin(a)])
        pose = Pose(look_at(eye, np.array([0, 0.04, 0])), eye)
        depth, _ = render(s, pose, 256, 192, 200.0)
        _, rgb = render(s, pose, 640, 480, 500.0)
        name = f"kf_{i:04d}"
        Image.fromarray(rgb).save(root / "scans" / scan / f"{name}.jpg", quality=95)
        Image.fromarray((depth * 1000).astype(np.uint16)).save(root / "scans" / scan / f"{name}_depth.png")
        Image.fromarray(np.where(depth > 0, 2, 0).astype(np.uint8)).save(root / "scans" / scan / f"{name}_conf.png")
        kfs.append({"keyframe_id": name, "image_file": f"scans/{scan}/{name}.jpg",
                    "depth_file": f"scans/{scan}/{name}_depth.png", "confidence_file": f"scans/{scan}/{name}_conf.png",
                    "depth_encoding": "uint16_mm", "camera": {**pose.to_contract()},
                    "intrinsics": {"fx": 500.0, "fy": 500.0, "cx": 320.0, "cy": 240.0, "width_px": 640,
                                   "height_px": 480}, "timestamp_s": i * 0.2, "tracking_state": "normal"})
    for i in range(8):
        a = 2 * np.pi * (i + 0.5) / 8
        eye = np.array([0.3 * np.cos(a), 0.25, 0.3 * np.sin(a)])
        pose = Pose(look_at(eye, np.array([0, 0.04, 0])), eye)
        _, rgb = render(s, pose, 1280, 960, 1000.0)
        depth, _ = render(s, pose, 256, 192, 200.0)
        still = f"{scan}_s{i + 1}"
        Image.fromarray(rgb).save(root / "stills" / f"{still}.jpg", quality=95)
        Image.fromarray((depth * 1000).astype(np.uint16)).save(root / "stills" / f"{still}_depth.png")
        (root / "stills" / f"{still}.json").write_text(json.dumps({
            "still_id": still, "scan_id": scan, "session_id": sid, "image_file": f"stills/{still}.jpg",
            "depth_file": f"stills/{still}_depth.png", "camera": pose.to_contract(), "width_px": 1280,
            "height_px": 960, "intrinsics": {"fx": 1000.0, "fy": 1000.0, "cx": 640.0, "cy": 480.0},
            "pose_reliable": True, "sharpness": 100.0}))
    pkg = {"scan_id": scan, "session_id": sid, "frame": "arkit_world", "keyframes": kfs,
           "crop_box": {"center_m": [0, CUBE / 2, 0], "extent_m": [CUBE * 1.2, CUBE * 1.05, CUBE * 1.2],
                        "quaternion_xyzw": matrix_to_quat(np.eye(3)), "frame": "arkit_world"},
           "support_plane": {"transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], "extent_m": [1, 1]}}
    (root / "scans" / scan / "scan_package.json").write_text(json.dumps(pkg))
    return root


def cube_distance(p: np.ndarray) -> np.ndarray:
    """Distance from points to the cube's surface or the table (whichever is nearer)."""
    h = CUBE / 2
    q = np.abs(p - [0, h, 0]) - h
    outside = np.linalg.norm(np.maximum(q, 0), axis=1) + np.minimum(q.max(1), 0)
    return np.minimum(np.abs(outside), np.abs(p[:, 1]))


def test_surface_follows_the_object_and_its_table(capture):
    from repairos.recreation.surface import scan_surface
    from repairos.recreation.texture import load_scan_dir

    pkg, _stills = load_scan_dir(capture, "scan_test")
    s = scan_surface(pkg)
    assert s.ground_y is not None and abs(s.ground_y) < 0.004
    err = cube_distance(s.vertices)
    assert np.median(err) < 0.002 and np.percentile(err, 95) < 0.006
    top = s.vertices[np.abs(s.vertices[:, 1] - CUBE) < 0.004]
    assert len(top) > 50                                      # the cube's top is there, not cut at the table
    assert (s.vertices[:, 1] > -0.02).all()


def test_texture_carries_the_photos_colours(capture):
    from repairos.recon.tsdf import ObjectBox
    from repairos.recreation.surface import scan_surface
    from repairos.recreation.texture import load_scan_dir, scan_views, texture_mesh

    pkg, stills = load_scan_dir(capture, "scan_test")
    s = scan_surface(pkg)
    views = scan_views(pkg, stills, box=ObjectBox.from_contract(pkg["crop_box"]))
    tex = texture_mesh(s.vertices, s.faces, views, atlas_px=1024)
    assert tex.stats["faces_unseen"] < 0.05 * len(s.faces)
    assert tex.stats["face_share_from_stills"] > 0.8
    atlas = np.asarray(tex.atlas, np.float64)
    c = tex.vertices[tex.faces].mean(1)
    uv = tex.uv[tex.faces].mean(1)
    px = atlas[np.clip((uv[:, 1] * 1024).astype(int), 0, 1023), np.clip((uv[:, 0] * 1024).astype(int), 0, 1023)]
    near = cube_distance(c) < 0.004
    err = np.abs(px[near] - paint(c[near])).mean(1)
    assert np.median(err) < 12                                # the texel at a face shows that face's colour


def test_usdz_is_aligned_and_loads_back(capture, tmp_path):
    from repairos.recreation.texture import load_scan_dir, scan_views, texture_mesh
    from repairos.recreation.surface import scan_surface
    from repairos.recreation.usdz import ALIGN, usdz_bytes

    pkg, stills = load_scan_dir(capture, "scan_test")
    s = scan_surface(pkg)
    tex = texture_mesh(s.vertices, s.faces, scan_views(pkg, stills), atlas_px=512)
    data = usdz_bytes(tex.vertices, tex.uv, tex.faces, tex.atlas_jpeg())
    p = tmp_path / "r.usdz"
    p.write_bytes(data)
    with zipfile.ZipFile(p) as z:
        names = z.namelist()
        assert names == ["recreation.usda", "texture.jpg"]
        assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())
        for i in z.infolist():                               # data offset = local header + name + its extra
            name_len, extra_len = struct.unpack("<HH", data[i.header_offset + 26:i.header_offset + 30])
            assert (i.header_offset + 30 + name_len + extra_len) % ALIGN == 0
        layer = z.read("recreation.usda").decode()
    assert layer.startswith("#usda 1.0") and 'upAxis = "Y"' in layer and "@texture.jpg@" in layer
    assert f"int[] faceVertexIndices = [{tex.faces[0, 0]}, " in layer


def test_splat_file_round_trip():
    from repairos.recreation.splat import read_splat, splat_bytes

    rng = np.random.default_rng(1)
    n = 50
    means, scales = rng.normal(size=(n, 3)), rng.uniform(0.001, 0.01, (n, 3))
    q = rng.normal(size=(n, 4))
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    q[q[:, 0] < 0] *= -1
    op, rgb = rng.uniform(0, 1, n), rng.uniform(0, 1, (n, 3))
    data = splat_bytes(means, scales, q, op, rgb)
    assert len(data) == 32 * n
    back = read_splat(data)
    order = np.argsort(-(np.prod(scales, 1) * op))
    assert np.allclose(back["means"], means[order], atol=1e-6)
    assert np.allclose(back["rgb"], rgb[order], atol=1 / 255 + 1e-6)
    assert np.allclose(back["quats"], q[order], atol=1 / 64)


def test_opencv_view_matrix_matches_the_projection():
    """The GPU trainer's world->OpenCV matrices put points where repairos.locate.camera.project does."""
    from repairos.locate.camera import Pinhole
    from repairos.recreation.splat import opencv_w2c

    eye = np.array([0.3, 0.2, 0.25])
    pose = Pose(look_at(eye, np.zeros(3)), eye)
    cam = Pinhole(800.0, 800.0, 320.0, 240.0, 640, 480)
    pts = np.random.default_rng(2).uniform(-0.05, 0.05, (20, 3))
    uv, z = project(pts, pose, cam)
    pc = (opencv_w2c(pose) @ np.c_[pts, np.ones(len(pts))].T).T[:, :3]
    k = np.array([[800.0, 0, 320.0], [0, 800.0, 240.0], [0, 0, 1]])
    proj = (k @ pc.T).T
    assert np.allclose(proj[:, :2] / proj[:, 2:], uv, atol=1e-6) and np.allclose(pc[:, 2], z)


def test_config_and_input_key():
    from repairos.recreation.service import input_key, payload, recreation_config

    cfg = recreation_config({"engine": "textured_mesh"})
    assert cfg["atlas_px"] == 4096 and cfg["timeout_s"] >= 1
    with pytest.raises(ValueError):
        recreation_config({"engine": "nerf"})
    with pytest.raises(ValueError):
        recreation_config({"atlas_px": 100})
    a = input_key(3, [{"still_id": "b"}, {"still_id": "a"}])
    assert a == input_key(3, [{"still_id": "a"}, {"still_id": "b"}]) != input_key(4, [{"still_id": "a"}])
    p = payload("scan_x", {"kind": "textured_mesh", "rev": 2, "format": "usdz", "bytes": 10})
    assert p["url"] == "/v1/scans/scan_x/recreation?rev=2" and p["frame"] == "arkit_world"


def test_recreation_route_and_live_event(gw):  # noqa: F811
    import asyncio

    from repairos.api.app import _named_event
    from repairos.pipeline.run import run_id_for
    from test_api_gateway import make_scan, new_session

    sid = new_session(gw)
    pkg = make_scan(gw, sid)
    assert gw.req("POST", "/v1/scans", pkg)[0] == 201
    status, out, _ = gw.req("GET", f"/v1/scans/{pkg['scan_id']}/recreation")
    assert status == 404 and out["detail"]["error"] == "no_recreation"
    body = b"PK" + bytes(100)
    ref = gw.blobs.put(body, "model/vnd.usdz+zip")
    asyncio.run(gw.state.transition("pipeline-run", run_id_for(pkg["scan_id"]), "pipeline.update", {
        "scan_id": pkg["scan_id"], "session_id": sid, "announce": "recreation_ready",
        "recreation": {"status": "done", "ref": ref, "rev": 2, "kind": "textured_mesh", "format": "usdz",
                       "bytes": len(body), "focus_center": [0.1, -0.2, 0.3], "focus_radius": 0.15}}))
    status, got, h = gw.req("GET", f"/v1/scans/{pkg['scan_id']}/recreation?rev=2")
    assert status == 200 and got == body
    assert h["x-recreation-rev"] == "2" and h["x-recreation-kind"] == "textured_mesh"
    assert h["x-recreation-format"] == "usdz" and h["x-recreation-focus"] == "0.1,-0.2,0.3,0.15"
    named = [n for n in (_named_event(e) for e in asyncio.run(gw.state.events_since(sid))) if n]
    ready = [p for name, p in named if name == "recreation_ready"]
    assert ready == [{"scan_id": pkg["scan_id"], "kind": "textured_mesh", "rev": 2, "format": "usdz",
                      "url": f"/v1/scans/{pkg['scan_id']}/recreation?rev=2", "frame": "arkit_world",
                      "bytes": len(body)}]


def test_splat_file_is_cut_to_the_size_cap_keeping_the_most_important():
    from repairos.recreation.splat import BYTES_PER_GAUSSIAN, read_splat, splat_bytes

    rng = np.random.default_rng(3)
    n = 200
    means, scales = rng.normal(size=(n, 3)), rng.uniform(0.001, 0.01, (n, 3))
    q, op, rgb = np.tile([1.0, 0, 0, 0], (n, 1)), rng.uniform(0, 1, n), rng.uniform(0, 1, (n, 3))
    data = splat_bytes(means, scales, q, op, rgb, max_bytes=50 * BYTES_PER_GAUSSIAN + 7)
    assert len(data) == 50 * BYTES_PER_GAUSSIAN
    top = np.argsort(-(np.prod(scales, 1) * op))[:50]
    assert np.allclose(read_splat(data)["means"], means[top], atol=1e-6)
    assert splat_bytes(means, scales, q, op, rgb, max_bytes=0) == b""


def test_splat_config_bounds_and_engine_override(monkeypatch):
    from repairos.recreation.service import recreation_config

    cfg = recreation_config({"engine": "textured_mesh"})
    assert cfg["splat_steps"] >= 1 and cfg["splat_timeout_s"] <= 120 and cfg["splat_max_mb"] <= 15
    assert cfg["splat_max_gaussians"] * 32 <= 15_000_000          # the Gaussian cap alone fits the file cap
    for bad in ({"splat_gpu_mem_gb": 0}, {"splat_max_mb": "big"}, {"splat_timeout_s": 0}, {"splat_log_every": 1.5}):
        with pytest.raises(ValueError):
            recreation_config(bad)
    monkeypatch.setenv("LEA_RECREATION_ENGINE", "splat")
    assert recreation_config({"engine": "textured_mesh"})["engine"] == "splat"
    monkeypatch.setenv("LEA_RECREATION_ENGINE", "nerf")
    with pytest.raises(ValueError):
        recreation_config({})


def test_splat_waits_for_its_kernels(monkeypatch, tmp_path):
    """On the pod the engine is not available until bootstrap's background setup wrote the ready marker."""
    torch = pytest.importorskip("torch")
    import importlib.util

    from repairos.recreation import splat

    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda n, *a: object() if n == "gsplat" else real(n, *a))
    marker = tmp_path / ".lea-gsplat-ready"
    monkeypatch.setenv("LEA_GSPLAT_READY", str(marker))
    assert "not compiled yet" in splat.unavailable_reason()
    marker.write_text("ok")
    assert splat.unavailable_reason() is None


def test_low_priority_gpu_step_waits_for_the_detector(tmp_path):
    import threading
    import time

    from repairos.pipeline.run import Context

    ctx = Context(state=None, blobs=None, root=tmp_path)
    ctx.gpu_wanted = 1
    got = []
    t = threading.Thread(target=lambda: (ctx.gpu_low_priority().__enter__(), got.append(time.monotonic())))
    t.start()
    time.sleep(0.1)
    assert not got                                  # a detector pass wants the GPU: the splat step waits
    released = time.monotonic()
    ctx.gpu_wanted = 0
    t.join(2)
    assert got and got[0] >= released


def test_splat_without_cuda_keeps_the_mesh_and_retries_later(gw, monkeypatch, tmp_path):  # noqa: F811
    import asyncio

    from repairos.pipeline.run import Context, run_id_for
    from repairos.recreation import splat
    from repairos.recreation.service import ensure_splat
    from test_api_gateway import make_scan, new_session

    sid = new_session(gw)
    pkg = make_scan(gw, sid)
    assert gw.req("POST", "/v1/scans", pkg)[0] == 201
    mesh = {"status": "done", "ref": gw.blobs.put(b"PK" + bytes(10), "model/vnd.usdz+zip"), "rev": 1,
            "kind": "textured_mesh", "format": "usdz"}
    asyncio.run(gw.state.transition("pipeline-run", run_id_for(pkg["scan_id"]), "pipeline.update",
                                    {"scan_id": pkg["scan_id"], "session_id": sid, "recreation": mesh}))
    ctx = Context(state=gw.state, blobs=gw.blobs, root=tmp_path)
    monkeypatch.delenv("LEA_RECREATION_ENGINE", raising=False)
    assert asyncio.run(ensure_splat(ctx, pkg["scan_id"])) == {"status": "off"}
    monkeypatch.setenv("LEA_RECREATION_ENGINE", "splat")
    monkeypatch.setattr(splat, "unavailable_reason", lambda: "no CUDA device")
    rec = asyncio.run(ensure_splat(ctx, pkg["scan_id"]))
    assert rec == {"status": "unavailable", "reason": "no CUDA device", "input_key": None}   # no key: tried again
    row = asyncio.run(gw.state.get("pipeline-run", run_id_for(pkg["scan_id"]))).data
    assert row["recreation"] == mesh and row["splat"]["status"] == "unavailable"
    status, got, h = gw.req("GET", f"/v1/scans/{pkg['scan_id']}/recreation")
    assert status == 200 and h["x-recreation-kind"] == "textured_mesh"           # the mesh is still served
    status, out, _ = gw.req("GET", f"/v1/scans/{pkg['scan_id']}/recreation?kind=splat")
    assert status == 404 and out["detail"]["reason"] == "no CUDA device"


def test_splat_route_and_live_event_name_the_mesh_fallback(gw):  # noqa: F811
    import asyncio

    from repairos.api.app import _named_event
    from repairos.pipeline.run import run_id_for
    from repairos.recreation.splat import splat_bytes
    from test_api_gateway import make_scan, new_session

    sid = new_session(gw)
    pkg = make_scan(gw, sid)
    assert gw.req("POST", "/v1/scans", pkg)[0] == 201
    scan_id = pkg["scan_id"]
    mesh_body, n = b"PK" + bytes(100), 5
    sp = splat_bytes(np.zeros((n, 3)), np.full((n, 3), 0.01), np.tile([1.0, 0, 0, 0], (n, 1)), np.ones(n),
                     np.ones((n, 3)))
    mesh = {"status": "done", "ref": gw.blobs.put(mesh_body, "model/vnd.usdz+zip"), "rev": 3, "kind": "textured_mesh",
            "format": "usdz", "bytes": len(mesh_body), "focus_center": [0, 0, 0], "focus_radius": 0.1}
    spl = {"status": "done", "ref": gw.blobs.put(sp, "application/octet-stream"), "rev": 1, "kind": "splat",
           "format": "splat", "bytes": len(sp), "focus_center": [0.0, 0.05, 0.0], "focus_radius": 0.1}
    for patch in ({"recreation": mesh, "announce": "recreation_ready"}, {"splat": spl, "announce": "splat_ready"}):
        asyncio.run(gw.state.transition("pipeline-run", run_id_for(scan_id), "pipeline.update",
                                        {"scan_id": scan_id, "session_id": sid, **patch}))
    status, got, h = gw.req("GET", f"/v1/scans/{scan_id}/recreation?kind=splat&rev=1")
    assert status == 200 and got == sp and h["x-recreation-kind"] == "splat" and h["x-recreation-format"] == "splat"
    assert h["x-recreation-focus"] == "0.0,0.05,0.0,0.1"
    status, got, h = gw.req("GET", f"/v1/scans/{scan_id}/recreation")
    assert status == 200 and got == mesh_body and h["x-recreation-kind"] == "textured_mesh"   # old clients: the mesh
    assert gw.req("GET", f"/v1/scans/{scan_id}/recreation?kind=nerf")[0] == 400
    named = [p for name, p in (n for n in (_named_event(e) for e in asyncio.run(gw.state.events_since(sid))) if n)
             if name == "recreation_ready"]
    assert [p["kind"] for p in named] == ["textured_mesh", "splat"]
    assert named[1] == {"scan_id": scan_id, "kind": "splat", "rev": 1, "format": "splat", "frame": "arkit_world",
                        "url": f"/v1/scans/{scan_id}/recreation?kind=splat&rev=1", "bytes": len(sp),
                        "fallback_url": f"/v1/scans/{scan_id}/recreation?rev=3", "fallback_kind": "textured_mesh",
                        "fallback_rev": 3}
