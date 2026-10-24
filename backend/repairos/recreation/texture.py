"""The object's LiDAR mesh textured from the scan's photos (view selection + one atlas + seam levelling).

    views = scan_views(pkg_local, stills_local)               # 12 MP stills (+ keyframes), posed, arkit_world
    mesh = object_mesh(pkg_local)                              # TSDF of the in-box LiDAR depth (pipeline.mesh)
    tex = texture_mesh(mesh.vertices, mesh.faces, views, atlas_px=4096)
    tex.ply_bytes(), tex.atlas_jpeg                            # or usdz.usdz_bytes(tex)

The method follows "Let There Be Color! Large-Scale Texturing of 3D Reconstructions" (Waechter, Moehrle, Goesele,
ECCV 2014; reference code nmoehrle/mvs-texturing, BSD-3, not used): one source view per face, chosen for detail and
smoothed so neighbouring faces share a view; each connected run of faces with one view is a chart whose texture is
that view's own pixels; colour differences across chart seams are levelled. Simplifications, all measured on real
scans: the data term is the face's projected area in the view's pixels times the view's sharpness (a proxy for
Waechter's gradient-magnitude integral); the smoothing is iterated conditional modes instead of graph cuts; seam
levelling is one multiplicative gain per chart and channel (least squares on the colours both sides see at seam
vertices) instead of per-vertex additive levelling. A chart's atlas tile is the chart's bounding rectangle in its
view, scaled, so the pixels just outside a chart are the real neighbouring pixels (natural padding for filtering).

Visibility: a face is a candidate for a view only when the ray from the view's camera to the face centre hits that
face first (Open3D RaycastingScene on the mesh itself) and the view's LiDAR depth there is not nearer by more than
`OCCLUDER_M` (something not in the mesh, a hand or a cable, stood in front).
"""
from __future__ import annotations

import io
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from repairos.locate.camera import Pinhole, Pose, project

EDGE_MARGIN_PX = 0.01       # a face counts as inside a view when all three corners are this share inside the edges
MIN_FACING = 0.15           # cos of the angle between the face normal and the view ray, below which a view is skipped
OCCLUDER_M = 0.03           # the view's LiDAR depth may be nearer than the face by at most this (noise)
SMOOTH_WEIGHT = 1.0         # Potts cost of a neighbour with another view, against -log(relative score) of the view
SMOOTH_ITERS = 6
KEYFRAME_PENALTY = 0.35     # a 640 px keyframe's score is scaled down (motion blur, JPEG at 640 px)
MAX_KEYFRAMES = 80          # keyframes kept as fallback views, evenly spread over the capture
TILE_PAD_PX = 2             # atlas pixels around each chart's tile
SEAM_REG = 0.05             # pulls every chart gain toward 1 (log-gain toward 0) against the seam equations
GAIN_LIMITS = (0.75, 1.33)
LOCAL_LIMIT = 60.0          # largest local seam correction, 0..255 units
UNSEEN_RGB = (118, 118, 122)


@dataclass
class View:
    name: str
    kind: str                       # still | keyframe
    image_path: Path
    pose: Pose                      # camera-to-world, ARKit camera axes
    cam: Pinhole                    # intrinsics at the image's own size (width/height set)
    depth_m: np.ndarray | None = None
    depth_cam: Pinhole | None = None
    sharpness: float = 1.0          # relative (1 = typical for its kind)
    weight: float = 1.0


@dataclass
class TexturedMesh:
    vertices: np.ndarray            # N x 3 float32, arkit_world metres (corners duplicated where charts meet)
    uv: np.ndarray                  # N x 2 float32, 0..1, origin at the atlas image's top-left (v down)
    faces: np.ndarray               # M x 3 int32
    atlas: Any                      # PIL.Image RGB
    stats: dict = field(default_factory=dict)

    def atlas_jpeg(self, quality: int = 88) -> bytes:
        buf = io.BytesIO()
        self.atlas.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue()

    def ply_bytes(self, texture_file: str = "texture.jpg") -> bytes:
        """Binary LE PLY: float x y z s t per vertex (t from the top, as the atlas is stored), int triangles."""
        v = np.asarray(self.vertices, "<f4")
        vb = np.empty(len(v), dtype=np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("s", "<f4"), ("t", "<f4")]))
        vb["x"], vb["y"], vb["z"] = v[:, 0], v[:, 1], v[:, 2]
        vb["s"], vb["t"] = self.uv[:, 0], self.uv[:, 1]
        fb = np.empty(len(self.faces), dtype=np.dtype([("n", "u1"), ("i", "<i4", (3,))]))
        fb["n"], fb["i"] = 3, self.faces
        head = ["ply", "format binary_little_endian 1.0", "comment repairos recreation, arkit_world metres",
                f"comment TextureFile {texture_file}", f"element vertex {len(v)}",
                *(f"property float {p}" for p in ("x", "y", "z", "s", "t")), f"element face {len(self.faces)}",
                "property list uchar int vertex_indices", "end_header"]
        return ("\n".join(head) + "\n").encode("ascii") + vb.tobytes() + fb.tobytes()


