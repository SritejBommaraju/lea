"""The shared spend guard (repairos.spend), checked BEFORE every paid call the voice routes make: each Cartesia
TTS/STT request and each Claude answer. Cached lines never reach Cartesia, so they never count.

repairos.spend is imported lazily: while it does not exist, every call is allowed. A refusal becomes VoicePaused,
which the routes turn into the phone's own voice (503 `voice_paused` for TTS/STT) or a plain spoken "paused"
answer; nothing retries.
"""
from __future__ import annotations

import hashlib


class VoicePaused(RuntimeError):
    def __init__(self, service: str, reason: str, retry_after_s: float | None):
        self.service, self.reason, self.retry_after_s = service, reason, retry_after_s
        super().__init__(f"{service} paused: {reason}")


def key_of(*parts: object) -> str:
    return hashlib.sha256("\n".join(map(str, parts)).encode()).hexdigest()[:32]


def charge(service: str, *, key: str, units: float = 1.0) -> None:
    """Raises VoicePaused when the guard refuses `service` now; returns quietly when it allows (or is absent)."""
    try:
        from repairos import spend
    except ImportError:
        return
    limit = getattr(spend, "SpendLimit", None)
    try:
        spend.charge(service, units, key=key)
    except Exception as e:
        if limit is not None and isinstance(e, limit):
            raise VoicePaused(service, str(getattr(e, "reason", e)), getattr(e, "retry_after_s", None)) from e
        raise
