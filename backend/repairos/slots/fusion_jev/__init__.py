"""FUSION=jev: Jev, TypeSafe's hosted System One model (card M.FUSION.1; SPEC A2, S5, A6, Q4).

Stdlib HTTP (urllib), no SDK dependency: the request is one documented JSON POST. Verified against the live docs on
2026-10-24:
  - endpoint  POST https://api.typesafe.ai/v1/systemone, header `Authorization: Bearer <key>`
              https://docs.typesafe.ai/api/system-one.md, https://api.typesafe.ai/openapi.json
  - body      {model, state, questions: {<id>: {type, instructions, criteria}}}; noul criteria {true, false};
              choice criteria {option: description}, 1-255 options, sent in our (seeded, shuffled) order
              https://docs.typesafe.ai/primitives/noul.md, https://docs.typesafe.ai/primitives/choice.md
  - response  {model, answers: {<id>: {type: noul, noul} | {type: choice, choice, confidence, probabilities}},
              usage}; `model` is the versioned id that answered (e.g. jev-1.14.0)
  - models    jev-latest -> jev-1.14.0 (text input, 64k per request / 32k per question)
              https://docs.typesafe.ai/models.md
  - errors    400 / 413 / 415 / 422 = our request is wrong; 401 = key; 429 / 529 = retry with backoff
              https://docs.typesafe.ai/api/errors.md
  - option order bias: https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#choice-option-order
  - SDK (not used): typesafe-sdk on PyPI, MIT, https://github.com/typesafe-ai/typesafe-sdk-python
Owner rules: Jev down is a blocker, never a degrade: every failure raises FusionUnavailable (or FusionBadAnswer /
FusionRequestRejected) and is never turned into an answer. Every exchange is recorded (data/fusion/jev/, gitignored)
with the exact request payload; the key is never written.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repairos.config import ROOT, get_settings
from repairos.registry import register
from repairos.slots.fusion import (Answers, FusionBadAnswer, FusionError, FusionUnavailable, Question,
                                   assert_words_only, check_answers)

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"          # the docs' default alias; the versioned id that answered is recorded per call
TIMEOUT_S = 3.0               # M.FUSION.1: 3 s timeout
RETRY_STATUSES = {429, 529}   # docs: retry these with backoff
RECORD_DIR = ROOT / "data" / "fusion" / "jev"

log = logging.getLogger(__name__)


class FusionRequestRejected(FusionError):
    """TypeSafe rejected the request itself (400/413/415/422): a bug on our side, not an outage."""


def request_body(model: str, state: dict[str, Any], questions: list[Question]) -> dict[str, Any]:
    qs: dict[str, Any] = {}
    for q in questions:
        entry: dict[str, Any] = {"type": q.type, "instructions": q.instructions}
        if q.options:
            entry["criteria"] = dict(q.options)   # insertion order = the order we shuffled to
        qs[q.id] = entry
    return {"model": model, "state": state, "questions": qs}


def parse_answers(questions: list[Question], data: dict[str, Any], latency_ms: int) -> Answers:
    try:
        raw = data["answers"]
        model = data["model"]
        by_id: dict[str, float | dict[str, float]] = {}
        conf: dict[str, float] = {}
        for q in questions:
            a = raw[q.id]
            if a.get("type") != q.type:
                raise FusionBadAnswer(f"{q.id}: answer type {a.get('type')!r} for a {q.type} question")
            if q.type == "noul":
                by_id[q.id] = float(a["noul"])
            else:
                by_id[q.id] = {k: float(v) for k, v in a["probabilities"].items()}
                if a.get("confidence") is not None:
                    conf[q.id] = float(a["confidence"])
    except (KeyError, TypeError, ValueError) as e:
        raise FusionBadAnswer(f"Jev response does not match the documented shape: {e!r}") from e
    answers = Answers(by_id=by_id, model=str(model), latency_ms=latency_ms, confidence=conf)
    check_answers(questions, answers)
    return answers


class JevEngine:
    def __init__(self, key: str | None = None, model: str = MODEL, timeout_s: float = TIMEOUT_S, retries: int = 1,
                 url: str = API_URL, record_dir: Path | None = RECORD_DIR):
        self._key = key or get_settings().require("TYPESAFE_KEY", "FUSION=jev calls TypeSafe's hosted Jev")
        self.model = model
        self.timeout_s = timeout_s
        self.retries = retries
        self.url = url
        self.record_dir = record_dir
        self.last_exchange: dict[str, Any] | None = None

    def ask(self, state: dict[str, Any], questions: list[Question]) -> Answers:
        if not questions:
            raise ValueError("ask() needs at least one question")
        assert_words_only(state, questions)                       # S5, before anything leaves the process
        body = request_body(self.model, state, questions)
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        log.debug("Jev request %s", payload.decode("utf-8"))
        attempt, t0 = 0, time.perf_counter()
        while True:
            attempt += 1
            status, text, err = self._post(payload)
            if err is None and status == 200:
                break
            retryable = err is not None or status in RETRY_STATUSES or (status or 0) >= 500
            if retryable and attempt <= self.retries:
                time.sleep(min(0.25 * attempt, 1.0))
                continue
            latency = int((time.perf_counter() - t0) * 1000)
            self._record(body, None, latency, error=err or f"HTTP {status}: {text[:500]}")
            log.error("Jev call failed (%s); request payload: %s", err or f"HTTP {status}", payload.decode("utf-8"))
            if err is None and status in (400, 413, 415, 422):
                raise FusionRequestRejected(f"TypeSafe rejected the request: HTTP {status}: {text[:500]}")
            if status == 401:
                raise FusionUnavailable("TypeSafe says the key is invalid (HTTP 401): check TYPESAFE_KEY")
            raise FusionUnavailable(f"Jev unavailable after {attempt} attempt(s): {err or f'HTTP {status}'}"
                                    f"{': ' + text[:300] if text else ''}")
        latency = int((time.perf_counter() - t0) * 1000)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            self._record(body, None, latency, error=f"invalid JSON: {text[:500]}")
            raise FusionBadAnswer(f"Jev returned invalid JSON: {e}") from e
        self._record(body, data, latency)
        return parse_answers(questions, data, latency)

    def _post(self, payload: bytes) -> tuple[int | None, str, str | None]:
        req = urllib.request.Request(self.url, data=payload, method="POST", headers={
            "Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
            "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.status, resp.read().decode("utf-8"), None
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return None, "", f"{type(e).__name__}: {getattr(e, 'reason', e)}"

    def _record(self, body: dict[str, Any], response: dict[str, Any] | None, latency_ms: int,
                error: str | None = None) -> None:
        rec = {"recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"), "url": self.url,
               "latency_ms": latency_ms, "request": body, "response": response, "error": error,
               "label": "recorded real Jev exchange (GEN3)"}
        self.last_exchange = rec
        if self.record_dir is None:
            return
        try:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:10]
            stamp = rec["recorded_at"].replace(":", "").replace("-", "")
            (self.record_dir / f"{stamp}_{digest}.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False),
                                                                    encoding="utf-8")
        except OSError as e:   # recording must not hide the answer, but it must not pass silently either
            log.warning("could not record the Jev exchange: %s", e)


@register("FUSION", "jev")
def _factory() -> JevEngine:
    return JevEngine()
