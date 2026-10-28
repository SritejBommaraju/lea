"""Import a folder of plain iPhone Camera photos (JPEG or HEIC) as a recorded session (PLAN.md §6a step 1).

    from repairos.importers.photos import import_photos
    result = import_photos(Path("~/Desktop/broken-radio"), Path("samples/sessions/sess_20261024_radio"),
                           CaptureMeta(symptom="won't turn on"))

Plain photos have no AR session: no camera pose, no depth, no world frame. So the session holds one scan package
with `depth_source: none` and no keyframes, and every photo becomes an `unguided` still with `pose_reliable: false`
and no `camera` (still.schema.json allows that only when pose_reliable is false). Nothing is made up: the design
engine judges the photos, and the report says the location was not computed.

Per photo:
- HEIC/HEIF is decoded with macOS `sips` into a lossless PNG (EXIF kept for reading); JPEG is read as is.
- EXIF is read once: capture time (DateTimeOriginal + OffsetTimeOriginal), orientation, focal length and its
  35 mm equivalent, camera model and iOS version. Intrinsics are estimated only from the 35 mm equivalent (or the
  focal length with the focal-plane resolution); otherwise they are left out and `import.json` says `unknown`.
- The pixels are turned upright (EXIF orientation applied) and encoded once as a full-resolution JPEG, quality 92,
  4:4:4, with **no metadata** (no EXIF, GPS, XMP, IPTC, maker notes, thumbnail or comment). Only the ICC colour
  profile is kept, so colours stay right. Every written JPEG is re-checked to carry no metadata segment.

Pillow (MIT-CMU) and numpy (BSD-3) as in repairos.importers.images; `sips` ships with macOS.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from repairos.importers import images
from repairos.importers.images import ImportFormatError
from repairos.importers.session import GRAVITY_ARKIT_WORLD, CaptureMeta, ImportResult
from repairos.sessions import ids
from repairos.sessions.layout import ext_for
from repairos.sessions.writer import SessionWriter

JPEG_QUALITY = 92
JPEG_EXTS = (".jpg", ".jpeg")
HEIC_EXTS = (".heic", ".heif")
PHOTO_EXTS = JPEG_EXTS + HEIC_EXTS
FULL_FRAME_DIAGONAL_MM = math.hypot(36.0, 24.0)    # 43.27 mm: what a 35 mm-equivalent focal length is relative to
SESSION_ID_RE = re.compile(r"^sess_[A-Za-z0-9_-]+$")   # samples/session.schema.json
SHARPNESS_STRIP_ROWS = 512

# EXIF tags (CIPA DC-008)
T_MAKE, T_MODEL, T_SOFTWARE, T_ORIENTATION = 0x010F, 0x0110, 0x0131, 0x0112
IFD_EXIF, IFD_GPS = 0x8769, 0x8825
T_DATETIME_ORIGINAL, T_OFFSET_ORIGINAL, T_SUBSEC_ORIGINAL = 0x9003, 0x9011, 0x9291
T_FOCAL_LENGTH, T_FOCAL_35MM, T_LENS_MODEL = 0x920A, 0xA405, 0xA434
T_FPLANE_XRES, T_FPLANE_YRES, T_FPLANE_UNIT = 0xA20E, 0xA20F, 0xA210
T_PIXEL_X, T_PIXEL_Y = 0xA002, 0xA003
MM_PER_UNIT = {2: 25.4, 3: 10.0, 4: 1.0, 5: 0.001}   # FocalPlaneResolutionUnit: inch, cm, mm, um

# JPEG markers that may carry metadata. APP0 (JFIF) and APP2 ICC_PROFILE are allowed; everything else is not.
APP0, APP1, APP2, APP13, APP15, COM, SOS = 0xE0, 0xE1, 0xE2, 0xED, 0xEF, 0xFE, 0xDA


class ToolMissing(RuntimeError):
    """A command-line tool the import needs is not installed."""


@dataclass
class Photo:
    """One photo's EXIF facts (read before the metadata is dropped). Pixels are decoded later, one photo at a time
    (`upright_rgb`), so a folder of 48 MP photos never sits in memory at once."""
    source: Path
    fmt: str                              # jpeg | heic
    decoded_from: Path                    # the file Pillow reads: the JPEG itself, or sips' PNG of a HEIC
    sha256: str
    width: int                            # upright size
    height: int
    orientation: int                      # EXIF orientation of the source (1 = already upright)
    captured_at: datetime
    captured_at_source: str
    exif: dict[str, Any] = field(default_factory=dict)   # the few fields kept for import.json (never GPS)
    gps_found: bool = False
    metadata_found: list[str] = field(default_factory=list)
    intrinsics: dict[str, float] | None = None
    intrinsics_source: str = "unknown"
    intrinsics_method: str | None = None


# ------------------------------------------------------------------------------------------------- JPEG metadata
def jpeg_metadata_segments(data: bytes) -> list[str]:
    """The metadata segments of a JPEG (EXIF, XMP, IPTC, comments, other APPn), as short names. APP0 JFIF and APP2
    ICC_PROFILE are not metadata about the photo and are not listed. Raises ValueError on a non-JPEG."""
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("not a JPEG")
    out, i = [], 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            raise ValueError(f"JPEG marker expected at byte {i}")
        marker = data[i + 1]
        if marker == 0xFF:          # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker in (SOS, 0xD9):
            break
        length = int.from_bytes(data[i + 2:i + 4], "big")
        body = data[i + 4:i + 2 + length]
        if marker == APP0 and body.startswith((b"JFIF\x00", b"JFXX\x00")):
            pass
        elif marker == APP2 and body.startswith(b"ICC_PROFILE\x00"):
            pass
        elif marker == COM:
            out.append("COM")
        elif APP0 <= marker <= APP15:
            ident = body[:12].split(b"\x00", 1)[0].decode("latin-1", "replace")
            out.append(f"APP{marker - APP0}" + (f" {ident}" if ident else ""))
        i += 2 + length
    return out


def clean_jpeg(rgb: np.ndarray, icc_profile: bytes | None = None, quality: int = JPEG_QUALITY) -> bytes:
    """Full-resolution JPEG with no metadata at all (only the ICC colour profile when given)."""
    import io

    from PIL import Image

    im = Image.fromarray(np.asarray(rgb, dtype=np.uint8), "RGB")
    im.info = {}
    kw: dict[str, Any] = {"quality": quality, "subsampling": 0}
    if icc_profile:
        kw["icc_profile"] = icc_profile
    buf = io.BytesIO()
    im.save(buf, "JPEG", **kw)
    data = buf.getvalue()
    left = jpeg_metadata_segments(data)
    if left:   # privacy: never write a photo that still carries metadata
        raise RuntimeError(f"the encoded JPEG still carries metadata segments {left}")
    return data


# ------------------------------------------------------------------------------------------------- EXIF reading
def _num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _text(v: Any) -> str | None:
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    if v is None:
        return None
    s = str(v).replace("\x00", "").strip()
    return s or None


def _offset(s: str | None) -> timezone | None:
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", s or "")
    if not m:
        return None
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return timezone(-delta if m.group(1) == "-" else delta)


def capture_time(exif_ifd: dict, fallback: Path) -> tuple[datetime, str]:
    """When the photo was taken, in UTC, and where that came from."""
    raw = _text(exif_ifd.get(T_DATETIME_ORIGINAL))
    if raw:
        try:
            t = datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
        except ValueError:
            t = None
        if t is not None:
            sub = _text(exif_ifd.get(T_SUBSEC_ORIGINAL))
            if sub and sub.isdigit():
                t = t.replace(microsecond=int(sub[:6].ljust(6, "0")))
            tz = _offset(_text(exif_ifd.get(T_OFFSET_ORIGINAL)))
            if tz is not None:
                return t.replace(tzinfo=tz).astimezone(UTC), "exif DateTimeOriginal + OffsetTimeOriginal"
            return (t.astimezone().astimezone(UTC),
                    "exif DateTimeOriginal without a UTC offset: read in this computer's time zone")
    return datetime.fromtimestamp(fallback.stat().st_mtime, UTC), "file modification time (no EXIF capture time)"


def estimate_intrinsics(exif_ifd: dict, width: int, height: int) -> tuple[dict[str, float] | None, str | None]:
    """Pinhole intrinsics (pixels of the upright width x height image) from EXIF, and how. None when EXIF does not
    say enough. The principal point is the image centre (EXIF does not record it); no lens distortion is modelled.

    35 mm equivalent (iPhone always writes it): f_px = f35 * image diagonal / 43.27 mm (the full-frame diagonal).
    Else focal length (mm) x focal-plane resolution (pixels per mm), scaled to this image's size."""
    diag = math.hypot(width, height)
    f35 = _num(exif_ifd.get(T_FOCAL_35MM))
    if f35:
        f = f35 * diag / FULL_FRAME_DIAGONAL_MM
        method = f"35 mm-equivalent focal length {f35:g} mm x image diagonal / {FULL_FRAME_DIAGONAL_MM:.2f} mm"
    else:
        fmm, xres = _num(exif_ifd.get(T_FOCAL_LENGTH)), _num(exif_ifd.get(T_FPLANE_XRES))
        unit = MM_PER_UNIT.get(int(exif_ifd.get(T_FPLANE_UNIT) or 2))
        px_w = _num(exif_ifd.get(T_PIXEL_X))
        if not (fmm and xres and unit):
            return None, None
        f = fmm * xres / unit
        if px_w:   # the resolution is for the recorded pixel width; rescale to this image (either orientation)
            f *= max(width, height) / max(px_w, _num(exif_ifd.get(T_PIXEL_Y)) or 0.0)
        method = f"focal length {fmm:g} mm x focal-plane resolution {xres:g} px per {unit:g} mm"
    return ({"fx": round(f, 3), "fy": round(f, 3), "cx": width / 2.0, "cy": height / 2.0},
            method + "; principal point at the image centre; distortion not modelled")


