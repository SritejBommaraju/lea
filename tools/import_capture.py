#!/usr/bin/env python3
"""Import another capture app's export as a recorded session (PLAN.md task B12). Format: samples/README.md.

    backend/.venv/bin/python tools/import_capture.py <export> [--out samples/sessions] [--device-model iPhone15,2]
        [--ios 18.1] [--symptom "..."] [--object-id obj_1a2b3c4d] [--product-id ...] [--operator ...]
        [--lighting room|torch|polarised] [--captured-at 2026-10-24T18:10:02Z]
        [--format stray|nerfcapture] [--fuser auto|open3d|numpy] [--spacing-mm 3] [--max-depth-m 3]

Formats (detected from the files): a Stray Scanner export folder (MIT app; LiDAR: fused PLY, a mesh when Open3D is
installed, keyframe stills) or a NeRFCapture offline export, folder or zip (MIT app; any ARKit iPhone: posed stills,
`depth_source: none`, no PLY). Needs `ffmpeg` for Stray videos. The new session folder is validated like
`make sessions-check`; problems are printed and the exit code is 1. Fill in truth.json, split etc. by hand afterwards
(samples/README.md). Only apps whose terms allow it are imported (PLAN.md section 10).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    from repairos.importers import FORMATS, CaptureMeta, ImportFormatError, import_capture
    from repairos.importers.video import ToolMissing

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("export", type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "samples" / "sessions")
    ap.add_argument("--format", choices=FORMATS)
    ap.add_argument("--device-model", default="unknown")
    ap.add_argument("--ios", default="unknown")
    ap.add_argument("--app-version")
    ap.add_argument("--symptom")
    ap.add_argument("--object-id")
    ap.add_argument("--product-id")
    ap.add_argument("--operator")
    ap.add_argument("--lighting", choices=("room", "torch", "polarised"))
    ap.add_argument("--notes")
    ap.add_argument("--captured-at", type=datetime.fromisoformat)
    ap.add_argument("--fuser", choices=("auto", "open3d", "numpy"), default="auto", help="Stray only")
    ap.add_argument("--spacing-mm", type=float, help="Stray only; default: measured from the depth")
    ap.add_argument("--max-depth-m", type=float, default=3.0, help="Stray only")
    a = ap.parse_args(argv)

    meta = CaptureMeta(device_model=a.device_model, ios=a.ios, app_version=a.app_version, symptom=a.symptom,
                       product_id=a.product_id, object_id=a.object_id, operator=a.operator, lighting=a.lighting,
                       notes=a.notes, captured_at=a.captured_at)
    fmt = a.format
    try:
        if fmt is None:
            from repairos.importers import detect_format
            fmt = detect_format(a.export)
        opts = ({"fuser": a.fuser, "spacing_mm": a.spacing_mm, "max_depth_m": a.max_depth_m}
                if fmt == "stray" else {})
        res = import_capture(a.export, a.out, meta, fmt=fmt, **opts)
    except (ImportFormatError, ToolMissing, FileExistsError) as e:
        print(f"import failed: {e}", file=sys.stderr)
        return 1
    print(f"{'FAIL' if res.problems else 'ok  '} {res.path}  ({fmt}: scan {res.scan_id}, {res.stills} stills, "
          f"{res.point_count if res.point_count is not None else 'no'} points)")
    for msg in res.problems:
        print(f"     - {msg}")
    return 1 if res.problems else 0


if __name__ == "__main__":
    sys.exit(main())
