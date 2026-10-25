"""What Lea looks at on a report turn, and where a spot she names sits on the 3D model.

Each turn about a finished scan can carry up to MAX_VIEWS images: a few of the scan's own photos (the ones most
likely to show what the question is about) and renders of the scan's 3D reconstruction aimed at that area. Lea
answers from them and can name a spot in one (`<point photo="s4" x=".." y=".." label=".."/>`); `point_at` turns
that pixel into a point on the model, so the phone can circle it, fly there and zoom in.

The surface is the scan's own: the gateway's photo-textured recreation (its USDZ: one ASCII USD layer and the photo
atlas, repairos.recreation.usdz) when there is one, else the phone's coloured LiDAR cloud cut to the object's box.
Both are in the scan frame (arkit_world, metres), the frame of the findings' points and of the phone's 3D view.
Each image comes with a point map (the surface point under each pixel), from a z-buffer of the surface's points
seen from that image's camera: a photo's own pose and intrinsics, or the render's camera. Pure numpy and Pillow.

Any object: nothing here knows what was scanned. The photos are chosen by the words of the question against the
located parts' own words, by whether the area projects into each photo, then by sharpness.
"""
from __future__ import annotations

import io
import math
import re
import threading
import zipfile
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from repairos.locate.camera import Pinhole, Pose, project

IMAGE_PX = 768            # the long side of every image Lea sees (what the phone sends on a looking turn)
POINT_MAP_STEP = 2        # point maps are kept at half resolution: a lookup needs a spot, not a pixel
MAX_VIEWS = 3
RENDER_FOV_DEG = 35.0
BACKGROUND = (22, 22, 26)  # the report's dark stage
MAX_SAMPLES = 500_000
SAMPLE_SPACING_M = 0.0012  # recreation surface samples about 1.2 mm apart
LOOKUP_RADIUS = 0.04       # a pixel with no surface under it takes the nearest one within 4 % of the image width

STOP = {"the", "and", "for", "with", "this", "that", "what", "where", "which", "does", "doing", "show", "tell",
        "how", "can", "you", "your", "are", "its", "it's", "from", "into", "onto", "about", "there", "here", "these",
        "those", "have", "has", "one", "please", "look", "see", "find", "point", "where's", "what's", "is", "my",
        "me", "part", "thing", "side", "on", "in", "of", "to", "a", "an"}


# ------------------------------------------------------------------------------------------------ surfaces
@dataclass
class Surface:
    """The model as coloured points (scan frame) plus its centre and radius."""
    points: np.ndarray        # N x 3 float32
    colors: np.ndarray        # N x 3 uint8
    spacing_m: float          # about how far apart neighbouring points are
    kind: str                 # recreation | cloud
    center: np.ndarray = field(default_factory=lambda: np.zeros(3))
    radius: float = 0.1

    def focus(self, center: Sequence[float] | None, radius: float | None) -> Surface:
        """Frames the object itself (the recreation's focus, the scan's box) rather than everything scanned."""
        if center is not None and len(center) == 3 and radius:
            self.center, self.radius = np.asarray(center, float), max(0.02, float(radius))
        return self

    def __post_init__(self) -> None:
        if len(self.points):
            lo, hi = np.percentile(self.points, 2, axis=0), np.percentile(self.points, 98, axis=0)
            self.center = (lo + hi) / 2
            self.radius = max(0.03, float(np.linalg.norm(hi - lo)) / 2)


_ARRAY = r"\[([^\]]*)\]"