# ------------------------------------------------------------------------------------------------- decoding
def sips_to_png(src: Path, dest_dir: Path) -> Path:
    """HEIC/HEIF -> lossless PNG with macOS `sips` (keeps the EXIF so it can be read, keeps full resolution)."""
    sips = shutil.which("sips")
    if sips is None:
        raise ToolMissing(f"{src.name}: HEIC photos need macOS `sips` to convert them, and it is not on this "
                          "computer. Convert the HEIC files to JPEG first (on the iPhone: Settings > Camera > "
                          "Formats > Most Compatible, or export as JPEG), then import again.")
    out = dest_dir / f"{hashlib.sha1(str(src).encode()).hexdigest()[:12]}.png"
    res = subprocess.run([sips, "-s", "format", "png", str(src), "--out", str(out)], capture_output=True, text=True)
    if res.returncode != 0 or not out.is_file():
        raise ImportFormatError(f"{src.name}: sips could not convert it ({(res.stderr or res.stdout).strip()})")
    return out


def _metadata_kinds(im, exif) -> list[str]:
    kinds = []
    if len(exif):
        kinds.append("EXIF")
    if exif.get_ifd(IFD_GPS):
        kinds.append("GPS")
    for key, name in (("xmp", "XMP"), ("XML:com.adobe.xmp", "XMP"), ("photoshop", "IPTC"), ("comment", "comment")):
        if im.info.get(key) and name not in kinds:
            kinds.append(name)
    for marker, body in getattr(im, "applist", []):
        if marker == "APP13" and "IPTC" not in kinds:
            kinds.append("IPTC")
        if marker == "APP1" and body.startswith(b"http://ns.adobe.com/xap") and "XMP" not in kinds:
            kinds.append("XMP")
    return kinds


