#!/usr/bin/env python3
"""Does a finding's 3D point (`location.point_m`) land on the defect in the photos? Measured on recorded scans.

    cd backend && .venv/bin/python ../tools/placement_check.py --data-dir $REPAIROS_DATA_DIR --out /tmp/placement

Read-only on the data dir (sqlite opened with mode=ro, blobs only read). For every finished report of a LiDAR scan:

1. Findings seen in 2+ stills (the report row's `crop` + `crops`, merged across photos): `point_m` is projected into
   every still that has a box for it; the pixel distance to that still's box centre, the same in mm at the point's
   depth (px * depth / fx), whether it lands inside the box, and the error over the box half-diagonal. The still the
   point was ray-cast from (`crop.still_id`) is the "source"; the others are the independent test.
   Each other still's box is also located on its own (`try_locate`, as the pipeline does): the 3D distance between
   that point and `point_m` is "relocate_mm".
2. Triangulation residual: the closest-approach distance (mm) between the rays through each still's box centre
   (every pair), with the angle between the rays.
3. Findings seen in one still: `point_m` is projected into every other still of the scan in which it is in front of
   the camera and inside the frame; the still's own LiDAR depth at that pixel says whether something is in front
   (occluded, > 3 cm nearer) and how well depths agree. Contact sheets (JPEG): per view, the whole still with the
   box and the projected point, and a 300 px full-resolution crop around the projected point with a crosshair.
4. Summary per scan and overall (median / p90 px and mm, share inside the box) and the error by distance of the
   point from the principal point (radius / half-diagonal of the image), to see whether errors grow at the edges.
5. Depth vs cloud: each still's own LiDAR depth (taken with the 12 MP photo), placed with the still's pose, against
   the scan cloud: median distance in mm (mostly the offset along the surface normal).
6. Still-to-still patch alignment, the pose test that does not depend on detector boxes: for textured pixels of
   still A (3D from A's own LiDAR depth, local plane from its gradient), still B is warped onto A's pixel grid through
   that plane with both poses, and the best NCC shift is the misregistration (a self-pair gives 0.02 px). It is split
   into the part along B's epipolar line (what an error in A's LiDAR depth would cause) and the part across it
   (pose / intrinsics only), and binned by image radius (lens distortion would grow toward the edges).

Box caveat: detector boxes are often large (a whole harness, ~1500 px), so a box centre is not a precise point;
the px numbers of 1 and 2 are an upper bound on placement error, 5 and 6 measure the pose itself.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from repairos.locate import try_locate  # noqa: E402
from repairos.locate.camera import Pinhole, Pose, pixel_rays, project  # noqa: E402
from repairos.locate.cloud import ScanCloud, read_ply  # noqa: E402
from repairos.pipeline import segments  # noqa: E402

CROP = 300
THUMB_W = 300
OCCLUDED_M = 0.03


# ------------------------------------------------------------------------------------------------ data access
class Data:
    def __init__(self, root: Path):
        self.root = root
        self.db = sqlite3.connect(f"file:{root / 'state.sqlite3'}?mode=ro", uri=True)

    def rows(self, kind: str, scan_id: str | None = None) -> list[dict]:
        q, a = "select json from rows where kind=?", [kind]
        if scan_id:
            q, a = q + " and scan_id=?", a + [scan_id]
        return [json.loads(j) for (j,) in self.db.execute(q, a)]

    def blob(self, ref: str) -> Path:
        sha = ref.split("sha256:", 1)[1]
        return self.root / "blobs" / sha[:2] / sha


def cam_of(s: dict) -> tuple[Pose, Pinhole]:
    return Pose.from_contract(s["camera"]), Pinhole.from_contract(s["intrinsics"], s.get("width_px"),
                                                                 s.get("height_px"))


def views_of(f: dict) -> list[tuple[str, list[float]]]:
    """(still_id, box) per distinct still of a report row, the first box of each still, source first."""
    out, seen = [], set()
    for c in [f.get("crop") or f.get("photo"), *(f.get("crops") or [])]:
        if c and c.get("box_px") and c["still_id"] not in seen:
            seen.add(c["still_id"])
            out.append((c["still_id"], [float(v) for v in c["box_px"]]))
    return out


def stats(a, scale=1.0) -> dict:
    a = np.asarray([v for v in a if v is not None and np.isfinite(v)], float) * scale
    if len(a) == 0:
        return {"n": 0, "median": None, "p90": None}
    return {"n": int(len(a)), "median": float(np.median(a)), "p90": float(np.percentile(a, 90))}


def ray_pair(o1, d1, o2, d2) -> tuple[float, float]:
    """Closest-approach distance (m) of two rays (t >= 0) and the angle between them (deg)."""
    w0 = o1 - o2
    a, b, c, d, e = d1 @ d1, d1 @ d2, d2 @ d2, d1 @ w0, d2 @ w0
    den = a * c - b * b
    if den < 1e-12:
        t1, t2 = 0.0, e / c
    else:
        t1, t2 = (b * e - c * d) / den, (a * e - b * d) / den
    t1, t2 = max(t1, 0.0), max(t2, 0.0)
    dist = float(np.linalg.norm((o1 + t1 * d1) - (o2 + t2 * d2)))
    return dist, float(np.degrees(np.arccos(np.clip(d1 @ d2, -1, 1))))


def depth_map(data: Data, s: dict) -> np.ndarray | None:
    if not s.get("depth_file"):
        return None
    try:
        return np.asarray(Image.open(data.blob(s["depth_file"])), dtype=np.float32) / 1000.0
    except Exception:  # noqa: BLE001
        return None


def conf_map(data: Data, s: dict) -> np.ndarray | None:
    if not s.get("confidence_file"):
        return None
    try:
        return np.asarray(Image.open(data.blob(s["confidence_file"])))
    except Exception:  # noqa: BLE001
        return None


def sample_depth(dm: np.ndarray | None, uv: np.ndarray, cam: Pinhole) -> float | None:
    if dm is None:
        return None
    h, w = dm.shape
    i, j = int(uv[0] * w / cam.width), int(uv[1] * h / cam.height)
    if not (0 <= i < w and 0 <= j < h):
        return None
    win = dm[max(0, j - 1):j + 2, max(0, i - 1):i + 2]
    win = win[win > 0]
    return float(np.median(win)) if len(win) else None


# ------------------------------------------------------------------------------------------------ per scan
def scan_inputs(data: Data, scan_id: str):
    pkg = next(iter(data.rows("scan-package", scan_id)), None)
    run = next(iter(data.rows("pipeline-run", scan_id)), None)
    seg_rec = (run or {}).get("segments")
    unplaced = segments.unplaced_segments(pkg, seg_rec)
    to0 = segments.aligned_transforms(seg_rec)
    stills, skipped = {}, defaultdict(int)
    for s in data.rows("still", scan_id):
        k = int(s.get("segment") or 0)
        if "camera" not in s or s.get("kind") == "macro_detail" or s.get("user_skipped"):
            skipped["no pose"] += 1
            continue
        if k in unplaced:
            skipped["unplaced segment"] += 1
            continue
        stills[s["still_id"]] = segments.restate_still(s, to0[k]) if k in to0 else s
    local = dict(pkg)
    local.pop("keyframes", None)
    local["points_file"] = str(data.blob((seg_rec or {}).get("cloud_ref") or pkg["points_file"]))
    if pkg.get("mesh_file"):
        local["mesh_file"] = str(data.blob(pkg["mesh_file"]))
    return pkg, local, stills, dict(skipped)


def measure_scan(data: Data, report: dict, tiles: list) -> dict | None:
    scan_id = report["scan_id"]
    pkg, local, stills, skipped = scan_inputs(data, scan_id)
    if pkg is None or (pkg.get("depth_source") or "lidar") != "lidar" or not pkg.get("points_file"):
        return None
    rows = [f for sec in ("findings", "cosmetic") for f in report.get(sec) or []
            if (f.get("location") or {}).get("status") == "computed"]
    if not rows:
        return None
    # one cloud per scan (try_locate's own cache is keyed by file, and re-run scans share a cloud file)
    cloud = ScanCloud.from_package(local, cell_m=max(0.004, 2.0 * float(pkg.get("voxel_mm") or 3.0) / 1000.0))
    multi, single = [], []
    for f in rows:
        P = np.asarray(f["location"]["point_m"], float)
        vs = [(sid, box) for sid, box in views_of(f) if sid in stills]
        if not vs:
            continue
        src = vs[0][0]
        rec = {"finding_id": f["finding_id"], "issue": f.get("issue"), "part": f.get("part_class"),
               "words": f.get("part_words"), "source": src, "loc_conf": f["location"].get("confidence_word"),
               "loc_notes": f["location"].get("notes"), "point_m": P.tolist(), "views": []}
        # recompute the source's location (checks that this tool reads the data as the pipeline did)
        r0 = try_locate(cloud, stills[src], tuple(vs[0][1]))
        rec["recompute_mm"] = (float(np.linalg.norm(np.asarray(r0.point_m) - P)) * 1000
                               if hasattr(r0, "point_m") else None)
        rays = []
        for sid, box in vs:
            s = stills[sid]
            pose, cam = cam_of(s)
            uv, dep = project(P[None], pose, cam)
            uv, dep = uv[0], float(dep[0])
            x0, y0, x1, y1 = box
            c = np.array([(x0 + x1) / 2, (y0 + y1) / 2])
            err = float(np.hypot(*(uv - c)))
            v = {"still": sid, "role": "source" if sid == src else "other", "box": box, "uv": uv.tolist(),
                 "depth_m": dep, "err_px": err, "err_mm": err * dep / cam.fx * 1000 if dep > 0 else None,
                 "inside": bool(x0 <= uv[0] <= x1 and y0 <= uv[1] <= y1),
                 "err_over_halfdiag": err / (0.5 * math.hypot(x1 - x0, y1 - y0)),
                 "box_px": math.hypot(x1 - x0, y1 - y0),
                 "r_norm": float(np.hypot(c[0] - cam.cx, c[1] - cam.cy) / math.hypot(cam.cx, cam.cy)),
                 "pose_reliable": s.get("pose_reliable"), "tracking": s.get("tracking_state")}
            o, d = pixel_rays(c[None], pose, cam)
            rays.append((sid, o, d[0]))
            if sid != src:
                r = try_locate(cloud, s, tuple(box))
                v["relocate_mm"] = (float(np.linalg.norm(np.asarray(r.point_m) - P)) * 1000
                                    if hasattr(r, "point_m") else None)
                vs_dir = P - pose.t
                src_dir = P - cam_of(stills[src])[0].t
                v["angle_deg"] = float(np.degrees(np.arccos(np.clip(
                    vs_dir @ src_dir / np.linalg.norm(vs_dir) / np.linalg.norm(src_dir), -1, 1))))
            rec["views"].append(v)
        if len(vs) >= 2:
            pairs = []
            for i in range(len(rays)):
                for k in range(i + 1, len(rays)):
                    dist, ang = ray_pair(rays[i][1], rays[i][2], rays[k][1], rays[k][2])
                    pairs.append({"a": rays[i][0], "b": rays[k][0], "closest_mm": dist * 1000, "angle_deg": ang})
            rec["ray_pairs"] = pairs
            multi.append(rec)
            tiles.append(("multi", scan_id, rec))
        else:
            others = []
            for sid, s in stills.items():
                if sid == src:
                    continue
                pose, cam = cam_of(s)
                uv, dep = project(P[None], pose, cam)
                uv, dep = uv[0], float(dep[0])
                if not (dep > 0.05 and 0 <= uv[0] < cam.width and 0 <= uv[1] < cam.height):
                    continue
                md = sample_depth(depth_map(data, s), uv, cam)
                d1 = P - pose.t
                d0 = P - cam_of(stills[src])[0].t
                others.append({"still": sid, "uv": uv.tolist(), "depth_m": dep, "map_depth_m": md,
                               "occluded": md is not None and md < dep - OCCLUDED_M,
                               "depth_diff_mm": (md - dep) * 1000 if md is not None else None,
                               "angle_deg": float(np.degrees(np.arccos(np.clip(
                                   d1 @ d0 / np.linalg.norm(d1) / np.linalg.norm(d0), -1, 1)))),
                               "r_norm": float(np.hypot(uv[0] - cam.cx, uv[1] - cam.cy) / math.hypot(cam.cx, cam.cy)),
                               "box": None, "role": "other"})
            # comparable views first (3-70 deg from the source, nearest angle first), then the rest
            rec["others"] = sorted(others, key=lambda o: (not 3 <= o["angle_deg"] <= 70, o["angle_deg"]))
            single.append(rec)
            tiles.append(("single", scan_id, rec))
    return {"scan_id": scan_id, "session_id": report.get("session_id"), "stills": len(stills), "skipped": skipped,
            "multi": multi, "single": single}


# ------------------------------------------------------------------------------------------------ pose checks
def depth_vs_cloud(data: Data, s: dict, pc) -> dict | None:
    """Median distance (mm) from the still's own high-confidence LiDAR depth, placed with the still's pose, to the
    scan cloud (overlapping part only). Mostly the offset along the surface normal; slides along a surface are not
    visible to it (the pair check measures those)."""
    import open3d as o3d

    from repairos.locate.camera import backproject

    dm, cf = depth_map(data, s), conf_map(data, s)
    if dm is None:
        return None
    pose, cam = cam_of(s)
    pts = backproject(dm, cam.scaled(dm.shape[1], dm.shape[0])).reshape(-1, 3)
    ok = (dm.reshape(-1) > 0.05) & (dm.reshape(-1) < 2.0)
    if cf is not None and cf.shape == dm.shape:
        ok &= cf.reshape(-1) >= 2
    pw = pose.to_world(pts[ok])
    if len(pw) < 200:
        return None
    d = np.asarray(o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pw)).compute_point_cloud_distance(pc))
    near = d < 0.02
    if near.sum() < 200:
        return None
    return {"still": s["still_id"], "overlap": float(near.mean()), "median_mm": float(np.median(d[near]) * 1000),
            "p90_mm": float(np.percentile(d[near], 90) * 1000)}


def gray_half(data: Data, s: dict) -> np.ndarray:
    im = Image.open(data.blob(s["image_file"]))
    W, H = int(s["width_px"]), int(s["height_px"])
    im.draft("L", (W // 2, H // 2))
    im = im.convert("L")
    if im.size != (W // 2, H // 2):
        im = im.resize((W // 2, H // 2), Image.BILINEAR)
    return np.asarray(im, dtype=np.float32)


def bilinear(img: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Sample at continuous pixel coords (pixel centres at i + 0.5); NaN outside."""
    h, w = img.shape
    x, y = u - 0.5, v - 0.5
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    ok = (x0 >= 0) & (y0 >= 0) & (x0 < w - 1) & (y0 < h - 1)
    x0c, y0c = np.clip(x0, 0, w - 2), np.clip(y0, 0, h - 2)
    fx, fy = x - x0c, y - y0c
    out = (img[y0c, x0c] * (1 - fx) * (1 - fy) + img[y0c, x0c + 1] * fx * (1 - fy)
           + img[y0c + 1, x0c] * (1 - fx) * fy + img[y0c + 1, x0c + 1] * fx * fy)
    out[~ok] = np.nan
    return out


