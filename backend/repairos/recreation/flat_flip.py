"""A thin object turned face down (a circuit board flipped for its underside), registered by what both faces share.

Shape cannot line a flipped board up: its two big faces never overlap and its outline is a near-symmetric rectangle
(four headings fit within 0.01 IoU on a real board). What both faces do share are the through-holes: mounting holes,
header pins, the legs of jacks and sockets. Seen from above they are socket openings and holes; from below, solder
joints and the same holes, at the same places, mirrored. So:

    res = register_flat_flip(seg0_stills, box0, table0, seg1_stills, box1, table1, lidar0, lidar1)

1. Each face's most top-down still is rectified onto its own measured table (recreation.flat) at RES_M per pixel, and
   its footprint found (flat.footprint).
2. Round features 0.8-3.5 mm across in each (difference of Gaussians, both polarities, inside the footprint).
3. Candidate poses: the flipped footprint's rectangle laid on the first one's (a flip mirrors it: four headings),
   each refined by a 2D rigid fit on matched features (within MATCH_MM), plus a capped random-sample search on feature
   pairs. Scored by matched features and their RMS.
4. Accepted when the best pose matches at least MIN_INLIERS features with RMS under MAX_RMS_MM, its outlines overlap
   (IoU at least MIN_IOU), and it beats the best pose turned away from it (over 20 deg) by MARGIN features and a
   factor of MARGIN_RATIO. Otherwise refused with the reason: a wrong placement is worse than none.
5. In 3D: the mirror is a turn of 180 deg about a horizontal axis in the board's plane; the board's bottom (the
   flipped segment's table side) goes to segment 0's table plus the board's thickness.
"""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

RES_M = 0.0002            # rectified faces at 0.2 mm per pixel
FEATURE_MM = (0.8, 3.5)   # through-hole features: header pins, jack legs, vias, mounting holes
MAX_FEATURES = 160
FEATURE_K = 2.0           # a feature stands this many robust spreads over the face's median response
MATCH_MM = 1.0            # a feature matched when within this of one on the other face
MIN_INLIERS = 12
MAX_RMS_MM = 0.8
MIN_IOU = 0.8
MARGIN = 6                # the best pose beats any pose turned away from it by this many matched features
MARGIN_RATIO = 1.3        # ... and by this factor (header rows also line up at a half turn)
RANSAC_ITERS = 400
BOARD_THICK_M = 0.0016    # the slab's top over the table when the thickness is not measured


class FlatFlipUnavailable(RuntimeError):
    pass


@dataclass
class Face:
    name: str               # still id
    grid: Any               # flat.Grid of the rectified face
    image: np.ndarray       # H x W x 3 uint8, rectified
    outline: np.ndarray     # K x 2 x, z (its own world)
    features: np.ndarray    # N x 2 x, z (its own world)
    table_y: float
    silhouette: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))   # x, z of the object's pixels


@dataclass
class FlatFlip:
    aligned: bool
    transform: np.ndarray | None          # 4x4: flipped segment's world -> segment 0's world
    reason: str | None
    stats: dict = field(default_factory=dict)
    faces: tuple[Face, Face] | None = None
    a2: np.ndarray | None = None          # the 2D map q0 = A q1 + b (x, z)
    b2: np.ndarray | None = None


# --- faces -----------------------------------------------------------------------------------------------------------

def _stills(stills: Sequence[Mapping[str, Any]], box: Any) -> list:
    """Top-down posed stills of one segment, in that segment's own world (a flipped segment's stills are marked
    pose_reliable false only because they are not in segment 0's frame)."""
    from repairos.recreation import flat

    # (load_stills skips other segments' photos for segment 0's slab; these are all of one segment, in its own world)
    return flat.load_stills([{**s, "pose_reliable": True, "segment": 0} for s in stills], box)


