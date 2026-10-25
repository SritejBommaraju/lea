"""Spend guards for every paid service: hard caps, a circuit breaker against spam or loop calls, and alerts.

    from repairos import spend
    spend.charge("anthropic", usd=spend.estimate_usd("anthropic"), key=request_hash)   # BEFORE the paid call
    ...                                                                                 # the call itself
    spend.record_cost("anthropic", actual_usd, estimate=that_estimate)                 # refine the USD estimate

    python -m repairos.spend status            per-service counts, caps and breakers (a table)
    python -m repairos.spend reset <service>   close a service's breaker now (`all` for every service)

`charge` records one call (or `units`) and raises SpendLimit BEFORE anything is spent when a cap would be exceeded:
`per_minute` (rolling 60 s), `per_day` (rolling 24 h), `usd_per_day` (rolling 24 h, estimated), `repeat_spam` (the same
request `key` more than `repeat_per_minute` times in 60 s: a loop asking the same thing). A trip opens the service's
breaker for `breaker_cooldown_s` (longer when the rolling window needs longer to free up), and every call while it is
open raises `breaker_open` at once. Refused calls are not counted as spent; they are counted as `blocked`.

Caps live in config/pipeline.yaml under `spend:` (REPAIROS_PIPELINE_CONFIG points elsewhere), per service, plus a
`default` block for a service the file does not name. Counters live in one small SQLite file shared by every process
(gateway, worker, live bridge): REPAIROS_SPEND_DB, else $REPAIROS_DATA_DIR/spend.sqlite3, else <repo>/data. WAL mode
and BEGIN IMMEDIATE make check-and-record atomic across threads and processes; restarts keep the counters. No network.

Alerts: every trip, and the first time a service passes `alert_at` (80 %) of a daily cap, logs one ERROR line starting
`SPEND ALERT` and stores the alert; the gateway publishes stored alerts as the live event `spend_alert` on every
session's event stream and on GET /v1/events. If the file cannot be used, this process counts in memory (still
guarded, not shared) and says so at ERROR level.
"""
from __future__ import annotations

import logging
import math
import os
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repairos.config import ROOT

log = logging.getLogger("repairos.spend")

PAUSED = "paused: usage limit reached"   # the words a region, a report or a voice answer shows when a cap stopped it
REASONS = ("per_minute", "per_day", "usd_per_day", "repeat_spam", "breaker_open")
CAP_KEYS = ("per_minute", "per_day", "usd_per_day", "repeat_per_minute", "breaker_cooldown_s")
NEAR = {"per_day": "near_per_day", "usd_per_day": "near_usd_per_day"}
NEAR_REPEAT_S = 3600.0   # a near-cap warning for one service and cap at most once an hour (rolling windows wobble)
DAY_MIN = 1440
KEEP_S = 120             # per-second counters and request keys kept this long (the per-minute window is 60 s)


class SpendLimit(RuntimeError):
    """A paid call was refused before it was made. `reason` is one of REASONS; `retry_after_s` is when the service
    may be tried again (the breaker's cooldown)."""

    def __init__(self, service: str, reason: str, retry_after_s: float, *, used: float | None = None,
                 cap: float | None = None, tripped: str | None = None):
        self.service, self.reason, self.retry_after_s = service, reason, float(max(0.0, retry_after_s))
        self.used, self.cap, self.tripped = used, cap, tripped or reason
        mins = max(1, math.ceil(self.retry_after_s / 60))
        why = reason if reason != "breaker_open" else f"breaker open after {self.tripped}"
        super().__init__(f"{service}: usage limit reached ({why}); paused for about {mins} min")


# ------------------------------------------------------------------------------------------------------ config
_cfg_lock = threading.Lock()
_cfg_cache: tuple[str, float, dict] | None = None


def config_path() -> Path:
    return Path(os.environ.get("REPAIROS_PIPELINE_CONFIG") or ROOT / "config" / "pipeline.yaml")


def _num(where: str, k: str, v: Any, *, allow_none: bool) -> float | None:
    if v is None and allow_none:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        raise ValueError(f"{where}: {k} must be a positive number{' or null' if allow_none else ''}")
    return float(v)