PATCH_R, SEARCH = 20, 24   # half-resolution px: a 41 px patch (82 px full res), +-24 (+-48 full res) search


def pair_check(sa: dict, sb: dict, ga: np.ndarray, gb: np.ndarray, da: np.ndarray, db: np.ndarray | None,
               max_pts: int = 80, why: dict | None = None) -> list[dict]:
    """For textured pixels of still A (3D from A's own LiDAR depth, local plane from its depth gradient), warp B onto
    A's pixel grid through that plane with both poses, and find the shift that best aligns B's patch with A's
    (normalised cross-correlation). Perfect poses and pinhole intrinsics give a zero shift; the shift is the
    misregistration, in A's full-resolution pixels."""
    from numpy.lib.stride_tricks import sliding_window_view

    pa, ca = cam_of(sa)
    pb, cb = cam_of(sb)
    ha, wa = ga.shape
    ca2 = ca.scaled(wa, ha)
    cb2 = cb.scaled(gb.shape[1], gb.shape[0])
    dh, dw = da.shape
    m = PATCH_R + SEARCH + 2
    us = np.linspace(m, wa - m, 40)
    vs = np.linspace(m, ha - m, 30)
    cand = []
    for v in vs:
        for u in us:
            if da[min(dh - 1, int(v * dh / ha)), min(dw - 1, int(u * dw / wa))] <= 0.05:
                continue
            p = ga[int(v) - PATCH_R:int(v) + PATCH_R + 1, int(u) - PATCH_R:int(u) + PATCH_R + 1]
            cand.append((float(p.std()), u, v))
    cand = [(t, math.floor(u) + 0.5, math.floor(v) + 0.5) for t, u, v in sorted(cand, reverse=True) if t > 12][:max_pts]
    why = why if why is not None else {}
    drop = lambda k: why.__setitem__(k, why.get(k, 0) + 1)  # noqa: E731
    out = []
    off = np.arange(-(PATCH_R + SEARCH), PATCH_R + SEARCH + 1, dtype=float)
    gu, gv = np.meshgrid(off, off)
    for _, u, v in cand:
        i, j = int(u * dw / wa), int(v * dh / ha)
        if not (1 <= i < dw - 1 and 1 <= j < dh - 1):
            drop("edge")
            continue
        z = float(da[j, i])
        if z <= 0.05:
            drop("no confident depth in A")
            continue
        def cpt(ii, jj):
            zz = float(da[jj, ii])
            uu, vv = (ii + 0.5) * wa / dw, (jj + 0.5) * ha / dh
            return np.array([(uu - ca2.cx) / ca2.fx * zz, -(vv - ca2.cy) / ca2.fy * zz, -zz]), zz
        X, _ = cpt(i, j)
        (Xr, zr), (Xd, zd) = cpt(i + 1, j), cpt(i, j + 1)
        n = np.array([0.0, 0.0, 1.0])
        if abs(zr - z) < 0.02 and abs(zd - z) < 0.02:
            nn = np.cross(Xr - X, Xd - X)
            if np.linalg.norm(nn) > 1e-12:
                n = nn / np.linalg.norm(nn)
        # the exact 3D point under (u, v): the ray through it meets the local plane
        def on_plane(uu, vv):
            d = np.stack([(uu - ca2.cx) / ca2.fx, -(vv - ca2.cy) / ca2.fy, -np.ones_like(uu)], -1)
            t = (X @ n) / (d @ n)
            return d * t[..., None], t
        P, t = on_plane(u + gu, v + gv)
        if not np.all(t > 0):
            drop("plane behind A")
            continue
        Pw = pa.to_world(P.reshape(-1, 3))
        uvb, depb = project(Pw, pb, cb2)
        if np.any(depb <= 0.05):
            drop("behind B")
            continue
        centre_b = uvb[len(uvb) // 2]
        if db is not None:
            bh, bw = db.shape
            ib, jb = int(centre_b[0] * bw / gb.shape[1]), int(centre_b[1] * bh / gb.shape[0])
            if not (0 <= ib < bw and 0 <= jb < bh) or db[jb, ib] <= 0 or abs(db[jb, ib] - depb[len(depb) // 2]) > 0.02:
                drop("occluded or no depth in B")
                continue   # occluded in B, or B's depth disagrees: not a usable match
        wb = bilinear(gb, uvb[:, 0], uvb[:, 1]).reshape(gu.shape)
        if np.isnan(wb).any():
            drop("outside B")
            continue
        a = ga[int(v) - PATCH_R:int(v) + PATCH_R + 1, int(u) - PATCH_R:int(u) + PATCH_R + 1].astype(float)
        a = (a - a.mean()) / (a.std() + 1e-6)
        win = sliding_window_view(wb, a.shape)
        mu = win.mean(axis=(2, 3))
        sd = win.std(axis=(2, 3)) + 1e-6
        ncc = np.einsum("ijkl,kl->ij", win, a) / a.size / sd - mu / sd * a.mean()
        k = np.unravel_index(int(np.argmax(ncc)), ncc.shape)
        best = float(ncc[k])
        if best < 0.6:
            drop("ncc < 0.6")
            continue
        if k[0] in (0, ncc.shape[0] - 1) or k[1] in (0, ncc.shape[1] - 1):
            drop("best at the search edge")
            continue
        # sub-pixel by a parabola per axis
        def sub(c0, cm, cp):
            den = cm - 2 * c0 + cp
            return 0.0 if abs(den) < 1e-9 else 0.5 * (cm - cp) / den
        dy = k[0] - SEARCH + sub(ncc[k], ncc[k[0] - 1, k[1]], ncc[k[0] + 1, k[1]])
        dx = k[1] - SEARCH + sub(ncc[k], ncc[k[0], k[1] - 1], ncc[k[0], k[1] + 1])
        # into B's pixels (local Jacobian of the warp), then split into the part a depth error in A explains (along
        # B's epipolar line of A's ray) and the rest (pose / intrinsics only)
        g = uvb.reshape(gu.shape + (2,))
        c0 = PATCH_R + SEARCH
        J = np.stack([(g[c0, c0 + 1] - g[c0, c0 - 1]) / 2, (g[c0 + 1, c0] - g[c0 - 1, c0]) / 2], axis=1)
        s_b = J @ np.array([dx, dy]) * 2                            # full-res B px
        Xw = pa.to_world(X[None])[0]
        ray = Xw - pa.t
        ray /= np.linalg.norm(ray)
        k = (project((Xw + 0.001 * ray)[None], pb, cb)[0][0] - project(Xw[None], pb, cb)[0][0])  # full-res px per mm
        kn = float(np.linalg.norm(k))
        along = float(s_b @ k / kn) if kn > 1e-9 else 0.0
        perp = float(abs(s_b[0] * k[1] - s_b[1] * k[0]) / kn) if kn > 1e-9 else float(np.linalg.norm(s_b))
        zb = float(depb[len(depb) // 2])
        sx, sy = 2 * dx, 2 * dy
        out.append({"u": 2 * u, "v": 2 * v, "depth_m": z, "dx_px": sx, "dy_px": sy, "shift_px": float(np.hypot(sx, sy)),
                    "shift_mm": float(np.hypot(sx, sy) * z / ca.fx * 1000), "ncc": best,
                    "ncc0": float(ncc[SEARCH, SEARCH]), "perp_px": perp, "perp_mm": perp * zb / cb.fx * 1000,
                    "depth_err_mm": along / kn if kn > 1e-9 else None, "px_per_mm_depth": kn,
                    "r_norm": float(np.hypot(2 * u - ca.cx, 2 * v - ca.cy) / math.hypot(ca.cx, ca.cy))})
    return out


def pick_pairs(stills: dict, used: set[str], max_pairs: int) -> list[tuple[str, str, float, float]]:
    """Pairs (A, B, angle deg, baseline m) of reliable-pose stills that look at the same area from 3-35 deg apart:
    A's centre ray depth projects inside B. Stills that hold findings come first."""
    rel = [s for s in stills.values() if s.get("pose_reliable") is not False and s.get("depth_file")
           and s.get("tracking_state") != "limited"]
    rel.sort(key=lambda s: (s["still_id"] not in used, s["still_id"]))
    out, seen = [], set()
    for sa in rel:
        pa, ca = cam_of(sa)
        za = -pa.r[:, 2]
        best = None
        for sb in rel:
            if sb["still_id"] == sa["still_id"] or frozenset((sa["still_id"], sb["still_id"])) in seen:
                continue
            pb, cb = cam_of(sb)
            ang = float(np.degrees(np.arccos(np.clip(za @ -pb.r[:, 2], -1, 1))))
            if not 3 <= ang <= 35:
                continue
            X = pa.t + za * 0.25
            uv, dep = project(X[None], pb, cb)
            if dep[0] <= 0 or not (0.2 * cb.width < uv[0, 0] < 0.8 * cb.width and 0.2 * cb.height < uv[0, 1] < 0.8 * cb.height):
                continue
            score = abs(ang - 15)
            if best is None or score < best[0]:
                best = (score, sb["still_id"], ang, float(np.linalg.norm(pa.t - pb.t)))
        if best:
            seen.add(frozenset((sa["still_id"], best[1])))
            out.append((sa["still_id"], best[1], best[2], best[3]))
        if len(out) >= max_pairs:
            break
    return out


def pose_checks(data: Data, r: dict, max_pairs: int) -> dict:
    import open3d as o3d

    _, local, stills, _ = scan_inputs(data, r["scan_id"])
    v = read_ply(local["points_file"])
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.stack([v["x"], v["y"], v["z"]], 1).astype(float)))
    dvc = [x for s in stills.values() if (x := depth_vs_cloud(data, s, pc)) is not None]
    used = {vv["still"] for rec in r["multi"] + r["single"] for vv in rec["views"]}
    pairs = []
    cache: dict[str, np.ndarray] = {}
    for a_id, b_id, ang, base in pick_pairs(stills, used, max_pairs):
        for k in (a_id, b_id):
            if k not in cache:
                cache[k] = gray_half(data, stills[k])
        da = depth_map(data, stills[a_id])
        ca_ = conf_map(data, stills[a_id])
        if ca_ is not None and da is not None and ca_.shape == da.shape:
            da = np.where(ca_ >= 2, da, 0)
        why: dict = {}
        pts = pair_check(stills[a_id], stills[b_id], cache[a_id], cache[b_id], da, depth_map(data, stills[b_id]),
                         why=why)
        sh = np.array([[p["dx_px"], p["dy_px"]] for p in pts]) if pts else np.zeros((0, 2))
        common = np.median(sh, axis=0) if len(sh) else np.array([np.nan, np.nan])
        pairs.append({"a": a_id, "b": b_id, "angle_deg": ang, "baseline_m": base, "points": pts, "dropped": why,
                      "common_dx_px": float(common[0]), "common_dy_px": float(common[1]),
                      "common_px": float(np.hypot(*common)),
                      "spread_px": float(np.median(np.hypot(*(sh - common).T))) if len(sh) else None})
    return {"depth_vs_cloud": dvc, "pairs": pairs}


# ------------------------------------------------------------------------------------------------ contact sheets
def _font(size=15):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def cross(d: ImageDraw.ImageDraw, x: float, y: float, r: int, col=(255, 40, 40)):
    for w, c in ((5, (255, 255, 255)), (2, col)):
        d.line([(x - r, y), (x - 4, y)], fill=c, width=w)
        d.line([(x + 4, y), (x + r, y)], fill=c, width=w)
        d.line([(x, y - r), (x, y - 4)], fill=c, width=w)
        d.line([(x, y + 4), (x, y + r)], fill=c, width=w)
    d.ellipse([x - 3, y - 3, x + 3, y + 3], outline=c, width=1)


def make_tile(im: Image.Image, v: dict, caption: str) -> Image.Image:
    W, H = im.size
    u, vv = v["uv"]
    cell = Image.new("RGB", (THUMB_W + CROP + 6, CROP + 40), (30, 30, 30))
    th = im.resize((THUMB_W, int(THUMB_W * H / W)), Image.BILINEAR)
    s = THUMB_W / W
    d = ImageDraw.Draw(th)
    if v.get("box"):
        x0, y0, x1, y1 = v["box"]
        d.rectangle([x0 * s, y0 * s, x1 * s, y1 * s], outline=(60, 220, 60), width=2)
    d.rectangle([(u - CROP / 2) * s, (vv - CROP / 2) * s, (u + CROP / 2) * s, (vv + CROP / 2) * s],
                outline=(255, 255, 0), width=1)
    if 0 <= u < W and 0 <= vv < H:
        cross(d, u * s, vv * s, 10)
    cell.paste(th, (0, (CROP - th.height) // 2))
    x0c, y0c = int(round(u - CROP / 2)), int(round(vv - CROP / 2))
    crop = Image.new("RGB", (CROP, CROP), (90, 90, 90))
    if x0c < W and y0c < H and x0c + CROP > 0 and y0c + CROP > 0:   # else the point is off the frame: grey
        crop.paste(im.crop((max(0, x0c), max(0, y0c), min(W, x0c + CROP), min(H, y0c + CROP))),
                   (max(0, -x0c), max(0, -y0c)))
    dc = ImageDraw.Draw(crop)
    if v.get("box"):
        bx0, by0, bx1, by1 = v["box"]
        dc.rectangle([bx0 - x0c, by0 - y0c, bx1 - x0c, by1 - y0c], outline=(60, 220, 60), width=2)
        cx, cy = (bx0 + bx1) / 2 - x0c, (by0 + by1) / 2 - y0c
        dc.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], outline=(80, 160, 255), width=3)
    cross(dc, CROP / 2, CROP / 2, 40)
    cell.paste(crop, (THUMB_W + 6, 0))
    ImageDraw.Draw(cell).text((4, CROP + 4), caption, fill=(235, 235, 235), font=_font(14))
    return cell


def build_sheets(data: Data, tiles: list, stills_by_scan: dict, out: Path, max_views: int = 9) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    # requests per still, so each 12 MP photo is decoded once
    reqs = defaultdict(list)
    sheets = []
    for kind, scan_id, rec in tiles:
        views = list(rec["views"])
        if kind == "single":
            vis = [o for o in rec["others"] if not o["occluded"]]
            views += vis[:max_views - 1]
        cells = []
        for k, v in enumerate(views):
            cells.append(None)
            reqs[(scan_id, v["still"])].append((len(sheets), k, v, rec))
        sheets.append({"kind": kind, "scan_id": scan_id, "rec": rec, "cells": cells})
    for (scan_id, sid), rs in reqs.items():
        s = stills_by_scan[scan_id][sid]
        im = Image.open(data.blob(s["image_file"])).convert("RGB")
        fx = s["intrinsics"]["fx"]
        for si, k, v, rec in rs:
            mm = CROP * v["depth_m"] / fx * 1000
            bits = [sid.split("_")[-1], v["role"], f"{v['depth_m']:.2f} m", f"crop {mm:.0f} mm"]
            if v["role"] == "other":
                bits.append(f"{v.get('angle_deg', 0):.0f} deg off src")
            if v.get("err_px") is not None and v["role"] == "other":
                bits.append(f"err {v['err_px']:.0f}px/{v['err_mm']:.0f}mm {'IN' if v['inside'] else 'OUT'}")
            if v.get("depth_diff_mm") is not None:
                bits.append(f"dz {v['depth_diff_mm']:+.0f}mm")
            sheets[si]["cells"][k] = make_tile(im, v, "  ".join(bits))
        im.close()
    paths = []
    for sh in sheets:
        rec, cells = sh["rec"], sh["cells"]
        cols = 2
        cw, chh = cells[0].size
        rows = math.ceil(len(cells) / cols)
        head = 46
        sheet = Image.new("RGB", (cw * cols + 6, head + rows * (chh + 6)), (15, 15, 15))
        d = ImageDraw.Draw(sheet)
        d.text((6, 4), f"{sh['scan_id']}  {rec['finding_id']}  {rec['issue']} {rec['part']}  "
                       f"(loc {rec['loc_conf']}, {sh['kind']}-still)", fill=(255, 255, 255), font=_font(16))
        d.text((6, 24), (rec.get("words") or "")[:150], fill=(200, 200, 200), font=_font(13))
        for i, c in enumerate(cells):
            sheet.paste(c, ((i % cols) * (cw + 6), head + (i // cols) * (chh + 6)))
        p = out / f"{sh['scan_id']}__{rec['finding_id']}__{sh['kind']}.jpg"
        sheet.save(p, quality=85)
        rec["sheet"] = str(p)
        paths.append(str(p))
    return paths


# ------------------------------------------------------------------------------------------------ summary
def summarise(scans: list[dict]) -> dict:
    def block(ss):
        ov = [v for s in ss for r in s["multi"] for v in r["views"] if v["role"] == "other"
              and v["pose_reliable"] is not False]
        bad = [v for s in ss for r in s["multi"] for v in r["views"] if v["role"] == "other"
               and v["pose_reliable"] is False]
        pairs = [p for s in ss for r in s["multi"] for p in r["ray_pairs"]]
        sing = [o for s in ss for r in s["single"] for o in r["others"] if not o["occluded"]]
        return {
            "multi_findings": sum(len(s["multi"]) for s in ss),
            "other_views": len(ov), "unreliable_pose_views_excluded": len(bad),
            "px": stats([v["err_px"] for v in ov]), "mm": stats([v["err_mm"] for v in ov]),
            "inside_share": (sum(v["inside"] for v in ov) / len(ov)) if ov else None,
            "err_over_halfdiag": stats([v["err_over_halfdiag"] for v in ov]),
            "box_diag_px": stats([v["box_px"] for v in ov]),
            "relocate_mm": stats([v.get("relocate_mm") for v in ov]),
            "ray_closest_mm": stats([p["closest_mm"] for p in pairs]),
            "ray_angle_deg": stats([p["angle_deg"] for p in pairs]),
            "single_findings": sum(len(s["single"]) for s in ss),
            "single_visible_views": len(sing),
            "single_depth_diff_mm": stats([abs(o["depth_diff_mm"]) for o in sing if o["depth_diff_mm"] is not None]),
            "recompute_mm": stats([r["recompute_mm"] for s in ss for r in s["multi"] + s["single"]]),
        }

    out = {"per_scan": {s["scan_id"]: block([s]) for s in scans}, "overall": block(scans)}
    ov = [v for s in scans for r in s["multi"] for v in r["views"] if v["role"] == "other"
          and v["pose_reliable"] is not False]
    bins = [(0, 0.35), (0.35, 0.65), (0.65, 1.01)]
    out["by_radius"] = [{"r_norm": f"{a:.2f}-{b:.2f}", **{k: stats([v[k] for v in ov if a <= v["r_norm"] < b])
                                                         for k in ("err_px", "err_mm", "err_over_halfdiag")}}
                        for a, b in bins]
    if len(ov) >= 5:
        r = np.array([v["r_norm"] for v in ov])
        e = np.array([v["err_over_halfdiag"] for v in ov])
        rk = lambda x: np.argsort(np.argsort(x))  # noqa: E731
        out["spearman_r_vs_err"] = float(np.corrcoef(rk(r), rk(e))[0, 1])
    return out


def fmt(st: dict, nd=0) -> str:
    return "-" if not st or st.get("n", 0) == 0 else f"{st['median']:.{nd}f} / {st['p90']:.{nd}f}"


def main() -> None:
    global SEARCH
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--scan", action="append", help="only these scan ids")
    ap.add_argument("--no-sheets", action="store_true")
    ap.add_argument("--no-pose", action="store_true", help="skip the pose checks (5, 6)")
    ap.add_argument("--pairs", type=int, default=10, help="still pairs per scan for the patch alignment check")
    ap.add_argument("--search", type=int, default=SEARCH, help="patch search radius, half-resolution px")
    a = ap.parse_args()
    SEARCH = a.search
    data = Data(a.data_dir)
    a.out.mkdir(parents=True, exist_ok=True)
    tiles, scans, stills_by_scan = [], [], {}
    latest, dupes = {}, {}
    for rep in sorted(data.rows("report"), key=lambda r: r.get("created_at") or ""):
        pkg = next(iter(data.rows("scan-package", rep["scan_id"])), None) or {}
        run = next(iter(data.rows("pipeline-run", rep["scan_id"])), None) or {}
        key = ((run.get("segments") or {}).get("cloud_ref") or pkg.get("points_file") or rep["scan_id"])
        if key in latest:
            dupes[latest[key]["scan_id"]] = rep["scan_id"]
        latest[key] = rep
    for rep in sorted(latest.values(), key=lambda r: r.get("created_at") or ""):
        if a.scan and rep["scan_id"] not in a.scan:
            continue
        r = measure_scan(data, rep, tiles)
        if r is None:
            continue
        scans.append(r)
        stills_by_scan[r["scan_id"]] = scan_inputs(data, r["scan_id"])[2]
        print(f"{r['scan_id']}: {len(r['multi'])} multi-still, {len(r['single'])} single-still located findings",
              file=sys.stderr)
    summary = summarise(scans)
    summary["superseded_reruns"] = dupes
    print(f"{len(dupes)} scans skipped: re-runs sharing the cloud of a later report", file=sys.stderr)
    pose = {}
    if not a.no_pose:
        for r in scans:
            pose[r["scan_id"]] = pose_checks(data, r, a.pairs)
            print(f"{r['scan_id']}: pose checks done ({len(pose[r['scan_id']]['pairs'])} pairs)", file=sys.stderr)
    sheets = [] if a.no_sheets else build_sheets(data, tiles, stills_by_scan, a.out)
    (a.out / "placement.json").write_text(json.dumps({"summary": summary, "scans": scans, "pose": pose},
                                                     indent=1, default=float))
    # ---- table
    print(f"\n{'scan':24} {'mult':>4} {'view':>4} {'px med/p90':>13} {'mm med/p90':>11} {'in':>5} "
          f"{'err/hd':>11} {'reloc mm':>11} {'ray mm':>11} {'sing':>4} {'svis':>5} {'|dz| mm':>9}")
    for sid, b in [*summary["per_scan"].items(), ("OVERALL", summary["overall"])]:
        ins = "-" if b["inside_share"] is None else f"{b['inside_share']:.0%}"
        print(f"{sid:24} {b['multi_findings']:4} {b['other_views']:4} {fmt(b['px']):>13} {fmt(b['mm']):>11} "
              f"{ins:>5} {fmt(b['err_over_halfdiag'], 2):>11} {fmt(b['relocate_mm']):>11} "
              f"{fmt(b['ray_closest_mm']):>11} {b['single_findings']:4} {b['single_visible_views']:5} "
              f"{fmt(b['single_depth_diff_mm']):>9}")
    print("\nby radius (box centre from principal point / half-diagonal), other views:")
    for b in summary["by_radius"]:
        print(f"  r {b['r_norm']}: n={b['err_px']['n']:3}  px {fmt(b['err_px'])}  mm {fmt(b['err_mm'])}  "
              f"err/halfdiag {fmt(b['err_over_halfdiag'], 2)}")
    if "spearman_r_vs_err" in summary:
        print(f"  spearman(radius, err/halfdiag) = {summary['spearman_r_vs_err']:+.2f}")
    if pose:
        dvc = [x for p in pose.values() for x in p["depth_vs_cloud"]]
        print(f"\nstill's own LiDAR depth (with its pose) vs the scan cloud, {len(dvc)} stills: median distance mm "
              f"{fmt(stats([x['median_mm'] for x in dvc]), 2)}")
        pts = [q for p in pose.values() for pr in p["pairs"] for q in pr["points"]]
        prs = [pr for p in pose.values() for pr in p["pairs"] if pr["points"]]
        print(f"\nstill-to-still patch alignment (plane-induced warp + NCC): {len(prs)} pairs, {len(pts)} matched points")
        print(f"  pair angle deg {fmt(stats([pr['angle_deg'] for pr in prs]))}, baseline mm "
              f"{fmt(stats([pr['baseline_m'] for pr in prs], 1000))}")
        print(f"  misregistration px (full res) {fmt(stats([q['shift_px'] for q in pts]), 1)}   mm "
              f"{fmt(stats([q['shift_mm'] for q in pts]), 2)}")
        print(f"  perpendicular to the epipolar line (pose / intrinsics only, a depth error cannot cause it) px "
              f"{fmt(stats([q['perp_px'] for q in pts]), 1)}   mm {fmt(stats([q['perp_mm'] for q in pts]), 2)}")
        print(f"  along it, as A's LiDAR depth error |dz| mm {fmt(stats([abs(q['depth_err_mm']) for q in pts]), 1)}; "
              f"per pair median dz (signed) mm {fmt(stats([float(np.median([q['depth_err_mm'] for q in pr['points']])) for pr in prs]), 1)}")
        print(f"  per pair: common shift px {fmt(stats([pr['common_px'] for pr in prs]), 1)}, spread around it px "
              f"{fmt(stats([pr['spread_px'] for pr in prs]), 1)}")
        drops: dict = {}
        for pr in prs:
            for k, n in pr["dropped"].items():
                drops[k] = drops.get(k, 0) + n
        print(f"  candidates dropped: {drops}")
        print(f"  ncc at zero shift -> best {fmt(stats([q['ncc0'] for q in pts]), 2)} -> {fmt(stats([q['ncc'] for q in pts]), 2)}")
        for lo, hi in ((0, 0.35), (0.35, 0.65), (0.65, 1.01)):
            sel = [q for q in pts if lo <= q["r_norm"] < hi]
            # residual after removing the pair's common shift: the part a per-photo pose fix cannot remove
            res = [math.hypot(q["dx_px"] - pr["common_dx_px"], q["dy_px"] - pr["common_dy_px"])
                   for pr in prs for q in pr["points"] if lo <= q["r_norm"] < hi]
            print(f"  r {lo:.2f}-{hi:.2f}: n={len(sel):4}  shift px {fmt(stats([q['shift_px'] for q in sel]), 1)}  "
                  f"perp px {fmt(stats([q['perp_px'] for q in sel]), 1)}  "
                  f"after removing the pair's common shift px {fmt(stats(res), 1)}")
    print(f"\n{len(sheets)} contact sheets in {a.out}; details in {a.out / 'placement.json'}")


if __name__ == "__main__":
    main()
