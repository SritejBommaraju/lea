"""Bright Data SERP API client (optional; off unless BRIGHTDATA_API_KEY and BRIGHTDATA_SERP_ZONE are set).

Docs read 2026-10-24: https://docs.brightdata.com/products/serp-api/send-your-first-request (POST
https://api.brightdata.com/request, `Authorization: Bearer <key>`, body {zone, url, format}),
https://docs.brightdata.com/scraping-automation/serp-api/features (`data_format: "parsed_light"` for parsed JSON) and
https://docs.brightdata.com/scraping-automation/serp-api/parsed-json-results (`organic[]`: global_rank, title, link,
description). Terms: https://brightdata.com/acceptable-use-policy (public data only, lawful use, no search-result
manipulation). Use here: one read-only search per identified product; only the result titles and descriptions are
read, to write short hints; nothing from the pages (no image, no page text) is fetched, stored or redistributed; the
hints and the result links are kept with the scan, and cached a day per query (cache/hints: hints + links only).

Stdlib only (urllib).
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

ENDPOINT = "https://api.brightdata.com/request"
TIMEOUT_S = 45.0          # measured 2026-10-24: 7.7 to 18.4 s per search
HINTS_TTL_S = 24 * 3600
MAX_RESULTS = 8


def credentials() -> tuple[str, str] | None:
    """(api key, SERP zone) from the settings, or None when reference hints are off."""
    from repairos.config import get_settings

    s = get_settings()
    key = s.brightdata_api_key.get_secret_value() if s.brightdata_api_key else ""
    zone = (s.brightdata_serp_zone or "").strip()
    return (key, zone) if key and zone else None


def query_for(identity: Mapping[str, Any]) -> str | None:
    """The search for an identified product, or None when it is not identified well enough (no brand or model, or a
    low-confidence answer): a search on a guess would give hints about another product."""
    name = " ".join(x for x in (identity.get("brand"), identity.get("model")) if x)
    if not name or identity.get("confidence") == "low":
        return None
    kind = identity.get("category") or ""
    return f"{name} {kind} teardown OR \"service manual\" OR datasheet".strip()


def request_body(query: str, zone: str) -> dict:
    url = "https://www.google.com/search?" + urllib.parse.urlencode({"q": query, "num": 10, "hl": "en"})
    return {"zone": zone, "url": url, "format": "raw", "data_format": "parsed_light"}


def parse(payload: Any) -> list[dict[str, str]]:
    """[{title, link, description}] of the organic results (parsed light JSON), at most MAX_RESULTS."""
    if isinstance(payload, (bytes, str)):
        payload = json.loads(payload)
    out = []
    for r in (payload or {}).get("organic") or []:
        if isinstance(r, Mapping) and r.get("link"):
            out.append({"title": str(r.get("title") or "")[:300], "link": str(r["link"])[:500],
                        "description": str(r.get("description") or "")[:600]})
    return out[:MAX_RESULTS]


def charge(query: str) -> None:
    """The spend guard before one SERP request (repairos.spend, when it is installed): raises its SpendLimit (a
    RuntimeError) when the Bright Data cap is reached, before anything is spent. Callers check their cache first,
    so a cached answer never counts. No spend module = allowed."""
    try:
        from repairos import spend
    except ImportError:
        return
    import hashlib

    spend.charge("brightdata", key=hashlib.sha256(query.encode()).hexdigest()[:16])


def is_spend_limit(e: BaseException) -> bool:
    return type(e).__name__ == "SpendLimit"


def search(query: str, key: str, zone: str, timeout: float = TIMEOUT_S, charged: bool = False) -> list[dict[str, str]]:
    if not charged:   # `charged`: the caller already passed the spend guard for this query
        charge(query)   # SpendLimit propagates: the caller skips the search, with no retry
    req = urllib.request.Request(ENDPOINT, data=json.dumps(request_body(query, zone)).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return parse(r.read())


def _hints_file(query: str):
    import hashlib
    import os
    from pathlib import Path

    from repairos.config import ROOT

    d = Path(os.environ.get("REPAIROS_DATA_DIR") or ROOT / "data") / "cache" / "hints"
    return d / f"{hashlib.sha256(query.encode()).hexdigest()[:32]}.json"


def cached_hints(query: str) -> dict | None:
    """The hints made for this search within HINTS_TTL_S: {hints, sources[{title, link}]} (never the result text)."""
    import time

    try:
        rec = json.loads(_hints_file(query).read_text())
    except (OSError, ValueError):
        return None
    return rec if time.time() - float(rec.get("at") or 0) < HINTS_TTL_S and rec.get("hints") else None


def remember_hints(query: str, hints: list[str], sources: list[dict]) -> None:
    import os
    import time

    if not hints:
        return
    f = _hints_file(query)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"at": time.time(), "hints": list(hints), "sources": list(sources)}))
    os.replace(tmp, f)