def read_photo(src: Path, tmp: Path, sha256: str = "") -> Photo:
    """A photo's EXIF facts and upright size (HEIC is converted with sips into `tmp` first)."""
    from PIL import Image, UnidentifiedImageError

    fmt = "heic" if src.suffix.lower() in HEIC_EXTS else "jpeg"
    path = sips_to_png(src, tmp) if fmt == "heic" else src
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            exif_ifd = dict(exif.get_ifd(IFD_EXIF))
            gps = bool(exif.get_ifd(IFD_GPS))
            kinds = _metadata_kinds(im, exif)
            orientation = int(exif.get(T_ORIENTATION) or 1)
            w, h = im.size
    except UnidentifiedImageError:
        raise ImportFormatError(f"{src.name}: not a readable image") from None
    if orientation in (5, 6, 7, 8):   # stored sideways: upright it is the other way round
        w, h = h, w
    captured_at, captured_src = capture_time(exif_ifd, src)
    intr, method = estimate_intrinsics(exif_ifd, w, h)
    kept = {"make": _text(exif.get(T_MAKE)), "model": _text(exif.get(T_MODEL)),
            "software": _text(exif.get(T_SOFTWARE)), "lens_model": _text(exif_ifd.get(T_LENS_MODEL)),
            "focal_length_mm": _num(exif_ifd.get(T_FOCAL_LENGTH)),
            "focal_length_35mm_equivalent_mm": _num(exif_ifd.get(T_FOCAL_35MM))}
    return Photo(source=src, fmt=fmt, decoded_from=path, sha256=sha256, width=w, height=h,
                 orientation=orientation, captured_at=captured_at,
                 captured_at_source=captured_src, exif={k: v for k, v in kept.items() if v is not None},
                 gps_found=gps, metadata_found=kinds, intrinsics=intr,
                 intrinsics_source="exif" if intr else "unknown", intrinsics_method=method)


