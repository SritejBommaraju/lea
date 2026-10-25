#!/usr/bin/env python3
"""Per-still inspection quality of a saved scan: how big the locked object is in each photo and how clean it is.

For every still in a session it projects the scan's locked box (`crop_box`) through the still's own pose and
intrinsics and reports:

  fill       the box's projected extent over the image's shorter side (max of width/height, clipped to the frame)
  area       the box's projected bounds as a share of the image area
  dist_m     distance to the object: median LiDAR depth inside the box (the still's depth file), else geometry
  px_mm      pixels per millimetre on the object (fx / distance)
  iso, exp   ISO and exposure time from the still record
  sharp      Laplacian variance of the luma inside the box crop, on 2 x 2 binned pixels (what the phone gates on)
  sharp_full the same at full resolution (noise inflates this one)
  noise      noise sigma inside the box (Immerkaer's estimator, 0-255 luma)

Usage: python3 tools/still_quality.py <session_dir> [<session_dir> ...] [--json out.json]
Needs numpy and Pillow (the backend venv has both).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ORBIT_PX_MM = 8.0
CLOSE_PX_MM = 15.0


def quat_to_matrix(q):
    x, y, z, w = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def box_corners(cb):
    c = np.array(cb["center_m"])
    h = np.array(cb["extent_m"]) / 2
    r = quat_to_matrix(cb["quaternion_xyzw"])
    return np.array([c + r @ (np.array([sx, sy, sz]) * h) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)])


def project(points, cam):
    """ARKit camera: looks down -Z, image v grows down. Returns (u, v, z_forward) per point."""
    t = np.array(cam["position_m"])
    r = quat_to_matrix(cam["quaternion_xyzw"])
    pc = (points - t) @ r            # world -> camera (R^T (p - t))
    fwd = -pc[:, 2]
    return pc, fwd


def laplacian_var(a: np.ndarray) -> float:
    if a.shape[0] < 3 or a.shape[1] < 3:
        return 0.0
    lap = a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:] - 4 * a[1:-1, 1:-1]
    return float(lap.var())


def immerkaer_sigma(a: np.ndarray) -> float:
    if a.shape[0] < 3 or a.shape[1] < 3:
        return 0.0
    k = (a[:-2, :-2] - 2 * a[:-2, 1:-1] + a[:-2, 2:]
         - 2 * a[1:-1, :-2] + 4 * a[1:-1, 1:-1] - 2 * a[1:-1, 2:]
         + a[2:, :-2] - 2 * a[2:, 1:-1] + a[2:, 2:])
    h, w = a.shape
    return float(math.sqrt(math.pi / 2) * np.abs(k).sum() / (6 * (w - 2) * (h - 2)))


def analyse_session(sdir: Path):
    pkgs = sorted(sdir.glob("scans/*/scan_package.json"))
    if not pkgs:
        raise SystemExit(f"no scan_package.json under {sdir}")
    pkg = json.loads(pkgs[0].read_text())
    cb = pkg.get("crop_box")
    if not cb:
        raise SystemExit(f"{sdir}: no locked box (crop_box) in the scan package")
    corners = box_corners(cb)
    size = cb["extent_m"]
    rows = []
    for meta_path in sorted(sdir.glob("stills/*_s*.json"), key=lambda p: int(p.stem.rsplit("_s", 1)[1])):
        m = json.loads(meta_path.read_text())
        img_path = sdir / m["image_file"]
        if not img_path.exists():
            continue
        k = m["intrinsics"]
        W, H = m["width_px"], m["height_px"]
        pc, fwd = project(corners, m["camera"])
        row = {"id": m["still_id"].rsplit("_", 1)[1], "kind": "closeup" if (m.get("target_id") or "").startswith("closeup")
               or m.get("kind") == "guided" else "orbit", "iso": m.get("iso"), "exp_s": m.get("exposure_s"),
               "torch": m.get("torch_level")}
        if (fwd > 0.01).all():
            u = k["fx"] * pc[:, 0] / fwd + k["cx"]
            v = -k["fy"] * pc[:, 1] / fwd + k["cy"]
            x0, x1 = max(0, u.min()), min(W, u.max())
            y0, y1 = max(0, v.min()), min(H, v.max())
        else:
            # Part of the box behind the camera: very close, the object fills the view.
            x0, x1, y0, y1 = 0, W, 0, H
        bw, bh = max(0.0, x1 - x0), max(0.0, y1 - y0)
        row["fill"] = round(max(bw, bh) / min(W, H), 3)
        row["area"] = round(bw * bh / (W * H), 3)
        # Distance: LiDAR median inside the box (depth map shares the image's orientation), else geometry.
        dist = None
        dfile = m.get("depth_file")
        if dfile and (sdir / dfile).exists() and bw > 0 and bh > 0:
            d = np.asarray(Image.open(sdir / dfile)).astype(np.float64)
            dh, dw = d.shape
            sx, sy = dw / W, dh / H
            patch = d[int(y0 * sy):max(int(y0 * sy) + 1, int(y1 * sy)), int(x0 * sx):max(int(x0 * sx) + 1, int(x1 * sx))]
            valid = patch[patch > 0]
            if valid.size >= 5:
                # Nearest third of the box pixels: the object, not the support behind its edges.
                dist = float(np.percentile(valid, 33)) / 1000
        if dist is None:
            t = np.array(m["camera"]["position_m"])
            dist = float(np.linalg.norm(np.array(cb["center_m"]) - t))
        row["dist_m"] = round(dist, 3)
        row["px_mm"] = round(k["fx"] / (dist * 1000), 1)
        img = Image.open(img_path).convert("L")
        crop = np.asarray(img.crop((int(x0), int(y0), int(math.ceil(x1)), int(math.ceil(y1))))).astype(np.float64)
        if crop.size and crop.shape[0] >= 4 and crop.shape[1] >= 4:
            hh, ww = crop.shape[0] // 2 * 2, crop.shape[1] // 2 * 2
            binned = crop[:hh, :ww].reshape(hh // 2, 2, ww // 2, 2).mean(axis=(1, 3))
            row["sharp"] = round(laplacian_var(binned), 1)
            row["sharp_full"] = round(laplacian_var(crop), 1)
            row["noise"] = round(immerkaer_sigma(crop), 2)
        else:
            row["sharp"] = row["sharp_full"] = row["noise"] = None
        target = CLOSE_PX_MM if row["kind"] == "closeup" else ORBIT_PX_MM
        row["detail_ok"] = row["px_mm"] >= target
        row["phone_sharp"] = m.get("sharpness")
        rows.append(row)
    return {"session": sdir.name, "scan": pkg.get("scan_id"), "box_cm": [round(s * 100, 1) for s in size], "stills": rows}


def summary(res):
    s = res["stills"]
    if not s:
        return {}
    med = lambda xs: float(np.median([x for x in xs if x is not None])) if any(x is not None for x in xs) else None
    return {"stills": len(s), "closeups": sum(r["kind"] == "closeup" for r in s),
            "fill_median": med([r["fill"] for r in s]), "area_median": med([r["area"] for r in s]),
            "dist_median_m": med([r["dist_m"] for r in s]), "px_mm_median": med([r["px_mm"] for r in s]),
            "iso_median": med([r["iso"] for r in s]), "exp_median_s": med([r["exp_s"] for r in s]),
            "sharp_median": med([r["sharp"] for r in s]), "noise_median": med([r["noise"] for r in s]),
            "detail_ok": sum(r["detail_ok"] for r in s)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+", type=Path)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    out = []
    for sd in a.sessions:
        res = analyse_session(sd)
        res["summary"] = summary(res)
        out.append(res)
        print(f"\n{res['session']}  {res['scan']}  box {res['box_cm']} cm")
        cols = ["id", "kind", "fill", "area", "dist_m", "px_mm", "iso", "exp_s", "torch", "sharp", "sharp_full", "noise", "detail_ok"]
        print("  ".join(f"{c:>9}" for c in cols))
        for r in res["stills"]:
            print("  ".join(f"{str(r.get(c)):>9}" for c in cols))
        print("summary:", json.dumps(res["summary"]))
    if a.json:
        a.json.write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    sys.exit(main())