def _blur(g: np.ndarray, sigma: float) -> np.ndarray:
    r = max(1, int(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    pad = np.pad(g, r, mode="edge")
    out = np.apply_along_axis(lambda v: np.convolve(v, k, mode="valid"), 0, pad)
    return np.apply_along_axis(lambda v: np.convolve(v, k, mode="valid"), 1, out)


def features(gray: np.ndarray, mask: np.ndarray, res: float, mm: tuple[float, float] = FEATURE_MM, k: float | None = None,
             cap: int = MAX_FEATURES) -> np.ndarray:
    """Round features (row, col) FEATURE_MM across: local maxima of |difference of Gaussians| over scales, inside
    `mask`, strongest MAX_FEATURES, at least a feature's radius apart."""
    radii = np.geomspace(mm[0] / 2, mm[1] / 2, 5) / 1000 / res
    k = FEATURE_K if k is None else k
    sig = radii / np.sqrt(2)
    blurs = [_blur(gray, s) for s in np.r_[sig, sig[-1] * 1.6]]
    dogs = np.stack([np.abs(blurs[k] - blurs[k + 1]) * sig[k] for k in range(len(sig))])
    best = dogs.max(0)
    scale = dogs.argmax(0)
    from repairos.recon.segment_align import _window_min

    peak = best >= -_window_min(-best, 3)
    peak &= mask & (best > np.median(best[mask]) + k * 1.4826 * np.median(np.abs(best[mask] - np.median(best[mask]))))
    ii, jj = np.nonzero(peak)
    order = np.argsort(-best[ii, jj])
    keep: list[tuple[int, int]] = []
    for k in order:
        p = (ii[k], jj[k])
        if all((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 > max(4.0, radii[scale[p]] ** 2) for q in keep):
            keep.append(p)
        if len(keep) >= cap:
            break
    return np.array(keep, float).reshape(-1, 2)


def face(stills: Sequence[Mapping[str, Any]], box: Any, table_y: float, lidar: np.ndarray | None,
         thick: float = BOARD_THICK_M) -> Face:
    """One face of the board: its best top-down still rectified at RES_M over the footprint, and its features."""
    from repairos.recreation import flat

    views = _stills(stills, box)
    if not views:
        raise FlatFlipUnavailable("no posed photo looks down at this side")
    v = views[0]
    seg = flat.box_grid(box, flat.SEG_RES_M)
    saved, flat.DEBUG = flat.DEBUG, {}
    try:
        outline, _ = flat.footprint(v, seg, table_y + thick, None if lidar is None or not len(lidar) else lidar[:, [0, 2]])
        piece = flat.DEBUG["piece"]
    finally:
        flat.DEBUG = saved
    pi, pj = np.nonzero(piece)
    sil = seg.world(0.0, np.stack([pi, pj], 1)[::2])[:, [0, 2]]   # its whole silhouette, jacks and all
    lo = np.minimum(outline.min(0), sil.min(0)) - 0.005     # the whole silhouette: overhanging connectors too
    hi = np.maximum(outline.max(0), sil.max(0)) + 0.005
    shape = (int((hi[1] - lo[1]) / RES_M), int((hi[0] - lo[0]) / RES_M))
    g = flat.Grid(lo, np.array([1.0, 0.0]), np.array([0.0, 1.0]), RES_M, shape)
    col, ok = flat.rectify(v.image, v.pose, v.cam, g, table_y + thick)
    img = np.clip(col, 0, 255).astype(np.uint8).reshape(*shape, 3)
    xz = g.world(0.0)[:, [0, 2]]
    inside = (flat._inside(xz, _shrunk(outline, 0.0015)) & ok).reshape(shape)
    rc = features(img.mean(2), inside, RES_M)
    pts = g.world(0.0, rc.astype(int))[:, [0, 2]] if len(rc) else np.zeros((0, 2))
    return Face(v.name, g, img, outline, pts, table_y, sil)


def _shrunk(poly: np.ndarray, d: float) -> np.ndarray:
    c = poly.mean(0)
    r = np.linalg.norm(poly - c, axis=1, keepdims=True)
    return c + (poly - c) * np.maximum(0.0, 1 - d / r)


# --- matching --------------------------------------------------------------------------------------------------------

def _fit(p: np.ndarray, q: np.ndarray, mirror: bool) -> tuple[np.ndarray, np.ndarray]:
    """Least-squares q = A p + b with A a rotation (times a reflection when `mirror`)."""
    m = np.diag([1.0, -1.0]) if mirror else np.eye(2)
    pm = p @ m.T
    cp, cq = pm.mean(0), q.mean(0)
    u, _, vt = np.linalg.svd((pm - cp).T @ (q - cq))
    r = vt.T @ u.T
    if np.linalg.det(r) < 0:
        vt[-1] *= -1
        r = vt.T @ u.T
    a = r @ m
    return a, cq - a @ p.mean(0)


def _score(a: np.ndarray, b: np.ndarray, p: np.ndarray, q: np.ndarray) -> tuple[int, float, np.ndarray, np.ndarray]:
    """Mutual nearest features within MATCH_MM: (count, RMS mm, matched p, matched q)."""
    if not len(p) or not len(q):
        return 0, float("inf"), p[:0], q[:0]
    t = p @ a.T + b
    d = np.linalg.norm(t[:, None, :] - q[None, :, :], axis=2)
    j = d.argmin(1)
    i_back = d.argmin(0)
    ok = (d[np.arange(len(p)), j] <= MATCH_MM / 1000) & (i_back[j] == np.arange(len(p)))
    if not ok.any():
        return 0, float("inf"), p[:0], q[:0]
    e = d[np.arange(len(p)), j][ok]
    return int(ok.sum()), float(np.sqrt(np.mean(e ** 2)) * 1000), p[ok], q[j[ok]]


def _refine(a: np.ndarray, b: np.ndarray, p: np.ndarray, q: np.ndarray, mirror: bool, iters: int = 8):
    for _ in range(iters):
        n, _, mp, mq = _score(a, b, p, q)
        if n < 3:
            break
        a2, b2 = _fit(mp, mq, mirror)
        if np.allclose(a2, a) and np.allclose(b2, b):
            break
        a, b = a2, b2
    n, rms, _, _ = _score(a, b, p, q)
    return a, b, n, rms


def _rect_starts(o1: np.ndarray, o0: np.ndarray, mirror: bool) -> list[tuple[np.ndarray, np.ndarray]]:
    """Every map of rectangle o1 onto rectangle o0 (corner order matched), mirrored or not."""
    out = []
    for k in range(4):
        for rev in (False, True):
            src = np.roll(o1, k, axis=0)
            if rev:
                src = src[::-1]
            a, b = _fit(src, o0, mirror)
            if np.linalg.det(a) * (1 if not mirror else -1) > 0:
                out.append((a, b))
    return out


def _angle(a: np.ndarray) -> float:
    m = a @ np.diag([1.0, -1.0]) if np.linalg.det(a) < 0 else a
    return float(np.degrees(np.arctan2(m[1, 0], m[0, 0])))


def match_faces(f0: Face, f1: Face, *, mirror: bool = True, seed: int = 0) -> dict:
    """The best 2D map of face 1's features onto face 0's (mirrored for a flip) and its runner-up turned away."""
    rng = np.random.default_rng(seed)
    p, q = f1.features, f0.features
    cands = []
    for a, b in _rect_starts(f1.outline, f0.outline, mirror):
        cands.append(_refine(a, b, p, q, mirror))
    if len(p) >= 2 and len(q) >= 2:   # capped random pairs: any pose the features support
        for _ in range(RANSAC_ITERS):
            i = rng.choice(len(p), 2, replace=False)
            j = rng.choice(len(q), 2, replace=False)
            dp, dq = np.linalg.norm(p[i[0]] - p[i[1]]), np.linalg.norm(q[j[0]] - q[j[1]])
            if dp < 0.005 or abs(dp - dq) > MATCH_MM / 1000:
                continue
            a, b = _fit(p[i], q[j], mirror)
            n, _, _, _ = _score(a, b, p, q)
            if n >= MIN_INLIERS // 2:
                cands.append(_refine(a, b, p, q, mirror))
    if not cands:
        return {"best": None}
    cands.sort(key=lambda c: (-c[2], c[3]))
    best = cands[0]
    rival = next((c for c in cands[1:] if abs((_angle(c[0]) - _angle(best[0]) + 180) % 360 - 180) > 20), None)
    return {"best": best, "rival": rival}


SIL_RES_M = 0.0005
MIN_SIL_IOU = 0.75        # the flipped silhouette (connectors overhanging an edge included) covers the first one
SIL_MARGIN = 0.04         # and beats any pose turned away from it by this much IoU


def _raster(xz: np.ndarray, lo: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    ij = np.floor((xz - lo) / SIL_RES_M).astype(int)
    ok = (ij[:, 0] >= 0) & (ij[:, 0] < shape[0]) & (ij[:, 1] >= 0) & (ij[:, 1] < shape[1])
    g = np.zeros(shape, bool)
    g[ij[ok, 0], ij[ok, 1]] = True
    return g


def silhouette_iou(f0: Face, f1: Face, a: np.ndarray, b: np.ndarray) -> float:
    """Overlap of face 0's silhouette with face 1's mapped by (a, b), on a 0.5 mm grid (small gaps closed)."""
    from repairos.recreation import flat

    lo = f0.silhouette.min(0) - 0.02
    shape = tuple(np.ceil((f0.silhouette.max(0) + 0.02 - lo) / SIL_RES_M).astype(int))
    g0 = flat._erode(flat._dilate(_raster(f0.silhouette, lo, shape), 1), 1)
    g1 = flat._erode(flat._dilate(_raster(f1.silhouette @ a.T + b, lo, shape), 1), 1)
    u = (g0 | g1).sum()
    return float((g0 & g1).sum() / u) if u else 0.0


def _turn(deg: float, about: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    t = np.radians(deg)
    r = np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])
    return r, about - r @ about


def silhouette_poses(f0: Face, f1: Face, mirror: bool = True) -> list[tuple[np.ndarray, np.ndarray, float]]:
    """Each rectangle-on-rectangle start refined by a small search (+-1 deg, +-2 mm) on silhouette IoU."""
    out = []
    c0 = f0.outline.mean(0)
    for a, b in _rect_starts(f1.outline, f0.outline, mirror):
        best = (a, b, silhouette_iou(f0, f1, a, b))
        for deg in (-1, 0, 1):
            r, rt = _turn(deg, c0)
            for dx in np.arange(-0.002, 0.0021, 0.001):
                for dz in np.arange(-0.002, 0.0021, 0.001):
                    a2, b2 = r @ a, r @ b + rt + np.array([dx, dz])
                    iou = silhouette_iou(f0, f1, a2, b2)
                    if iou > best[2]:
                        best = (a2, b2, iou)
        out.append(best)
    return sorted(out, key=lambda c: -c[2])


def outline_iou(f0: Face, f1: Face, a: np.ndarray, b: np.ndarray) -> float:
    """Overlap of the two footprint polygons at the pose, on a 0.5 mm grid round face 0's."""
    from repairos.recreation import flat

    lo, hi = f0.outline.min(0) - 0.01, f0.outline.max(0) + 0.01
    xs = np.arange(lo[0], hi[0], SIL_RES_M)
    zs = np.arange(lo[1], hi[1], SIL_RES_M)
    xz = np.stack(np.meshgrid(xs, zs), -1).reshape(-1, 2)
    in0 = flat._inside(xz, f0.outline)
    in1 = flat._inside(xz, f1.outline @ a.T + b)
    union = (in0 | in1).sum()
    return float((in0 & in1).sum() / union) if union else 0.0


def lift(a: np.ndarray, b: np.ndarray, table0: float, table1: float, thick: float) -> np.ndarray:
    """The 3D flip from the 2D mirrored map: (x, z) by A, b; height y1 -> table0 + thick - (y1 - table1)."""
    t = np.eye(4)
    t[0, 0], t[0, 2], t[2, 0], t[2, 2] = a[0, 0], a[0, 1], a[1, 0], a[1, 1]
    t[1, 1] = -1.0 if np.linalg.det(a) < 0 else 1.0
    t[0, 3], t[2, 3] = b[0], b[1]
    t[1, 3] = table0 + thick + table1 if t[1, 1] < 0 else table0 - table1
    return t


def decide(f0: Face, f1: Face, m: dict, table0: float, table1: float, thick: float) -> FlatFlip:
    """Through-hole features when they pin the pose; else the whole silhouette (connectors overhanging one end make
    a board's outline asymmetric) among the rectangle's mirrored poses, with its own margin."""
    best, rival = m.get("best"), m.get("rival")
    sil = m.get("silhouette") or []
    if best is None and not sil:
        return FlatFlip(False, None, "no pose matched any through-hole features or the silhouette")
    if best is None:
        best = (sil[0][0], sil[0][1], 0, float("inf"))
    a, b, n, rms = best
    rn = rival[2] if rival else 0
    iou = outline_iou(f0, f1, a, b)
    stats = {"features_top": len(f0.features), "features_bottom": len(f1.features), "inliers": n,
             "rms_mm": round(rms, 3), "runner_up_inliers": rn, "runner_up_turned_deg":
             None if rival is None else round(abs((_angle(rival[0]) - _angle(a) + 180) % 360 - 180), 1),
             "outline_iou": round(iou, 3), "mirrored": bool(np.linalg.det(a) < 0), "top_still": f0.name,
             "bottom_still": f1.name}
    if sil:
        s_best = sil[0]
        s_rival = next((c for c in sil[1:] if abs((_angle(c[0]) - _angle(s_best[0]) + 180) % 360 - 180) > 20), None)
        stats.update(silhouette_iou=round(s_best[2], 3), silhouette_runner_up=None if s_rival is None
                     else round(s_rival[2], 3))
    why = None
    holes_ok = n >= MIN_INLIERS and rms <= MAX_RMS_MM and n - rn >= MARGIN and n >= MARGIN_RATIO * rn
    if not holes_ok and sil:
        s_best = sil[0]
        s_rival = next((c for c in sil[1:] if abs((_angle(c[0]) - _angle(s_best[0]) + 180) % 360 - 180) > 20), None)
        margin = s_best[2] - (s_rival[2] if s_rival else 0.0)
        if s_best[2] >= MIN_SIL_IOU and margin >= SIL_MARGIN:
            a, b = s_best[0], s_best[1]
            stats.update(decided_by="silhouette", inliers=_score(a, b, f1.features, f0.features)[0],
                         outline_iou=round(outline_iou(f0, f1, a, b), 3))
            return FlatFlip(True, lift(a, b, table0, table1, thick), None, stats, (f0, f1), a, b)
        why = (f"the flipped silhouette fits best at IoU {s_best[2]:.2f} and {s_best[2] - margin:.2f} turned away "
               f"(needs {MIN_SIL_IOU} and a {SIL_MARGIN} margin); {n} through-hole features match")
    elif holes_ok:
        stats["decided_by"] = "through-holes"
    if why:
        pass
    elif n < MIN_INLIERS:
        why = f"only {n} through-hole features match (needs {MIN_INLIERS})"
    elif rms > MAX_RMS_MM:
        why = f"matched features are {rms:.2f} mm apart (needs {MAX_RMS_MM})"
    elif iou < MIN_IOU:
        why = f"the outlines overlap {iou:.2f} at that pose (needs {MIN_IOU})"
    elif n - rn < MARGIN or n < MARGIN_RATIO * rn:
        why = (f"not unique: {n} features match, and {rn} at a pose turned "
               f"{stats['runner_up_turned_deg']} deg away (needs {MARGIN} more and {MARGIN_RATIO}x)")
    if why:
        return FlatFlip(False, None, why + "; lay it on an edge, or keep the rim and sides in view", stats, (f0, f1), a, b)
    return FlatFlip(True, lift(a, b, table0, table1, thick), None, stats, (f0, f1), a, b)


def register_flat_flip(stills0, box0, table0, stills1, box1, table1, lidar0=None, lidar1=None,
                       thick: float = BOARD_THICK_M) -> FlatFlip:
    t0 = time.perf_counter()
    try:
        f0 = face(stills0, box0, table0, lidar0, thick)
        f1 = face(stills1, box1, table1, lidar1, thick)
    except Exception as e:  # noqa: BLE001 - footprint/rectification failures are refusals with the reason
        return FlatFlip(False, None, f"could not rectify both faces ({e})")
    t1 = time.perf_counter()
    m = match_faces(f0, f1)
    m["silhouette"] = silhouette_poses(f0, f1)
    res = decide(f0, f1, m, table0, table1, thick)
    res.stats["seconds"] = {"faces_s": round(t1 - t0, 3), "match_s": round(time.perf_counter() - t1, 3)}
    return res


def bottom_texture(res: FlatFlip, outline0: np.ndarray) -> tuple[Any, np.ndarray]:
    """The board's underside image (the flipped face, rectified) and UVs (0..1, v down) for segment 0's outline
    corners seen from below."""
    from PIL import Image

    f1 = res.faces[1]
    ainv = np.linalg.inv(res.a2)
    q1 = (outline0 - res.b2) @ ainv.T        # segment 0's outline in the flipped face's own plane
    rc = f1.grid.to_pixel(q1)
    h, w = f1.image.shape[:2]
    return Image.fromarray(f1.image), np.stack([rc[:, 1] / w, rc[:, 0] / h], 1)


# --- semantic landmarks (one vision call) ----------------------------------------------------------------------------

LANDMARK_PROMPT = """Photo A is one face of a thin object (a circuit board or similar) seen straight from above. Photo B \
is the SAME object turned over, its other face seen from above and then mirrored left to right, so it reads like an \
X-ray through A (a feature on A's left edge is on B's left edge too, maybe turned). List \
landmarks you can see in BOTH photos, each the same physical point: the corners of connector bodies that overhang an \
edge (their metal or plastic shows from both sides), mounting holes, both ends of each header row (plastic on one \
side, a row of solder pads on the other), corner notches of the outline. For each give a short name and its centre \
as fractions of each photo (x right, y down, 0..1). Only landmarks you are sure of in both photos; 4-10 is ideal."""

LANDMARK_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["landmarks"], "properties": {
    "landmarks": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["name", "ax", "ay", "bx", "by"],
        "properties": {"name": {"type": "string"}, "ax": {"type": "number"}, "ay": {"type": "number"},
                       "bx": {"type": "number"}, "by": {"type": "number"}}}}}}
