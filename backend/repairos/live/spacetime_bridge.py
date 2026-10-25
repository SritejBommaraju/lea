"""Mirror the backend's live scan state into SpacetimeDB, for viewers that subscribe (the big-screen page).

    python -m repairos.live.spacetime_bridge                 run the mirror (a no-op exit 0 when not configured)
    python -m repairos.live.spacetime_bridge --check         print the configuration (no secrets) and reachability
    python -m repairos.live.spacetime_bridge --mint-identity FILE
                                                             make a new SpacetimeDB identity for the bridge; the token
                                                             goes to FILE (0600), only the public identity is printed

The backend stays the source of truth and does all compute; this process only reads it and writes a copy. It reads the
gateway's live events (GET /v1/sessions/{id}/events, SSE) and calls the module's reducers (spacetime/module) over
HTTP. When SpacetimeDB is down the writes wait and coalesce (the newest state of each row wins) and the backend and the
phone are untouched; when the gateway is down the event streams reconnect with backoff.

Sessions are found with GET /v1/sessions (the most recently active first), so the bridge can run on any machine
that reaches the gateway.

Image bytes never go to SpacetimeDB. With SPACETIME_MEDIA_PORT set, this process also serves still thumbnails and the
scan mesh at short-lived signed URLs (HMAC, expiry) and writes those URLs; without it there are no thumbnails or mesh.

Environment (secrets are never printed):
  SPACETIME_HOST          e.g. https://maincloud.spacetimedb.com or http://127.0.0.1:3000
  SPACETIME_DB            the database name, e.g. lea-live
  SPACETIME_TOKEN         the bridge identity's token (or SPACETIME_TOKEN_FILE: a file from --mint-identity)
  REPAIROS_GATEWAY        the gateway, default http://127.0.0.1:$PORT (PORT default 8000)
  REPAIROS_DEVICE_TOKEN   a paired device token; else REPAIROS_BRIDGE_TOKEN_FILE (default
                          $REPAIROS_DATA_DIR/live/spacetime_bridge.device_token), else the bridge pairs once with
                          REPAIROS_PAIRING_CODE and keeps the token in that file
  REPAIROS_DATA_DIR       where the bridge keeps its device token file, default <repo>/data
  SPACETIME_MEDIA_PORT    serve thumbnails + mesh on this port (off when unset)
  SPACETIME_MEDIA_BASE    the public URL of that port (default http://127.0.0.1:<port>)
  SPACETIME_MEDIA_TTL_S   signed URL lifetime, default 7200 (re-signed before it ends)
  SPACETIME_KEEP_SCANS    scans kept in the mirror, default 8
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import io
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("repairos.live.spacetime_bridge")

ROOT = Path(__file__).resolve().parents[3]
NOT_AN_ISSUE = {"ok", "insufficient_evidence", "unsure", "not_inspected", "not_seen_in_photo"}
SEVERITY_RANK = {"safety": 0, "functional": 1, "": 2, "cosmetic": 3}
MAX_SESSIONS = 6
DISCOVER_EVERY_S = 0.5
HEARTBEAT_EVERY_S = 10.0
NO_RETRY = {400, 401, 403, 404, 530}   # bad arguments, not allowed, no such reducer, the reducer refused


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _u32(v) -> int:
    try:
        return max(0, min(int(v or 0), 2**32 - 1))
    except (TypeError, ValueError):
        return 0


def _s(v) -> str:
    return "" if v is None else str(v)


# ------------------------------------------------------------------ SpacetimeDB writes

def verdict_line(rep: dict) -> str:
    """The verdict's words with its caveat (how many areas still need a closer photo): the viewer shows one line, and
    `no_problems_seen` without its caveat would read as more certain than the report is."""
    reason = rep.get("verdict_reason") or {}
    return " ".join(x for x in (_s(reason.get("text")), _s(reason.get("caveat"))) if x)

class Spacetime:
    """Calls reducers over HTTP (POST /v1/database/<db>/call/<reducer>, a JSON array of arguments)."""

    def __init__(self, host: str, db: str, token: str):
        u = urllib.parse.urlsplit(host.rstrip("/"))
        if u.scheme not in ("http", "https") or not u.hostname:
            raise SystemExit("SPACETIME_HOST must be an http(s) URL")
        self.https, self.netloc, self.base_path = u.scheme == "https", u.netloc, u.path
        self.db, self._token = db, token
        self._conn: http.client.HTTPConnection | None = None

    def _connection(self) -> http.client.HTTPConnection:
        if self._conn is None:
            cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
            self._conn = cls(self.netloc, timeout=20)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def call(self, reducer: str, args: list) -> tuple[int, str]:
        path = f"{self.base_path}/v1/database/{urllib.parse.quote(self.db)}/call/{reducer}?confirmed=false"
        body = json.dumps(args, separators=(",", ":"), allow_nan=False).encode()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self._token}"}
        for attempt in (0, 1):   # one silent retry on a stale keep-alive connection
            try:
                c = self._connection()
                c.request("POST", path, body=body, headers=headers)
                r = c.getresponse()
                text = r.read().decode(errors="replace")
                return r.status, text
            except (http.client.HTTPException, OSError):
                self.close()
                if attempt:
                    raise
        raise RuntimeError("unreachable")

    def ping(self) -> bool:
        try:
            c = (http.client.HTTPSConnection if self.https else http.client.HTTPConnection)(self.netloc, timeout=5)
            c.request("GET", f"{self.base_path}/v1/ping")
            ok = c.getresponse().status == 200
            c.close()
            return ok
        except OSError:
            return False


class Outbox:
    """Reducer calls waiting to go out, one per key (the row they write). A newer call for a key replaces the waiting
    one in its place, so a backlog while SpacetimeDB is down stays the size of the mirror, not of the event stream."""

    def __init__(self):
        self._items: OrderedDict[str, tuple[str, list]] = OrderedDict()
        self._cv = threading.Condition()

    def put(self, key: str, reducer: str, args: list) -> None:
        with self._cv:
            self._items[key] = (reducer, args)
            self._cv.notify()

    def take(self, timeout: float) -> tuple[str, str, list] | None:
        with self._cv:
            if not self._items and not self._cv.wait_for(lambda: bool(self._items), timeout):
                return None
            key, (reducer, args) = self._items.popitem(last=False)
            return key, reducer, args

    def put_back(self, key: str, reducer: str, args: list) -> None:
        with self._cv:
            if key not in self._items:   # a newer call for this key already waits: it wins
                self._items[key] = (reducer, args)
                self._items.move_to_end(key, last=False)

    def __len__(self) -> int:
        with self._cv:
            return len(self._items)


def _pace_s() -> float:
    """Seconds between reducer calls: the `spacetime` per_minute cap spread evenly (50 a second by default), so a
    burst waits in the outbox, where newer writes of the same row replace older ones (coalesced)."""
    try:
        from repairos import spend
        return 60.0 / spend.caps("spacetime")["per_minute"]
    except Exception:  # noqa: BLE001 - no spend config: the default pace
        return 0.02


def _charge() -> float | None:
    """The spend guard before one reducer call (repairos.spend): None = go, else seconds to wait (the breaker is
    open). The waiting write stays in the outbox, coalesced; the backend and the phone are untouched."""
    from repairos import spend
    try:
        spend.charge("spacetime")
    except spend.SpendLimit as e:
        return max(1.0, min(e.retry_after_s, 30.0))
    except Exception as e:  # noqa: BLE001 - a broken spend config holds the writes rather than killing the writer
        log.error("spend guard unusable (%s: %s); writes held for 30 s", type(e).__name__, e)
        return 30.0
    return None


def writer_loop(st: Spacetime, outbox: Outbox, stop: threading.Event) -> None:
    backoff, down, refused, paused = 0.5, False, set(), False
    pace, last = _pace_s(), 0.0
    while not stop.is_set():
        item = outbox.take(timeout=1.0)
        if item is None:
            continue
        key, reducer, args = item
        wait = last + pace - time.monotonic()
        if wait > 0:
            stop.wait(wait)
        hold = _charge()
        if hold is not None:
            outbox.put_back(key, reducer, args)
            if not paused:
                log.error("SpacetimeDB writes paused: usage limit reached; %d waiting, coalesced", len(outbox))
                paused = True
            stop.wait(hold)
            continue
        if paused:
            log.info("SpacetimeDB writes resumed after the usage pause")
            paused = False
        last = time.monotonic()
        try:
            status, text = st.call(reducer, args)
        except OSError as e:
            outbox.put_back(key, reducer, args)
            if not down:
                log.warning("SpacetimeDB unreachable (%s); writes wait and coalesce (%d waiting)", type(e).__name__,
                            len(outbox))
                down = True
            stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)
            continue
        if status < 300:
            if down:
                log.info("SpacetimeDB reachable again")
            down, backoff = False, 0.5
        elif status in NO_RETRY:
            if (reducer, status) not in refused:   # say it once per reducer, not once per row
                refused.add((reducer, status))
                log.error("SpacetimeDB refused %s (HTTP %d): %s", reducer, status, text[:300])
        else:
            outbox.put_back(key, reducer, args)
            log.warning("SpacetimeDB %s: HTTP %d; retrying", reducer, status)
            stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)


# ------------------------------------------------------------------ the gateway
class Gateway:
    def __init__(self, base: str, data_dir: Path):
        self.base = base.rstrip("/")
        self.data_dir = data_dir
        self._token: str | None = None
        self._lock = threading.Lock()

    def _token_file(self) -> Path:
        return Path(os.environ.get("REPAIROS_BRIDGE_TOKEN_FILE") or
                    self.data_dir / "live" / "spacetime_bridge.device_token")

    def token(self, renew: bool = False) -> str:
        with self._lock:
            if self._token and not renew:
                return self._token
            env = os.environ.get("REPAIROS_DEVICE_TOKEN", "").strip()
            if env and not renew:
                self._token = env
                return env
            f = self._token_file()
            if f.exists() and not renew:
                self._token = f.read_text().strip()
                return self._token
            code = os.environ.get("REPAIROS_PAIRING_CODE", "").strip()
            if not code:
                try:
                    from repairos.config import get_settings
                    secret = get_settings().repairos_pairing_code
                    code = secret.get_secret_value() if secret else ""
                except Exception:  # noqa: BLE001 - settings are optional here; the message below says what is missing
                    code = ""
            if not code:
                raise RuntimeError("no device token: set REPAIROS_DEVICE_TOKEN or REPAIROS_PAIRING_CODE")
            body = json.dumps({"code": code, "name": "spacetime-bridge"}).encode()
            req = urllib.request.Request(self.base + "/v1/devices/pair", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                tok = json.loads(r.read())["token"]
            f.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(tok)
            log.info("paired with the gateway as device 'spacetime-bridge' (token kept in %s)", f)
            self._token = tok
            return tok

    def request(self, path: str, timeout: float = 20, headers: dict | None = None):
        """GET a gateway path with the device token; a token the gateway no longer knows is renewed once."""
        for attempt in (0, 1):
            req = urllib.request.Request(self.base + path, headers={
                "Authorization": f"Bearer {self.token(renew=attempt == 1)}", **(headers or {})})
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code == 403 and attempt == 0 and not os.environ.get("REPAIROS_DEVICE_TOKEN"):
                    continue
                raise
        raise RuntimeError("unreachable")

    def get_json(self, path: str):
        with self.request(path) as r:
            return json.loads(r.read())

    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/healthz", timeout=5) as r:
                return r.status == 200
        except OSError:
            return False


class Discovery:
    """Which sessions to follow, from GET /v1/sessions (the most recently active first)."""

    def __init__(self, gw: Gateway, lookback_s: float):
        self.gw, self.lookback_s = gw, lookback_s
        self.seen: dict[str, int] = {}   # session_id -> its newest event id at the last look
        self._said = False

    def _list(self) -> list[dict]:
        try:
            body = self.gw.get_json(f"/v1/sessions?limit={MAX_SESSIONS}")
        except urllib.error.HTTPError as e:
            if e.code in (404, 405) and not self._said:
                log.error("the gateway has no GET /v1/sessions (an older gateway?): no sessions to follow")
                self._said = True
            return []
        items = body.get("sessions", []) if isinstance(body, dict) else body
        return [r for r in items if isinstance(r, dict) and r.get("session_id")]

    def start(self) -> list[tuple[str, int]]:
        """(session_id, replay from event id) for the sessions to follow at start: those active in the lookback, or
        else the latest one."""
        rows = self._list()
        since = (datetime.now(UTC) - timedelta(seconds=self.lookback_s)).strftime("%Y-%m-%dT%H:%M:%S")
        active = [r for r in rows if (r.get("last_event_at") or "") >= since] or rows[:1]
        for r in rows:
            self.seen[r["session_id"]] = int(r.get("last_event_id") or 0)
        return [(r["session_id"], 0) for r in active]

    def poll(self) -> list[str]:
        """Sessions with something new since the last look (a new session, or newer events)."""
        out = []
        for r in self._list():
            sid, last = r["session_id"], int(r.get("last_event_id") or 0)
            if last > self.seen.get(sid, -1):
                out.append(sid)
            self.seen[sid] = last
        return out


# ------------------------------------------------------------------ signed media URLs (thumbnails + mesh)
class Media:
    def __init__(self, gw: Gateway, secret: str, port: int, base: str, ttl_s: int):
        self.gw, self.port, self.base, self.ttl_s = gw, port, base.rstrip("/"), ttl_s
        self.key = hashlib.sha256(b"lea-media-url:" + secret.encode()).digest()
        self._thumbs: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.Lock()

    def _sig(self, path: str, e: int) -> str:
        return hmac.new(self.key, f"{path}|{e}".encode(), hashlib.sha256).hexdigest()[:32]

    def url(self, path: str, extra: str = "") -> str:
        e = int(time.time()) + self.ttl_s
        return f"{self.base}{path}?e={e}&s={self._sig(path, e)}{extra}"

    def still_url(self, still_id: str) -> str:
        return self.url(f"/m/still/{urllib.parse.quote(still_id)}.jpg")

    def mesh_url(self, scan_id: str, which: str, rev: int) -> str:
        return self.url(f"/m/{which}/{urllib.parse.quote(scan_id)}.ply", f"&rev={rev}")

    def verify(self, path: str, query: dict) -> bool:
        try:
            e, s = int(query.get("e", ["0"])[0]), query.get("s", [""])[0]
        except ValueError:
            return False
        return e >= time.time() and hmac.compare_digest(self._sig(path, e), s)

    def thumbnail(self, still_id: str) -> bytes:
        with self._lock:
            if still_id in self._thumbs:
                self._thumbs.move_to_end(still_id)
                return self._thumbs[still_id]
        from PIL import Image   # the backend's dependency; only needed when the media server is on

        still = self.gw.get_json(f"/v1/stills/{urllib.parse.quote(still_id)}")
        ref = still.get("image_file") or ""
        m = re.fullmatch(r"blob://sha256:([0-9a-f]{64})", ref)
        if not m:
            raise FileNotFoundError(still_id)
        with self.gw.request(f"/v1/blobs/{m.group(1)}", timeout=30) as r:
            raw = r.read()
        im = Image.open(io.BytesIO(raw))
        im.draft("RGB", (640, 640))   # JPEG: decode at a reduced scale, fast
        im = im.convert("RGB")
        im.thumbnail((360, 360))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=78)
        data = out.getvalue()
        with self._lock:
            self._thumbs[still_id] = data
            while len(self._thumbs) > 300:
                self._thumbs.popitem(last=False)
        return data

    def serve(self, stop: threading.Event) -> None:
        media = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):   # quiet: no URLs (signatures) in the logs
                pass

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

            def do_OPTIONS(self):  # noqa: N802
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_GET(self):  # noqa: N802
                u = urllib.parse.urlsplit(self.path)
                q = urllib.parse.parse_qs(u.query)
                m = re.fullmatch(r"/m/(still|mesh|cloud)/([A-Za-z0-9_.-]+)\.(jpg|ply)", u.path)
                if not m or not media.verify(u.path, q):
                    self.send_response(403)
                    self._cors()
                    self.end_headers()
                    return
                what, oid = m.group(1), urllib.parse.unquote(m.group(2))
                try:
                    if what == "still":
                        data = media.thumbnail(oid)
                        self.send_response(200)
                        self._cors()
                        self.send_header("Content-Type", "image/jpeg")
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Cache-Control", "public, max-age=3600")
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    with media.gw.request(f"/v1/scans/{urllib.parse.quote(oid)}/{what}", timeout=60) as r:
                        self.send_response(200)
                        self._cors()
                        self.send_header("Content-Type", "application/x-ply")
                        if r.headers.get("Content-Length"):
                            self.send_header("Content-Length", r.headers["Content-Length"])
                        self.send_header("Cache-Control", "public, max-age=3600")
                        self.end_headers()
                        while chunk := r.read(1 << 20):
                            self.wfile.write(chunk)
                except (urllib.error.HTTPError, FileNotFoundError):
                    self.send_response(404)
                    self._cors()
                    self.end_headers()
                except (OSError, ValueError) as e:
                    log.warning("media %s %s: %s", what, oid, type(e).__name__)
                    try:
                        self.send_response(502)
                        self._cors()
                        self.end_headers()
                    except OSError:
                        pass

        srv = ThreadingHTTPServer((os.environ.get("SPACETIME_MEDIA_HOST", "0.0.0.0"), self.port), Handler)
        srv.daemon_threads = True
        log.info("serving thumbnails and the mesh on :%d (signed URLs, %d s)", self.port, self.ttl_s)
        threading.Thread(target=srv.serve_forever, name="media", daemon=True).start()
        stop.wait()
        srv.shutdown()


# ------------------------------------------------------------------ the mirror
class Mirror:
    """Turns backend row events into reducer calls. Only changes go out (the same state twice is sent once)."""

    def __init__(self, outbox: Outbox, media: Media | None, keep_scans: int):
        self.outbox, self.media, self.keep = outbox, media, keep_scans
        self._lock = threading.Lock()
        self._sent: dict[str, str] = {}
        self._scans: OrderedDict[str, dict] = OrderedDict()   # scan_id -> {session, status, report_rev, stills, mesh}
        self.events = 0
        self._focus_at = ""

    def _put(self, key: str, reducer: str, args: list, digest_args: list | None = None) -> None:
        d = hashlib.sha256(json.dumps(digest_args if digest_args is not None else args[:-1], default=str)
                           .encode()).hexdigest()
        if self._sent.get(key) == d:
            return
        self._sent[key] = d
        self.outbox.put(key, reducer, args)

    def _scan(self, scan_id: str, session_id: str, at: str) -> dict:
        s = self._scans.get(scan_id)
        if s is None:
            s = self._scans[scan_id] = {"session": session_id, "status": "capturing", "error": "", "report_rev": 0,
                                        "stills": {}, "mesh": None, "places": {}, "members": {}}
            self._put(f"scan:{scan_id}", "upsert_scan", [scan_id, session_id, "capturing", "", at])
            while len(self._scans) > self.keep:
                old, gone = self._scans.popitem(last=False)
                keys = {f"{p}:{old}" for p in ("scan", "ident", "prog", "mesh", "rep")}
                keys |= {f"still:{x}" for x in gone["stills"]}
                for k in [k for k in self._sent if k in keys or k.startswith(f"f:{old}:")]:
                    self._sent.pop(k, None)
                self.outbox.put(f"forget:{old}", "forget_scan", [old])
        return s

    def _follow(self, scan_id: str, at: str) -> None:
        """The big screen follows the scan with the newest capture activity (event time, not arrival order: the
        sessions replay in parallel at start)."""
        self._scans.move_to_end(scan_id)
        if at >= self._focus_at:
            self._focus_at = at
            self._put("focus", "follow_scan", [scan_id, at])

    def _status(self, scan_id: str, s: dict, at: str) -> None:
        self._put(f"scan:{scan_id}", "upsert_scan", [scan_id, s["session"], s["status"], s["error"], at])

    def handle(self, session_id: str, ev: dict) -> None:
        kind, row, at = ev.get("kind"), ev.get("row") or {}, ev.get("at") or _now_iso()
        scan_id = row.get("scan_id")
        if not scan_id or kind not in ("still", "scan-package", "pipeline-run", "finding", "report"):
            return
        with self._lock:
            self.events += 1
            s = self._scan(scan_id, session_id, at)
            if kind == "still":
                self._still(scan_id, s, row, at)
                self._follow(scan_id, at)
            elif kind == "scan-package":
                self._status(scan_id, s, at)
                self._follow(scan_id, at)
            elif kind == "pipeline-run":
                self._run(scan_id, s, row, at)
            elif kind == "finding":
                self._finding(scan_id, s, row, at)
            elif kind == "report":
                self._report(scan_id, s, row, int(ev.get("rev") or 1), at)

    def _still(self, scan_id: str, s: dict, row: dict, at: str) -> None:
        sid = row["still_id"]
        info = [sid, scan_id, s["session"], _u32(row.get("segment")), _s(row.get("kind")), _s(row.get("captured_at"))]
        s["stills"][sid] = info
        url = self.media.still_url(sid) if self.media else ""
        self._put(f"still:{sid}", "add_still", [*info, url, at], digest_args=info)

    def _run(self, scan_id: str, s: dict, row: dict, at: str) -> None:
        s["status"], s["error"] = _s(row.get("status")) or s["status"], _s(row.get("error"))[:500]
        self._status(scan_id, s, at)
        ident = row.get("identity") or {}
        if ident.get("status") == "done":
            self._put(f"ident:{scan_id}", "set_identity", [
                scan_id, s["session"], _s(ident.get("category")) or "unknown", _s(ident.get("brand")),
                _s(ident.get("model")), _s(ident.get("confidence")) or "low", _s(ident.get("label_text")), at])
        p = row.get("progress") or {}
        if p:
            self._put(f"prog:{scan_id}", "set_progress", [
                scan_id, s["session"], *(_u32(p.get(k)) for k in ("stills_received", "stills_done", "stills_inspected",
                                                                   "areas_checked", "could_not_judge", "to_look_at")),
                at])
        m = row.get("mesh") or {}
        if m.get("status") == "done":
            s["mesh"] = m
            self._mesh(scan_id, s, at)
        elif m.get("status") in ("unavailable", "failed"):
            self._put(f"mesh:{scan_id}", "set_mesh", [scan_id, s["session"], 0, "", "", 0, "", "", 0,
                                                     _s(m.get("reason"))[:300] or "no mesh", at])

    def _mesh(self, scan_id: str, s: dict, at: str, force: bool = False) -> None:
        m, rev = s["mesh"], _u32(s["mesh"].get("rev"))
        url = self.media.mesh_url(scan_id, "mesh", rev) if self.media else ""
        cloud = self.media.mesh_url(scan_id, "cloud", rev) if self.media and m.get("cloud_ref") else ""
        base = [scan_id, s["session"], rev, _s(m.get("format")) or "ply", _u32(m.get("triangles")),
                _s(m.get("hero")) or "mesh"]
        args = [*base[:3], url, *base[3:], cloud, _u32(m.get("cloud_points")), "", at]
        self._put(f"mesh:{scan_id}", "set_mesh", args, digest_args=args[:-1] if force else [*base, bool(url)])

    def _finding(self, scan_id: str, s: dict, row: dict, at: str) -> None:
        """One live row per place on the scan (place_key): a region another still's answer was reused for is the same
        place, so it updates that row instead of adding a card. The row shows the place's most severe answer."""
        if row.get("state") in NOT_AN_ISSUE or s["status"] in ("done", "failed", "closed") and s["report_rev"]:
            return
        rid = s["places"].setdefault(place_key(row), row["finding_id"])
        members = s["members"].setdefault(rid, {})
        members[row["finding_id"]] = {"issue": _s(row.get("state")), "severity": _s(row.get("severity")),
                                      "words": _s(row.get("part_words")) or _s((row.get("evidence") or [""])[0])}
        lead = min(members.values(), key=lambda m: SEVERITY_RANK.get(m["severity"], 2))
        words = members.get(rid, lead)["words"]
        self._put(f"f:{scan_id}:{rid}", "upsert_finding", [scan_id, s["session"], {
            "finding_id": rid, "rank": 1000, "issue": lead["issue"], "severity": lead["severity"],
            "confidence_word": "", "words": words[:300], "part_class": "", "has_point": False,
            "x": 0.0, "y": 0.0, "z": 0.0}, at])

    def _report(self, scan_id: str, s: dict, rep: dict, rev: int, at: str) -> None:
        s["report_rev"] = rev
        rows = []
        issues = [x for x in rep.get("findings") or [] if x.get("issue") not in (None, "none")]
        covered = {fid for x in issues for fid in [*(x.get("finding_ids") or [])[1:], *(x.get("superseded_ids") or [])]
                   if fid != x["finding_id"]}   # a row another row stands for (collapsed or superseded) goes
        for i, f in enumerate(x for x in issues if x["finding_id"] not in covered):
            loc = f.get("location") or {}
            p = loc.get("point_m") if loc.get("status") == "computed" else None
            rows.append({"finding_id": f["finding_id"], "rank": i, "issue": _s(f.get("issue")),
                         "severity": _s(f.get("severity")) or "functional", "confidence_word": _s(f.get("confidence_word")),
                         "words": (_s(f.get("part_words")) or _s((f.get("evidence") or [""])[0]))[:300],
                         "part_class": _s(f.get("part_class")), "has_point": bool(p),
                         "x": float(p[0]) if p else 0.0, "y": float(p[1]) if p else 0.0, "z": float(p[2]) if p else 0.0})
        for k in [k for k in self._sent if k.startswith(f"f:{scan_id}:")]:
            self._sent.pop(k)
        s["places"], s["members"] = {}, {}
        self._put(f"rep:{scan_id}", "set_report", [
            scan_id, s["session"], _s(rep.get("report_id")), rev, _s(rep.get("verdict")),
            verdict_line(rep), _s((rep.get("summary") or {}).get("text")),
            [_s(x.get("text")) for x in rep.get("safety") or [] if x.get("text")], len(rows),
            len(rep.get("cosmetic") or []), len(rep.get("not_inspected") or []),
            len(rep.get("insufficient_evidence") or []), rows, at])

    def resign(self) -> None:
        """Signed URLs expire: write fresh ones for the scans in the mirror."""
        if not self.media:
            return
        with self._lock:
            at = _now_iso()
            for scan_id, s in self._scans.items():
                for info in s["stills"].values():
                    self.outbox.put(f"still:{info[0]}", "add_still", [*info, self.media.still_url(info[0]), at])
                if s["mesh"]:
                    self._mesh(scan_id, s, at, force=True)


