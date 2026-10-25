""""Fix it": where to buy the part the report's top finding is about (optional; off unless BRIGHTDATA_API_KEY and
BRIGHTDATA_SERP_ZONE are set, the same SERP zone as the reference hints).

Docs read 2026-10-24:
- https://docs.brightdata.com/api-reference/rest-api/serp/scrape-serp : POST https://api.brightdata.com/request,
  `Authorization: Bearer <key>`, body {zone, url, format}; `format: "raw"` returns the body as is.
- https://docs.brightdata.com/scraping-automation/serp-api/features : parsed results, either `brd_json=1` in the
  target URL (full parsed JSON) or `data_format` in the body (`parsed`; `parsed_light` = organic results only).
- https://docs.brightdata.com/products/serp-api/parsed-json-results : full parsed JSON `shopping[]` (title, link,
  price as displayed e.g. "$599.00", shop = the merchant, image, ...) and the product-listing-ad arrays `top_pla[]`,
  `bottom_pla[]`, `jackpot_pla[]` (title, link, price, shop, view_all, ...); `organic[]` (title, link, description).
Terms: https://brightdata.com/acceptable-use-policy (lawful use, public data only, no data behind a login, no
search-result manipulation). Use here: two read-only searches of public Google results per (object, part), at most
once a day per worker data dir (cache); from the results only title, displayed price, merchant name and link are kept (at most three
options) plus one guide link. No page is opened, no page text or image is fetched or stored.

The search is generic: the object in the report's own words (brand and model only when the report has them, else the
generic category) and the part as its generic class (vision/generic_classes.py). Nothing here names a product.
Stdlib only (urllib).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repairos.references import brightdata

REQUEST_TIMEOUT_S = 45.0          # one Bright Data request (measured 2026-10-24: 4.8 to 20 s, one shopping 45 s+)
SHOP_WAIT_S = 20.0                # Shopping answered in 4.8 to 7.9 s, or slowly with nothing (45 s, empty body)
BUDGET_S = 60.0                   # the whole search; the report never waits for it (pipeline/run.py attach_fix)
MAX_LINK = 2000                   # a longer link is left out, never cut (a cut link breaks)
CACHE_TTL_S = 24 * 3600
EMPTY_TTL_S = 3600                # "nothing found" is kept for an hour, not a day
MAX_OPTIONS = 3
NOT_A_PART = {"", "unknown_part", "whole photo", "label", "scene"}
CURRENCY = {"US$": "USD", "$": "USD", "£": "GBP", "€": "EUR", "CA$": "CAD", "C$": "CAD", "A$": "AUD", "AU$": "AUD",
            "¥": "JPY", "₹": "INR", "USD": "USD", "GBP": "GBP", "EUR": "EUR", "CAD": "CAD", "AUD": "AUD"}
_PRICE = re.compile(r"^\s*(US\$|CA\$|AU\$|C\$|A\$|[$£€¥₹]|USD|GBP|EUR|CAD|AUD)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")


def credentials() -> tuple[str, str] | None:
    return brightdata.credentials()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------------------------------------- the query
def object_words(obj: Mapping[str, Any] | None) -> str | None:
    """Brand and model when the report has them (only as printed or recognised beyond doubt), else the generic
    category; None when the object is unknown."""
    obj = obj or {}
    name = " ".join(str(obj[k]).strip() for k in ("brand", "model") if obj.get(k))
    if name and obj.get("confidence_word") != "low":
        cat = str(obj.get("category") or "").strip()
        return f"{name} {cat}".strip() if not obj.get("model") and cat and cat != "unknown" else name
    cat = str(obj.get("category") or "").strip()
    return cat if cat and cat.lower() != "unknown" else None


def top_finding(report: Mapping[str, Any]) -> dict | None:
    """The report's first "most likely" row (an issue, ranked in code), or None."""
    ids = list(report.get("most_likely") or [])
    for f in report.get("findings") or []:
        if ids and f.get("finding_id") == ids[0] and f.get("issue") not in (None, "none"):
            return dict(f)
    return None


def part_for(report: Mapping[str, Any]) -> tuple[str, str, dict] | None:
    """(object words, part words, the top finding) to search for, or None when there is nothing to fix or nothing
    generic enough to search: no ranked issue, an unknown object, or a region with no part class."""
    f = top_finding(report)
    if f is None:
        return None
    obj = object_words(report.get("object"))
    part = str(f.get("part_class") or "").replace("_", " ").strip()
    if obj is None or part.lower() in NOT_A_PART:
        return None
    return obj, part, f


def queries(obj: str, part: str) -> tuple[str, str, str]:
    """(the Google Shopping search, the web search with purchase intent, the guide search)."""
    return f"{obj} {part} replacement", f"buy {obj} {part} replacement", f"{obj} {part} replacement guide"