LANDMARK_MM = 4.0         # a landmark pair agrees with a pose within this
MIN_LANDMARKS = 3
AGREE_MM, AGREE_DEG = 5.0, 5.0   # the refined pose stays this close to the landmarks' pose
FACE_PX = 1024            # each rectified face sent at most this many pixels on its longer side


def _jpeg(img: np.ndarray) -> tuple[bytes, float]:
    import io

    from PIL import Image

    im = Image.fromarray(img)
    k = min(1.0, FACE_PX / max(im.size))
    if k < 1:
        im = im.resize((int(im.width * k), int(im.height * k)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return buf.getvalue(), k


def ask_landmarks(f0: Face, f1: Face, *, key: str, model: str, scan_id: str = "") -> list[dict]:
    """One vision call (through the spend guard): landmarks seen on both faces, as pairs of world x, z."""
    import base64
    import json

    import anthropic

    from repairos.voice.spend_guard import charge, key_of, record_claude

    ja, _ = _jpeg(f0.image)
    jb, _ = _jpeg(np.ascontiguousarray(f1.image[:, ::-1]))   # mirrored: the model compares like with like
    content = [{"type": "text", "text": "Photo A:"},
               {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.b64encode(ja).decode()}},
               {"type": "text", "text": "Photo B:"},
               {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.b64encode(jb).decode()}},
               {"type": "text", "text": LANDMARK_PROMPT}]
    est = charge("anthropic", key=key_of("flat_flip_landmarks", scan_id))
    client = anthropic.Anthropic(api_key=key, max_retries=1, timeout=90.0)
    r = client.messages.create(model=model, max_tokens=3000, messages=[{"role": "user", "content": content}],
                               output_config={"effort": "low", "format": {"type": "json_schema",
                                                                          "schema": LANDMARK_SCHEMA}})
    record_claude((getattr(r, "model", None), model), getattr(r, "usage", None), est)
    if r.stop_reason == "refusal":
        return []
    text = next((b.text for b in r.content if getattr(b, "type", None) == "text"), "{}")
    out = []
    for it in json.loads(text).get("landmarks") or []:
        try:
            ax, ay, bx, by = (float(it[k]) for k in ("ax", "ay", "bx", "by"))
        except (KeyError, TypeError, ValueError):
            continue
        if not all(0 <= v <= 1 for v in (ax, ay, bx, by)):
            continue
        pa = f0.grid.world(0.0, np.array([[ay * f0.image.shape[0], ax * f0.image.shape[1]]]))[0, [0, 2]]
        pb = f1.grid.world(0.0, np.array([[by * f1.image.shape[0], (1 - bx) * f1.image.shape[1]]]))[0, [0, 2]]
        out.append({"name": str(it.get("name", ""))[:60], "a": pa, "b": pb})
    return out