# ------------------------------------------------------------------------------------------------ inputs
def _same_frame_segments(pkg: Mapping[str, Any]) -> set[int]:
    return {0} | {int(s["segment"]) for s in pkg.get("segments") or []
                  if s.get("reason") == "continued" and s.get("aligned") is not False}


def _image_size(path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as im:
        return im.size


def _sharpness(path: Path, cam: Pinhole, pose: Pose, box: Any | None) -> float:
    """Variance of the Laplacian of the grey image at about 640 px wide, inside the object box's projection."""
    from PIL import Image

    with Image.open(path) as im:
        w, h = im.size
        im.draft("L", (max(1, w // 6), max(1, h // 6)))
        g = np.asarray(im.convert("L").resize((640, max(1, round(640 * h / w)))), np.float32)
    if box is not None:
        uv, z = project(box.corners(), pose, cam.scaled(g.shape[1], g.shape[0]))
        if (z > 0.05).all():
            x0, y0 = np.clip(uv.min(0).astype(int), 0, [g.shape[1] - 1, g.shape[0] - 1])
            x1, y1 = np.clip(uv.max(0).astype(int) + 1, 1, [g.shape[1], g.shape[0]])
            if (x1 - x0) > 16 and (y1 - y0) > 16:
                g = g[y0:y1, x0:x1]
    lap = g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    return float(lap.var())


def _depth(path: Path | None) -> np.ndarray | None:
    if path is None or not Path(path).exists():
        return None
    from PIL import Image

    return np.asarray(Image.open(path), dtype=np.float32) / 1000.0


def scan_views(pkg: Mapping[str, Any], stills: Sequence[Mapping[str, Any]], *, box: Any | None = None,
               max_keyframes: int = MAX_KEYFRAMES, use_keyframes: bool = True) -> list[View]:
    """Posed views of a scan whose file fields are local paths: its stills (same frame, reliable pose) and its
    keyframes (evenly thinned to `max_keyframes`), with a relative sharpness per kind."""
    segs = _same_frame_segments(pkg)
    views: list[View] = []
    for s in stills:
        if int(s.get("segment") or 0) not in segs or s.get("pose_reliable") is False or not s.get("camera"):
            continue
        p = Path(s["image_file"])
        if not p.exists():
            continue
        w, h = (s.get("width_px"), s.get("height_px"))
        if not (w and h):
            w, h = _image_size(p)
        cam = Pinhole.from_contract(s["intrinsics"], w, h)
        d = _depth(Path(s["depth_file"])) if s.get("depth_file") else None
        views.append(View(s["still_id"], "still", p, Pose.from_contract(s["camera"]), cam, d,
                          cam.scaled(d.shape[1], d.shape[0]) if d is not None else None))
    if use_keyframes:
        kfs = [k for k in pkg.get("keyframes") or [] if int(k.get("segment") or 0) in segs and k.get("image_file")]
        step = max(1, int(np.ceil(len(kfs) / max(1, max_keyframes))))
        for k in kfs[::step]:
            p = Path(k["image_file"])
            if not p.exists():
                continue
            cam = Pinhole.from_contract(k["intrinsics"])
            if not (cam.width and cam.height):
                w, h = _image_size(p)
                cam = Pinhole(cam.fx, cam.fy, cam.cx, cam.cy, w, h)
            d = _depth(Path(k["depth_file"])) if k.get("depth_file") else None
            views.append(View(k["keyframe_id"], "keyframe", p, Pose.from_contract(k["camera"]), cam, d,
                              cam.scaled(d.shape[1], d.shape[0]) if d is not None else None,
                              weight=KEYFRAME_PENALTY))
    for kind in ("still", "keyframe"):
        group = [v for v in views if v.kind == kind]
        if not group:
            continue
        sh = np.array([_sharpness(v.image_path, v.cam, v.pose, box) for v in group])
        med = float(np.median(sh)) or 1.0
        for v, s in zip(group, sh, strict=True):
            v.sharpness = float(np.clip(s / med, 0.25, 2.0))
    return views


# ------------------------------------------------------------------------------------------------ mesh
@dataclass
class ObjectMesh:
    vertices: np.ndarray
    faces: np.ndarray
    voxel_m: float
    seconds: dict


def object_mesh(pkg_local: Mapping[str, Any], *, max_triangles: int = 120_000, voxel_m: float | None = None,
                smooth_iters: int = 8) -> ObjectMesh:
    """The object's surface: high-confidence in-box LiDAR depth above the support plane, TSDF (pipeline.mesh's
    steps), the pieces that reach the box's middle, Taubin-smoothed (no shrinking) and decimated."""
    import open3d as o3d

    from repairos.importers.fuse import make_fuser
    from repairos.pipeline.mesh import (MeshUnavailable, central_pieces, keyframes_in_frame, object_depths,
                                        plane_height, voxel_for)
    from repairos.recon.tsdf import ObjectBox, frames_from_scan_package

    t0 = time.perf_counter()
    pkg = {**pkg_local, "keyframes": keyframes_in_frame(pkg_local)}
    frames = frames_from_scan_package(pkg, Path("/"))
    if not frames:
        raise MeshUnavailable("the scan has no keyframes with depth in its frame")
    if not pkg.get("crop_box"):
        raise MeshUnavailable("the scan has no object box")
    box = ObjectBox.from_contract(pkg["crop_box"])
    depths, _info = object_depths(frames, box, plane_height(pkg))
    spacing = voxel_m or voxel_for(box)
    fuser = make_fuser(spacing, prefer="open3d", min_confidence=2, min_depth_m=0.05, max_depth_m=1.5)
    for f, d in zip(frames, depths, strict=True):
        if (d > 0).any():
            fuser.add_depth(d, f.confidence, None, f.pose, f.depth_cam)
    mesh = fuser.result()["mesh"]
    if not mesh or not len(mesh["faces"]):
        raise MeshUnavailable("no object surface above the support plane inside the box")
    t1 = time.perf_counter()
    v, f, _dropped = central_pieces(np.asarray(mesh["vertices"], float), np.asarray(mesh["faces"]), box)
    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(v), o3d.utility.Vector3iVector(f.astype(np.int32)))
    if smooth_iters:
        m = m.filter_smooth_taubin(number_of_iterations=smooth_iters)
    if len(m.triangles) > max_triangles:
        m = m.simplify_quadric_decimation(target_number_of_triangles=int(max_triangles))
    m.remove_unreferenced_vertices()
    m.remove_degenerate_triangles()
    return ObjectMesh(np.asarray(m.vertices), np.asarray(m.triangles), spacing,
                      {"fuse_s": round(t1 - t0, 3), "clean_s": round(time.perf_counter() - t1, 3)})


# ------------------------------------------------------------------------------------------------ view selection
def _face_adjacency(faces: np.ndarray) -> np.ndarray:
    """M x 3: the face across each edge (-1 at a border or a non-manifold edge)."""
    m = len(faces)
    e = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    owner = np.concatenate([np.arange(m)] * 3)
    slot = np.repeat(np.arange(3), m)
    key = np.sort(e, axis=1)
    k = key[:, 0].astype(np.int64) * (int(faces.max()) + 1) + key[:, 1]
    order = np.argsort(k, kind="stable")
    ks = k[order]
    adj = np.full((m, 3), -1, np.int64)
    same = np.nonzero(ks[1:] == ks[:-1])[0]
    # exactly two faces on the edge (a third one makes it non-manifold: left unconnected)
    ok = same[((same == 0) | (ks[same - 1] != ks[same])) & ((same + 2 >= len(ks)) | (ks[np.minimum(same + 2, len(ks) - 1)] != ks[same]))]
    a, b = order[ok], order[ok + 1]
    adj[owner[a], slot[a]] = owner[b]
    adj[owner[b], slot[b]] = owner[a]
    return adj


def view_scores(v: np.ndarray, f: np.ndarray, views: Sequence[View]) -> np.ndarray:
    """M x V float32: per face and view, the face's area in the view's pixels x sharpness x the kind's weight, 0 where
    the view does not see the face (outside the image, facing away, occluded by the mesh or by its LiDAR depth)."""
    import open3d as o3d

    tri = v[f]
    centre = tri.mean(axis=1)
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(v.astype(np.float32)), o3d.core.Tensor(f.astype(np.uint32)))
    out = np.zeros((len(f), len(views)), np.float32)
    for j, view in enumerate(views):
        cam = view.cam
        w, h = cam.width, cam.height
        uv, z = project(v, view.pose, cam)
        fu, fz = uv[f], z[f]
        mx, my = EDGE_MARGIN_PX * w, EDGE_MARGIN_PX * h
        inside = ((fz > 0.05).all(1) & (fu[..., 0] > mx).all(1) & (fu[..., 0] < w - mx).all(1)
                  & (fu[..., 1] > my).all(1) & (fu[..., 1] < h - my).all(1))
        ray = centre - view.pose.t
        dist = np.linalg.norm(ray, axis=1)
        facing = -(n * ray).sum(1) / np.maximum(dist, 1e-9)
        cand = np.nonzero(inside & (facing > MIN_FACING))[0]
        if not len(cand):
            continue
        d = ray[cand] / dist[cand, None]
        rays = np.concatenate([np.repeat(view.pose.t[None], len(cand), 0), d], axis=1).astype(np.float32)
        hit = scene.cast_rays(o3d.core.Tensor(rays))
        tid = hit["primitive_ids"].numpy()
        thit = hit["t_hit"].numpy()
        vis = (tid == cand) | (thit >= dist[cand] - (0.002 + 0.01 * dist[cand]))
        cand = cand[vis]
        if view.depth_m is not None and len(cand):
            duv, dz = project(centre[cand], view.pose, view.depth_cam)
            dh, dw = view.depth_m.shape
            du = np.clip(duv[:, 0].astype(int), 0, dw - 1)
            dv = np.clip(duv[:, 1].astype(int), 0, dh - 1)
            dd = view.depth_m[dv, du]
            cand = cand[~((dd > 0) & (dd < dz - OCCLUDER_M))]
        p = fu[cand]
        area = 0.5 * np.abs((p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1])
                            - (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 1, 1] - p[:, 0, 1]))
        out[cand, j] = area * view.sharpness * view.weight
    return out


