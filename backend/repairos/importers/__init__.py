"""Capture importers (PLAN.md task B12): convert another capture app's export into a recorded session folder
(samples/README.md) that passes `make sessions-check`.

    from repairos.importers import import_capture
    result = import_capture(Path("71de12f9"), Path("samples/sessions"))   # format detected from the files

Supported (both MIT): Stray Scanner (LiDAR: video, depth, confidence, poses -> fused PLY + keyframe stills) and
NeRFCapture offline (any ARKit iPhone: posed images -> stills, `depth_source: none`, no PLY). Frame conventions:
`repairos.locate.camera`; per-app details: `repairos.importers.stray`, `repairos.importers.nerfcapture`.
"""
from __future__ import annotations

from pathlib import Path

from repairos.importers.images import ImportFormatError
from repairos.importers.session import CaptureMeta, ImportResult

FORMATS = ("stray", "nerfcapture")


def detect_format(src: Path) -> str:
    from repairos.importers.nerfcapture import is_nerfcapture_export
    from repairos.importers.stray import is_stray_export

    src = Path(src)
    if src.is_dir() and is_stray_export(src):
        return "stray"
    if is_nerfcapture_export(src):
        return "nerfcapture"
    raise ImportFormatError(f"{src}: neither a Stray Scanner export (odometry.csv + rgb.mp4) nor a NeRFCapture "
                            "export (transforms.json, folder or zip)")


def import_capture(src: Path, out_root: Path, meta: CaptureMeta | None = None, *, fmt: str | None = None,
                   **options) -> ImportResult:
    fmt = fmt or detect_format(src)
    if fmt == "stray":
        from repairos.importers.stray import import_stray
        return import_stray(src, out_root, meta, **options)
    if fmt == "nerfcapture":
        from repairos.importers.nerfcapture import import_nerfcapture
        return import_nerfcapture(src, out_root, meta, **options)
    raise ValueError(f"unknown format {fmt!r}; one of {FORMATS}")


__all__ = ["FORMATS", "CaptureMeta", "ImportFormatError", "ImportResult", "detect_format", "import_capture"]