def upright_rgb(photo: Photo) -> tuple[np.ndarray, bytes | None]:
    """The photo's pixels turned upright (EXIF orientation applied), H x W x 3 uint8, and its ICC profile."""
    from PIL import Image, ImageOps

    with Image.open(photo.decoded_from) as im:
        icc = im.info.get("icc_profile")
        rgb = np.asarray(ImageOps.exif_transpose(im).convert("RGB"))
    if rgb.shape[:2] != (photo.height, photo.width):
        raise ImportFormatError(f"{photo.source.name}: decoded {rgb.shape[1]}x{rgb.shape[0]}, expected "
                                f"{photo.width}x{photo.height}")
    return rgb, icc


def sharpness(rgb: np.ndarray) -> float:
    """images.sharpness (variance of the 4-neighbour Laplacian of the grey image) computed in row strips, so a
    48 MP photo does not need gigabytes of float arrays. Same value as images.sharpness on the whole image."""
    h, w = rgb.shape[:2]
    if h < 3 or w < 3:
        return 0.0
    n, total, total_sq = 0, 0.0, 0.0
    for r0 in range(1, h - 1, SHARPNESS_STRIP_ROWS):
        r1 = min(r0 + SHARPNESS_STRIP_ROWS, h - 1)
        g = images.gray(rgb[r0 - 1:r1 + 1])
        lap = g[1:-1, :-2] + g[1:-1, 2:] + g[:-2, 1:-1] + g[2:, 1:-1] - 4 * g[1:-1, 1:-1]
        n += lap.size
        total += float(lap.sum())
        total_sq += float(np.square(lap).sum())
    mean = total / n
    return max(total_sq / n - mean * mean, 0.0)


