"""Image helpers for importers: sharpness, JPEG encoding, depth and confidence PNG decoding.

Pillow (MIT-CMU, permissive) and numpy (BSD-3) are imported lazily; both are in requirements.lock.
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np

JPEG_QUALITY = 90   # stills need q >= 85 (still.schema.json)


class ImportFormatError(ValueError):
    """The export is not in the documented format; the message says what was expected."""


def gray(rgb: np.ndarray) -> np.ndarray:
    rgb = np.asarray(rgb, dtype=float)
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def sharpness(rgb: np.ndarray) -> float:
    """Variance of the 4-neighbour Laplacian of the grey image (0-255 scale): higher is sharper."""
    g = gray(rgb) if np.asarray(rgb).ndim == 3 else np.asarray(rgb, dtype=float)
    if g.shape[0] < 3 or g.shape[1] < 3:
        return 0.0
    lap = g[1:-1, :-2] + g[1:-1, 2:] + g[:-2, 1:-1] + g[2:, 1:-1] - 4 * g[1:-1, 1:-1]
    return float(lap.var())


def jpeg_bytes(rgb: np.ndarray, quality: int = JPEG_QUALITY) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(np.asarray(rgb, dtype=np.uint8), "RGB").save(buf, "JPEG", quality=quality, subsampling=0)
    return buf.getvalue()


def read_rgb(path: Path) -> np.ndarray:
    """An image file as H x W x 3 uint8, in its stored pixel orientation (EXIF orientation NOT applied: capture apps
    write the sensor's pixel grid, which is the grid the intrinsics describe)."""
    from PIL import Image

    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"))


def read_depth_mm(path: Path) -> np.ndarray:
    """A 16-bit single-channel depth PNG in millimetres (or a .npy of millimetres) as float metres."""
    if path.suffix == ".npy":
        arr = np.load(path, allow_pickle=False)
    else:
        from PIL import Image

        with Image.open(path) as im:
            if im.mode not in ("I;16", "I;16B", "I;16L", "I"):
                raise ImportFormatError(f"{path.name}: depth PNG mode {im.mode}, expected 16-bit grey (millimetres)")
            arr = np.asarray(im)
    if arr.ndim != 2:
        raise ImportFormatError(f"{path.name}: depth must be one channel, got shape {arr.shape}")
    return arr.astype(np.float32) / 1000.0


def read_confidence(path: Path) -> np.ndarray:
    """An 8-bit ARKit confidence PNG (0 low, 1 medium, 2 high) as uint8."""
    from PIL import Image

    with Image.open(path) as im:
        if im.mode not in ("L", "P", "I", "I;16"):
            raise ImportFormatError(f"{path.name}: confidence PNG mode {im.mode}, expected 8-bit grey")
        arr = np.asarray(im)
    if arr.ndim != 2 or (arr.size and int(arr.max()) > 2):
        raise ImportFormatError(f"{path.name}: confidence values must be 0, 1 or 2 (got max {int(arr.max())})")
    return arr.astype(np.uint8)