def select_views(scores: np.ndarray, adj: np.ndarray, *, smooth: float = SMOOTH_WEIGHT,
                 iters: int = SMOOTH_ITERS) -> np.ndarray:
    """Per face, the chosen view (-1 when none sees it): the best score, then iterated conditional modes on
    unary -log(score / best score of the face) + `smooth` x (neighbours with another view)."""
    m, nv = scores.shape
    best = scores.max(axis=1)
    label = np.where(best > 0, scores.argmax(axis=1), -1)
    seen = best > 0
    with np.errstate(divide="ignore"):
        unary = np.where(scores > 0, -np.log(scores / np.maximum(best[:, None], 1e-12)), np.inf)
    rows = np.arange(m)
    for _ in range(iters):
        nb = np.where(adj >= 0, label[np.maximum(adj, 0)], -2)                # M x 3 neighbour labels
        cands = np.concatenate([label[:, None], nb], axis=1)                 # M x 4
        cost = np.full(cands.shape, np.inf)
        for k in range(4):
            c = cands[:, k]
            ok = (c >= 0) & seen
            u = np.full(m, np.inf)
            u[ok] = unary[rows[ok], c[ok]]
            cost[:, k] = u + smooth * (nb != c[:, None]).sum(axis=1)
        pick = cands[rows, cost.argmin(axis=1)]
        new = np.where(seen & (pick >= 0), pick, label)
        if (new == label).all():
            break
        label = new
    return label