# ------------------------------------------------------------------------------------------------- folder
def list_photos(folder: Path) -> tuple[list[Path], list[dict[str, str]]]:
    """The photos in a folder (not its subfolders), by name, and what was skipped and why."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise ImportFormatError(f"{folder}: not a folder (give the folder that holds the photos)")
    photos, skipped = [], []
    for p in sorted(folder.iterdir(), key=lambda q: q.name.lower()):
        if p.name.startswith("."):
            if p.name != ".DS_Store" and not p.name.startswith("._"):
                skipped.append({"file": p.name, "reason": "hidden file"})
            continue
        if p.is_dir():
            skipped.append({"file": p.name, "reason": "a subfolder (only the folder itself is read)"})
        elif p.suffix.lower() in PHOTO_EXTS:
            photos.append(p)
        else:
            skipped.append({"file": p.name, "reason": "not a JPEG or HEIC photo (e.g. a Live Photo video, PNG or "
                                                      "screenshot)"})
    if not photos:
        raise ImportFormatError(f"{folder}: no JPEG or HEIC photos in it (.jpg, .jpeg, .heic, .heif)")
    return photos, skipped


def session_dir(out: Path | None, out_root: Path, now: datetime | None = None) -> Path:
    """The session folder to write: `out` (its name is the session id), or a new id under `out_root`."""
    if out is None:
        return Path(out_root) / ids.new_session_id(now)
    out = Path(out).expanduser()
    if not SESSION_ID_RE.match(out.name):
        raise ImportFormatError(f"--out {out}: the folder name is the session id and must look like sess_<letters, "
                                f"digits, _ or ->, e.g. {out.parent / ids.new_session_id(now)}")
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} already exists and is not empty")
    return out


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _device(meta: CaptureMeta, photos: list[Photo]) -> tuple[dict[str, str], str]:
    models = Counter(p.exif.get("model") for p in photos if p.exif.get("model"))
    ios_versions = Counter(p.exif["software"] for p in photos
                           if re.fullmatch(r"\d+(\.\d+){0,2}", p.exif.get("software", "")))
    model, ios_v = meta.device_model, meta.ios
    src = "operator"
    if model == "unknown" and len(models) == 1:
        model, src = next(iter(models)), "exif Model (a marketing name, not a hardware identifier)"
    if ios_v == "unknown" and len(ios_versions) == 1:
        ios_v = next(iter(ios_versions))
    device = {"model": model, "ios": ios_v}
    if meta.app_version:
        device["app_version"] = meta.app_version
    return device, src


def import_photos(folder: Path, out: Path | None = None, meta: CaptureMeta | None = None, *,
                  out_root: Path | None = None) -> ImportResult:
    """Import every JPEG/HEIC photo in `folder` into the session folder `out` (or a new id under `out_root`)."""
    from repairos.config import ROOT

    meta = meta or CaptureMeta()
    folder = Path(folder).expanduser()
    files, skipped = list_photos(folder)
    target = session_dir(out, out_root or ROOT / "samples" / "sessions")

    with tempfile.TemporaryDirectory(prefix="import-photos-") as tmp:
        photos: list[Photo] = []
        seen: dict[str, str] = {}
        for f in files:
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            if digest in seen:
                skipped.append({"file": f.name, "reason": f"the same file as {seen[digest]}"})
                continue
            seen[digest] = f.name
            photos.append(read_photo(f, Path(tmp), digest))
        photos.sort(key=lambda p: (p.captured_at, p.source.name.lower()))
        existed = target.exists()
        try:
            return _write(photos, files, skipped, folder, target, meta)
        except BaseException:
            if not existed:   # never leave a half-written session behind
                shutil.rmtree(target, ignore_errors=True)
            raise


def _write(photos: list[Photo], files: list[Path], skipped: list[dict[str, str]], folder: Path, target: Path,
           meta: CaptureMeta) -> ImportResult:
    from repairos.sessions.validate import validate_session

    session_id = target.name
    scan_id = ids.new_scan_id()
    captured_at = meta.captured_at or photos[0].captured_at
    device, device_src = _device(meta, photos)
    w = SessionWriter(target.parent, session_id)
    mem: dict[str, bytes] = {}

    def put(data: bytes) -> str:
        key = f"mem:{len(mem)}"
        mem[key] = data
        return key

    def fetch(key: str) -> tuple[bytes, str]:
        data = mem.pop(key)   # written once; dropping it keeps one photo's JPEG in memory at a time
        return data, ext_for(data)

    w.write_session({"device": dict(device), "source": "converter", "created_at": _iso(datetime.now(UTC)),
                     "symptom": meta.symptom, "product_id": meta.product_id, "object_id": meta.object_id,
                     "operator": meta.operator, "lighting": meta.lighting,
                     "notes": meta.notes or "plain iPhone photos imported by tools/import_photos.py: no poses, no "
                                            "depth, metadata removed (see import.json)"})
    pkg: dict[str, Any] = {"scan_id": scan_id, "product_id": meta.product_id or "unidentified",
                           "session_id": session_id, "captured_at": _iso(captured_at), "device": device,
                           "frame": "arkit_world", "units": "m", "depth_source": "none",
                           "gravity_arkit_world": GRAVITY_ARKIT_WORLD, "keyframes": [],
                           "supporting_plane_removed": False}
    if meta.symptom:
        pkg["symptom"] = meta.symptom
    w.write_scan(pkg, fetch)

    records = []
    for i, p in enumerate(photos, start=1):
        rgb, icc = upright_rgb(p)
        still = {"still_id": ids.still_id(i), "scan_id": scan_id, "session_id": session_id, "kind": "unguided",
                 "captured_at": _iso(p.captured_at), "image_file": put(clean_jpeg(rgb, icc)),
                 "width_px": p.width, "height_px": p.height, "pose_reliable": False, "targets": [],
                 "tracking_state": "unknown", "sharpness": round(sharpness(rgb), 3)}
        del rgb
        if p.intrinsics:
            still["intrinsics"] = p.intrinsics
        w.write_still(still, fetch)
        records.append({"still_id": still["still_id"], "source_file": p.source.name, "source_format": p.fmt,
                        "source_sha256": p.sha256,
                        "converted_with": "sips (HEIC -> lossless PNG), then one JPEG encode" if p.fmt == "heic"
                        else None,
                        "width_px": p.width, "height_px": p.height, "exif_orientation": p.orientation,
                        "orientation_applied": p.orientation != 1, "captured_at": still["captured_at"],
                        "captured_at_source": p.captured_at_source, "intrinsics_source": p.intrinsics_source,
                        "intrinsics_method": p.intrinsics_method, "exif_read": p.exif,
                        "metadata_removed": p.metadata_found, "gps_removed": p.gps_found})
    prov = {
        "source_app": "iPhone Camera (plain photos)", "importer": "repairos.importers.photos",
        "source_folder": folder.name, "scan_id": scan_id, "depth_source": "none",
        "photos": {"found": len(files), "imported": len(photos), "skipped": skipped},
        "jpeg": {"quality": JPEG_QUALITY, "subsampling": "4:4:4", "resolution": "full (as taken)",
                 "orientation": "EXIF orientation applied to the pixels, so every still is upright"},
        "privacy": {"metadata_removed": "all: EXIF (camera, time, lens), GPS location, XMP, IPTC, maker notes, "
                                        "embedded thumbnails and comments are not in the written JPEGs",
                    "kept": "the ICC colour profile only (colour, not personal data)",
                    "verified": "every written JPEG was re-read: no APP1/APP13/other metadata segment",
                    "photos_with_gps_removed": sum(p.gps_found for p in photos),
                    "kept_in_json": "capture time per still (still.captured_at), camera model and iOS version "
                                    "(session.json device), never a location"},
        "intrinsics": {"source": sorted({p.intrinsics_source for p in photos}),
                       "note": "estimated from EXIF before it was removed; absent on a still when unknown"},
        "device_model_source": device_src,
        "captured_at_source": "operator" if meta.captured_at else "the earliest photo's capture time",
        "stills": records,
        "assumptions": [
            "plain photos have no camera pose: every still is unguided, pose_reliable false, no camera; no pose is "
            "made up",
            "no AR session and no depth: the scan package has depth_source none and no keyframes; its frame "
            "(arkit_world) and gravity_arkit_world are the contract's fixed convention, not a measurement",
            "the location of a finding cannot be computed from these photos: the report says location not computed",
            "tracking state does not apply to plain photos: written as unknown",
            "stills are ordered by capture time",
        ],
    }
    (w.dir / "import.json").write_text(json.dumps(prov, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return ImportResult(w.dir, session_id, scan_id, len(photos), None, validate_session(w.dir))
