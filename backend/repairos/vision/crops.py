"""Full-resolution crops (BUILD M.SEG.0 crop rules, V3.5).

Crops are cut from the ORIGINAL still. The whole still is never downscaled before cropping: a small part that is
40 px wide at 4032 px would be 10 px at 1024 px and lost. Only the cut crop may be downsized (to `max_side`), never
upscaled. Every crop keeps the transform back to still pixels so findings are stored in full-resolution coordinates.

Rule: region = box padded by `pad_px` (pose/box uncertainty, 40 px at 4032 px wide, scaled with the still width)
plus `pad_frac` (15 %) of the box side, made square, at least `min_side`, shifted (not truncated) to stay inside the
still, and clamped only when the still itself is smaller than the square.

Pillow (HPND, permissive) is imported lazily; it comes with the `vision` extra.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

REFERENCE_WIDTH_PX = 4032
DEFAULT_PAD_PX = 40
DEFAULT_PAD_FRAC = 0.15
DEFAULT_MIN_SIDE = 256
DEFAULT_MAX_SIDE = 1024

Box = tuple[float, float, float, float]


def load_still(path: str | Path) -> Any:
    """The still as a full-resolution RGB PIL image, EXIF orientation applied (so pixels match width_px/height_px)."""
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        return im.convert("RGB")


def as_pil(image: Any) -> Any:
    """PIL image from a PIL image or an H x W x 3 uint8 array."""
    from PIL import Image

    if isinstance(image, Image.Image):
        return image if image.mode == "RGB" else image.convert("RGB")
    return Image.fromarray(image).convert("RGB")


def crop_region(box: Box, still_w: int, still_h: int, *, pad_px: float | None = None,
                pad_frac: float = DEFAULT_PAD_FRAC, min_side: int = DEFAULT_MIN_SIDE,
                square: bool = True) -> tuple[int, int, int, int]:
    """Integer [x0, y0, x1, y1) region of the still to cut for `box` (still pixels)."""
    x0, y0, x1, y1 = (float(v) for v in box)
    if not (x1 > x0 and y1 > y0):
        raise ValueError(f"empty box {box}")
    if pad_px is None:
        pad_px = DEFAULT_PAD_PX * still_w / REFERENCE_WIDTH_PX
    bw, bh = x1 - x0, y1 - y0
    w = bw + 2 * pad_px + 2 * pad_frac * bw
    h = bh + 2 * pad_px + 2 * pad_frac * bh
    if square:
        w = h = max(w, h)
    w, h = max(w, min_side), max(h, min_side)
    w, h = min(w, still_w), min(h, still_h)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rx0 = min(max(cx - w / 2, 0.0), still_w - w)
    ry0 = min(max(cy - h / 2, 0.0), still_h - h)
    ix0, iy0 = int(round(rx0)), int(round(ry0))
    ix1, iy1 = min(int(round(rx0 + w)), still_w), min(int(round(ry0 + h)), still_h)
    return ix0, iy0, ix1, iy1


@dataclass(frozen=True)
class Crop:
    image: Any                       # PIL image: the cut region, downsized to max_side if larger, never upscaled
    region_px: tuple[int, int, int, int]  # [x0, y0, x1, y1) in full-resolution still pixels
    scale: float                     # crop pixels per still pixel (<= 1)
    box_px: Box                      # the detection box this crop is about, still pixels
    still_id: str | None = None

    def to_still(self, u: float, v: float) -> tuple[float, float]:
        """Crop pixel -> still pixel."""
        return self.region_px[0] + u / self.scale, self.region_px[1] + v / self.scale

    def from_still(self, x: float, y: float) -> tuple[float, float]:
        """Still pixel -> crop pixel: ((x - x0) * s, (y - y0) * s)."""
        return (x - self.region_px[0]) * self.scale, (y - self.region_px[1]) * self.scale

    def box_to_still(self, box: Box) -> Box:
        a, b = self.to_still(box[0], box[1])
        c, d = self.to_still(box[2], box[3])
        return a, b, c, d

    def box_in_crop(self) -> Box:
        a, b = self.from_still(self.box_px[0], self.box_px[1])
        c, d = self.from_still(self.box_px[2], self.box_px[3])
        return a, b, c, d


def cut(still: Any, box: Box, *, still_id: str | None = None, max_side: int = DEFAULT_MAX_SIDE,
        **region_kw) -> Crop:
    """Cut the crop for `box` from the full-resolution `still` (PIL image or array)."""
    from PIL import Image

    im = as_pil(still)
    region = crop_region(box, im.width, im.height, **region_kw)
    piece = im.crop(region)
    side = max(piece.width, piece.height)
    scale = 1.0
    if side > max_side:
        scale = max_side / side
        piece = piece.resize((max(1, round(piece.width * scale)), max(1, round(piece.height * scale))),
                             Image.Resampling.LANCZOS)
    return Crop(image=piece, region_px=region, scale=scale, box_px=tuple(float(v) for v in box), still_id=still_id)


def context_view(still: Any, box: Box | None = None, *, max_side: int = 1024) -> Any:
    """A downsized whole-still view for VLM CONTEXT ONLY (where the crop sits), the box outlined. Never cropped
    from: crops always come from the full-resolution still via `cut`."""
    from PIL import Image, ImageDraw

    im = as_pil(still)
    s = min(1.0, max_side / max(im.width, im.height))
    view = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.Resampling.LANCZOS) \
        if s < 1 else im.copy()
    if box is not None:
        d = ImageDraw.Draw(view)
        lw = max(2, round(max(view.size) / 300))
        d.rectangle([box[0] * s, box[1] * s, box[2] * s, box[3] * s], outline=(255, 0, 255), width=lw)
    return view


def jpeg_bytes(image: Any, quality: int = 90) -> bytes:
    import io

    buf = io.BytesIO()
    as_pil(image).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
