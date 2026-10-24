"""B5 gateway (Phase 1): pairing, chunked resumable hash-checked uploads, scan/still/reading ingestion validated
against the contracts and the capture files, reads, SSE. Runs a real uvicorn server on localhost; the client is
stdlib urllib. Bodies are the public contract examples; files are format-only fixtures (not captures)."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import socket
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

from repairos.api.app import create_app
from repairos.sessions.validate import validate_session
from repairos.slots.blob_local import LocalBlobStore
from repairos.slots.state_sqlite import SqliteState

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "replay"))
from format_files import jpeg_header, ply_bytes  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[3] / "spec" / "contracts" / "examples"
CODE = "pairing-code-for-tests-only"


def ex(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


class Client:
    def __init__(self, base: str):
        self.base, self.token = base, None

    def req(self, method: str, path: str, body=None, headers=None, raw: bytes | None = None, auth=True):
        h = dict(headers or {})
        data = raw
        if body is not None:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        if auth and self.token:
            h.setdefault("Authorization", f"Bearer {self.token}")
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                payload = resp.read()
                return resp.status, (json.loads(payload) if payload and "json" in resp.headers.get("Content-Type", "")
                                     else payload), resp.headers
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                payload = json.loads(payload)
            except ValueError:
                pass
            return e.code, payload, e.headers

    def upload(self, data: bytes, content_type="application/octet-stream") -> str:
        status, out, _ = self.req("PUT", f"/v1/blobs/{hashlib.sha256(data).hexdigest()}", raw=data,
                                  headers={"Content-Type": content_type})
        assert status in (200, 201), out
        return out["ref"]


@contextmanager
def serve(app):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not server.started:
        assert time.time() < deadline, "gateway did not start"
        time.sleep(0.02)
    c = Client(f"http://127.0.0.1:{port}")
    c.port = port
    try:
        yield c
    finally:
        server.should_exit = True
        t.join(timeout=10)


@pytest.fixture
def gw(tmp_path):
    state, blobs = SqliteState(tmp_path / "state.sqlite3"), LocalBlobStore(tmp_path / "blobs")
    with serve(create_app(state, blobs, pairing_code=CODE, recordings=tmp_path / "sessions")) as c:
        status, out, _ = c.req("POST", "/v1/devices/pair", {"code": CODE, "name": "test phone"})
        assert status == 201
        c.token = out["token"]
        c.state, c.blobs, c.tmp = state, blobs, tmp_path
        yield c


def make_scan(c: Client, session_id: str, *, n_points=4, kf_size=None) -> dict:
    pkg = ex("scan-package")
    pkg.pop("alignment")
    pkg["session_id"] = session_id
    pkg["points_file"] = c.upload(ply_bytes([(0.0, 0.0, -0.4 - i / 100) for i in range(n_points)]))
    pkg["point_count"] = n_points
    for kf in pkg["keyframes"]:
        w, h = kf_size or (kf["intrinsics"]["width_px"], kf["intrinsics"]["height_px"])
        kf["image_file"] = c.upload(jpeg_header(w, h) + kf["keyframe_id"].encode(), "image/jpeg")
        for fld in ("depth_file", "confidence_file"):
            if fld in kf:
                kf[fld] = c.upload(b"\x89PNG format fixture " + fld.encode(), "image/png")
    return pkg


def make_still(c: Client, pkg: dict, size=None) -> dict:
    st = ex("still")
    st["session_id"], st["scan_id"] = pkg["session_id"], pkg["scan_id"]
    w, h = size or (st["width_px"], st["height_px"])
    st["image_file"] = c.upload(jpeg_header(w, h), "image/jpeg")
    for fld in ("depth_file", "confidence_file"):
        if fld in st:
            st[fld] = c.upload(b"\x89PNG still " + fld.encode(), "image/png")
    return st


def new_session(c: Client) -> str:
    status, out, _ = c.req("POST", "/v1/sessions", {"symptom": "no output when switched on"})
    assert status == 201
    return out["session_id"]


# ---------------------------------------------------------------------- tests
def test_health_and_openapi(gw):
    assert gw.req("GET", "/healthz", auth=False)[0] == 200
    status, spec, _ = gw.req("GET", "/openapi.json", auth=False)
    assert status == 200 and "/v1/scans" in spec["paths"] and "/v1/blobs/{sha256}" in spec["paths"]


def test_pairing_and_device_auth(gw):
    assert gw.req("POST", "/v1/devices/pair", {"code": "wrong"}, auth=False)[0] == 403
    assert gw.req("POST", "/v1/sessions", {}, auth=False)[0] == 401
    assert gw.req("POST", "/v1/sessions", {}, headers={"Authorization": "Bearer nope"})[0] == 403
    assert gw.req("POST", "/v1/sessions", {})[0] == 201


def test_pairing_without_a_configured_code_is_refused(tmp_path, monkeypatch):
    import repairos.api.app as appmod
    from repairos.config import Settings
    monkeypatch.delenv("REPAIROS_PAIRING_CODE", raising=False)
    monkeypatch.setattr(appmod, "get_settings", lambda: Settings(_env_file=None))
    with serve(create_app(SqliteState(tmp_path / "s.db"), LocalBlobStore(tmp_path / "b"))) as c:
        status, out, _ = c.req("POST", "/v1/devices/pair", {"code": "anything"})
    assert status == 503 and "REPAIROS_PAIRING_CODE" in out["detail"]


def test_blob_upload_whole_duplicate_and_hash_mismatch(gw):
    data = os.urandom(1000)
    sha = hashlib.sha256(data).hexdigest()
    status, out, _ = gw.req("PUT", f"/v1/blobs/{sha}", raw=data)
    assert status == 201 and out["ref"] == f"blob://sha256:{sha}" and out["size"] == 1000
    status, out2, _ = gw.req("PUT", f"/v1/blobs/{sha}", raw=data)
    assert status == 200 and out2["ref"] == out["ref"]
    status, body, _ = gw.req("GET", f"/v1/blobs/{sha}")
    assert status == 200 and body == data
    bad = hashlib.sha256(b"other").hexdigest()
    status, out, _ = gw.req("PUT", f"/v1/blobs/{bad}", raw=data)
    assert status == 422 and out["error"] == "hash_mismatch"
    assert not gw.blobs.exists(f"blob://sha256:{bad}")
    assert gw.req("PUT", "/v1/blobs/not-a-hash", raw=data)[0] == 400


def test_presigned_blob_url_needs_no_token(gw):
    ref = gw.upload(b"presigned bytes")
    url = gw.blobs.presign(ref, 60)
    status, body, _ = gw.req("GET", url, auth=False)
    assert status == 200 and body == b"presigned bytes"
    assert gw.req("GET", url.replace("sig=", "sig=0"), auth=False)[0] == 401


def test_5mb_upload_resumes_after_a_dropped_connection(gw):
    data = os.urandom(5 * 1024 * 1024)
    sha, total = hashlib.sha256(data).hexdigest(), len(data)
    first = 2 * 1024 * 1024
    status, out, h = gw.req("PUT", f"/v1/blobs/{sha}", raw=data[:first],
                            headers={"Content-Range": f"bytes 0-{first - 1}/{total}"})
    assert status == 202 and int(h["Upload-Offset"]) == first
    # Drop the connection half way through the second chunk.
    end = total - 1
    with socket.create_connection(("127.0.0.1", gw.port)) as s:
        head = (f"PUT /v1/blobs/{sha} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {gw.token}\r\n"
                f"Content-Range: bytes {first}-{end}/{total}\r\nContent-Length: {total - first}\r\n\r\n").encode()
        s.sendall(head + data[first:first + 1024 * 1024])
    time.sleep(0.5)
    status, _, h = gw.req("HEAD", f"/v1/blobs/{sha}")
    offset = int(h["Upload-Offset"])
    assert status == 204 and first <= offset < total
    # A chunk at the wrong offset is refused with the right one.
    status, out, h = gw.req("PUT", f"/v1/blobs/{sha}", raw=data[:10], headers={"Content-Range": f"bytes 0-9/{total}"})
    assert status == 409 and int(h["Upload-Offset"]) == offset
    status, out, _ = gw.req("PUT", f"/v1/blobs/{sha}", raw=data[offset:],
                            headers={"Content-Range": f"bytes {offset}-{end}/{total}"})
    assert status == 201 and out["size"] == total
    assert gw.blobs.get(out["ref"]) == data


def test_scan_package_upload_validated_and_recorded(gw):
    sid = new_session(gw)
    pkg = make_scan(gw, sid)
    status, out, _ = gw.req("POST", "/v1/scans", pkg)
    assert status == 201 and out["rev"] == 1 and out["jobs"]
    status, again, _ = gw.req("POST", "/v1/scans", copy.deepcopy(pkg))  # retry: same row, no second job
    assert status == 201 and again["rev"] == 1 and again["jobs"] == out["jobs"]
    job = asyncio.run(gw.state.get_job(out["jobs"][0]))
    assert job.kind == "detect" and job.attempts == 0 and job.scan_id == pkg["scan_id"]
    status, got, _ = gw.req("GET", f"/v1/scans/{pkg['scan_id']}")
    assert status == 200 and got["scan"] == pkg
    st = make_still(gw, pkg)
    status, out, _ = gw.req("POST", "/v1/stills", st)
    assert status == 201 and out["jobs"]
    rd = {**ex("meter-reading"), "session_id": sid, "scan_id": pkg["scan_id"]}
    assert gw.req("POST", "/v1/readings", rd)[0] == 201
    status, full, _ = gw.req("GET", f"/v1/sessions/{sid}")
    assert status == 200 and [s["scan_id"] for s in full["scans"]] == [pkg["scan_id"]]
    assert len(full["stills"]) == 1 and len(full["readings"]) == 1
    # The run was written as a replayable session folder in the samples/ layout.
    folder = gw.tmp / "sessions" / sid
    assert validate_session(folder) == [], validate_session(folder)


@pytest.mark.parametrize("mutate, expect", [
    (lambda p: p.pop("units"), "schema"),
    (lambda p: p.__setitem__("frame", "design"), "schema"),
    (lambda p: p.__setitem__("point_count", 99), "capture_files"),
    (lambda p: p.__setitem__("points_file", "scans/x.ply"), "capture_files"),
    (lambda p: p.__setitem__("points_file", "blob://sha256:" + "0" * 64), "capture_files"),
])
def test_scan_package_rejects(gw, mutate, expect):
    sid = new_session(gw)
    pkg = make_scan(gw, sid)
    mutate(pkg)
    status, out, _ = gw.req("POST", "/v1/scans", pkg)
    assert status == 422
    assert (out.get("error") or out["detail"]["error"]) == expect
    if expect == "schema":
        assert out["path"].startswith("/")


def test_scan_rejects_a_non_ply_points_file_and_unknown_session(gw):
    sid = new_session(gw)
    pkg = make_scan(gw, sid)
    pkg["points_file"] = gw.upload(b"not a ply file at all")
    status, out, _ = gw.req("POST", "/v1/scans", pkg)
    assert status == 422 and "PLY" in json.dumps(out)
    pkg = make_scan(gw, "sess_unknown")
    assert gw.req("POST", "/v1/scans", pkg)[0] == 404


def test_still_must_be_full_resolution_jpeg(gw):
    sid = new_session(gw)
    pkg = make_scan(gw, sid)
    assert gw.req("POST", "/v1/scans", pkg)[0] == 201
    st = make_still(gw, pkg, size=(1920, 1440))
    status, out, _ = gw.req("POST", "/v1/stills", st)
    assert status == 422 and "full resolution" in json.dumps(out)
    st = make_still(gw, pkg)
    st["image_file"] = gw.upload(b"\x89PNG not a jpeg")
    assert gw.req("POST", "/v1/stills", st)[0] == 422


def test_reading_invariant_and_finding_suspect_reads(gw):
    sid = new_session(gw)
    pkg = make_scan(gw, sid)
    gw.req("POST", "/v1/scans", pkg)
    rd = {**ex("meter-reading"), "session_id": sid, "scan_id": pkg["scan_id"]}
    bad = copy.deepcopy(rd)
    bad["value"] = {"volts": 5}  # continuity reading with a volts value
    assert gw.req("POST", "/v1/readings", bad)[0] == 422
    assert gw.req("POST", "/v1/readings", {**rd, "session_id": "sess_other"})[0] == 422  # REF rule
    gw.req("POST", "/v1/readings", rd)
    f = {**ex("finding"), "scan_id": pkg["scan_id"]}
    sus = {**ex("suspect"), "scan_id": pkg["scan_id"]}

    async def stage_writes():
        await gw.state.transition("finding", f["finding_id"], "finding.create", f)
        await gw.state.transition("suspect", sus["suspect_id"], "suspect.update_from_fusion", sus)
    asyncio.run(stage_writes())
    assert gw.req("GET", f"/v1/findings/{f['finding_id']}")[1] == f
    assert gw.req("GET", f"/v1/suspects/{sus['suspect_id']}")[1] == sus
    assert [x["finding_id"] for x in gw.req("GET", f"/v1/scans/{pkg['scan_id']}/findings")[1]] == [f["finding_id"]]
    assert len(gw.req("GET", f"/v1/scans/{pkg['scan_id']}/suspects")[1]) == 1
    assert gw.req("GET", "/v1/findings/nope")[0] == 404


def _sse_events(port: int, token: str, session_id: str, want: int, last_id: str | None = None, timeout=5.0):
    """Read `want` events from the SSE stream; returns [(id, event, data, received_at)]."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    extra = f"Last-Event-ID: {last_id}\r\n" if last_id else ""
    s.sendall((f"GET /v1/sessions/{session_id}/events HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\n"
               f"Accept: text/event-stream\r\n{extra}\r\n").encode())
    buf, out = b"", []
    deadline = time.time() + timeout
    while len(out) < want and time.time() < deadline:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            fields = dict(line.split(": ", 1) for line in block.decode(errors="replace").splitlines()
                          if ": " in line and not line.startswith(":"))
            if "event" in fields:
                out.append((fields.get("id"), fields["event"], json.loads(fields["data"]), time.monotonic()))
    s.close()
    return out