def place_key(finding: dict) -> str:
    """The backend's identity of a place on the scan for a live finding: the still and crop box that were judged
    (pipeline/regions.py: a region another still already judged reuses that answer, with that still's crop and box
    in its evidence_refs; pipeline/run.py progress_counts counts places the same way). A finding without a crop box
    is its own place."""
    refs = finding.get("evidence_refs") or {}
    still, box = (refs.get("still_ids") or [""])[0], refs.get("crop_box_px")
    if still and box:
        return f"{still}@{','.join(str(round(float(v))) for v in box)}"
    return f"id:{finding['finding_id']}"


def follow_session(gw: Gateway, mirror: Mirror, sid: str, after: int, stop: threading.Event) -> None:
    """One session's SSE stream, reconnecting with backoff and resuming from the last event id."""
    cursor, backoff = after, 1.0
    while not stop.is_set():
        try:
            with gw.request(f"/v1/sessions/{urllib.parse.quote(sid)}/events?after={cursor}", timeout=60,
                            headers={"Accept": "text/event-stream"}) as r:
                backoff = 1.0
                event, data, eid = "", [], None
                for raw in r:
                    if stop.is_set():
                        return
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        if data and "." in event:   # `<kind>.<op>` row events; the named ones repeat them
                            try:
                                mirror.handle(sid, json.loads("\n".join(data)))
                            except (ValueError, KeyError, TypeError) as e:
                                log.warning("session %s: skipped an event (%s)", sid, type(e).__name__)
                        if eid and eid.isdigit():
                            cursor = int(eid)
                        event, data, eid = "", [], None
                        continue
                    if line.startswith(":"):
                        continue
                    field, _, value = line.partition(":")
                    value = value[1:] if value.startswith(" ") else value
                    if field == "event":
                        event = value
                    elif field == "data":
                        data.append(value)
                    elif field == "id":
                        eid = value
        except urllib.error.HTTPError as e:
            if e.code == 404:
                log.info("session %s is gone; not following it", sid)
                return
            log.warning("session %s events: HTTP %d; reconnecting in %.0f s", sid, e.code, backoff)
        except (OSError, RuntimeError) as e:
            log.warning("session %s events: %s; reconnecting in %.0f s", sid, type(e).__name__, backoff)
        stop.wait(backoff)
        backoff = min(backoff * 2, 30.0)


