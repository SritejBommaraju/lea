"""Zip one scan of a session folder (samples/README.md layout) for the design engine.

Root entries: `session.json` when there is one, `scans/<scan_id>/...` (scan_package.json, the PLY, keyframes), the
`stills/` that belong to the scan and the `readings/` typed or read for it. The `grain/` folder (the design engine's own
answers) is never sent back. Entries are written in sorted order with a fixed timestamp, so the same folder always zips
to the same bytes and the design engine can tell a retry from a new upload.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

FIXED_TIME = (2020, 1, 1, 0, 0, 0)


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def session_files(session_dir: Path, scan_id: str) -> list[Path]:
    """The files of `scan_id` in the session folder, relative paths sorted."""
    session_dir = Path(session_dir)
    out: list[Path] = []
    if (session_dir / "session.json").is_file():
        out.append(Path("session.json"))
    scan_dir = session_dir / "scans" / scan_id
    out += sorted(p.relative_to(session_dir) for p in scan_dir.rglob("*") if p.is_file())
    stills_dir = session_dir / "stills"
    if stills_dir.is_dir():
        mine = {p.stem for p in stills_dir.glob("*.json") if _json(p).get("scan_id") == scan_id}
        out += sorted(p.relative_to(session_dir) for p in stills_dir.iterdir()
                      if p.is_file() and any(p.stem == sid or p.stem.startswith(sid + "_") for sid in mine))
    readings_dir = session_dir / "readings"
    if readings_dir.is_dir():
        out += sorted(p.relative_to(session_dir) for p in readings_dir.glob("*.json")
                      if _json(p).get("scan_id") == scan_id)
    return out


def zip_session(session_dir: Path, scan_id: str) -> bytes:
    """The zip bytes; ValueError when the scan has no folder (nothing was received for it)."""
    session_dir = Path(session_dir)
    if not (session_dir / "scans" / scan_id / "scan_package.json").is_file():
        raise ValueError(f"no scan {scan_id} in {session_dir.name}: its scan_package.json was not recorded")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for rel in session_files(session_dir, scan_id):
            info = zipfile.ZipInfo(rel.as_posix(), FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, (session_dir / rel).read_bytes(), compresslevel=1)
    return buf.getvalue()
