"""A Gaussian splat of the scanned object, trained on the GPU pod from the scan's posed photos (no COLMAP: the
cameras are ARKit's), cropped to the object and its table patch, written as a `.splat` file, in the scan's ARKit world
frame (the findings' frame).

Rasteriser: gsplat (nerfstudio-project/gsplat, Apache-2.0), its `rasterization` and its `DefaultStrategy`
(densify / prune / reset). The original Inria 3DGS code is non-commercial and is not used.

    reason = unavailable_reason()            # None when CUDA + gsplat are importable
    out = train_scan(pkg_local, stills_local, box, steps=4000, max_gaussians=600_000, timeout_s=120)
    out.data                                  # .splat bytes (32 bytes per Gaussian)

STATUS: the data preparation (cameras, images, initial points), the crop and the .splat writer are tested on the
Mac (tests/unit/test_recreation.py). The training loop needs CUDA and has NOT been run here; run it on the pod
first (`python -m repairos.recreation.splat <session_dir> <scan_id> <out.splat>`) and record its time and quality.

The phone shows `kind: splat` only with a splat renderer (MetalSplatter, MIT, is the candidate); until then the
phone keeps its textured shell.
"""
from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from repairos.locate.camera import FLIP_YZ, Pinhole, Pose

SH_C0 = 0.28209479177387814
STILL_TRAIN_PX = 1008          # stills are trained at this width (12 MP is 4032); keyframes at their own 640 px
REGION_GROW = 0.6              # Gaussians kept within the box footprint grown by this share of its larger side


def unavailable_reason() -> str | None:
    try:
        import torch
    except ImportError:
        return "torch is not installed"
    if not torch.cuda.is_available():
        return "no CUDA device"
    try:
        import gsplat  # noqa: F401
    except ImportError:
        return "gsplat is not installed"
    return None


@dataclass
class TrainView:
    image_path: Path
    width: int
    height: int
    k: np.ndarray              # 3x3 at (width, height)
    w2c: np.ndarray            # 4x4 world -> OpenCV camera (x right, y down, z forward)


@dataclass
class SplatResult:
    data: bytes
    count: int
    seconds: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)


def opencv_w2c(pose: Pose) -> np.ndarray:
    """World -> OpenCV camera for an ARKit camera-to-world pose (ARKit camera: x right, y up, looks down -z)."""
    c2w = np.eye(4)
    c2w[:3, :3] = pose.r @ FLIP_YZ
    c2w[:3, 3] = pose.t
    return np.linalg.inv(c2w)


def train_views(pkg: Mapping[str, Any], stills: Sequence[Mapping[str, Any]], *, max_keyframes: int = 80
                ) -> list[TrainView]:
    """Every posed still (trained at STILL_TRAIN_PX wide) and keyframe (640 px) of the scan's frame."""
    from repairos.recreation.texture import _same_frame_segments

    segs = _same_frame_segments(pkg)
    out: list[TrainView] = []
    for s in stills:
        if int(s.get("segment") or 0) not in segs or s.get("pose_reliable") is False or not s.get("camera"):
            continue
        w, h = int(s["width_px"]), int(s["height_px"])
        cam = Pinhole.from_contract(s["intrinsics"], w, h)
        tw = min(STILL_TRAIN_PX, w)
        th = round(h * tw / w)
        c = cam.scaled(tw, th)
        out.append(TrainView(Path(s["image_file"]), tw, th, np.array([[c.fx, 0, c.cx], [0, c.fy, c.cy], [0, 0, 1]]),
                             opencv_w2c(Pose.from_contract(s["camera"]))))
    kfs = [k for k in pkg.get("keyframes") or [] if int(k.get("segment") or 0) in segs and k.get("image_file")]
    for k in kfs[:: max(1, math.ceil(len(kfs) / max(1, max_keyframes)))]:
        c = Pinhole.from_contract(k["intrinsics"])
        out.append(TrainView(Path(k["image_file"]), int(c.width), int(c.height),
                             np.array([[c.fx, 0, c.cx], [0, c.fy, c.cy], [0, 0, 1]]),
                             opencv_w2c(Pose.from_contract(k["camera"]))))
    return out