# ------------------------------------------------------------------ main
def _spacetime_token() -> str:
    tok = os.environ.get("SPACETIME_TOKEN", "").strip()
    f = os.environ.get("SPACETIME_TOKEN_FILE", "").strip()
    if not tok and f and Path(f).exists():
        text = Path(f).read_text().strip()
        try:
            tok = json.loads(text)["token"]
        except (ValueError, KeyError, TypeError):
            tok = text
    return tok


def _config() -> dict:
    data_dir = Path(os.environ.get("REPAIROS_DATA_DIR") or ROOT / "data").expanduser()
    port = os.environ.get("SPACETIME_MEDIA_PORT", "").strip()
    return {
        "host": os.environ.get("SPACETIME_HOST", "").strip(), "db": os.environ.get("SPACETIME_DB", "").strip(),
        "token": _spacetime_token(), "data_dir": data_dir,
        "gateway": os.environ.get("REPAIROS_GATEWAY") or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}",
        "media_port": int(port) if port.isdigit() else None,
        "media_base": os.environ.get("SPACETIME_MEDIA_BASE", "").strip() or (f"http://127.0.0.1:{port}" if port else ""),
        "media_ttl": int(os.environ.get("SPACETIME_MEDIA_TTL_S") or 7200),
        "keep": int(os.environ.get("SPACETIME_KEEP_SCANS") or 8),
        "lookback": float(os.environ.get("SPACETIME_LOOKBACK_S") or 1800),
    }