def _parse(data: dict, where: str) -> dict:
    spend = data.get("spend")
    if not isinstance(spend, dict) or not isinstance(spend.get("default"), dict):
        raise ValueError(f"{where}: a `spend:` section with a `default` block is required")
    alert_at = spend.get("alert_at")
    if isinstance(alert_at, bool) or not isinstance(alert_at, (int, float)) or not 0 < alert_at < 1:
        raise ValueError(f"{where}: spend.alert_at must be a fraction between 0 and 1")
    services = {}
    for name, c in spend.items():
        if name == "alert_at":
            continue
        if not isinstance(c, dict):
            raise ValueError(f"{where}: spend.{name} must be a mapping")
        w = f"{where}: spend.{name}"
        out = {"label": str(c.get("label") or name),
               "per_minute": _num(w, "per_minute", c.get("per_minute"), allow_none=False),
               "per_day": _num(w, "per_day", c.get("per_day"), allow_none=False),
               "usd_per_day": _num(w, "usd_per_day", c.get("usd_per_day"), allow_none=True),
               "repeat_per_minute": _num(w, "repeat_per_minute", c.get("repeat_per_minute"), allow_none=True),
               "breaker_cooldown_s": _num(w, "breaker_cooldown_s", c.get("breaker_cooldown_s"), allow_none=False),
               "usd_per_call": _num(w, "usd_per_call", c.get("usd_per_call"), allow_none=True)}
        prices = c.get("usd_per_mtok") or {}
        if not isinstance(prices, dict):
            raise ValueError(f"{w}: usd_per_mtok must map model ids to prices")
        out["usd_per_mtok"] = {m: {k: float(p.get(k, 0.0)) for k in ("input", "output", "cache_write", "cache_read")}
                               for m, p in prices.items()}
        services[name] = out
    return {"alert_at": float(alert_at), "services": services}


def load_config() -> dict:
    """The `spend:` section, re-read when the file changes (one stat per call)."""
    global _cfg_cache
    path = config_path()
    mtime = path.stat().st_mtime
    with _cfg_lock:
        if _cfg_cache and _cfg_cache[0] == str(path) and _cfg_cache[1] == mtime:
            return _cfg_cache[2]
    import yaml

    cfg = _parse(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, str(path))
    with _cfg_lock:
        _cfg_cache = (str(path), mtime, cfg)
    return cfg


def caps(service: str) -> dict:
    s = load_config()["services"]
    return s.get(service) or {**s["default"], "label": service}