def request_body(query: str, zone: str, shopping: bool) -> dict:
    """The shopping search asks Google Shopping (tbm=shop) for the full parsed JSON with `brd_json=1` in the URL: the
    body field `data_format: "parsed"` returned an empty body for tbm=shop (measured 2026-10-24), while brd_json=1
    returned 40 `shopping[]` results in 4.8 to 7.9 s. The guide search is a web search with `data_format: "parsed"`
    (organic[]). hl/gl are explicit so the zone's defaults never matter."""
    q = {"q": query, "hl": "en", "gl": "us", "num": 10}
    if shopping:
        q.update(tbm="shop", brd_json="1")
    body = {"zone": zone, "url": "https://www.google.com/search?" + urllib.parse.urlencode(q), "format": "raw"}
    if not shopping:
        body["data_format"] = "parsed"
    return body


# ------------------------------------------------------------------------------------------------- parsing
def parse_price(text: Any) -> tuple[float, str] | None:
    """(amount, ISO currency) from a displayed price such as "$1,299.99"; None when it is not a plain price (a
    range, "from", no currency mark). Searches ask for gl=us, so "$" is US dollars."""
    m = _PRICE.match(str(text or ""))
    if not m:
        return None
    try:
        amount = float(m.group(2).replace(",", ""))
    except ValueError:
        return None
    return (amount, CURRENCY[m.group(1)]) if amount > 0 else None


def _payload(raw: Any) -> dict:
    if isinstance(raw, (bytes, str)):
        raw = json.loads(raw)
    if isinstance(raw, Mapping) and isinstance(raw.get("body"), str):   # format "json" wraps the body
        raw = json.loads(raw["body"])
    return dict(raw) if isinstance(raw, Mapping) else {}


def _link_ok(link: Any) -> bool:
    u = urllib.parse.urlsplit(str(link or ""))
    return u.scheme in ("http", "https") and bool(u.netloc) and len(str(link)) <= MAX_LINK


def _host(link: str) -> str:
    h = urllib.parse.urlsplit(link).hostname or ""
    return h[4:] if h.startswith("www.") else h


def options_from(raw: Any) -> list[dict]:
    """Up to MAX_OPTIONS purchase options {title, price, currency, seller, link} from the parsed shopping results
    (shopping[], then the product-listing-ad arrays), in Google's order, one per seller; when there are none, organic
    results as options without a price. Only these five fields are kept."""
    p = _payload(raw)
    out: list[dict] = []
    sellers: set[str] = set()
    for key in ("shopping", "top_pla", "jackpot_pla", "bottom_pla"):
        for r in p.get(key) or []:
            if not isinstance(r, Mapping) or r.get("view_all") or not _link_ok(r.get("link")):
                continue
            price = parse_price(r.get("price"))
            seller = str(r.get("shop") or "").strip() or _host(str(r["link"]))
            title = str(r.get("title") or "").strip()
            if price is None or not title or seller.lower() in sellers:
                continue
            sellers.add(seller.lower())
            out.append({"title": title[:200], "price": round(price[0], 2), "currency": price[1],
                        "seller": seller[:80], "link": str(r["link"])})
            if len(out) == MAX_OPTIONS:
                return out
    if out:
        return out
    for r in p.get("organic") or []:
        if not isinstance(r, Mapping) or not _link_ok(r.get("link")) or not str(r.get("title") or "").strip():
            continue
        seller = _host(str(r["link"]))
        if seller.lower() in sellers:
            continue
        sellers.add(seller.lower())
        out.append({"title": str(r["title"]).strip()[:200], "seller": seller[:80], "link": str(r["link"])})
        if len(out) == MAX_OPTIONS:
            break
    return out


def guide_from(raw: Any) -> str | None:
    for r in _payload(raw).get("organic") or []:
        if isinstance(r, Mapping) and _link_ok(r.get("link")):
            return str(r["link"])
    return None


# ------------------------------------------------------------------------------------------------- the cache
def cache_dir() -> Path:
    from repairos.config import ROOT

    return Path(os.environ.get("REPAIROS_DATA_DIR") or ROOT / "data") / "cache" / "fix"


def cache_key(obj: str, part: str) -> str:
    return hashlib.sha256(f"{obj.strip().lower()}|{part.strip().lower()}".encode()).hexdigest()[:32]


def cached(obj: str, part: str, now: float | None = None) -> dict | None:
    """The cached search for (object, part) when younger than its TTL: {"fix": {...} | None, "at": epoch s}."""
    f = cache_dir() / f"{cache_key(obj, part)}.json"
    try:
        rec = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    ttl = CACHE_TTL_S if rec.get("fix") else EMPTY_TTL_S
    return rec if (now or time.time()) - float(rec.get("at") or 0) < ttl else None


def remember(obj: str, part: str, fix: dict | None) -> None:
    d = cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{cache_key(obj, part)}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps({"at": time.time(), "fix": fix}))
    os.replace(tmp, d / f"{cache_key(obj, part)}.json")