def landmark_pose(pairs: list[dict], seed: int = 0) -> dict:
    """Mirrored 2D rigid fit (face 1 -> face 0) over the landmark pairs, by random pairs of pairs; the best by inliers
    within LANDMARK_MM, then refitted on them."""
    if len(pairs) < 2:
        return {"inliers": 0}
    a = np.array([p["a"] for p in pairs])
    b = np.array([p["b"] for p in pairs])
    best = None
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            if np.linalg.norm(b[i] - b[j]) < 0.008:
                continue
            A, t = _fit(b[[i, j]], a[[i, j]], True)
            e = np.linalg.norm(b @ A.T + t - a, axis=1) * 1000
            ok = e <= LANDMARK_MM
            if best is None or ok.sum() > best[2].sum():
                best = (A, t, ok)
    if best is None or best[2].sum() < 2:
        return {"inliers": 0}
    A, t = _fit(b[best[2]], a[best[2]], True)
    e = np.linalg.norm(b @ A.T + t - a, axis=1) * 1000
    ok = e <= LANDMARK_MM
    return {"A": A, "b": t, "inliers": int(ok.sum()), "pairs": len(pairs),
            "rms_mm": float(np.sqrt(np.mean(e[ok] ** 2))) if ok.any() else float("inf"),
            "names": [p["name"] for p, k in zip(pairs, ok, strict=True) if k]}


