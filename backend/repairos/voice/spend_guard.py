"""The shared spend guard (repairos.spend), checked BEFORE every paid call the voice routes make: each Cartesia
TTS/STT request and each Claude answer. Cached lines never reach Cartesia, so they never count.

repairos.spend is imported lazily: while it does not exist, every call is allowed. A refusal becomes VoicePaused,
which the routes turn into the phone's own voice (503 `voice_paused` for TTS/STT) or a plain spoken "paused"
answer; nothing retries. A Claude answer's measured token cost then replaces the estimate (record_claude).
"""
from __future__ import annotations

import hashlib
import logging

log = logging.getLogger(__name__)


class VoicePaused(RuntimeError):
    def __init__(self, service: str, reason: str, retry_after_s: float | None):
        self.service, self.reason, self.retry_after_s = service, reason, retry_after_s
        super().__init__(f"{service} paused: {reason}")


def key_of(*parts: object) -> str:
    return hashlib.sha256("\n".join(map(str, parts)).encode()).hexdigest()[:32]


def charge(service: str, *, key: str, units: float = 1.0) -> float:
    """Raises VoicePaused when the guard refuses `service` now; when it allows (or is absent) returns the estimated
    USD it charged, which record_claude later replaces with the measured cost."""
    try:
        from repairos import spend
    except ImportError:
        return 0.0
    limit = getattr(spend, "SpendLimit", None)
    estimate = getattr(spend, "estimate_usd", None)
    est = float(estimate(service)) if estimate else 0.0
    try:
        if estimate:
            spend.charge(service, units, usd=est, key=key)
        else:
            spend.charge(service, units, key=key)
    except Exception as e:
        if limit is not None and isinstance(e, limit):
            raise VoicePaused(service, str(getattr(e, "reason", e)), getattr(e, "retry_after_s", None)) from e
        raise
    return est


def record_claude(models: tuple[str | None, ...], usage: object, estimate: float) -> None:
    """After a Claude answer: its measured cost from the response's token counts (repairos.spend.anthropic_usd,
    priced by the first of `models` with a price: the model that answered, then the one asked) replaces the
    estimate `charge` returned, as every other Claude caller does. No usage or no price: the estimate stands. Never
    raises: a bookkeeping error must not cost the person their answer."""
    if usage is None:
        return
    try:
        from repairos import spend
    except ImportError:
        return
    price, record = getattr(spend, "anthropic_usd", None), getattr(spend, "record_cost", None)
    if price is None or record is None:
        return
    counts = {k: getattr(usage, k, 0) or 0 for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                                       "cache_read_input_tokens")}
    try:
        usd = next((u for m in models if m and (u := price(m, counts)) is not None), None)
        if usd is not None:
            record("anthropic", usd, estimate=estimate)
    except Exception as e:  # noqa: BLE001
        log.warning("voice: could not record the Claude cost: %s", e)