# ------------------------------------------------------------------------------------------------- the search
def _post(body: dict, key: str, timeout: float) -> bytes:
    req = urllib.request.Request(brightdata.ENDPOINT, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _parsed(fut: Future, timeout: float) -> Any:
    """A request's parsed payload, or None when it failed or answered with an empty or non-JSON body (the shopping
    search sometimes answers 200 with an empty body: measured 2026-10-24)."""
    try:
        return _payload(fut.result(timeout=timeout))
    except Exception:  # noqa: BLE001 - one search of three; the others may still answer
        return None


def search(obj: str, part: str, key: str, zone: str, timeout: float = REQUEST_TIMEOUT_S) -> dict | None:
    """The report's `fix` ({part, options, guide_link?, searched_at}) for (object, part), or None when nothing was
    found. Three requests at once: Google Shopping; a web search with purchase intent (its embedded shopping results,
    else its organic results without a price), used only when Shopping gave nothing; and the guide search. A failed
    guide search only drops the guide link."""
    shop_q, buy_q, guide_q = queries(obj, part)
    pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="fix-search")

    def submit(q: str, shopping: bool) -> Future:
        brightdata.charge(q)   # the spend guard before every SERP request; SpendLimit skips the search
        return pool.submit(_post, request_body(q, zone, shopping), key, timeout)
    try:
        shop = submit(shop_q, True)
        buy = submit(buy_q, False)
        guide = submit(guide_q, False)
        first = _parsed(shop, SHOP_WAIT_S)   # Shopping first; a slow one gives way to the purchase search
        options = options_from(first or {})
        if not options:
            second = _parsed(buy, timeout + 5)
            options = options_from(second or {})
            if not options and not shop.done():
                first = _parsed(shop, timeout + 5)
                options = options_from(first or {})
            if not options and first is None and second is None:   # all failed: an error (never cached)
                raise (shop.exception(timeout=0) if shop.done() else None) or \
                    TimeoutError("Bright Data gave no answer in time")
        guide_link = guide_from(_parsed(guide, timeout + 5) or {}) if options else None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)   # never wait on a search whose answer is not needed
    if not options:
        return None
    fix = {"part": part, "options": options, "searched_at": _now()}
    if guide_link:
        fix["guide_link"] = guide_link
    return fix


_inflight: dict[str, Future] = {}
_inflight_lock = threading.Lock()


def find_fix(report: Mapping[str, Any], creds: tuple[str, str] | None = None) -> tuple[dict | None, dict]:
    """(fix or None, a record of what happened: status, seconds, cached). Never raises for a network or parse error:
    the record says it failed. Off (status `off`) without credentials or without a part to search."""
    creds = creds if creds is not None else credentials()
    what = part_for(report)
    if creds is None or what is None:
        return None, {"status": "off" if creds is None else "nothing_to_search"}
    return find_fix_for(what[0], what[1], creds)


def find_fix_for(obj: str, part: str, creds: tuple[str, str], wait_s: float = BUDGET_S) -> tuple[dict | None, dict]:
    """find_fix for (object, part): the cache (24 h, checked before any spend), else the search this process already
    has under way for the same pair (prewarm, during the scan), else a new search (each SERP request passes the
    spend guard first: brightdata.charge; at the cap the status is `limited` and nothing is retried or cached)."""
    t0 = time.monotonic()
    hit = cached(obj, part)
    if hit is not None:
        return hit.get("fix"), {"status": "done" if hit.get("fix") else "none", "cached": True,
                                "seconds": round(time.monotonic() - t0, 3)}
    key = cache_key(obj, part)
    with _inflight_lock:
        fut = _inflight.get(key)
        mine = fut is None
        if mine:
            fut = _inflight[key] = Future()
    if not mine:
        try:
            fix, rec = fut.result(timeout=wait_s)
        except FutureTimeout:
            return None, {"status": "timeout", "seconds": round(time.monotonic() - t0, 3)}
        return fix, {**rec, "shared": True, "seconds": round(time.monotonic() - t0, 3)}
    try:
        fix = search(obj, part, *creds)
    except Exception as e:  # noqa: BLE001 - optional; the record says why (never the key or the URL)
        status = "limited" if brightdata.is_spend_limit(e) else "failed"   # a spend cap: skipped, never retried
        out = None, {"status": status, "error": type(e).__name__, "seconds": round(time.monotonic() - t0, 3)}
    else:
        remember(obj, part, fix)
        out = fix, {"status": "done" if fix else "none", "cached": False, "seconds": round(time.monotonic() - t0, 3)}
    with _inflight_lock:
        _inflight.pop(key, None)
    fut.set_result(out)
    return out


def prewarm(obj: str, part: str, creds: tuple[str, str]) -> bool:
    """Start the search for (object, part) in the background unless it is cached or under way (True when started),
    so it is ready, or nearly, when the report's top finding is known."""
    if cached(obj, part) is not None:
        return False
    with _inflight_lock:
        if cache_key(obj, part) in _inflight:
            return False
    threading.Thread(target=find_fix_for, args=(obj, part, creds), name="fix-prewarm", daemon=True).start()
    return True


def payload(scan_id: str, fix: Mapping[str, Any]) -> dict:
    """The live event `fix_ready`."""
    return {"scan_id": scan_id, **{k: fix[k] for k in ("part", "options", "guide_link", "searched_at") if k in fix}}