def decide_with_landmarks(f0: Face, f1: Face, lm: dict, table0: float, table1: float, thick: float) -> FlatFlip:
    """The landmarks' pose, refined on the through-hole features and checked: accepted when at least MIN_LANDMARKS
    landmarks agree, the refined pose stays within AGREE_MM / AGREE_DEG of it, and the silhouettes agree there as
    well as anywhere (no pose turned away fits them clearly better)."""
    stats = {"features_top": len(f0.features), "features_bottom": len(f1.features),
             "landmarks": lm.get("pairs", 0), "landmark_inliers": lm.get("inliers", 0),
             "landmark_rms_mm": None if "rms_mm" not in lm else round(lm["rms_mm"], 2),
             "landmark_names": lm.get("names", [])}
    if lm.get("inliers", 0) < MIN_LANDMARKS:
        return FlatFlip(False, None, f"only {lm.get('inliers', 0)} landmarks seen on both faces agree "
                        f"(needs {MIN_LANDMARKS}); lay it on an edge, or keep the rim and sides in view", stats, (f0, f1))
    a0, b0 = lm["A"], lm["b"]
    a, b, n, rms = _refine(a0, b0, f1.features, f0.features, True)
    c = f1.outline.mean(0)
    moved = float(np.linalg.norm((a @ c + b) - (a0 @ c + b0)) * 1000)
    turned = abs((_angle(a) - _angle(a0) + 180) % 360 - 180)
    if moved > AGREE_MM or turned > AGREE_DEG or n < 3:      # the holes did not hold it: keep the landmarks' pose
        a, b, n, rms = a0, b0, *_score(a0, b0, f1.features, f0.features)[:2]
    iou = silhouette_iou(f0, f1, a, b)
    sil = silhouette_poses(f0, f1)
    s_best = sil[0] if sil else None
    rival = next((c_ for c_ in sil if abs((_angle(c_[0]) - _angle(a) + 180) % 360 - 180) > 20), None)
    stats.update(hole_inliers=n, hole_rms_mm=None if not np.isfinite(rms) else round(rms, 2),
                 refine_moved_mm=round(moved, 2), refine_turned_deg=round(turned, 2), silhouette_iou=round(iou, 3),
                 silhouette_best_any=None if s_best is None else round(s_best[2], 3),
                 silhouette_turned_away=None if rival is None else round(rival[2], 3),
                 top_still=f0.name, bottom_still=f1.name, decided_by="landmarks")
    if rival is not None and rival[2] > iou + SIL_MARGIN:
        return FlatFlip(False, None, f"the landmarks' pose fits the silhouettes at {iou:.2f}, a pose turned away at "
                        f"{rival[2]:.2f}: they disagree", stats, (f0, f1), a, b)
    return FlatFlip(True, lift(a, b, table0, table1, thick), None, stats, (f0, f1), a, b)


