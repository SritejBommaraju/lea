"""Write a session folder (samples/README.md). Used by the gateway (every object it receives, so a real run is
replayable), the phone exporter (C1.6) and the converters (C1.7).

`fetch(value) -> (bytes, extension)` resolves a file field: a blob ref for the gateway, a local path for a
converter. Files land at their place in the layout; JSON is the contract object with relative paths.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from repairos.sessions.layout import map_files

Fetch = Callable[[str], tuple[bytes, str]]


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class SessionWriter:
    def __init__(self, root: Path, session_id: str):
        self.session_id = session_id
        self.dir = Path(root) / session_id
        self.dir.mkdir(parents=True, exist_ok=True)

    def _files(self, kind: str, data: dict, fetch: Fetch) -> dict:
        def put(value: str, base: str) -> str:
            body, ext = fetch(value)
            rel = base + ext
            dest = self.dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists() or dest.stat().st_size != len(body):
                dest.write_bytes(body)
            return rel
        return map_files(kind, data, put)

    def write_session(self, meta: dict) -> None:
        path = self.dir / "session.json"
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        _write_json(path, {**current, **{k: v for k, v in meta.items() if v is not None},
                           "session_id": self.session_id})

    def write_scan(self, pkg: dict, fetch: Fetch) -> dict:
        out = self._files("scan-package", pkg, fetch)
        _write_json(self.dir / "scans" / pkg["scan_id"] / "scan_package.json", out)
        return out

    def write_still(self, still: dict, fetch: Fetch) -> dict:
        out = self._files("still", still, fetch)
        _write_json(self.dir / "stills" / f"{still['still_id']}.json", out)
        return out

    def write_reading(self, reading: dict) -> None:
        _write_json(self.dir / "readings" / f"{reading['reading_id']}.json", reading)

    def write_part(self, part: dict) -> None:
        _write_json(self.dir / "parts" / f"{part['item_id']}.json", part)

    def write_truth(self, truth: dict) -> None:
        _write_json(self.dir / "truth.json", truth)