def mint_identity(host: str, path: Path) -> int:
    if not host:
        raise SystemExit("set SPACETIME_HOST first (e.g. https://maincloud.spacetimedb.com)")
    req = urllib.request.Request(host.rstrip("/") + "/v1/identity", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"identity": d["identity"], "token": d["token"]}, fh)
    print(d["identity"])
    print(f"token written to {path} (0600). Publish with WRITER_IDENTITY={d['identity']} and run the bridge with "
          f"SPACETIME_TOKEN_FILE={path}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="print the configuration (no secrets) and reachability")
    ap.add_argument("--mint-identity", metavar="FILE", help="make a bridge identity; the token goes to FILE")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = _config()
    if a.mint_identity:
        return mint_identity(cfg["host"], Path(a.mint_identity).expanduser())
    if not (cfg["host"] and cfg["db"] and cfg["token"]):
        log.info("SPACETIME_HOST, SPACETIME_DB and SPACETIME_TOKEN are not all set: the live mirror is off")
        return 0
    st = Spacetime(cfg["host"], cfg["db"], cfg["token"])
    gw = Gateway(cfg["gateway"], cfg["data_dir"])
    if a.check:
        print(json.dumps({"spacetime_host": cfg["host"], "spacetime_db": cfg["db"], "spacetime_token": "set",
                          "spacetime_ping": st.ping(), "gateway": cfg["gateway"], "gateway_ok": gw.healthy(),
                          "data_dir": str(cfg["data_dir"]),
                          "media": cfg["media_base"] or "off"}, indent=1))
        return 0

    stop = threading.Event()
    outbox = Outbox()
    media = Media(gw, cfg["token"], cfg["media_port"], cfg["media_base"], cfg["media_ttl"]) \
        if cfg["media_port"] else None
    mirror = Mirror(outbox, media, cfg["keep"])
    threading.Thread(target=writer_loop, args=(st, outbox, stop), name="writer", daemon=True).start()
    if media:
        threading.Thread(target=media.serve, args=(stop,), name="media-server", daemon=True).start()
    log.info("mirroring %s -> %s/%s (media %s)", cfg["gateway"], cfg["host"], cfg["db"],
             "on" if media else "off")

    followed: OrderedDict[str, threading.Event] = OrderedDict()

    def follow(sid: str, after: int) -> None:
        if sid in followed:
            followed.move_to_end(sid)
            return
        ev = threading.Event()
        followed[sid] = ev
        threading.Thread(target=follow_session, args=(gw, mirror, sid, after, ev), name=f"sse-{sid}",
                         daemon=True).start()
        log.info("following session %s (from event %d)", sid, after)
        while len(followed) > MAX_SESSIONS:
            _old, old_ev = followed.popitem(last=False)
            old_ev.set()

    disc = Discovery(gw, cfg["lookback"])
    started = False
    last_beat = last_sign = 0.0
    try:
        while True:
            try:
                if not started:
                    for sid, after in disc.start():
                        follow(sid, after)
                    started = True
                else:
                    for sid in disc.poll():
                        follow(sid, 0)
            except Exception as e:  # noqa: BLE001 - discovery retries; the mirror never takes the backend down
                log.warning("session discovery: %s", type(e).__name__)
            now = time.monotonic()
            if now - last_beat > HEARTBEAT_EVERY_S:
                ok = gw.healthy()
                outbox.put("hb", "heartbeat", [ok, f"following {len(followed)} session(s), {mirror.events} events"])
                last_beat = now
            if media and now - last_sign > media.ttl_s / 2:
                if last_sign:
                    mirror.resign()
                last_sign = now
            time.sleep(DISCOVER_EVERY_S)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for ev in followed.values():
            ev.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