def region_mask(p: np.ndarray, box: Any, ground_y: float | None) -> np.ndarray:
    """Points in the object's region: the box footprint grown by REGION_GROW of its larger side, from just under the
    table (or the box bottom) to a little above the box."""
    local = box.local(p)
    side = max(box.extent[0], box.extent[2])
    gx = 0.5 + REGION_GROW * side / box.extent[0]
    gz = 0.5 + REGION_GROW * side / box.extent[2]
    bottom = (ground_y if ground_y is not None else box.centre[1] - box.extent[1] / 2) - 0.01
    top = box.centre[1] + box.extent[1] / 2 + 0.03
    return (np.abs(local[:, 0]) <= gx) & (np.abs(local[:, 2]) <= gz) & (p[:, 1] >= bottom) & (p[:, 1] <= top)


def initial_points(pkg: Mapping[str, Any], box: Any, *, max_points: int = 150_000) -> tuple[np.ndarray, np.ndarray]:
    """Seeds: high-confidence keyframe depth points (with their keyframe's colours) in a wide region around the box
    (the background is trained too, then cropped), thinned to `max_points`. (N x 3 metres, N x 3 0..1)."""
    from PIL import Image

    from repairos.locate.camera import backproject, project
    from repairos.pipeline.mesh import keyframes_in_frame
    from repairos.recon.tsdf import frames_from_scan_package

    frames = frames_from_scan_package({**pkg, "keyframes": keyframes_in_frame(pkg)}, Path("/"))
    pts, cols = [], []
    for f in frames[:: max(1, len(frames) // 40)]:
        p = f.pose.to_world(backproject(f.depth_m, f.depth_cam))
        ok = np.isfinite(f.depth_m) & (f.depth_m > 0)
        if f.confidence is not None:
            ok &= f.confidence >= 2
        p = p[ok]
        p = p[np.linalg.norm(p - box.centre, axis=1) < 3.0 * max(box.extent)]
        if not len(p):
            continue
        with Image.open(f.image_path) as im:
            rgb = np.asarray(im.convert("RGB"), np.float32) / 255.0
        uv, _ = project(p, f.pose, f.image_cam.scaled(rgb.shape[1], rgb.shape[0]))
        u = np.clip(uv[:, 0].astype(int), 0, rgb.shape[1] - 1)
        v = np.clip(uv[:, 1].astype(int), 0, rgb.shape[0] - 1)
        pts.append(p)
        cols.append(rgb[v, u])
    if not pts:
        return np.zeros((0, 3)), np.zeros((0, 3))
    p, c = np.concatenate(pts), np.concatenate(cols)
    if len(p) > max_points:
        keep = np.random.default_rng(0).choice(len(p), max_points, replace=False)
        p, c = p[keep], c[keep]
    return p, c


def splat_bytes(means: np.ndarray, scales: np.ndarray, quats_wxyz: np.ndarray, opacity: np.ndarray, rgb: np.ndarray
                ) -> bytes:
    """The `.splat` layout (antimatter15/splat; read by MetalSplatter): per Gaussian float32 x y z, float32 scale
    x y z (linear), uint8 r g b a (a = opacity), uint8 quaternion w x y z (q * 128 + 128); largest first."""
    n = len(means)
    order = np.argsort(-(np.prod(scales, axis=1) * opacity.reshape(-1)))
    q = quats_wxyz / np.maximum(np.linalg.norm(quats_wxyz, axis=1, keepdims=True), 1e-12)
    rec = np.empty(n, dtype=np.dtype([("p", "<f4", 3), ("s", "<f4", 3), ("c", "u1", 4), ("q", "u1", 4)]))
    rec["p"] = means[order]
    rec["s"] = scales[order]
    rec["c"][:, :3] = np.clip(rgb[order] * 255, 0, 255).astype(np.uint8)
    rec["c"][:, 3] = np.clip(opacity.reshape(-1)[order] * 255, 0, 255).astype(np.uint8)
    rec["q"] = np.clip(q[order] * 128 + 128, 0, 255).astype(np.uint8)
    return rec.tobytes()


def read_splat(data: bytes) -> dict[str, np.ndarray]:
    rec = np.frombuffer(data, dtype=np.dtype([("p", "<f4", 3), ("s", "<f4", 3), ("c", "u1", 4), ("q", "u1", 4)]))
    return {"means": rec["p"].astype(float), "scales": rec["s"].astype(float), "rgb": rec["c"][:, :3] / 255.0,
            "opacity": rec["c"][:, 3] / 255.0, "quats": (rec["q"].astype(float) - 128) / 128}


def _load_image(v: TrainView) -> np.ndarray:
    from PIL import Image

    with Image.open(v.image_path) as im:
        im = im.convert("RGB")
        if im.size != (v.width, v.height):
            im = im.resize((v.width, v.height), Image.LANCZOS)
        return np.asarray(im, np.float32) / 255.0


def train_scan(pkg: Mapping[str, Any], stills: Sequence[Mapping[str, Any]], box: Any, *, steps: int,
               max_gaussians: int, timeout_s: float) -> SplatResult:   # pragma: no cover - needs CUDA
    """Trains the splat (L1 + 0.2 D-SSIM, gsplat's default densification) and returns the cropped `.splat`."""
    import torch
    import torch.nn.functional as F
    from gsplat import rasterization
    from gsplat.strategy import DefaultStrategy

    from repairos.recreation.surface import GROW, ground_height
    from repairos.recon.tsdf import frames_from_scan_package

    t0 = time.monotonic()
    dev = torch.device("cuda")
    views = train_views(pkg, stills)
    if len(views) < 3:
        raise RuntimeError("fewer than 3 posed views")
    images = [torch.from_numpy(_load_image(v)).to(dev) for v in views]
    ks = [torch.tensor(v.k, dtype=torch.float32, device=dev) for v in views]
    w2cs = [torch.tensor(v.w2c, dtype=torch.float32, device=dev) for v in views]
    p0, c0 = initial_points(pkg, box)
    if len(p0) < 100:
        raise RuntimeError("too few depth points to seed the splat")
    load_s = time.monotonic() - t0

    pts = torch.tensor(p0, dtype=torch.float32, device=dev)
    # initial scale: mean distance to the 3 nearest seeds (chunked)
    d2 = []
    for i in range(0, len(pts), 4096):
        d = torch.cdist(pts[i:i + 4096], pts)
        d2.append(d.topk(4, largest=False).values[:, 1:].mean(1))
    dist = torch.cat(d2).clamp_min(1e-4)
    n = len(pts)
    params = torch.nn.ParameterDict({
        "means": torch.nn.Parameter(pts),
        "scales": torch.nn.Parameter(torch.log(dist)[:, None].repeat(1, 3)),
        "quats": torch.nn.Parameter(torch.tensor([[1.0, 0, 0, 0]], device=dev).repeat(n, 1)),
        "opacities": torch.nn.Parameter(torch.logit(torch.full((n,), 0.1, device=dev))),
        "sh0": torch.nn.Parameter(((torch.tensor(c0, dtype=torch.float32, device=dev) - 0.5) / SH_C0)[:, None, :]),
    })
    extent = float(np.linalg.norm(box.extent)) * 2
    lrs = {"means": 1.6e-4 * extent, "scales": 5e-3, "quats": 1e-3, "opacities": 5e-2, "sh0": 2.5e-3}
    optimizers = {k: torch.optim.Adam([{"params": params[k], "lr": lr, "name": k}], eps=1e-15)
                  for k, lr in lrs.items()}
    strategy = DefaultStrategy(refine_stop_iter=int(steps * 0.6), reset_every=max(1000, steps // 2),
                               refine_every=100, verbose=False)
    strategy.check_sanity(params, optimizers)
    state = strategy.initialize_state(scene_scale=extent)

    def ssim(a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
        a, b = a.permute(2, 0, 1)[None], b.permute(2, 0, 1)[None]
        w = torch.ones(3, 1, 7, 7, device=dev) / 49
        mu_a, mu_b = F.conv2d(a, w, groups=3), F.conv2d(b, w, groups=3)
        sa = F.conv2d(a * a, w, groups=3) - mu_a ** 2
        sb = F.conv2d(b * b, w, groups=3) - mu_b ** 2
        sab = F.conv2d(a * b, w, groups=3) - mu_a * mu_b
        c1, c2 = 0.01 ** 2, 0.03 ** 2
        return (((2 * mu_a * mu_b + c1) * (2 * sab + c2)) / ((mu_a ** 2 + mu_b ** 2 + c1) * (sa + sb + c2))).mean()

    rng = np.random.default_rng(0)
    step = 0
    for step in range(steps):
        if time.monotonic() - t0 > timeout_s:
            break
        i = int(rng.integers(len(views)))
        v = views[i]
        colors, alphas, info = rasterization(
            params["means"], F.normalize(params["quats"], dim=-1), torch.exp(params["scales"]),
            torch.sigmoid(params["opacities"]), params["sh0"], w2cs[i][None], ks[i][None], v.width, v.height,
            sh_degree=0, packed=False)
        img = colors[0].clamp(0, 1)
        loss = 0.8 * (img - images[i]).abs().mean() + 0.2 * (1 - ssim(img, images[i]))
        strategy.step_pre_backward(params, optimizers, state, step, info)
        loss.backward()
        for opt in optimizers.values():
            opt.step()
            opt.zero_grad(set_to_none=True)
        strategy.step_post_backward(params, optimizers, state, step, info, packed=False)
        if len(params["means"]) > max_gaussians and step > strategy.refine_stop_iter:
            break
    train_s = time.monotonic() - t0 - load_s

    with torch.no_grad():
        means = params["means"].cpu().numpy().astype(np.float64)
        scales = torch.exp(params["scales"]).cpu().numpy()
        quats = F.normalize(params["quats"], dim=-1).cpu().numpy()
        opac = torch.sigmoid(params["opacities"]).cpu().numpy()
        rgb = np.clip(params["sh0"][:, 0].cpu().numpy() * SH_C0 + 0.5, 0, 1)
    frames = frames_from_scan_package(pkg, Path("/"))
    keep = region_mask(means, box, ground_height(frames, box, GROW)) & (opac > 0.02)
    data = splat_bytes(means[keep], scales[keep], quats[keep], opac[keep], rgb[keep])
    return SplatResult(data, int(keep.sum()), {"load_s": round(load_s, 2), "train_s": round(train_s, 2),
                                               "total_s": round(time.monotonic() - t0, 2)},
                       {"steps": step + 1, "views": len(views), "gaussians_trained": int(len(means)),
                        "gaussians_kept": int(keep.sum())})


def main(argv: Sequence[str] | None = None) -> int:   # pragma: no cover - a pod tool
    import argparse
    import json

    from repairos.recon.tsdf import ObjectBox
    from repairos.recreation.texture import load_scan_dir

    ap = argparse.ArgumentParser(description="Train a scan's splat on the GPU (repairos.recreation.splat)")
    ap.add_argument("session_dir")
    ap.add_argument("scan_id")
    ap.add_argument("out")
    ap.add_argument("--steps", type=int, default=4000)
    ap.add_argument("--timeout-s", type=float, default=120)
    a = ap.parse_args(argv)
    why = unavailable_reason()
    if why:
        print(json.dumps({"ok": False, "reason": why}))
        return 1
    pkg, stills = load_scan_dir(a.session_dir, a.scan_id)
    r = train_scan(pkg, stills, ObjectBox.from_contract(pkg["crop_box"]), steps=a.steps, max_gaussians=600_000,
                   timeout_s=a.timeout_s)
    Path(a.out).write_bytes(r.data)
    print(json.dumps({"ok": True, "gaussians": r.count, "bytes": len(r.data), **r.seconds, **r.stats}))
    return 0


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())

