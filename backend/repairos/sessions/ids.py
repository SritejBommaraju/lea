"""Ids for recorded sessions (BUILD.md B0.7). Opaque and random: never a product name (GEN1)."""
from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime

SESSION_RE = re.compile(r"^sess_[A-Za-z0-9_-]+$")
OBJECT_RE = re.compile(r"^obj_[0-9a-f]{8}$")


def new_session_id(when: datetime | None = None) -> str:
    return f"sess_{(when or datetime.now(UTC)):%Y%m%d}_{secrets.token_hex(2)}"


def new_scan_id() -> str:
    return f"scan_{secrets.token_hex(4)}"


def new_object_id() -> str:
    return f"obj_{secrets.token_hex(4)}"


def still_id(n: int) -> str:
    return f"s{n}"


def is_session_id(s: str) -> bool:
    return bool(SESSION_RE.match(s))
