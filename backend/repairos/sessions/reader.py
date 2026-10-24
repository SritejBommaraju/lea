"""Read a recorded session folder (samples/README.md) as contract objects.

    s = open_session("sess_20261024_1a2b")          # samples/sessions/<id>, or any folder path
    s.scans(), s.stills(), s.readings(), s.parts()  # validated contract dicts (paths relative to s.dir)

Held-out sessions (split: heldout) are refused unless `allow_heldout=True`; only `make eval` passes it (M5).
"""
from __future__ import annotations

import json
from functools import cached_property
from pathlib import Path
from typing import Any

from repairos import contracts
from repairos.config import ROOT

SAMPLES = ROOT / "samples" / "sessions"


class SessionError(ValueError):
    pass


class HeldoutSession(SessionError):
    """A held-out session was opened outside the evaluation (M5)."""


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SessionError(f"{path}: {e}") from None


class Session:
    def __init__(self, path: Path):
        self.dir = Path(path)
        if not (self.dir / "session.json").is_file():
            raise SessionError(f"{self.dir}: no session.json")
        self.meta: dict[str, Any] = _load(self.dir / "session.json")

    @property
    def session_id(self) -> str:
        return self.meta.get("session_id", self.dir.name)

    @property
    def split(self) -> str:
        return self.meta.get("split", "unassigned")

    @cached_property
    def truth(self) -> dict[str, Any] | None:
        p = self.dir / "truth.json"
        return _load(p) if p.is_file() else None

    def _objects(self, pattern: str, kind: str) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.dir.glob(pattern)):
            data = _load(p)
            contracts.validate(kind, data)
            out.append(data)
        return out

    def scans(self) -> list[dict[str, Any]]:
        return self._objects("scans/*/scan_package.json", "scan-package")

    def stills(self) -> list[dict[str, Any]]:
        return self._objects("stills/*.json", "still")

    def readings(self) -> list[dict[str, Any]]:
        return self._objects("readings/*.json", "meter-reading")

    def parts(self) -> list[dict[str, Any]]:
        return self._objects("parts/*.json", "part-on-hand")

    def file(self, rel: str) -> Path:
        p = (self.dir / rel).resolve()
        if self.dir.resolve() not in p.parents:
            raise SessionError(f"{rel!r} points outside the session folder")
        return p


def session_path(ref: str | Path, root: Path = SAMPLES) -> Path:
    p = Path(ref)
    return p if p.is_dir() else Path(root) / str(ref)


def open_session(ref: str | Path, *, root: Path = SAMPLES, allow_heldout: bool = False) -> Session:
    s = Session(session_path(ref, root))
    if s.split == "heldout" and not allow_heldout:
        raise HeldoutSession(f"{s.session_id} is held out: only the evaluation (make eval) may open it")
    return s


def list_sessions(root: Path = SAMPLES, *, include_heldout: bool = False) -> list[Path]:
    """Session folders under `root` (held-out ones only when asked)."""
    out = []
    for p in sorted(Path(root).glob("*/session.json")):
        split = _load(p).get("split")
        if include_heldout or split != "heldout":
            out.append(p.parent)
    return out