def test_sse_finding_reaches_the_phone_within_1_s_and_resumes(gw):
    sid = new_session(gw)
    pkg = make_scan(gw, sid)
    gw.req("POST", "/v1/scans", pkg)
    f = {**ex("finding"), "scan_id": pkg["scan_id"]}
    result = {}

    def listen():
        result["events"] = _sse_events(gw.port, gw.token, sid, want=3)

    t = threading.Thread(target=listen)
    t.start()
    time.sleep(0.5)
    written = time.monotonic()
    asyncio.run(gw.state.transition("finding", f["finding_id"], "finding.create", f))
    t.join(timeout=10)
    events = result["events"]
    assert [e[1] for e in events] == ["session.insert", "scan-package.insert", "finding.insert"]
    assert events[2][2]["row"] == f and events[2][3] - written <= 1.0
    resumed = _sse_events(gw.port, gw.token, sid, want=1, last_id=events[1][0])
    assert resumed[0][1] == "finding.insert" and resumed[0][0] == events[2][0]


def test_json_p95_under_100_ms(gw):
    sid = new_session(gw)
    gw.req("POST", "/v1/scans", make_scan(gw, sid))
    times = []
    for _ in range(40):
        t0 = time.perf_counter()
        assert gw.req("GET", f"/v1/sessions/{sid}")[0] == 200
        times.append(time.perf_counter() - t0)
    assert statistics.quantiles(times, n=20)[-1] < 0.1


def test_gateway_imports_no_provider_package():
    src = (Path(__file__).resolve().parents[2] / "repairos" / "api").rglob("*.py")
    imports = [line for p in src for line in p.read_text(encoding="utf-8").splitlines()
               if line.lstrip().startswith(("import ", "from "))]
    assert imports and not [i for i in imports if "slots.state_" in i or "slots.blob_" in i or "grain" in i.lower()]