# --- discrete side cues (one vision call, answered per image) --------------------------------------------------------

EDGES = ("top", "bottom", "left", "right", "none")
SIDES_PROMPT = """Two photos of the SAME flat object (e.g. a circuit board), each seen straight from above: Photo A is \\
one face, Photo B the other face (it was turned over). Answer for EACH photo on its own, in that photo's own image \\
directions (top = the top of the image): on which image edge is the large metal connector shell (e.g. a USB socket) \\
that sticks out past the board edge, and in which half of that edge (first = left half for top/bottom edges, top half \\
for left/right edges); the same for the round barrel power jack; and which long edge carries the header (row of pin \\
sockets or solder pads) that has a gap in it. Use "none" when you cannot see it. Give a confidence 0-1 for each."""

_CUE = {"type": "object", "additionalProperties": False, "required": ["edge", "half", "confidence"], "properties": {
    "edge": {"type": "string", "enum": list(EDGES)}, "half": {"type": "string", "enum": ["first", "second", "none"]},
    "confidence": {"type": "number"}}}
_FACE_CUES = {"type": "object", "additionalProperties": False, "required": ["usb", "jack", "gap_header"],
              "properties": {"usb": _CUE, "jack": _CUE, "gap_header": _CUE}}
SIDES_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["photo_a", "photo_b"],
                "properties": {"photo_a": _FACE_CUES, "photo_b": _FACE_CUES}}
CUE_MIN_CONFIDENCE = 0.6
CUE_TOL_M = 0.025         # a cue agrees when the two faces put it within this (cues are edge quarters: coarse)
MIN_CUES = 2
SIL_SLACK = 0.02          # the chosen pose's silhouette IoU may trail the best other pose's by this much


def ask_sides(f0: Face, f1: Face, *, key: str, model: str, scan_id: str = "") -> dict:
    """The raw per-photo answers (photo_a = face 0, photo_b = face 1, unmirrored), through the spend guard."""
    import base64
    import json

    import anthropic

    from repairos.voice.spend_guard import charge, key_of, record_claude

    ja, _ = _jpeg(f0.image)
    jb, _ = _jpeg(f1.image)
    content = [{"type": "text", "text": "Photo A:"},
               {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.b64encode(ja).decode()}},
               {"type": "text", "text": "Photo B:"},
               {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.b64encode(jb).decode()}},
               {"type": "text", "text": SIDES_PROMPT}]
    est = charge("anthropic", key=key_of("flat_flip_sides", scan_id))
    client = anthropic.Anthropic(api_key=key, max_retries=1, timeout=90.0)
    r = client.messages.create(model=model, max_tokens=1500, messages=[{"role": "user", "content": content}],
                               output_config={"effort": "low", "format": {"type": "json_schema",
                                                                          "schema": SIDES_SCHEMA}})
    record_claude((getattr(r, "model", None), model), getattr(r, "usage", None), est)
    if r.stop_reason == "refusal":
        return {}
    return json.loads(next((b.text for b in r.content if getattr(b, "type", None) == "text"), "{}"))


def cue_point(cue: Mapping[str, Any], origin: np.ndarray, size_m: np.ndarray) -> np.ndarray | None:
    """A cue as a world x, z point: the middle of its edge's half (or of the edge) of a face image whose pixel
    (0, 0) is at `origin` and which spans `size_m` (x, z) metres (columns along +x, rows along +z)."""
    if cue.get("edge") not in ("top", "bottom", "left", "right") or float(cue.get("confidence") or 0) < CUE_MIN_CONFIDENCE:
        return None
    along = {"first": 0.25, "second": 0.75}.get(cue.get("half"), 0.5)
    fx, fz = {"top": (along, 0.0), "bottom": (along, 1.0), "left": (0.0, along), "right": (1.0, along)}[cue["edge"]]
    return origin + np.array([fx, fz]) * size_m


def choose_by_cues(answers: Mapping[str, Any], face0: Mapping[str, Any], face1: Mapping[str, Any],
                   poses: Sequence[tuple[np.ndarray, np.ndarray]], ious: Sequence[float]) -> dict:
    """Among the candidate poses (face 1 -> face 0), the one where the most cues agree; accepted when it is unique,
    at least MIN_CUES agree, and its silhouette IoU is no worse than the best other's minus SIL_SLACK. `face0` /
    `face1`: {origin, size_m} of the images the answers refer to."""
    a_ans, b_ans = answers.get("photo_a") or {}, answers.get("photo_b") or {}
    counts, agree = [], []
    for a, b in poses:
        ok = []
        for name in ("usb", "jack", "gap_header"):
            p0 = cue_point(a_ans.get(name) or {}, np.asarray(face0["origin"]), np.asarray(face0["size_m"]))
            p1 = cue_point(b_ans.get(name) or {}, np.asarray(face1["origin"]), np.asarray(face1["size_m"]))
            if p0 is not None and p1 is not None and np.linalg.norm(a @ p1 + b - p0) <= CUE_TOL_M:
                ok.append(name)
        counts.append(len(ok))
        agree.append(ok)
    k = int(np.argmax(counts)) if counts else -1
    out = {"counts": counts, "agree": agree, "chosen": k, "ious": [round(float(x), 3) for x in ious]}
    if k < 0 or counts[k] < MIN_CUES:
        return {**out, "accepted": False, "why": f"at most {max(counts, default=0)} side cues agree on any pose "
                f"(needs {MIN_CUES})"}
    if sorted(counts)[-2] == counts[k] if len(counts) > 1 else False:
        return {**out, "accepted": False, "why": "two poses agree with the side cues equally"}
    rival = max((x for i, x in enumerate(ious) if i != k), default=0.0)
    if ious[k] < rival - SIL_SLACK:
        return {**out, "accepted": False, "why": f"the cues' pose fits the silhouettes at {ious[k]:.2f}, another at "
                f"{rival:.2f}"}
    return {**out, "accepted": True, "why": None}


