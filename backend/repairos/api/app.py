"""FastAPI gateway (PLAN.md task B5, Phase 1): the only server the phone talks to.

Device pairing, chunked resumable blob uploads (content-hashed), scan package / still / reading ingestion
validated with repairos.contracts.validate() plus the capture files themselves, read endpoints, SSE live events, the pipeline trigger and the report (B10, B11).
Every write goes through StateStore.transition (PLAN.md invariants I1-I4 enforced there). Every object received is also written
into a session folder (data/sessions/<session_id>/, the samples/README.md layout) so a real run is replayable.
The gateway imports slot Protocols only; implementations come from the registry. No Grain calls in Phase 1.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import jsonschema
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict

from repairos import contracts
from repairos.config import ROOT, get_settings
from repairos.sessions import files
from repairos.sessions.ids import new_session_id
from repairos.sessions.ingest import session_row
from repairos.sessions.layout import ext_for, file_values
from repairos.sessions.writer import SessionWriter
from repairos.slots.blob import (REF_PREFIX, BlobError, BlobNotFound, BlobStore, HashMismatch, OffsetMismatch,
                                 is_sha256, ref_of)
from repairos.slots.state import InvariantViolation, Job, NotFound, RevConflict, StateError

MAX_BLOB_BYTES = 4 << 30
SSE_POLL_S = 0.25
SSE_KEEPALIVE_S = 15.0
_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
# Jobs queued when an object arrives; the pipeline workers (B10, `make run-workers`) consume them. The whole run
# (fusion, locations, report) is queued by POST /v1/scans/{scan_id}/complete once the upload is complete.
JOBS_ON = {"scan-package": ["detect"], "still": ["detect"]}
REPORT_KIND, RUN_KIND = "report", "pipeline-run"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _named_event(ev) -> tuple[str, dict] | None:
    """The app-level SSE event a row change means, besides its `<kind>.<op>` event: `report_ready` when a report is
    published, `pipeline_failed` when a run fails (the stage and the error)."""
    if ev.kind == REPORT_KIND:
        return "report_ready", {"scan_id": ev.row["scan_id"], "report_id": ev.row["report_id"],
                                "verdict": ev.row["verdict"]}
    if ev.kind == RUN_KIND and ev.row.get("status") == "failed":
        return "pipeline_failed", {"scan_id": ev.row["scan_id"], "error": ev.row.get("error")}
    return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class NewSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str | None = None
    make_model: str | None = None
    symptom: str | None = None


class Pair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    name: str | None = None


async def device(request: Request, authorization: Annotated[str | None, Header()] = None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "device token required (Authorization: Bearer <token> from /v1/devices/pair)",
                            headers={"WWW-Authenticate": "Bearer"})
    row = await request.app.state.store.get("device", _token_hash(authorization[7:].strip()))
    if row is None:
        raise HTTPException(403, "unknown device token")
    return row.data


Device = Annotated[dict, Depends(device)]


def create_app(state=None, blobs: BlobStore | None = None, *, pairing_code: str | None = None,
               recordings: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state is None or blobs is None:
            from repairos.registry import default_registry
            reg = default_registry()
            app.state.store = state or reg.slot("STATE")
            app.state.blobs = blobs or reg.slot("BLOB")
        yield

    app = FastAPI(title="RepairOS gateway", version="0.1.0", lifespan=lifespan)
    app.state.store, app.state.blobs = state, blobs
    app.state.recordings = recordings

    def store(request: Request):
        return request.app.state.store

    def blob_store(request: Request) -> BlobStore:
        return request.app.state.blobs

    def recorder(request: Request, session_id: str) -> SessionWriter:
        root = request.app.state.recordings
        if root is None:
            root = Path(os.environ.get("REPAIROS_DATA_DIR") or ROOT / "data") / "sessions"
        return SessionWriter(root, session_id)

    # ---------------------------------------------------------------- errors
    @app.exception_handler(jsonschema.ValidationError)
    async def _schema(_req, e: jsonschema.ValidationError):
        return JSONResponse(status_code=422, content={
            "error": "schema", "path": "/" + "/".join(map(str, e.absolute_path)),
            "schema_path": "/" + "/".join(map(str, e.absolute_schema_path)), "message": e.message})

    @app.exception_handler(InvariantViolation)
    async def _rule(_req, e: InvariantViolation):
        return JSONResponse(status_code=422, content={"error": "rule", "rule": e.rule, "message": str(e)})

    @app.exception_handler(RevConflict)
    async def _conflict(_req, e: RevConflict):
        return JSONResponse(status_code=409, content={"error": "conflict", "message": str(e)})

    @app.exception_handler(NotFound)
    async def _missing(_req, e: NotFound):
        return JSONResponse(status_code=404, content={"error": "not_found", "message": str(e)})

    @app.exception_handler(StateError)
    async def _state(_req, e: StateError):
        return JSONResponse(status_code=400, content={"error": "state", "message": str(e)})


    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/v1/devices/pair", status_code=201)
    async def pair(body: Pair, request: Request):
        code = pairing_code
        if code is None:
            secret = get_settings().repairos_pairing_code
            code = secret.get_secret_value() if secret else None
        if not code:
            raise HTTPException(503, "REPAIROS_PAIRING_CODE is not set (backend/.env)")
        if not hmac.compare_digest(body.code.encode(), code.encode()):
            raise HTTPException(403, "wrong pairing code")
        token, device_id = secrets.token_urlsafe(32), f"dev_{secrets.token_hex(4)}"
        th = _token_hash(token)
        await store(request).transition("device", th, "device.pair", {
            "token_sha256": th, "device_id": device_id, "name": body.name, "paired_at": _now()})
        return {"device_id": device_id, "token": token}

    # ----------------------------------------------------------------- blobs
    def _sha(sha256: str) -> str:
        if not is_sha256(sha256):
            raise HTTPException(400, "path must be the blob's lower-case sha256 hex digest")
        return sha256

    @app.head("/v1/blobs/{sha256}")
    async def blob_status(sha256: str, request: Request, _d: Device):
        b, sha = blob_store(request), _sha(sha256)
        if b.exists(ref_of(sha)):
            return Response(status_code=200, headers={"Upload-Offset": str(b.size(ref_of(sha))),
                                                      "Upload-Complete": "1"})
        return Response(status_code=204, headers={"Upload-Offset": str(b.upload_offset(sha)),
                                                  "Upload-Complete": "0"})

    @app.put("/v1/blobs/{sha256}", responses={201: {"description": "complete"}, 202: {"description": "partial"}})
    async def blob_upload(sha256: str, request: Request, _d: Device,
                          content_range: Annotated[str | None, Header()] = None,
                          content_type: Annotated[str | None, Header()] = None):
        """Upload a blob, whole or in chunks. Chunks carry `Content-Range: bytes <start>-<end>/<total>`; `start`
        must equal the staged offset (HEAD gives it as Upload-Offset after a dropped connection). The blob exists
        only once the bytes hash to the sha256 in the path."""
        b, sha = blob_store(request), _sha(sha256)
        ref = ref_of(sha)
        if b.exists(ref):
            return JSONResponse(status_code=200, content={"ref": ref, "sha256": sha, "size": b.size(ref)})
        if content_range:
            m = _RANGE.match(content_range.strip())
            if not m:
                raise HTTPException(400, "Content-Range must be 'bytes <start>-<end>/<total>'")
            start, end, total = map(int, m.groups())
            if end < start or end >= total:
                raise HTTPException(400, "Content-Range end must be >= start and < total")
        else:
            start, end, total = 0, None, None
        if total is not None and total > MAX_BLOB_BYTES:
            raise HTTPException(413, f"blob larger than {MAX_BLOB_BYTES} bytes")
        offset = b.upload_offset(sha)
        if start != offset:
            return JSONResponse(status_code=409, headers={"Upload-Offset": str(offset)},
                                content={"error": "offset", "upload_offset": offset})
        async for chunk in request.stream():
            if chunk:
                try:
                    offset = await asyncio.to_thread(b.upload_append, sha, offset, chunk)
                except OffsetMismatch as e:  # a concurrent upload of the same blob moved the offset
                    return JSONResponse(status_code=409, headers={"Upload-Offset": str(e.expected)},
                                        content={"error": "offset", "upload_offset": e.expected})
                if offset > MAX_BLOB_BYTES:
                    raise HTTPException(413, f"blob larger than {MAX_BLOB_BYTES} bytes")
        if end is not None and offset != end + 1:
            return JSONResponse(status_code=409, headers={"Upload-Offset": str(offset)},
                                content={"error": "short_chunk", "upload_offset": offset})
        if total is not None and offset < total:
            return JSONResponse(status_code=202, headers={"Upload-Offset": str(offset)},
                                content={"upload_offset": offset})
        try:
            ref = await asyncio.to_thread(b.upload_commit, sha, content_type or "application/octet-stream")
        except HashMismatch as e:
            return JSONResponse(status_code=422, content={"error": "hash_mismatch", "message": str(e)})
        return JSONResponse(status_code=201, content={"ref": ref, "sha256": sha, "size": b.size(ref)})

    @app.get("/v1/blobs/{sha256}")
    async def blob_get(sha256: str, request: Request, exp: int | None = None, sig: str | None = None,
                       authorization: Annotated[str | None, Header()] = None):
        b, sha = blob_store(request), _sha(sha256)
        if exp is None or sig is None or not b.verify_presign(sha, exp, sig):
            await device(request, authorization)  # no valid signature: a paired device may still read it
        ref = ref_of(sha)
        try:
            f = b.open(ref)
        except BlobNotFound:
            raise HTTPException(404, "no such blob") from None

        def chunks():
            with f:
                while data := f.read(1 << 20):
                    yield data
        return StreamingResponse(chunks(), media_type=b.content_type(ref),
                                 headers={"Content-Length": str(b.size(ref))})

    # ------------------------------------------------------- capture checks
    def _blob_ref(b: BlobStore, value: str, where: str, missing: list[str]) -> str | None:
        if not value.startswith(REF_PREFIX):
            missing.append(f"{where}: {value!r} is not a blob ref (upload it to PUT /v1/blobs/<sha256> first)")
            return None
        try:
            if b.exists(value):
                return value
        except BlobError:
            pass
        missing.append(f"{where}: blob {value} has not been uploaded")
        return None

    def _check_jpeg(b: BlobStore, ref: str, where: str, size: tuple[int, int], problems: list[str]) -> None:
        try:
            with b.open(ref) as f:
                got = files.jpeg_size(f)
        except files.CaptureFileError as e:
            problems.append(f"{where}: {e}")
            return
        if got != size:
            problems.append(f"{where}: JPEG is {got[0]}x{got[1]} px, declared {size[0]}x{size[1]} (full resolution)")

    def _check_scan(b: BlobStore, pkg: dict) -> list[str]:
        problems: list[str] = []
        for v in file_values("scan-package", pkg):
            _blob_ref(b, v, "scan package file", problems)
        if problems:
            return problems
        try:
            if pkg.get("points_file"):  # absent when depth_source is none (no-LiDAR capture, A8)
                with b.open(pkg["points_file"]) as f:
                    h = files.ply_header(f, b.size(pkg["points_file"]))
                if h["vertex_count"] != pkg["point_count"]:
                    problems.append(f"points_file has {h['vertex_count']} vertices, point_count says "
                                    f"{pkg['point_count']}")
        except files.CaptureFileError as e:
            problems.append(f"points_file: {e}")
        for kf in pkg["keyframes"]:
            _check_jpeg(b, kf["image_file"], f"keyframe {kf['keyframe_id']} image_file",
                        (kf["intrinsics"]["width_px"], kf["intrinsics"]["height_px"]), problems)
        return problems

    def _check_still(b: BlobStore, st: dict) -> list[str]:
        problems: list[str] = []
        for v in file_values("still", st):
            _blob_ref(b, v, "still file", problems)
        if not problems:
            _check_jpeg(b, st["image_file"], "image_file", (st["width_px"], st["height_px"]), problems)
        return problems

    def _refuse(problems: list[str]) -> None:
        if problems:
            raise HTTPException(422, {"error": "capture_files", "problems": problems})

    def _fetch(b: BlobStore):
        def fetch(ref: str) -> tuple[bytes, str]:
            data = b.get(ref)
            return data, ext_for(data, b.content_type(ref))
        return fetch

    async def _session(request: Request, session_id: str):
        row = await store(request).get("session", session_id)
        if row is None:
            raise HTTPException(404, f"no session {session_id} (POST /v1/sessions first)")
        return row

    async def _jobs(request: Request, kind: str, data: dict) -> list[str]:
        """Queue the stages for a new object. Job ids are derived from the object, so a retried upload queues
        nothing new (enqueue is idempotent per job_id)."""
        oid = data.get("still_id") or data["scan_id"]
        out = []
        for job_kind in JOBS_ON.get(kind, []):
            if kind == "still" and data.get("kind") == "label":
                job_kind = "ocr"
            job_id = f"job_{job_kind}_{hashlib.sha256(f'{kind}:{oid}'.encode()).hexdigest()[:16]}"
            out.append(await store(request).enqueue(Job(kind=job_kind, job_id=job_id, session_id=data.get("session_id"),
                                                        scan_id=data.get("scan_id"), inputs={"kind": kind, "id": oid})))
        return out

    # --------------------------------------------------------------- writes
    @app.post("/v1/sessions", status_code=201)
    async def create_session(body: NewSession, request: Request, _d: Device):
        sid = new_session_id()
        data = session_row(sid, {**body.model_dump(), "created_at": _now()})
        row = await store(request).transition("session", sid, "session.create", data)
        recorder(request, sid).write_session({**data, "source": "gateway", "split": "unassigned"})
        return row.data

    @app.post("/v1/scans", status_code=201)
    async def post_scan(request: Request, _d: Device, body: Annotated[dict[str, Any], Body()]):
        contracts.validate("scan-package", body)
        await _session(request, body["session_id"])
        b = blob_store(request)
        _refuse(await asyncio.to_thread(_check_scan, b, body))
        row = await store(request).transition("scan-package", body["scan_id"], "scan.create", body)
        jobs = await _jobs(request, "scan-package", body)
        w = recorder(request, body["session_id"])
        await asyncio.to_thread(w.write_scan, body, _fetch(b))
        w.write_session({"device": body["device"], "symptom": body.get("symptom")})
        return {"scan_id": row.id, "rev": row.rev, "jobs": jobs}

    @app.post("/v1/stills", status_code=201)
    async def post_still(request: Request, _d: Device, body: Annotated[dict[str, Any], Body()]):
        contracts.validate("still", body)
        b = blob_store(request)
        _refuse(await asyncio.to_thread(_check_still, b, body))
        row = await store(request).transition("still", body["still_id"], "still.create", body)
        jobs = await _jobs(request, "still", body)
        await asyncio.to_thread(recorder(request, body["session_id"]).write_still, body, _fetch(b))
        return {"still_id": row.id, "rev": row.rev, "jobs": jobs}

    @app.post("/v1/readings", status_code=201)
    async def post_reading(request: Request, _d: Device, body: Annotated[dict[str, Any], Body()]):
        contracts.validate("meter-reading", body)
        row = await store(request).transition("meter-reading", body["reading_id"], "reading.record", body)
        recorder(request, body["session_id"]).write_reading(body)
        return {"reading_id": row.id, "rev": row.rev}

    # ---------------------------------------------------------------- reads
    async def _get(request: Request, kind: str, id: str) -> dict:
        row = await store(request).get(kind, id)
        if row is None:
            raise HTTPException(404, f"no {kind} {id}")
        return row.data

    async def _list(request: Request, kind: str, **by) -> list[dict]:
        return [r.data for r in await store(request).list(kind, **by)]

    @app.get("/v1/sessions/{session_id}")
    async def get_session(session_id: str, request: Request, _d: Device):
        row = await _session(request, session_id)
        by = {"session_id": session_id}
        return {"session": row.data, "scans": await _list(request, "scan-package", **by),
                "stills": await _list(request, "still", **by), "findings": await _list(request, "finding", **by),
                "suspects": await _list(request, "suspect", **by),
                "next_actions": await _list(request, "next-action", **by),
                "readings": await _list(request, "meter-reading", **by)}

    @app.get("/v1/scans/{scan_id}")
    async def get_scan(scan_id: str, request: Request, _d: Device):
        by = {"scan_id": scan_id}
        return {"scan": await _get(request, "scan-package", scan_id), "stills": await _list(request, "still", **by),
                "findings": await _list(request, "finding", **by), "suspects": await _list(request, "suspect", **by),
                "next_actions": await _list(request, "next-action", **by)}

    @app.get("/v1/scans/{scan_id}/findings")
    async def scan_findings(scan_id: str, request: Request, _d: Device):
        await _get(request, "scan-package", scan_id)
        return await _list(request, "finding", scan_id=scan_id)

    @app.get("/v1/scans/{scan_id}/suspects")
    async def scan_suspects(scan_id: str, request: Request, _d: Device):
        await _get(request, "scan-package", scan_id)
        return await _list(request, "suspect", scan_id=scan_id)

    # ------------------------------------------------------------ pipeline + report (B10, B11)
    @app.post("/v1/scans/{scan_id}/complete", status_code=202)
    async def complete_scan(scan_id: str, request: Request, _d: Device):
        """The upload of this scan and its stills is complete: queue the pipeline run (inspect what is left,
        fuse with Jev, locate, report). Idempotent for the same set of stills; a new still queues a new run."""
        scan = await _get(request, "scan-package", scan_id)
        stills = sorted(r.id for r in await store(request).list("still", scan_id=scan_id))
        digest = hashlib.sha256(f"{scan_id}:{','.join(stills)}".encode()).hexdigest()[:16]
        job_id = await store(request).enqueue(Job(kind="pipeline", job_id=f"job_pipeline_{digest}",
                                                  session_id=scan["session_id"], scan_id=scan_id,
                                                  inputs={"kind": "scan-package", "scan_id": scan_id}))
        return {"scan_id": scan_id, "job_id": job_id, "stills": len(stills)}

    def _run_view(run: dict | None) -> dict:
        if run is None:
            return {"status": "not_started"}
        out = {k: run.get(k) for k in ("status", "attempt", "started_at", "finished_at", "report_id", "error")
               if run.get(k) is not None}
        out["stages"] = {k: {"status": v.get("status")} for k, v in (run.get("stages") or {}).items()}
        out["stills"] = {k: {"status": v.get("status"), **({"error": v["error"]} if v.get("error") else {})}
                         for k, v in (run.get("stills") or {}).items()}
        return out

    async def _run(request: Request, scan_id: str) -> dict | None:
        row = await store(request).get(RUN_KIND, f"run_{scan_id}")
        return row.data if row else None

    @app.get("/v1/scans/{scan_id}/pipeline")
    async def scan_pipeline(scan_id: str, request: Request, _d: Device):
        await _get(request, "scan-package", scan_id)
        return _run_view(await _run(request, scan_id))

    async def _report(request: Request, scan_id: str) -> dict:
        await _get(request, "scan-package", scan_id)
        run = await _run(request, scan_id)
        if run and run.get("status") == "failed":
            raise HTTPException(409, {"error": "pipeline_failed", "pipeline": _run_view(run)})
        reports = await _list(request, REPORT_KIND, scan_id=scan_id)
        if not reports or (run and run.get("status") != "done"):
            raise HTTPException(404, {"error": "no_report", "pipeline": _run_view(run)})
        return reports[-1]

    @app.get("/v1/scans/{scan_id}/report")
    async def scan_report(scan_id: str, request: Request, _d: Device):
        """The scan's report (spec/contracts/report.schema.json). 404 while it is being made (with the pipeline
        status), 409 when the pipeline failed (with the stage and the error)."""
        return await _report(request, scan_id)

    @app.get("/v1/scans/{scan_id}/report/export", responses={200: {"content": {"text/html": {}, "text/markdown": {}}}})
    async def scan_report_export(scan_id: str, request: Request, _d: Device,
                                 format: Annotated[str, Query(pattern="^(html|md)$")] = "html"):
        """A printable copy of the report for sharing: self-contained HTML (crops embedded) or Markdown."""
        from repairos.report.export import crop_thumbnail, to_html, to_markdown

        report = await _report(request, scan_id)
        if format == "md":
            return PlainTextResponse(to_markdown(report), media_type="text/markdown; charset=utf-8")
        b = blob_store(request)

        def crop(ref: str) -> bytes | None:
            try:
                return crop_thumbnail(b.get(ref))
            except BlobError:
                return None
        return HTMLResponse(await asyncio.to_thread(to_html, report, crop))

    @app.get("/v1/stills/{still_id}")
    async def get_still(still_id: str, request: Request, _d: Device):
        return await _get(request, "still", still_id)

    @app.get("/v1/findings/{finding_id}")
    async def get_finding(finding_id: str, request: Request, _d: Device):
        return await _get(request, "finding", finding_id)

    @app.get("/v1/suspects/{suspect_id}")
    async def get_suspect(suspect_id: str, request: Request, _d: Device):
        return await _get(request, "suspect", suspect_id)

    # --------------------------------------------------------------- events
    @app.get("/v1/sessions/{session_id}/events", response_class=StreamingResponse,
             responses={200: {"content": {"text/event-stream": {}}}})
    async def events(session_id: str, request: Request, _d: Device,
                     last_event_id: Annotated[str | None, Header()] = None,
                     after: Annotated[int | None, Query(ge=0)] = None):
        """Server-sent events: one `event: <kind>.<op>` per row change, `id` = event id. Reconnect with
        Last-Event-ID (or ?after=) to resume without gaps."""
        await _session(request, session_id)
        start = after if after is not None else int(last_event_id) if (last_event_id or "").isdigit() else 0
        st = store(request)

        async def stream() -> AsyncIterator[bytes]:
            cursor, last_sent = start, time.monotonic()
            yield b"retry: 1000\n\n"
            while not await request.is_disconnected():
                batch = await st.events_since(session_id, cursor)
                for ev in batch:
                    cursor = ev.event_id
                    payload = json.dumps({"kind": ev.kind, "id": ev.id, "op": ev.op, "rev": ev.rev, "row": ev.row,
                                          "at": ev.at}, separators=(",", ":"))
                    yield f"id: {ev.event_id}\nevent: {ev.kind}.{ev.op}\ndata: {payload}\n\n".encode()
                    named = _named_event(ev)
                    if named:
                        yield f"id: {ev.event_id}\nevent: {named[0]}\ndata: {json.dumps(named[1])}\n\n".encode()
                    last_sent = time.monotonic()
                if not batch:
                    if time.monotonic() - last_sent > SSE_KEEPALIVE_S:
                        yield b": keepalive\n\n"
                        last_sent = time.monotonic()
                    await asyncio.sleep(SSE_POLL_S)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app