def charts(label: np.ndarray, adj: np.ndarray) -> np.ndarray:
    """Per face, its chart: connected runs of faces (across shared edges) with the same view."""
    m = len(label)
    parent = np.arange(m)

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for k in range(3):
        a = np.arange(m)
        b = adj[:, k]
        ok = (b >= 0) & (label == label[np.maximum(b, 0)]) & (a < b)
        for i, j in zip(a[ok], b[ok], strict=True):
            ri, rj = root(int(i)), root(int(j))
            if ri != rj:
                parent[rj] = ri
    roots = np.array([root(i) for i in range(m)])
    _, chart = np.unique(roots, return_inverse=True)
    return chart


# ------------------------------------------------------------------------------------------------ atlas
def _shelf_pack(sizes: np.ndarray, width: int) -> tuple[np.ndarray, int]:
    """Top-left positions for (w, h) rectangles packed in shelves of `width`; returns (positions, used height)."""
    order = np.argsort(-sizes[:, 1], kind="stable")
    pos = np.zeros_like(sizes)
    x = y = shelf = 0
    for i in order:
        w, h = sizes[i]
        if x + w > width:
            y += shelf
            x = shelf = 0
        pos[i] = (x, y)
        x += w
        shelf = max(shelf, h)
    return pos, y + shelf


