#!/usr/bin/env python3
"""Import a folder of plain iPhone Camera photos (JPEG or HEIC) as a recorded session (PLAN.md §6a step 1).

    python3 -I tools/import_photos.py <folder> [--out samples/sessions/sess_<name>] [--symptom "won't turn on"]
        [--device-model iPhone16,1] [--ios 18.1] [--object-id obj_1a2b3c4d] [--product-id ...] [--operator ...]
        [--lighting room|torch|polarised] [--notes ...]
    make replay SESSION=sess_<name>

Every photo becomes an `unguided` still with no pose (`pose_reliable: false`) in a scan with `depth_source: none`,
so detection, Jev and the report run, and the report says the location was not computed. HEIC needs macOS `sips`.
All EXIF, GPS and other metadata is removed from the copied JPEGs (full resolution, quality 92); `import.json` in
the session says what was read, removed and assumed. The session is validated like `make sessions-check`;
problems are printed and the exit code is 1. Runs itself with backend/.venv/bin/python (`make bootstrap`).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / "backend" / ".venv"


def _in_venv() -> None:
    """Re-run with the backend venv's Python when started with another one (e.g. `python3 -I`)."""
    try:
        import numpy  # noqa: F401
        import PIL  # noqa: F401
        import repairos  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    py = VENV / "bin" / "python"
    if py.exists() and Path(sys.prefix).resolve() != VENV.resolve():
        os.execv(str(py), [str(py), "-I", str(Path(__file__).resolve()), *sys.argv[1:]])
    sys.exit("import_photos: needs the backend venv with Pillow and numpy: run `make bootstrap` first")


def main(argv: list[str] | None = None) -> int:
    from repairos.importers import CaptureMeta, ImportFormatError
    from repairos.importers.photos import ToolMissing, import_photos

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folder", type=Path, help="the folder that holds the photos (AirDrop, Image Capture, Files)")
    ap.add_argument("--out", type=Path, help="the session folder; its name is the session id (sess_...). Default: "
                                             "a new samples/sessions/sess_<date>_<hex>")
    ap.add_argument("--symptom", help="one line from the user, e.g. \"won't turn on\"")
    ap.add_argument("--device-model", default="unknown", help="default: the EXIF camera model")
    ap.add_argument("--ios", default="unknown", help="default: the EXIF software version")
    ap.add_argument("--product-id")
    ap.add_argument("--object-id")
    ap.add_argument("--operator")
    ap.add_argument("--lighting", choices=("room", "torch", "polarised"))
    ap.add_argument("--notes")
    a = ap.parse_args(argv)

    meta = CaptureMeta(device_model=a.device_model, ios=a.ios, symptom=a.symptom, product_id=a.product_id,
                       object_id=a.object_id, operator=a.operator, lighting=a.lighting, notes=a.notes)
    out = a.out if a.out is None or a.out.is_absolute() else Path.cwd() / a.out
    try:
        res = import_photos(a.folder, out, meta)
    except (ImportFormatError, ToolMissing, FileExistsError) as e:
        print(f"import failed: {e}", file=sys.stderr)
        return 1
    print(f"{'FAIL' if res.problems else 'ok  '} {res.path}  (photos: scan {res.scan_id}, {res.stills} stills, "
          "no poses, no depth, metadata removed)")
    for msg in res.problems:
        print(f"     - {msg}")
    if not res.problems:
        here = res.path.parent.resolve() == (ROOT / "samples" / "sessions").resolve()
        print(f"next: make replay SESSION={res.session_id if here else res.path}")
    return 1 if res.problems else 0


if __name__ == "__main__":
    _in_venv()
    sys.exit(main())