# ------------------------------------------------------------------------------------------------------ storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS sec (service TEXT NOT NULL, t INTEGER NOT NULL, units REAL NOT NULL,
                                PRIMARY KEY (service, t)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS mins (service TEXT NOT NULL, m INTEGER NOT NULL, units REAL NOT NULL DEFAULT 0,
                                 usd REAL NOT NULL DEFAULT 0, blocked REAL NOT NULL DEFAULT 0,
                                 PRIMARY KEY (service, m)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS keys (service TEXT NOT NULL, key TEXT NOT NULL, t INTEGER NOT NULL, n INTEGER NOT NULL,
                                 PRIMARY KEY (service, key, t)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS breaker (service TEXT PRIMARY KEY, open_until REAL NOT NULL, reason TEXT NOT NULL,
                                    opened_at REAL NOT NULL, used REAL, cap REAL);
CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, service TEXT NOT NULL,
                                   reason TEXT NOT NULL, used REAL, cap REAL, retry_after_s REAL,
                                   message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cost (service TEXT PRIMARY KEY, avg_usd REAL NOT NULL, n INTEGER NOT NULL);
"""
_local = threading.local()
_mem_lock = threading.Lock()
_fallback: dict[str, sqlite3.Connection] = {}


def db_path() -> Path:
    p = os.environ.get("REPAIROS_SPEND_DB")
    if p:
        return Path(p).expanduser()
    return Path(os.environ.get("REPAIROS_DATA_DIR") or ROOT / "data").expanduser() / "spend.sqlite3"


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path), timeout=10.0, isolation_level=None, check_same_thread=False)
    try:
        c.execute("PRAGMA busy_timeout=10000")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.executescript(SCHEMA)
    except BaseException:
        c.close()
        raise
    return c


def _conn() -> tuple[sqlite3.Connection, threading.Lock | None]:
    """This thread's connection to the shared file (one per thread and process), or, when the file is unusable, one
    in-memory database for the process (guarded by a lock)."""
    path = str(db_path())
    key = (os.getpid(), path)
    if path in _fallback:
        return _fallback[path], _mem_lock
    c = getattr(_local, "conns", {}).get(key)
    if c is not None:
        return c, None
    c = err = None
    for attempt in range(8):   # a second process creating the file or switching it to WAL at the same moment: wait
        try:
            c = _open(Path(path))
            break
        except sqlite3.OperationalError as e:
            err = e
            time.sleep(0.05 * (attempt + 1))
        except (sqlite3.Error, OSError) as e:
            err = e
            break
    if c is None:
        e = err
        log.error("SPEND ALERT spend file %s unusable (%s): this process counts in memory, not shared", path, e)
        mem = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        mem.executescript(SCHEMA)
        _fallback[path] = mem
        return mem, _mem_lock
    if not hasattr(_local, "conns"):
        _local.conns = {}
    _local.conns[key] = c
    return c, None


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT (ROLLBACK on error): one writer at a time across every process."""

    def __enter__(self) -> sqlite3.Connection:
        self.c, self.lock = _conn()
        if self.lock:
            self.lock.acquire()
        self.c.execute("BEGIN IMMEDIATE")
        return self.c

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.c.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            if self.lock:
                self.lock.release()


def _now() -> float:
    return time.time()


def _sum(c: sqlite3.Connection, sql: str, *args: Any) -> float:
    return float(c.execute(sql, args).fetchone()[0] or 0.0)


def _window(c: sqlite3.Connection, service: str, now: float) -> dict[str, float]:
    s, m = int(now), int(now // 60)
    return {
        "minute": _sum(c, "SELECT SUM(units) FROM sec WHERE service=? AND t>?", service, s - 60),
        "hour": _sum(c, "SELECT SUM(units) FROM mins WHERE service=? AND m>?", service, m - 60),
        "day": _sum(c, "SELECT SUM(units) FROM mins WHERE service=? AND m>?", service, m - DAY_MIN),
        "usd_day": _sum(c, "SELECT SUM(usd) FROM mins WHERE service=? AND m>?", service, m - DAY_MIN),
        "blocked_day": _sum(c, "SELECT SUM(blocked) FROM mins WHERE service=? AND m>?", service, m - DAY_MIN),
    }


def _frees_after(c: sqlite3.Connection, service: str, now: float, column: str, used: float, need: float,
                 cap: float) -> float:
    """Seconds until the rolling day has room for `need` more (oldest minutes leave the window first)."""
    m = int(now // 60)
    over = used + need - cap
    if over <= 0:
        return 0.0
    gone = 0.0
    for bucket, v in c.execute(f"SELECT m, {column} FROM mins WHERE service=? AND m>? ORDER BY m",
                               (service, m - DAY_MIN)):
        gone += float(v)
        if gone >= over:
            return max(0.0, (bucket + DAY_MIN + 1) * 60 - now)
    return DAY_MIN * 60.0


def _alert(c: sqlite3.Connection, now: float, service: str, reason: str, used: float | None, cap: float | None,
           retry_after_s: float | None, message: str) -> None:
    c.execute("INSERT INTO alerts (at, service, reason, used, cap, retry_after_s, message) VALUES (?,?,?,?,?,?,?)",
              (now, service, reason, used, cap, retry_after_s, message))


def _log_alert(message: str) -> None:
    log.error("SPEND ALERT %s", message)


def _trip(c: sqlite3.Connection, now: float, service: str, cfg: dict, reason: str, used: float, cap: float,
          frees_s: float = 0.0) -> SpendLimit:
    retry = max(cfg["breaker_cooldown_s"], frees_s)
    c.execute("INSERT OR REPLACE INTO breaker (service, open_until, reason, opened_at, used, cap) VALUES (?,?,?,?,?,?)",
              (service, now + retry, reason, now, used, cap))
    msg = (f"{service}: {reason} cap reached ({_fmt(used)} of {_fmt(cap)}); every {service} call is refused for "
           f"about {max(1, math.ceil(retry / 60))} min. Look for a loop or spam calls; "
           f"`python -m repairos.spend reset {service}` reopens it now.")
    _alert(c, now, service, reason, used, cap, retry, msg)
    return SpendLimit(service, reason, retry, used=used, cap=cap)


def _near(c: sqlite3.Connection, now: float, service: str, kind: str, before: float, after: float,
          cap: float | None, alert_at: float) -> str | None:
    if cap is None or not (before < alert_at * cap <= after):
        return None
    reason = NEAR[kind]
    recent = c.execute("SELECT 1 FROM alerts WHERE service=? AND reason=? AND at>?",
                       (service, reason, now - NEAR_REPEAT_S)).fetchone()
    if recent:
        return None
    msg = (f"{service}: {_fmt(after)} of the {kind} cap {_fmt(cap)} used in the last 24 h "
           f"({round(100 * after / cap)} %); calls stop at the cap.")
    _alert(c, now, service, reason, after, cap, None, msg)
    return msg


def _fmt(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{int(v)}" if v == int(v) else f"{v:.2f}"


def _block(c: sqlite3.Connection, service: str, now: float, units: float) -> None:
    c.execute("INSERT INTO mins (service, m, blocked) VALUES (?,?,?) "
              "ON CONFLICT(service, m) DO UPDATE SET blocked=blocked+excluded.blocked", (service, int(now // 60), units))


# ------------------------------------------------------------------------------------------------------ the API
def estimate_usd(service: str) -> float:
    """What one call of `service` probably costs: the running average of measured costs (record_cost), else the
    configured `usd_per_call`, else 0 (USD not tracked for that service)."""
    c, lock = _conn()
    if lock:
        with lock:
            row = c.execute("SELECT avg_usd FROM cost WHERE service=?", (service,)).fetchone()
    else:
        row = c.execute("SELECT avg_usd FROM cost WHERE service=?", (service,)).fetchone()
    if row:
        return float(row[0])
    return caps(service)["usd_per_call"] or 0.0


def charge(service: str, units: float = 1.0, *, usd: float | None = None, key: str | None = None) -> None:
    """Record `units` calls of `service` (and `usd`, else the estimate) or raise SpendLimit before anything is spent.
    `key`: a fingerprint of the request (e.g. a hash of the prompt and image ids); the same key more than
    `repeat_per_minute` times in 60 s is `repeat_spam`."""
    if units <= 0:
        raise ValueError("units must be positive")
    cfg, alert_at = caps(service), load_config()["alert_at"]
    est = float(usd) if usd is not None else estimate_usd(service)
    messages: list[str] = []
    err: SpendLimit | None = None
    with _Tx() as c:
        now = _now()
        b = c.execute("SELECT open_until, reason, used, cap FROM breaker WHERE service=?", (service,)).fetchone()
        if b and b[0] > now:
            _block(c, service, now, units)
            err = SpendLimit(service, "breaker_open", b[0] - now, used=b[2], cap=b[3], tripped=b[1])
        else:
            if b:
                c.execute("DELETE FROM breaker WHERE service=?", (service,))
            w = _window(c, service, now)
            s = int(now)
            if w["minute"] + units > cfg["per_minute"]:
                err = _trip(c, now, service, cfg, "per_minute", w["minute"], cfg["per_minute"])
            elif w["day"] + units > cfg["per_day"]:
                err = _trip(c, now, service, cfg, "per_day", w["day"], cfg["per_day"],
                            _frees_after(c, service, now, "units", w["day"], units, cfg["per_day"]))
            elif cfg["usd_per_day"] is not None and w["usd_day"] + est > cfg["usd_per_day"]:
                err = _trip(c, now, service, cfg, "usd_per_day", w["usd_day"], cfg["usd_per_day"],
                            _frees_after(c, service, now, "usd", w["usd_day"], est, cfg["usd_per_day"]))
            elif key and cfg["repeat_per_minute"] is not None:
                n = _sum(c, "SELECT SUM(n) FROM keys WHERE service=? AND key=? AND t>?", service, key, s - 60)
                if n + 1 > cfg["repeat_per_minute"]:
                    err = _trip(c, now, service, cfg, "repeat_spam", n, cfg["repeat_per_minute"])
            if err is not None:
                _block(c, service, now, units)
                messages.append(str(c.execute("SELECT message FROM alerts ORDER BY id DESC LIMIT 1").fetchone()[0]))
            else:
                c.execute("INSERT INTO sec (service, t, units) VALUES (?,?,?) "
                          "ON CONFLICT(service, t) DO UPDATE SET units=units+excluded.units", (service, s, units))
                c.execute("INSERT INTO mins (service, m, units, usd) VALUES (?,?,?,?) ON CONFLICT(service, m) "
                          "DO UPDATE SET units=units+excluded.units, usd=usd+excluded.usd",
                          (service, s // 60, units, est))
                if key:
                    c.execute("INSERT INTO keys (service, key, t, n) VALUES (?,?,?,1) "
                              "ON CONFLICT(service, key, t) DO UPDATE SET n=n+1", (service, key, s))
                c.execute("DELETE FROM sec WHERE service=? AND t<?", (service, s - KEEP_S))
                c.execute("DELETE FROM keys WHERE service=? AND t<?", (service, s - KEEP_S))
                if s % 97 == 0:
                    c.execute("DELETE FROM mins WHERE m<?", (s // 60 - DAY_MIN - 60,))
                    c.execute("DELETE FROM alerts WHERE at<?", (now - 7 * 86400,))
                for kind, before, add in (("per_day", w["day"], units), ("usd_per_day", w["usd_day"], est)):
                    msg = _near(c, now, service, kind, before, before + add, cfg[kind], alert_at)
                    if msg:
                        messages.append(msg)
    for m in messages:
        _log_alert(m)
    if err is not None:
        raise err


def record_cost(service: str, usd: float, *, estimate: float = 0.0) -> None:
    """After a call: replace its estimate with the measured cost (e.g. from token counts) and update the running
    average used for the next estimates. A day over `usd_per_day` opens the breaker for the next call."""
    if usd < 0:
        raise ValueError("usd must not be negative")
    cfg, alert_at = caps(service), load_config()["alert_at"]
    messages: list[str] = []
    with _Tx() as c:
        now = _now()
        before = _window(c, service, now)["usd_day"]
        c.execute("INSERT INTO mins (service, m, usd) VALUES (?,?,?) ON CONFLICT(service, m) "
                  "DO UPDATE SET usd=MAX(0, usd+excluded.usd)", (service, int(now // 60), usd - estimate))
        row = c.execute("SELECT avg_usd, n FROM cost WHERE service=?", (service,)).fetchone()
        avg = usd if not row else row[0] + (usd - row[0]) * (0.2 if row[1] >= 5 else 1.0 / (row[1] + 1))
        c.execute("INSERT OR REPLACE INTO cost (service, avg_usd, n) VALUES (?,?,?)",
                  (service, avg, (row[1] if row else 0) + 1))
        after = before + usd - estimate
        cap = cfg["usd_per_day"]
        if cap is not None and after > cap:
            b = c.execute("SELECT open_until FROM breaker WHERE service=?", (service,)).fetchone()
            if not b or b[0] <= now:
                _trip(c, now, service, cfg, "usd_per_day", after, cap,
                      _frees_after(c, service, now, "usd", after, 0.0, cap))
                messages.append(str(c.execute("SELECT message FROM alerts ORDER BY id DESC LIMIT 1").fetchone()[0]))
        else:
            msg = _near(c, now, service, "usd_per_day", before, after, cap, alert_at)
            if msg:
                messages.append(msg)
    for m in messages:
        _log_alert(m)


def anthropic_usd(model: str, usage: dict) -> float | None:
    """USD for one Claude response from its token counts and the configured price per million tokens; None when the
    model has no price in config (then the estimate stands)."""
    p = caps("anthropic")["usd_per_mtok"].get(model)
    if p is None:
        return None
    n = {}
    for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        v = usage.get(k) or 0
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None   # no usable token count: the estimate stands
        n[k] = v
    return (n["input_tokens"] * p["input"] + n["output_tokens"] * p["output"]
            + n["cache_creation_input_tokens"] * p["cache_write"] + n["cache_read_input_tokens"] * p["cache_read"]) / 1e6


def reset(service: str) -> list[str]:
    """Close the breaker of `service` (`all` = every service) and forget its request keys; returns what was open."""
    with _Tx() as c:
        if service == "all":
            rows = [r[0] for r in c.execute("SELECT service FROM breaker")]
            c.execute("DELETE FROM breaker")
            c.execute("DELETE FROM keys")
        else:
            rows = [r[0] for r in c.execute("SELECT service FROM breaker WHERE service=?", (service,))]
            c.execute("DELETE FROM breaker WHERE service=?", (service,))
            c.execute("DELETE FROM keys WHERE service=?", (service,))
    if rows:
        log.warning("spend breaker reset by hand: %s", ", ".join(rows))
    return rows


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _alert_dict(r: tuple) -> dict:
    aid, at, service, reason, used, cap, retry, message = r
    return {"alert_id": aid, "at": _iso(at), "service": service, "label": caps(service)["label"], "reason": reason,
            "used": used, "cap": cap, "retry_after_s": None if retry is None else round(retry),
            "until": None if retry is None else _iso(at + retry), "message": message}


def alerts_since(after_id: int = 0, *, within_s: float | None = None, limit: int = 50) -> list[dict]:
    """Stored alerts after `after_id` (and, with `within_s`, no older than that), oldest first."""
    c, lock = _conn()
    sql = "SELECT id, at, service, reason, used, cap, retry_after_s, message FROM alerts WHERE id>?"
    args: list[Any] = [after_id]
    if within_s is not None:
        sql += " AND at>?"
        args.append(_now() - within_s)
    sql += " ORDER BY id LIMIT ?"
    args.append(limit)
    if lock:
        with lock:
            rows = c.execute(sql, args).fetchall()
    else:
        rows = c.execute(sql, args).fetchall()
    return [_alert_dict(r) for r in rows]


def last_alert_id() -> int:
    c, lock = _conn()
    if lock:
        with lock:
            row = c.execute("SELECT MAX(id) FROM alerts").fetchone()
    else:
        row = c.execute("SELECT MAX(id) FROM alerts").fetchone()
    return int(row[0] or 0)


def event_payload(alert: dict) -> dict:
    """The live event `spend_alert`: {service, reason, used, cap} plus the label, the pause and the alert id."""
    return {k: alert[k] for k in ("alert_id", "service", "label", "reason", "used", "cap", "retry_after_s", "until",
                                  "at", "message")}


def usage() -> dict:
    """Per service: calls in the last minute, hour and day, estimated USD for the day, refused calls, the caps, the
    breaker; plus the day's USD total and the last alert."""
    cfg = load_config()
    c, lock = _conn()
    if lock:
        lock.acquire()
    try:
        now = _now()
        seen = {r[0] for r in c.execute("SELECT DISTINCT service FROM mins WHERE m>?", (int(now // 60) - DAY_MIN,))}
        names = [n for n in cfg["services"] if n != "default"] + sorted(seen - set(cfg["services"]))
        out: dict[str, Any] = {}
        for name in names:
            cap = caps(name)
            w = _window(c, name, now)
            b = c.execute("SELECT open_until, reason, opened_at FROM breaker WHERE service=?", (name,)).fetchone()
            is_open = bool(b and b[0] > now)
            out[name] = {
                "label": cap["label"], "last_minute": w["minute"], "last_hour": w["hour"], "last_day": w["day"],
                "usd_day": round(w["usd_day"], 4), "usd_tracked": cap["usd_per_call"] is not None
                or bool(cap["usd_per_mtok"]), "blocked_day": w["blocked_day"],
                "day_share": round(w["day"] / cap["per_day"], 4),
                "caps": {k: cap[k] for k in CAP_KEYS},
                "breaker": {"open": is_open, "reason": b[1] if is_open else None,
                            "retry_after_s": round(b[0] - now) if is_open else 0,
                            "until": _iso(b[0]) if is_open else None},
            }
        last = c.execute("SELECT id, at, service, reason, used, cap, retry_after_s, message FROM alerts "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        if lock:
            lock.release()
    return {"at": _iso(now), "window": {"minute": "last 60 s", "hour": "last 60 min", "day": "last 24 h"},
            "alert_at": cfg["alert_at"], "services": out,
            "usd_day_total": round(sum(v["usd_day"] for v in out.values()), 4),
            "last_alert": _alert_dict(last) if last else None}


# ------------------------------------------------------------------------------------------------------ CLI
def _table(u: dict) -> str:
    head = ("service", "min", "hour", "day", "day cap", "USD day", "USD cap", "blocked", "breaker")
    rows = [head]
    for name, s in u["services"].items():
        c = s["caps"]
        br = f"open {s['breaker']['reason']} {s['breaker']['retry_after_s']}s" if s["breaker"]["open"] else "closed"
        rows.append((name, f"{_fmt(s['last_minute'])}/{_fmt(c['per_minute'])}", _fmt(s["last_hour"]),
                     _fmt(s["last_day"]), _fmt(c["per_day"]), f"{s['usd_day']:.3f}" if s["usd_tracked"] else "-",
                     _fmt(c["usd_per_day"]), _fmt(s["blocked_day"]), br))
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(head))]
    lines = ["  ".join(str(v).ljust(widths[i]) for i, v in enumerate(r)) for r in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    lines.append(f"\nestimated USD in the last 24 h: {u['usd_day_total']:.3f}   (counters: {db_path()})")
    if u["last_alert"]:
        a = u["last_alert"]
        lines.append(f"last alert {a['at']}: {a['message']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m repairos.spend", description="Spend guards: status and reset.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="per-service counts, caps and breakers")
    r = sub.add_parser("reset", help="close a service's breaker now")
    r.add_argument("service", help="a service name, or `all`")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print(_table(usage()))
        return 0
    closed = reset(a.service)
    print(f"breaker closed: {', '.join(closed)}" if closed else f"no open breaker for {a.service}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