def _sample_mean(img: np.ndarray, uv: np.ndarray, r: int = 2) -> np.ndarray:
    h, w = img.shape[:2]
    out = np.zeros((len(uv), 3))
    cnt = 0
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            x = np.clip(np.round(uv[:, 0]).astype(int) + dx, 0, w - 1)
            y = np.clip(np.round(uv[:, 1]).astype(int) + dy, 0, h - 1)
            out += img[y, x]
            cnt += 1
    return out / cnt


def seam_gains(v: np.ndarray, f: np.ndarray, chart: np.ndarray, label: np.ndarray, views: Sequence[View],
               thumbs: dict[int, np.ndarray], *, by_view: bool = True) -> np.ndarray:
    """C x 3 multiplicative gains: at every vertex shared by faces of two charts, both charts' views are sampled
    there; least squares on log colour differences, regularised toward 1, clipped to GAIN_LIMITS. `by_view`: one gain
    per source view (all its charts alike) instead of one per chart."""
    nchart = int(chart.max()) + 1
    corner_v = f.reshape(-1)
    corner_c = np.repeat(chart, 3)
    pairs = np.unique(np.stack([corner_v, corner_c], 1), axis=0)            # (vertex, chart) incidences
    starts = np.r_[0, np.nonzero(np.diff(pairs[:, 0]))[0] + 1]
    counts = np.diff(np.r_[starts, len(pairs)])
    eq_a, eq_b, eq_v = [], [], []
    for s, c in zip(starts[counts >= 2], counts[counts >= 2], strict=True):
        cs = pairs[s:s + c, 1]
        for i in range(1, c):
            eq_a.append(cs[0])
            eq_b.append(cs[i])
            eq_v.append(pairs[s, 0])
    if not eq_a:
        return np.ones((nchart, 3))
    eq_a, eq_b, eq_v = np.array(eq_a), np.array(eq_b), np.array(eq_v)
    chart_view = np.zeros(nchart, int)
    chart_view[chart] = label
    col_a = np.zeros((len(eq_a), 3))
    col_b = np.zeros((len(eq_a), 3))
    for which, cols in ((eq_a, col_a), (eq_b, col_b)):
        vw = chart_view[which]
        for j in np.unique(vw):
            if j < 0 or j not in thumbs:
                continue
            sel = vw == j
            img = thumbs[j]
            cam = views[j].cam.scaled(img.shape[1], img.shape[0])
            uv, _ = project(v[eq_v[sel]], views[j].pose, cam)
            cols[sel] = _sample_mean(img, uv)
    ok = ((col_a > 12) & (col_a < 245) & (col_b > 12) & (col_b < 245)).all(1)
    ok &= (chart_view[eq_a] >= 0) & (chart_view[eq_b] >= 0)
    if ok.sum() < 3:
        return np.ones((nchart, 3))
    eq_a, eq_b = eq_a[ok], eq_b[ok]
    d = np.log(col_b[ok]) - np.log(col_a[ok])      # want g_a - g_b = d
    if by_view:
        # one gain per view (exposure, white balance and torch differ per photo, not per chart)
        nv = len(views)
        g = _solve_gains(chart_view[eq_a], chart_view[eq_b], d, nv)
        out = np.ones((nchart, 3))
        out[chart_view >= 0] = g[chart_view[chart_view >= 0]]
        return out
    return _solve_gains(eq_a, eq_b, d, nchart)


def _solve_gains(eq_a: np.ndarray, eq_b: np.ndarray, d: np.ndarray, nchart: int) -> np.ndarray:
    keep = eq_a != eq_b
    eq_a, eq_b, d = eq_a[keep], eq_b[keep], d[keep]
    if not len(eq_a):
        return np.ones((nchart, 3))
    # normal equations of sum (g_a - g_b - d)^2 + reg * sum g^2
    ata = np.zeros((nchart, nchart))
    np.add.at(ata, (eq_a, eq_a), 1)
    np.add.at(ata, (eq_b, eq_b), 1)
    np.add.at(ata, (eq_a, eq_b), -1)
    np.add.at(ata, (eq_b, eq_a), -1)
    ata += SEAM_REG * np.eye(nchart) * max(1.0, len(eq_a) / nchart)
    g = np.zeros((nchart, 3))
    for ch in range(3):
        atb = np.zeros(nchart)
        np.add.at(atb, eq_a, d[:, ch])
        np.add.at(atb, eq_b, -d[:, ch])
        g[:, ch] = np.linalg.solve(ata, atb)
    return np.clip(np.exp(g), *GAIN_LIMITS)