def _floats(text: str, name: str, width: int) -> np.ndarray | None:
    m = re.search(rf"{re.escape(name)}\s*=\s*{_ARRAY}", text, re.S)
    if not m:
        return None
    vals = np.array(re.findall(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", m.group(1)), dtype=np.float64)
    return vals.reshape(-1, width) if len(vals) % width == 0 else None


def surface_from_usdz(data: bytes, *, max_samples: int = MAX_SAMPLES, seed: int = 0) -> Surface | None:
    """The recreation's textured mesh as dense coloured points: random points on every triangle (by area), each
    coloured from the photo atlas at its texture coordinate. None when the file is not our USDZ layout."""
    from PIL import Image

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            layer = next(n for n in names if n.endswith(".usda"))
            text = z.read(layer).decode("utf-8", "replace")
            tex = next(n for n in names if n.lower().endswith((".jpg", ".jpeg", ".png")))
            atlas = np.asarray(Image.open(io.BytesIO(z.read(tex))).convert("RGB"))
    except (zipfile.BadZipFile, StopIteration, OSError, KeyError):
        return None
    v = _floats(text, "point3f[] points", 3)
    st = _floats(text, "texCoord2f[] primvars:st", 2)
    f = _floats(text, "int[] faceVertexIndices", 3)
    if v is None or st is None or f is None or len(st) != len(v) or not len(f):
        return None
    f = f.astype(np.int64)
    if f.max() >= len(v):
        return None
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    total = float(area.sum())
    n = int(min(max_samples, max(20_000, total / SAMPLE_SPACING_M ** 2)))
    rng = np.random.default_rng(seed)
    tri = rng.choice(len(f), size=n, p=area / total) if total > 0 else rng.integers(0, len(f), n)
    r1, r2 = rng.random(n), rng.random(n)
    s = np.sqrt(r1)
    wa, wb, wc = 1 - s, s * (1 - r2), s * r2
    pts = wa[:, None] * a[tri] + wb[:, None] * b[tri] + wc[:, None] * c[tri]
    uv = wa[:, None] * st[f[tri, 0]] + wb[:, None] * st[f[tri, 1]] + wc[:, None] * st[f[tri, 2]]
    h, w = atlas.shape[:2]
    # USD's st has t up (repairos.recreation.usdz): the atlas row is from the top.
    col = np.clip((uv[:, 0] * w).astype(int), 0, w - 1)
    row = np.clip(((1 - uv[:, 1]) * h).astype(int), 0, h - 1)
    spacing = math.sqrt(total / n) if n else SAMPLE_SPACING_M
    return Surface(pts.astype(np.float32), atlas[row, col].astype(np.uint8), spacing, "recreation")


def surface_from_cloud(vertex: Mapping[str, np.ndarray], crop_box: Mapping | None, voxel_mm: float | None = None
                       ) -> Surface | None:
    """The phone's coloured LiDAR cloud (the scan package's points_file, read with repairos.locate.cloud.read_ply),
    cut to the object's box when the package has one."""
    try:
        p = np.stack([vertex["x"], vertex["y"], vertex["z"]], axis=1).astype(np.float32)
    except KeyError:
        return None
    names = [k for k in ("red", "green", "blue") if k in vertex]
    rgb = np.stack([vertex[k] for k in names], axis=1).astype(np.uint8) if len(names) == 3 else \
        np.full((len(p), 3), 170, np.uint8)
    if crop_box and crop_box.get("extent_m") and crop_box.get("center_m"):
        from repairos.recon.tsdf import ObjectBox

        box = ObjectBox.from_contract({"quaternion_xyzw": [0, 0, 0, 1], **crop_box})
        keep = box.contains(p, 0.05)
        if keep.sum() >= 200:
            p, rgb = p[keep], rgb[keep]
    if len(p) < 50:
        return None
    out = Surface(p, rgb, (voxel_mm or 3.0) / 1000.0, "cloud")
    if crop_box and crop_box.get("extent_m") and crop_box.get("center_m"):
        out.focus(crop_box["center_m"], float(np.linalg.norm(crop_box["extent_m"])) / 2)
    return out


# ------------------------------------------------------------------------------------------------ z-buffer
def look_pose(eye: Sequence[float], look: Sequence[float]) -> Pose:
    """Camera-to-world for a camera at `eye` looking at `look`, world Y up (ARKit camera axes: -z forward)."""
    eye, look = np.asarray(eye, float), np.asarray(look, float)
    back = eye - look
    back /= max(np.linalg.norm(back), 1e-9)
    up = np.array([0.0, 1.0, 0.0])
    if abs(float(back @ up)) > 0.995:          # straight above or below: any horizontal "up" will do
        up = np.array([0.0, 0.0, -1.0])
    right = np.cross(up, back)
    right /= np.linalg.norm(right)
    return Pose(np.stack([right, np.cross(back, right), back], axis=1), eye)


def zbuffer(s: Surface, pose: Pose, cam: Pinhole, width: int, height: int) -> np.ndarray:
    """For each pixel of a width x height image, the index of the nearest surface point drawn there (-1: none).
    Each point covers a little square about its own size on screen, so the surface reads as solid."""
    uv, depth = project(s.points, pose, cam)
    ok = (depth > 0.01) & np.isfinite(uv).all(axis=1)
    idx = np.nonzero(ok)[0]
    uv, depth = uv[ok], depth[ok]
    # Half the side of each point's square on screen, in pixels (0: one pixel).
    reach = np.clip(np.ceil(cam.fx * s.spacing_m * 1.2 / depth) // 2, 0, 2).astype(int)
    # Far to near, so in each pass the nearest point drawn at a pixel is written last (numpy assigns repeated
    # indices in order); a pass may only overwrite what lies further away.
    order = np.argsort(-depth, kind="stable")
    idx, depth, reach = idx[order], depth[order], reach[order]
    px, py = np.floor(uv[order, 0]).astype(np.int64), np.floor(uv[order, 1]).astype(np.int64)
    out = np.full(width * height, -1, np.int64)
    best = np.full(width * height, np.inf, np.float32)
    top = int(reach.max(initial=0))
    for dx in range(-top, top + 1):
        for dy in range(-top, top + 1):
            sel = reach >= max(abs(dx), abs(dy))
            x, y = px[sel] + dx, py[sel] + dy
            inside = (x >= 0) & (x < width) & (y >= 0) & (y < height)
            pix, d, ii = (y * width + x)[inside], depth[sel][inside], idx[sel][inside]
            near = d < best[pix]
            pix, d, ii = pix[near], d[near], ii[near]
            best[pix] = d
            out[pix] = ii
    return out.reshape(height, width)


def _point_map(s: Surface, index: np.ndarray) -> np.ndarray:
    pts = np.full(index.shape + (3,), np.nan, np.float32)
    hit = index >= 0
    pts[hit] = s.points[index[hit]]
    return pts[::POINT_MAP_STEP, ::POINT_MAP_STEP]


# ------------------------------------------------------------------------------------------------ the views
@dataclass
class View:
    """One image Lea sees: `key` is what she names it by (s4 for a photo, v1 for a render)."""
    key: str
    kind: str                 # photo | render
    jpeg: bytes
    points: np.ndarray        # (h / POINT_MAP_STEP) x (w / POINT_MAP_STEP) x 3 surface points (NaN: none), as sent
    caption: str
    still_id: str | None = None


def _jpeg(rgb: np.ndarray, quality: int = 70, grid: bool = False) -> bytes:
    from PIL import Image, ImageDraw

    im = Image.fromarray(np.ascontiguousarray(rgb))
    if grid:
        # A faint labelled grid every tenth of the image: a vision model reads a spot's position off it far better
        # than it estimates fractions unaided (measured on the owner's controller: the face buttons came out about
        # 0.15 of the height too low without it). Only on the images Lea sees, never on what the person sees.
        im = im.convert("RGBA")
        over = Image.new("RGBA", im.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(over)
        w, h = im.size
        for i in range(1, 10):
            x, y = round(w * i / 10), round(h * i / 10)
            d.line([(x, 0), (x, h)], fill=(255, 255, 255, 70), width=1)
            d.line([(0, y), (w, y)], fill=(255, 255, 255, 70), width=1)
            for pos, text in (((x + 2, 2), f".{i}"), ((x + 2, h - 14), f".{i}"), ((2, y + 1), f".{i}"),
                              ((w - 18, y + 1), f".{i}")):
                d.text((pos[0] + 1, pos[1] + 1), text, fill=(0, 0, 0, 200))
                d.text(pos, text, fill=(255, 255, 0, 230))
        im = Image.alpha_composite(im, over).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def render_view(s: Surface, eye: Sequence[float], look: Sequence[float], key: str, caption: str,
                size: int = IMAGE_PX, grid: bool = True) -> View:
    """The model drawn from `eye` towards `look` on the dark stage, with its point map."""
    f = size / 2 / math.tan(math.radians(RENDER_FOV_DEG) / 2)
    cam = Pinhole(f, f, size / 2, size / 2, size, size)
    index = zbuffer(s, look_pose(eye, look), cam, size, size)
    rgb = np.empty((size, size, 3), np.uint8)
    rgb[:] = BACKGROUND
    hit = index >= 0
    rgb[hit] = s.colors[index[hit]]
    return View(key, "render", _jpeg(rgb, grid=grid), _point_map(s, index), caption)


def upright(still: Mapping) -> bool:
    """Whether the still is turned at all to show it upright (quarter_turns)."""
    return quarter_turns(still) != 0


def quarter_turns(still: Mapping) -> int:
    """Clockwise quarter turns that show a still upright: the camera's roll from its ARKit pose, so gravity points
    down in the image (stills are stored in the sensor's own orientation, however the phone was held). Shot from
    almost straight above, gravity says nothing about up: the phone's usual portrait hold (one turn for a landscape
    sensor image)."""
    landscape = int(still.get("width_px") or 0) > int(still.get("height_px") or 0)
    try:
        r = Pose.from_contract(still["camera"]).r
    except (KeyError, ValueError, TypeError):
        return 1 if landscape else 0
    d = r.T @ np.array([0.0, -1.0, 0.0])        # world down in camera axes (x right, y up)
    g = np.array([d[0], -d[1]])                 # in image axes (u right, v down)
    if float(np.linalg.norm(g)) < 0.2:
        return 1 if landscape else 0
    best, turns = -2.0, 0
    for k in range(4):
        if g[1] > best:
            best, turns = float(g[1]), k
        g = np.array([-g[1], g[0]])             # one clockwise turn of the image
    return turns


def turn_fraction(k: int, x: float, y: float) -> tuple[float, float]:
    """A spot (fractions of the stored image) after k clockwise quarter turns."""
    for _ in range(k % 4):
        x, y = 1 - y, x
    return x, y


def unturn_fraction(k: int, x: float, y: float) -> tuple[float, float]:
    """The inverse of turn_fraction: fractions of the shown image -> of the stored image."""
    for _ in range(k % 4):
        x, y = y, 1 - x
    return x, y


def photo_view(s: Surface, still: Mapping, image: bytes, key: str, size: int = IMAGE_PX) -> View | None:
    """A still as the phone sends one (turned upright, IMAGE_PX on the long side, JPEG 0.65) with the surface point
    under each of its pixels, from the still's own pose. None without a pose or a readable image."""
    from PIL import Image

    try:
        pose = Pose.from_contract(still["camera"])
        im = Image.open(io.BytesIO(image)).convert("RGB")
    except (KeyError, ValueError, OSError):
        return None
    w0, h0 = im.size
    cam = Pinhole.from_contract(still["intrinsics"], still.get("width_px") or w0, still.get("height_px") or h0)
    scale = size / max(w0, h0)
    w, h = max(1, round(w0 * scale)), max(1, round(h0 * scale))
    # The point map only: computed straight at its own (half) resolution.
    wm, hm = max(1, w // POINT_MAP_STEP), max(1, h // POINT_MAP_STEP)
    index = zbuffer(s, pose, cam.scaled(wm, hm), wm, hm)
    pts = np.full(index.shape + (3,), np.nan, np.float32)
    pts[index >= 0] = s.points[index[index >= 0]]
    rgb = np.asarray(im.resize((w, h), Image.Resampling.LANCZOS))
    k = quarter_turns(still)
    if k:
        rgb, pts = np.rot90(rgb, k=-k), np.rot90(pts, k=-k)   # k clockwise turns: gravity down
    return View(key, "photo", _jpeg(np.ascontiguousarray(rgb), 65, grid=True), np.ascontiguousarray(pts),
                f"Photo {key}: a real photo of the object taken during the scan.", str(still.get("still_id")))


def point_at(view: View, x: float, y: float) -> np.ndarray | None:
    """The surface point under (x, y) (fractions of the image as sent, 0,0 top left), or the nearest one within
    LOOKUP_RADIUS of the image width; None when the spot is not on the model."""
    pm = view.points
    h, w = pm.shape[:2]
    if not (0 <= x <= 1 and 0 <= y <= 1) or not w or not h:
        return None
    cx, cy = min(w - 1, int(x * w)), min(h - 1, int(y * h))
    if np.isfinite(pm[cy, cx]).all():
        return pm[cy, cx].astype(float)
    r = max(1, int(LOOKUP_RADIUS * w))
    y0, y1, x0, x1 = max(0, cy - r), min(h, cy + r + 1), max(0, cx - r), min(w, cx + r + 1)
    patch = pm[y0:y1, x0:x1]
    ok = np.isfinite(patch).all(axis=-1)
    if not ok.any():
        return None
    yy, xx = np.nonzero(ok)
    d = (yy + y0 - cy) ** 2 + (xx + x0 - cx) ** 2
    k = int(np.argmin(d))
    return patch[yy[k], xx[k]].astype(float)


# ------------------------------------------------------------------------------------------------ choosing
def words(text: str | None) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) < 2 or w in STOP:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return out


def area_target(question: str, parts: Sequence[Mapping[str, Any]]) -> np.ndarray | None:
    """The area the question is about: the mean point of the located parts whose words it shares most (None when
    it names none of them)."""
    q = words(question)
    scored = []
    for p in parts:
        pt = p.get("point")
        if pt is None:
            continue
        n = len(q & words(f"{p.get('part')} {p.get('where')}"))
        if n:
            scored.append((n, np.asarray(pt, float)))
    if not scored:
        return None
    top = max(n for n, _ in scored)
    return np.mean([pt for n, pt in scored if n == top], axis=0)


def _in_view(still: Mapping, p: np.ndarray) -> float:
    """How well a still shows point p: 0 outside the frame or behind the camera, up to 1 at the centre."""
    try:
        pose = Pose.from_contract(still["camera"])
        cam = Pinhole.from_contract(still["intrinsics"], still.get("width_px"), still.get("height_px"))
    except (KeyError, ValueError):
        return 0.0
    (u, v), d = (a[0] for a in project(p[None], pose, cam))
    w, h = cam.width or 1, cam.height or 1
    if not (d > 0 and 0 <= u <= w and 0 <= v <= h):
        return 0.0
    return float(1 - min(1.0, math.hypot(u / w - 0.5, v / h - 0.5) * 1.4))


def usable(still: Mapping) -> bool:
    """A still Lea can look at and point in: posed, in the main scan's frame (a flipped bottom's photos are in their
    own frame until registered), with an image."""
    return bool(still.get("camera") and still.get("intrinsics") and still.get("image_file")) and \
        int(still.get("segment") or 0) == 0 and still.get("pose_reliable") is not False


def pick_photos(stills: Sequence[Mapping], question: str, parts: Sequence[Mapping[str, Any]],
                center: np.ndarray | None, k: int = 2) -> list[Mapping]:
    """The k stills most likely to show what the question is about: the photos of the parts it names, then those
    that frame the area (or the whole object) well, then the sharper ones."""
    target = area_target(question, parts)
    q = words(question)
    sharp = [float(s.get("sharpness") or 0) for s in stills]
    top_sharp = max(sharp, default=0) or 1.0
    scored = []
    for s, sh in zip(stills, sharp):
        if not usable(s):
            continue
        own = sum(len(q & words(f"{p.get('part')} {p.get('where')}")) for p in parts if p.get("still_id") == s.get("still_id"))
        aim = target if target is not None else center
        score = 3.0 * own + (2.0 * _in_view(s, aim) if aim is not None else 0.0) + 0.5 * sh / top_sharp
        scored.append((score, str(s.get("still_id")), s))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [s for _, _, s in scored[:k]]


def short_key(still_id: str) -> str:
    """"scan_ab12_s4" -> "s4": the name Lea gives a photo."""
    tail = str(still_id).rsplit("_", 1)[-1]
    return tail if re.fullmatch(r"s\d+", tail) else re.sub(r"[^A-Za-z0-9]", "", str(still_id))[-8:]


def side_words(direction: np.ndarray) -> str:
    d = direction / max(np.linalg.norm(direction), 1e-9)
    if d[1] > 0.8:
        return "from above"
    if d[1] < -0.8:
        return "from below"
    return "from the side the scan saw most" if d[1] >= -0.2 else "from low down"


def render_plan(s: Surface, target: np.ndarray | None, camera_dirs: Sequence[np.ndarray], n: int
                ) -> list[tuple[np.ndarray, np.ndarray, str]]:
    """(eye, look, words) for up to n renders. With an area: close on it from the side it faces, then the whole
    object from that side. Without: the whole object from where the scan's photos were taken, then from behind."""
    dist = s.radius / math.sin(math.radians(RENDER_FOV_DEG) / 2) * 1.05
    main = np.mean(camera_dirs, axis=0) if len(camera_dirs) else np.array([0.3, 0.6, 1.0])
    if np.linalg.norm(main) < 1e-6:
        main = np.array([0.3, 0.6, 1.0])
    main = main / np.linalg.norm(main)
    out = []
    if target is not None:
        out_dir = target - s.center
        out_dir[1] = max(out_dir[1], 0) + 0.5 * float(np.linalg.norm(out_dir[[0, 2]]))
        out_dir = out_dir / np.linalg.norm(out_dir) if np.linalg.norm(out_dir) > 1e-6 else main
        out.append((target + out_dir * dist * 0.5, target, "close on the area " + side_words(out_dir)))
        out.append((s.center + out_dir * dist, s.center, "the whole object " + side_words(out_dir)))
    else:
        back = main * np.array([-1, 1, -1])
        out.append((s.center + main * dist, s.center, "the whole object " + side_words(main)))
        out.append((s.center + back * dist, s.center, "the whole object from the opposite side"))
    return out[:n]


RENDER_CAPTION = ("View {key}: a render of the 3D reconstruction of this real object, {words}. It is built from its "
                  "own LiDAR scan and textured with its own photos: real evidence of its shape, parts and surface, "
                  "not a mockup (fine detail is softer than in a photo).")


# ------------------------------------------------------------------------------------------------ caches
class Lru:
    def __init__(self, size: int):
        self.size, self.items, self.lock = size, OrderedDict(), threading.Lock()

    def get(self, key):
        with self.lock:
            if key in self.items:
                self.items.move_to_end(key)
                return self.items[key]
        return None

    def put(self, key, value) -> None:
        with self.lock:
            self.items[key] = value
            self.items.move_to_end(key)
            while len(self.items) > self.size:
                self.items.popitem(last=False)


SURFACES = Lru(3)
VIEWS = Lru(24)


def build_views(surface: Surface, stills: Sequence[Mapping], image_of, question: str,
                parts: Sequence[Mapping[str, Any]], *, scope: str, max_views: int = MAX_VIEWS) -> list[View]:
    """The images for one turn (at most `max_views`): two photos and one render when the photos frame the area,
    one photo and two renders when they do not (few or no close photos of it). `image_of(still)` returns the
    still's image bytes or None. Views are cached per scan, photo and area."""
    target = area_target(question, parts)
    photos = pick_photos(stills, question, parts, surface.center, k=2)
    aim = target if target is not None else surface.center
    framed = [p for p in photos if _in_view(p, aim) > 0.3]
    n_photos = 2 if len(framed) >= 2 else min(1, len(photos))
    out: list[View] = []
    for st in (framed or photos)[:n_photos]:
        key = (scope, "photo", st.get("still_id"))
        v = VIEWS.get(key)
        if v is None:
            img = image_of(st)
            v = photo_view(surface, st, img, short_key(str(st.get("still_id")))) if img else None
            if v is not None:
                VIEWS.put(key, v)
        if v is not None:
            out.append(v)
    dirs = []
    for st in stills:
        cam = (st.get("camera") or {}).get("position_m")
        if usable(st) and cam:
            d = np.asarray(cam, float) - surface.center
            if np.linalg.norm(d) > 1e-6:
                dirs.append(d / np.linalg.norm(d))
    area = "whole" if target is None else ",".join(f"{c:.3f}" for c in target)
    for i, (eye, look, w) in enumerate(render_plan(surface, target, dirs, max_views - len(out)), 1):
        key = (scope, "render", area, i)
        v = VIEWS.get(key)
        if v is None:
            v = render_view(surface, eye, look, f"v{i}", RENDER_CAPTION.format(key=f"v{i}", words=w))
            VIEWS.put(key, v)
        out.append(v)
    return out[:max_views]


# ------------------------------------------------------------------------------------------------ the photo of a spot
RING_RGB = (255, 45, 210)    # Lea's pointer colour (the live view's magenta ring)
CROP_PX = 1024


def _draw_ring(rgb: np.ndarray, x: float, y: float, r: float) -> np.ndarray:
    from PIL import Image, ImageDraw

    im = Image.fromarray(np.ascontiguousarray(rgb))
    d = ImageDraw.Draw(im)
    width = max(4, round(r * 0.14))
    d.ellipse([x - r - 2, y - r - 2, x + r + 2, y + r + 2], outline=(0, 0, 0), width=width + 4)   # contrast halo
    d.ellipse([x - r, y - r, x + r, y + r], outline=RING_RGB, width=width)
    return np.asarray(im)


def photo_candidates(stills: Sequence[Mapping], p: np.ndarray) -> list[tuple[float, Mapping, float, float, float]]:
    """Stills that see point p, best first: (score, still, u, v, depth) in full-resolution sensor pixels. Closer
    (bigger on the photo) first, then sharper, then nearer the frame's centre."""
    sharp = [float(s.get("sharpness") or 0) for s in stills]
    top = max(sharp, default=0) or 1.0
    out = []
    for s, sh in zip(stills, sharp):
        if not usable(s):
            continue
        try:
            pose = Pose.from_contract(s["camera"])
            cam = Pinhole.from_contract(s["intrinsics"], s.get("width_px"), s.get("height_px"))
        except (KeyError, ValueError):
            continue
        (u, v), d = (a[0] for a in project(np.asarray(p, float)[None], pose, cam))
        w, h = cam.width or 0, cam.height or 0
        if not (d > 0.05 and 0.04 * w <= u <= 0.96 * w and 0.04 * h <= v <= 0.96 * h):
            continue
        size = cam.fx / d
        centred = 1 - min(1.0, math.hypot(u / w - 0.5, v / h - 0.5) * 1.4)
        out.append((size * (0.6 + 0.4 * sh / top) * (0.8 + 0.2 * centred), s, float(u), float(v), float(d)))
    out.sort(key=lambda t: -t[0])
    return out


def spot_photo(surface: Surface, stills: Sequence[Mapping], image_of, p: Sequence[float], radius_m: float,
               *, scope: str = "", prefer: str | None = None) -> tuple[bytes, str | None]:
    """The real photo of a spot with a ring drawn on it: (JPEG, still_id). The closest, sharpest still that sees
    the point unblocked (a soft or dark one still counts); a full-resolution crop around it, turned upright. With no
    photo that sees it: a render of the model close on the spot, ringed, and still_id None."""
    from PIL import Image

    p = np.asarray(p, float)
    cands = photo_candidates(stills, p)
    # The photo the spot was found or refined in first: a point is exact in its own photo, while small pose and
    # surface offsets between photos can move it a centimetre or more in another one.
    cands.sort(key=lambda c: c[1].get("still_id") != prefer)
    for _, st, u, v, depth in cands[:4]:
        key = (scope, "photo", st.get("still_id"))
        view = VIEWS.get(key)
        if view is None:
            img = image_of(st)
            view = photo_view(surface, st, img, short_key(str(st.get("still_id")))) if img else None
            if view is not None:
                VIEWS.put(key, view)
        if view is None:
            continue
        w0, h0 = int(st.get("width_px") or 0), int(st.get("height_px") or 0)
        x, y = turn_fraction(quarter_turns(st), u / w0, v / h0)
        seen = point_at(view, x, y)
        cam_t = np.asarray(st["camera"]["position_m"], float)
        if seen is not None and np.linalg.norm(seen - cam_t) < depth - 0.03:
            continue        # something nearer the camera hides the spot in this photo
        img = image_of(st)
        if not img:
            continue
        im = Image.open(io.BytesIO(img)).convert("RGB")
        fx = float(st["intrinsics"]["fx"]) * (im.size[0] / (w0 or im.size[0]))
        u, v = u * im.size[0] / (w0 or im.size[0]), v * im.size[1] / (h0 or im.size[1])
        side = int(np.clip(fx * max(0.06, 7 * radius_m) / depth, 480, 1800))
        x0 = int(np.clip(u - side / 2, 0, max(0, im.size[0] - side)))
        y0 = int(np.clip(v - side / 2, 0, max(0, im.size[1] - side)))
        crop = im.crop((x0, y0, min(im.size[0], x0 + side), min(im.size[1], y0 + side)))
        scale = min(1.0, CROP_PX / max(crop.size))
        crop = crop.resize((max(1, round(crop.size[0] * scale)), max(1, round(crop.size[1] * scale))),
                           Image.Resampling.LANCZOS)
        rx, ry, rr = (u - x0) * scale, (v - y0) * scale, max(22.0, fx * radius_m / depth * scale)
        rgb = np.asarray(crop)
        for _ in range(quarter_turns(st)):     # turned upright like every view of it: (x, y) -> (h - y, x)
            rx, ry = rgb.shape[0] - ry, rx
            rgb = np.rot90(rgb, k=-1)
        return _jpeg(_draw_ring(rgb, rx, ry, rr), 82), str(st.get("still_id"))
    out_dir = p - surface.center
    out_dir[1] = max(out_dir[1], 0) + 0.5 * float(np.linalg.norm(out_dir[[0, 2]]))
    out_dir = out_dir / np.linalg.norm(out_dir) if np.linalg.norm(out_dir) > 1e-6 else np.array([0.3, 0.8, 0.5])
    dist = max(4 * radius_m, surface.radius * 0.9) / math.tan(math.radians(RENDER_FOV_DEG) / 2)
    view = render_view(surface, p + out_dir * dist, p, "v0", "", size=CROP_PX, grid=False)   # the person sees it
    from PIL import Image as _Image

    rgb = np.asarray(_Image.open(io.BytesIO(view.jpeg)).convert("RGB"))
    f = CROP_PX / 2 / math.tan(math.radians(RENDER_FOV_DEG) / 2)
    return _jpeg(_draw_ring(rgb, CROP_PX / 2, CROP_PX / 2, max(22.0, f * radius_m / dist)), 82), None


# ------------------------------------------------------------------------------------------------ zoom and refine
UPRIGHT = Lru(4)    # full-resolution stills turned upright, for refine crops


def upright_image(still: Mapping, image: bytes):
    """The still at full resolution, turned upright like every view of it (PIL image)."""
    from PIL import Image

    key = (still.get("still_id"), len(image), hash(image[:2048]), hash(image[-2048:]))
    hit = UPRIGHT.get(key)
    if hit is not None:
        return hit
    im = Image.open(io.BytesIO(image)).convert("RGB")
    k = quarter_turns(still)
    if k:   # k clockwise turns (PIL's ROTATE_270 is one clockwise turn)
        im = im.transpose({1: Image.Transpose.ROTATE_270, 2: Image.Transpose.ROTATE_180,
                           3: Image.Transpose.ROTATE_90}[k])
    UPRIGHT.put(key, im)
    return im


def to_upright(still: Mapping, u: float, v: float) -> tuple[float, float]:
    """Full-resolution sensor pixel -> fractions of the upright image."""
    w0, h0 = float(still.get("width_px") or 1), float(still.get("height_px") or 1)
    return turn_fraction(quarter_turns(still), u / w0, v / h0)


def project_upright(still: Mapping, p: Sequence[float]) -> tuple[float, float, float] | None:
    """Where point p is in the upright still (fractions) and how far (m); None when behind or outside it."""
    try:
        pose = Pose.from_contract(still["camera"])
        cam = Pinhole.from_contract(still["intrinsics"], still.get("width_px"), still.get("height_px"))
    except (KeyError, ValueError):
        return None
    (u, v), d = (a[0] for a in project(np.asarray(p, float)[None], pose, cam))
    if not (d > 0.05 and 0 <= u <= (cam.width or 0) and 0 <= v <= (cam.height or 0)):
        return None
    x, y = to_upright(still, float(u), float(v))
    return x, y, float(d)


def refine_crop(still: Mapping, image: bytes, x: float, y: float, side: float, size: int = 640
                ) -> tuple[bytes, tuple[float, float, float, float]]:
    """A full-resolution square window of the upright still centred on (x, y) (fractions), `side` a fraction of the
    image's shorter side, with the grid; and its rectangle in upright fractions (x0, y0, x1, y1)."""
    from PIL import Image

    im = upright_image(still, image)
    w, h = im.size
    s = max(32, int(side * min(w, h)))
    cx, cy = x * w, y * h
    x0 = int(np.clip(cx - s / 2, 0, max(0, w - s)))
    y0 = int(np.clip(cy - s / 2, 0, max(0, h - s)))
    crop = im.crop((x0, y0, min(w, x0 + s), min(h, y0 + s)))
    crop = crop.resize((size, round(size * crop.size[1] / max(1, crop.size[0]))), Image.Resampling.LANCZOS)
    return _jpeg(np.asarray(crop), 80, grid=True), (x0 / w, y0 / h, min(w, x0 + s) / w, min(h, y0 + s) / h)


def from_crop(rect: tuple[float, float, float, float], cx: float, cy: float) -> tuple[float, float]:
    """A spot given as fractions of a crop -> fractions of the whole upright image."""
    x0, y0, x1, y1 = rect
    return x0 + cx * (x1 - x0), y0 + cy * (y1 - y0)


def text_likeness(still: Mapping, image: bytes, x: float, y: float) -> float:
    """How much the spot at (x, y) (upright fractions) looks like printed text: the share of bright pixels in a small
    window. A socket or contact a wire plugs into is a dark hole; a printed label is bright strokes. Used to keep the
    reading of a pin that sits on its socket when two close-ups read the same label."""
    im = upright_image(still, image)
    w, h = im.size
    r = max(6, int(0.006 * min(w, h)))
    cx, cy = int(x * w), int(y * h)
    patch = np.asarray(im.crop((max(0, cx - r), max(0, cy - r), min(w, cx + r), min(h, cy + r))).convert("L"))
    return float((patch > 170).mean()) if patch.size else 0.0


def photo_spot(still: Mapping, image: bytes, x: float, y: float, ring_frac: float, side_frac: float = 0.14) -> bytes:
    """The real photo of a spot known only in that photo (a photo-only part, no 3D point yet): a full-resolution
    crop of the upright still around (x, y), ringed. `ring_frac`: the ring's radius as a share of the shorter side."""
    from PIL import Image

    im = upright_image(still, image)
    w, h = im.size
    s = max(64, int(side_frac * min(w, h)))
    x0 = int(np.clip(x * w - s / 2, 0, max(0, w - s)))
    y0 = int(np.clip(y * h - s / 2, 0, max(0, h - s)))
    crop = im.crop((x0, y0, min(w, x0 + s), min(h, y0 + s)))
    scale = min(1.0, CROP_PX / max(crop.size))
    crop = crop.resize((max(1, round(crop.size[0] * scale)), max(1, round(crop.size[1] * scale))),
                       Image.Resampling.LANCZOS)
    rr = max(18.0, ring_frac * min(w, h) * scale)
    return _jpeg(_draw_ring(np.asarray(crop), (x * w - x0) * scale, (y * h - y0) * scale, rr), 82)