def rect_poses(o1: np.ndarray, o0: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    """The distinct mirrored rectangle-on-rectangle poses (corner orders that give the same map are one pose)."""
    out: list[tuple[np.ndarray, np.ndarray]] = []
    for a, b in _rect_starts(o1, o0, True):
        if not any(np.allclose(a, a2, atol=0.02) and np.linalg.norm(b - b2) < 0.001 for a2, b2 in out):
            out.append((a, b))
    return out


def face_frame(f: Face) -> dict:
    h, w = f.image.shape[:2]
    return {"origin": [float(f.grid.origin[0]), float(f.grid.origin[1])], "size_m": [w * f.grid.res, h * f.grid.res]}


# --- the automatic rule ---------------------------------------------------------------------------------------------

SURE = 0.8                # a side cue counts when the model gives it at least this on BOTH faces
IOU_NEAR = 0.02           # the chosen pose's silhouette IoU is within this of the best pose's


def cue_votes(answers: Mapping[str, Any], face0: Mapping[str, Any], face1: Mapping[str, Any],
              poses: Sequence[tuple[np.ndarray, np.ndarray]], sure: float = SURE) -> dict[str, list[int]]:
    """For each side cue the model is sure of on both faces (>= `sure`), the candidate poses it agrees with."""
    a_ans, b_ans = answers.get("photo_a") or {}, answers.get("photo_b") or {}
    out: dict[str, list[int]] = {}
    for name in ("usb", "jack", "gap_header"):
        ca, cb = a_ans.get(name) or {}, b_ans.get(name) or {}
        if min(float(ca.get("confidence") or 0), float(cb.get("confidence") or 0)) < sure:
            continue
        p0 = cue_point(ca, np.asarray(face0["origin"]), np.asarray(face0["size_m"]))
        p1 = cue_point(cb, np.asarray(face1["origin"]), np.asarray(face1["size_m"]))
        if p0 is None or p1 is None:
            continue
        out[name] = [i for i, (a, b) in enumerate(poses) if np.linalg.norm(a @ p1 + b - p0) <= CUE_TOL_M]
    return out


def decide_pose(answers: Mapping[str, Any], face0: Mapping[str, Any], face1: Mapping[str, Any],
                poses: Sequence[tuple[np.ndarray, np.ndarray]], ious: Sequence[float],
                holes: Sequence[int] | None = None) -> dict:
    """Pick one of the mirrored rectangle poses, or refuse. Accepted when the pose (1) fits the silhouettes within
    IOU_NEAR of the best pose, (2) agrees with at least one side cue the model is sure of on both faces, (3) no sure
    cue agrees with another pose, and (4) the through-hole features do not clearly favour another pose (MARGIN more
    matches and MARGIN_RATIO x). `holes`: matched features per pose, when measured."""
    votes = cue_votes(answers, face0, face1, poses)
    best = max(ious) if len(ious) else 0.0
    backed = sorted({i for v in votes.values() for i in v})
    rec = {"ious": [round(float(x), 3) for x in ious], "sure_cues": votes, "holes": None if holes is None else list(holes),
           "chosen": None, "accepted": False}
    if not votes:
        return {**rec, "why": f"no side cue the model is sure of (>= {SURE}) on both faces"}
    if len(backed) != 1:
        return {**rec, "why": "the sure side cues agree with no pose" if not backed else
                f"the sure side cues agree with different poses {backed}"}
    k = backed[0]
    rec["chosen"] = k
    if ious[k] < best - IOU_NEAR:
        return {**rec, "why": f"the cues' pose fits the silhouettes at {ious[k]:.3f}, the best at {best:.3f}"}
    if holes is not None:
        other = max((h for i, h in enumerate(holes) if i != k), default=0)
        if other - holes[k] >= MARGIN and other >= MARGIN_RATIO * max(1, holes[k]):
            return {**rec, "why": f"the through-holes favour another pose ({other} vs {holes[k]} matched)"}
    return {**rec, "accepted": True, "why": None}


# --- the pipeline's side --------------------------------------------------------------------------------------------

BOARD_SIDES_MM = 6.0      # the two faces' measured outlines agree within this (the same board, both sides)


def register_thin_flip(stills0: Sequence[Mapping[str, Any]], box0: Any, table0: float, lidar0: np.ndarray | None,
                       stills1: Sequence[Mapping[str, Any]], box1: Any, table1: float, lidar1: np.ndarray | None, *,
                       key: str | None, model: str, scan_id: str = "", thick: float = BOARD_THICK_M) -> dict:
    """A thin object's flipped segment registered by its two faces: the mirrored rectangle poses, their silhouette
    IoU and through-hole matches, one side-cue call, decide_pose. Returns {aligned, transform (4x4 or None), why,
    record (numbers for the segment record)}."""
    t0 = time.perf_counter()
    out: dict = {"aligned": False, "transform": None, "why": None, "record": {}}
    try:
        f0 = face(stills0, box0, table0, lidar0, thick)
        f1 = face(stills1, box1, table1, lidar1, thick)
    except Exception as e:  # noqa: BLE001 - no faces: a refusal with the reason
        out["why"] = f"could not rectify both faces ({e})"
        return out
    s0 = sorted(np.linalg.norm(np.diff(np.r_[f0.outline, f0.outline[:1]], axis=0), axis=1)[:2] * 1000)
    s1 = sorted(np.linalg.norm(np.diff(np.r_[f1.outline, f1.outline[:1]], axis=0), axis=1)[:2] * 1000)
    poses = rect_poses(f1.outline, f0.outline)
    ious = [silhouette_iou(f0, f1, a, b) for a, b in poses]
    holes = [_score(a, b, f1.features, f0.features)[0] for a, b in poses]
    rec = {"top_still": f0.name, "bottom_still": f1.name, "sides_mm": [[round(float(x), 1) for x in s0],
                                                                       [round(float(x), 1) for x in s1]]}
    if len(poses) != 4 or max(abs(a - b) for a, b in zip(s0, s1, strict=True)) > BOARD_SIDES_MM:
        out["why"] = f"the two faces' outlines differ ({s0[0]:.0f} x {s0[1]:.0f} vs {s1[0]:.0f} x {s1[1]:.0f} mm)"
        out["record"] = {**rec, "seconds": round(time.perf_counter() - t0, 3)}
        return out
    answers: dict = {}
    if key:
        try:
            answers = ask_sides(f0, f1, key=key, model=model, scan_id=scan_id)
        except Exception as e:  # noqa: BLE001 - no answer: no sure cue, refused below
            rec["cue_error"] = f"{type(e).__name__}: {e}"[:200]
    d = decide_pose(answers, face_frame(f0), face_frame(f1), poses, ious, holes)
    rec.update(decision={k: v for k, v in d.items()}, answers=answers, seconds=round(time.perf_counter() - t0, 3))
    out["record"] = rec
    if not d["accepted"]:
        out["why"] = d["why"]
        return out
    a, b = poses[d["chosen"]]
    out.update(aligned=True, transform=lift(a, b, table0, table1, thick))
    return out


def add_underside(model: Any, still: Any, to_k: np.ndarray, table_y: float, res: float = 0.0001) -> Any:
    """The slab's bottom face (flat.FlatModel: its outline's k vertices at the table, the third block of k after
    the top and its edge) textured from the flipped segment's still: each texel's point on the underside taken into
    that segment's world by `to_k` (segment 0 -> segment k) and read where that still sees it."""
    from PIL import Image

    from repairos.locate.camera import project
    from repairos.recreation import flat

    o = np.asarray(model.outline, float)
    k = len(o)
    lo, hi = o.min(0), o.max(0)
    shape = (int(np.ceil((hi[1] - lo[1]) / res)), int(np.ceil((hi[0] - lo[0]) / res)))
    g = flat.Grid(lo, np.array([1.0, 0.0]), np.array([0.0, 1.0]), res, shape)
    p0 = g.world(table_y)
    pk = p0 @ to_k[:3, :3].T + to_k[:3, 3]
    uv, z = project(pk, still.pose, still.cam)
    h, w = still.image.shape[:2]
    ok = (z > 0.05) & (uv[:, 0] >= 0) & (uv[:, 0] < w - 1) & (uv[:, 1] >= 0) & (uv[:, 1] < h - 1)
    if ok.mean() < 0.5:
        model.stats["underside"] = f"not textured: the bottom photo sees {ok.mean():.0%} of it"
        return model
    img = np.full((len(pk), 3), 60, np.uint8)
    img[ok] = still.image[uv[ok, 1].astype(int), uv[ok, 0].astype(int)]
    img = img.reshape(*shape, 3)
    atlas = np.asarray(model.atlas)
    H0, W0 = atlas.shape[:2]
    if img.shape[1] > W0:   # keep the atlas's width: the underside scaled to it
        f = W0 / img.shape[1]
        img = np.asarray(Image.fromarray(img).resize((W0, max(1, int(img.shape[0] * f)))))
    pad = np.full((img.shape[0], W0, 3), 60, np.uint8)
    pad[:, :img.shape[1]] = img
    H = H0 + img.shape[0]
    model.uv = model.uv.copy()
    model.uv[:, 1] *= H0 / H
    rc = g.to_pixel(o)                                    # (row, col) in the underside image at full res
    sx = img.shape[1] / shape[1]
    # its own k corners after every other vertex (the sides keep the grey bottom edge at 2k..3k), the bottom fan
    # moved onto them
    u0 = len(model.vertices)
    model.vertices = np.vstack([model.vertices, model.vertices[2 * k:3 * k]]).astype(np.float32)
    model.uv = np.vstack([model.uv, np.stack([rc[:, 1] * sx / W0, (H0 + rc[:, 0] * sx) / H], 1)]).astype(np.float32)
    faces = np.asarray(model.faces).copy()
    fan = ((faces >= 2 * k) & (faces < 3 * k)).all(1)
    faces[fan] += u0 - 2 * k
    model.faces = faces.astype(np.int32)
    model.stats["underside_first_vertex"] = int(u0)
    model.atlas = Image.fromarray(np.vstack([atlas, pad]))
    model.stats["underside"] = f"textured from {still.name}"
    return model


def underside_still(stills: Sequence[Mapping[str, Any]], box: Any) -> Any | None:
    """The flipped segment's most top-down still (its own world)."""
    v = _stills(stills, box)
    return v[0] if v else None


def with_underside(model: Any, pkg_local: Mapping[str, Any], stills_local: Sequence[Mapping[str, Any]],
                   segment_transforms: Mapping[int, Any] | None) -> Any:
    """The flat model with its bottom face textured from each registered flipped segment's own photo (add_underside),
    so the recreation, the report's mesh and its cloud all show the same two-faced object."""
    from repairos.recon.tsdf import ObjectBox

    for seg in pkg_local.get("segments") or []:
        k = int(seg["segment"])
        if not segment_transforms or k not in segment_transforms or not seg.get("crop_box"):
            continue
        st = underside_still([s for s in stills_local if int(s.get("segment") or 0) == k],
                             ObjectBox.from_contract(seg["crop_box"]))
        if st is not None:
            model = add_underside(model, st, np.linalg.inv(np.asarray(segment_transforms[k], float)),
                                  float(model.vertices[2 * len(model.outline), 1]))
            if str(model.stats.get("underside", "")).startswith("textured"):
                model.stats["bottom_segments"] = [k]   # the registered flip that closes it underneath
            break   # one underside
    return model