def seam_corrections(v: np.ndarray, f: np.ndarray, chart: np.ndarray, label: np.ndarray, views: Sequence[View],
                     thumbs: dict[int, np.ndarray], gains: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Local seam levelling (Waechter et al. 2014, section 5.2): at every vertex where charts meet, each chart's
    (gain-corrected) colour there is moved to the mean of them all. Returns (vertex, chart, additive RGB correction)
    per incidence; the atlas spreads them over each chart's tile (`spread`)."""
    chart_view = np.full(int(chart.max()) + 1, -1)
    chart_view[chart] = label
    pairs = np.unique(np.stack([f.reshape(-1), np.repeat(chart, 3)], 1), axis=0)
    pairs = pairs[chart_view[pairs[:, 1]] >= 0]
    starts = np.r_[0, np.nonzero(np.diff(pairs[:, 0]))[0] + 1]
    counts = np.diff(np.r_[starts, len(pairs)])
    seam = np.repeat(counts >= 2, counts)
    pv, pc = pairs[seam, 0], pairs[seam, 1]
    if not len(pv):
        return pv, pc, np.zeros((0, 3))
    col = np.zeros((len(pv), 3))
    vw = chart_view[pc]
    for j in np.unique(vw):
        if j not in thumbs:
            continue
        sel = vw == j
        img = thumbs[j]
        uv, _ = project(v[pv[sel]], views[j].pose, views[j].cam.scaled(img.shape[1], img.shape[0]))
        col[sel] = _sample_mean(img, uv, 1) * gains[pc[sel]]
    # mean per vertex
    uniq, inv = np.unique(pv, return_inverse=True)
    tot = np.zeros((len(uniq), 3))
    np.add.at(tot, inv, col)
    n = np.bincount(inv, minlength=len(uniq))[:, None]
    corr = tot[inv] / n[inv] - col
    return pv, pc, np.clip(corr, -LOCAL_LIMIT, LOCAL_LIMIT)


def spread(points: np.ndarray, values: np.ndarray, size: tuple[int, int], step: int = 4) -> np.ndarray:
    """A smooth H x W x 3 field through scattered `values` at `points` (x, y in the tile's pixels): push-pull on a
    grid of `step` pixels (weighted averages down a pyramid, holes filled from the coarser level on the way up),
    zero where no value reaches."""
    from PIL import Image

    w, h = size
    gw, gh = max(1, -(-w // step)), max(1, -(-h // step))
    acc = np.zeros((gh, gw, 3))
    wt = np.zeros((gh, gw))
    gx = np.clip((points[:, 0] / step).astype(int), 0, gw - 1)
    gy = np.clip((points[:, 1] / step).astype(int), 0, gh - 1)
    np.add.at(acc, (gy, gx), values)
    np.add.at(wt, (gy, gx), 1.0)
    levels = [(acc, wt)]
    while max(levels[-1][1].shape) > 1:
        a, b = levels[-1]
        hh, ww = (a.shape[0] + 1) // 2 * 2, (a.shape[1] + 1) // 2 * 2
        a = np.pad(a, ((0, hh - a.shape[0]), (0, ww - a.shape[1]), (0, 0)))
        b = np.pad(b, ((0, hh - b.shape[0]), (0, ww - b.shape[1])))
        a = a.reshape(hh // 2, 2, ww // 2, 2, 3).sum((1, 3))
        b = b.reshape(hh // 2, 2, ww // 2, 2).sum((1, 3))
        levels.append((a, b))
    a, b = levels[-1]
    field = np.where(b[..., None] > 0, a / np.maximum(b, 1e-9)[..., None], 0.0)
    for a, b in reversed(levels[:-1]):
        up = np.repeat(np.repeat(field, 2, 0), 2, 1)[: a.shape[0], : a.shape[1]]
        own = a / np.maximum(b, 1e-9)[..., None]
        k = np.clip(b, 0, 1)[..., None]
        field = k * own + (1 - k) * up
        # soften the blockiness of nearest upsampling
        field = 0.5 * field + 0.125 * (np.roll(field, 1, 0) + np.roll(field, -1, 0) + np.roll(field, 1, 1)
                                       + np.roll(field, -1, 1))
    out = np.stack([np.asarray(Image.fromarray(field[..., c].astype(np.float32), "F").resize((w, h), Image.BILINEAR))
                    for c in range(3)], -1)
    return out


def texture_mesh(v: np.ndarray, f: np.ndarray, views: Sequence[View], *, atlas_px: int = 4096,
                 max_source_scale: float = 1.0, level_seams: bool = True) -> TexturedMesh:
    """The textured mesh (see the module docstring). `atlas_px` is the square atlas side; tiles are scaled down
    uniformly until they fit, never up beyond `max_source_scale` of the source pixels."""
    from PIL import Image

    t = {}
    t0 = time.perf_counter()
    v = np.asarray(v, float)
    f = np.asarray(f, np.int64)
    adj = _face_adjacency(f)
    scores = view_scores(v, f, views)
    t["visibility_s"] = time.perf_counter() - t0
    t1 = time.perf_counter()
    label = select_views(scores, adj)
    chart = charts(label, adj)
    nchart = int(chart.max()) + 1
    t["select_s"] = time.perf_counter() - t1

    # corner UVs in source pixels, per chart its bounding rectangle
    t2 = time.perf_counter()
    corner_px = np.zeros((len(f), 3, 2))
    for j in np.unique(label[label >= 0]):
        sel = label == j
        uv, _ = project(v, views[j].pose, views[j].cam)
        corner_px[sel] = uv[f[sel]]
    lo = np.full((nchart, 2), np.inf)
    hi = np.full((nchart, 2), -np.inf)
    seen = label >= 0
    np.minimum.at(lo, chart[seen], corner_px[seen].min(axis=1))
    np.maximum.at(hi, chart[seen], corner_px[seen].max(axis=1))
    chart_view = np.full(nchart, -1)
    chart_view[chart] = label
    real = chart_view >= 0
    lo[real] = np.floor(lo[real]) - 1
    hi[real] = np.ceil(hi[real]) + 1
    src = np.where(real[:, None], hi - lo, 0.0)
    unseen_tile = 16
    # the largest uniform scale whose tiles fit the atlas (shelf packing)
    area = float(((src[:, 0] + 2 * TILE_PAD_PX) * (src[:, 1] + 2 * TILE_PAD_PX))[real].sum())
    scale = min(max_source_scale, float(np.sqrt(0.85 * atlas_px * atlas_px / max(area, 1.0))))
    while True:
        tile = np.where(real[:, None], np.ceil(src * scale) + 2 * TILE_PAD_PX, 0).astype(int)
        tile = np.maximum(tile, np.where(real[:, None], 2 * TILE_PAD_PX + 1, 0))
        sizes = np.vstack([tile[real], [[unseen_tile, unseen_tile]]])
        pos, used = _shelf_pack(sizes, atlas_px)
        if used <= atlas_px:
            break
        scale *= 0.95
    tile_pos = np.zeros((nchart, 2), int)
    tile_pos[real] = pos[:-1]
    unseen_pos = pos[-1]
    t["pack_s"] = time.perf_counter() - t2

    # seam gains from small copies of the views
    t3 = time.perf_counter()
    used_views = sorted({int(j) for j in chart_view[real]})
    thumbs: dict[int, np.ndarray] = {}
    gains = np.ones((nchart, 3))
    if level_seams and nchart > 1:
        for j in used_views:
            with Image.open(views[j].image_path) as im:
                im.draft("RGB", (im.size[0] // 4, im.size[1] // 4))
                im = im.convert("RGB")
                if im.size[0] > 1100:
                    im = im.resize((1008, round(1008 * im.size[1] / im.size[0])), Image.BILINEAR)
                thumbs[j] = np.asarray(im, np.float64)
        gains = seam_gains(v, f, chart, label, views, thumbs)
        sv, sc, scorr = seam_corrections(v, f, chart, label, views, thumbs, gains)
    else:
        sv, sc, scorr = np.zeros(0, int), np.zeros(0, int), np.zeros((0, 3))
    by_chart = {int(c): np.nonzero(sc == c)[0] for c in np.unique(sc)}
    t["seams_s"] = time.perf_counter() - t3

    # the atlas: each chart's rectangle of its view's pixels, scaled, gain-corrected
    t4 = time.perf_counter()
    atlas = Image.new("RGB", (atlas_px, atlas_px), (0, 0, 0))
    atlas.paste(UNSEEN_RGB, (int(unseen_pos[0]), int(unseen_pos[1]), int(unseen_pos[0]) + unseen_tile,
                             int(unseen_pos[1]) + unseen_tile))
    src_px = 0
    for j in used_views:
        with Image.open(views[j].image_path) as im:
            im = im.convert("RGB")
            iw, ih = im.size
            for c in np.nonzero(chart_view == j)[0]:
                tw, th = (int(x) for x in tile[c])
                k = (tile[c] - 2 * TILE_PAD_PX) / np.maximum(src[c], 1e-9)       # atlas px per source px
                s0 = lo[c] - TILE_PAD_PX / k                                       # source px at the tile's corner
                s1 = s0 + np.array([tw, th]) / k
                c0 = np.maximum(s0, 0.0)
                c1 = np.minimum(s1, [iw, ih])
                a0 = np.round((c0 - s0) * k).astype(int)
                a1 = np.minimum(np.round((c1 - s0) * k).astype(int), [tw, th])
                if (a1 - a0 < 1).any():
                    continue
                piece = im.resize((int(a1[0] - a0[0]), int(a1[1] - a0[1])), Image.BILINEAR,
                                  box=(float(c0[0]), float(c0[1]), float(c1[0]), float(c1[1])))
                src_px += float(np.prod(c1 - c0))
                idx = by_chart.get(int(c))
                if (idx is not None and len(idx)) or not np.allclose(gains[c], 1.0, atol=0.01):
                    a = np.asarray(piece, np.float32) * gains[c][None, None].astype(np.float32)
                    if idx is not None and len(idx):
                        uvp, _ = project(v[sv[idx]], views[j].pose, views[j].cam)
                        pts = (uvp - c0) * k                                   # in the piece's pixels
                        a += spread(pts, scorr[idx], (a.shape[1], a.shape[0])).astype(np.float32)
                    piece = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
                atlas.paste(piece, (int(tile_pos[c, 0] + a0[0]), int(tile_pos[c, 1] + a0[1])))
    t["atlas_s"] = time.perf_counter() - t4

    # vertices: one per (mesh vertex, chart)
    corner_v = f.reshape(-1)
    corner_c = np.repeat(np.where(seen, chart, nchart), 3)          # unseen faces share one extra "chart"
    key = corner_v * (nchart + 1) + corner_c
    uniq, inv = np.unique(key, return_inverse=True)
    out_v = v[uniq // (nchart + 1)].astype(np.float32)
    uv = np.zeros((len(uniq), 2))
    cpx = corner_px.reshape(-1, 2)
    cc = np.repeat(chart, 3)
    sf = np.repeat(seen, 3)
    full = np.zeros((len(corner_v), 2))
    full[sf] = (cpx[sf] - lo[cc[sf]]) * (tile[cc[sf]] - 2 * TILE_PAD_PX) / np.maximum(src[cc[sf]], 1e-9) \
        + tile_pos[cc[sf]] + TILE_PAD_PX
    full[~sf] = unseen_pos + unseen_tile / 2
    uv[inv] = full
    uv /= atlas_px
    faces = inv.reshape(-1, 3).astype(np.int32)
    t["total_s"] = time.perf_counter() - t0
    stats = {"faces": int(len(f)), "vertices": int(len(out_v)), "charts": int(real.sum()),
             "views": len(views), "views_used": len(used_views),
             "stills_used": sum(views[j].kind == "still" for j in used_views),
             "faces_unseen": int((~seen).sum()),
             "face_share_from_stills": round(float(np.mean([views[j].kind == "still" for j in label[seen]]))
                                             if seen.any() else 0.0, 3),
             "atlas_px": atlas_px, "source_scale": round(scale, 3),
             "gain_range": [round(float(gains.min()), 3), round(float(gains.max()), 3)],
             "seconds": {k: round(x, 3) for k, x in t.items()}}
    return TexturedMesh(out_v, uv.astype(np.float32), faces, atlas, stats)


# ------------------------------------------------------------------------------------------------ session folders
def load_scan_dir(session_dir: str | Path, scan_id: str) -> tuple[dict, list[dict]]:
    """A capture folder (the phone's `Documents/sessions/<id>/` layout, also the gateway's) as (scan package,
    stills) with every file field an absolute path."""
    base = Path(session_dir)
    pkg = json.loads((base / "scans" / scan_id / "scan_package.json").read_text())

    def absolute(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: (str(base / x) if k.endswith("_file") and isinstance(x, str) else absolute(x))
                    for k, x in o.items()}
        if isinstance(o, list):
            return [absolute(x) for x in o]
        return o
    stills = []
    for p in sorted((base / "stills").glob(f"{scan_id}_s*.json")):
        stills.append(absolute(json.loads(p.read_text())))
    return absolute(pkg), stills
