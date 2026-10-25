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
from typing import Annotated, Any, Literal

import jsonschema
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

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


def finite_json(o: Any) -> Any:
    """`o` with every non-finite float (inf, nan) as None: strict JSON cannot carry them, and one stray stat in a stored
    report (a ratio over nothing) otherwise turns GET .../report into a 500 for that scan."""
    if isinstance(o, float):
        return o if o == o and o not in (float("inf"), float("-inf")) else None
    if isinstance(o, dict):
        return {k: finite_json(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [finite_json(v) for v in o]
    return o


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def identified_payload(scan_id: str, ident: dict) -> dict:
    return {"scan_id": scan_id, "category": ident.get("category") or "unknown", "brand": ident.get("brand") or None,
            "model": ident.get("model") or None, "confidence": ident.get("confidence") or "low",
            "label_visible": bool(ident.get("label_visible")), "label_text": ident.get("label_text") or None}


def mesh_payload(scan_id: str, mesh: dict) -> dict:
    out = {"scan_id": scan_id, "mesh_url": f"/v1/scans/{scan_id}/mesh?rev={mesh['rev']}", "mesh_rev": mesh["rev"],
           "format": mesh["format"], "triangles": mesh["triangles"], "frame": "arkit_world",
           "hero": mesh.get("hero") or "mesh", "hero_reason": mesh.get("hero_reason")}
    if mesh.get("cloud_ref"):
        out.update(cloud_url=f"/v1/scans/{scan_id}/cloud?rev={mesh['rev']}", cloud_points=mesh.get("cloud_points"))
    return out


def _named_event(ev) -> tuple[str, dict] | None:
    """The app-level SSE event a row change means, besides its `<kind>.<op>` event: `report_ready` when a report is
    published, `pipeline_failed` when a run fails (the stage and the error), and from the run row's one-write
    `announce` marker: `identified` (what the object is), `progress` (live counts after each still), `mesh_ready`
    (the 3D mesh was built or rebuilt), `fix_ready` (where to buy the top finding's part: repairos.references.parts)."""
    if ev.kind == RUN_KIND and ev.row.get("announce"):
        sid, what = ev.row["scan_id"], ev.row["announce"]
        if what == "identified" and ev.row.get("identity"):
            return "identified", identified_payload(sid, ev.row["identity"])
        if what == "progress" and ev.row.get("progress"):
            return "progress", {"scan_id": sid, **{k: v for k, v in ev.row["progress"].items() if k != "at"}}
        if what == "mesh_ready" and (ev.row.get("mesh") or {}).get("status") == "done":
            return "mesh_ready", mesh_payload(sid, ev.row["mesh"])
        if what == "recreation_ready" and (ev.row.get("recreation") or {}).get("status") == "done":
            from repairos.recreation.service import payload as recreation_payload
            return "recreation_ready", recreation_payload(sid, ev.row["recreation"])
        if what == "lead":
            from repairos.pipeline.story import lead_payload
            p = lead_payload(sid, ev.row)
            if p:
                return "lead", p
        if what == "fix_ready" and ev.row.get("fix"):
            from repairos.references.parts import payload
            fix = ev.row["fix"]
            if fix.get("options"):
                return "fix_ready", {**payload(sid, fix), "status": "done"}
            return "fix_ready", {"scan_id": sid, "status": fix.get("status") or "none", "options": []}
    if ev.kind == "story" and ev.row.get("announce") == "product_brief":
        from repairos.pipeline.story import brief_payload
        p = brief_payload(ev.row["session_id"], ev.row)
        if p:
            return "product_brief", p
    if ev.kind == REPORT_KIND:
        return "report_ready", {"scan_id": ev.row["scan_id"], "report_id": ev.row["report_id"],
                                "verdict": ev.row["verdict"], "rev": ev.rev}
    if ev.kind == RUN_KIND and ev.row.get("status") == "failed":
        return "pipeline_failed", {"scan_id": ev.row["scan_id"], "error": ev.row.get("error")}
    return None


SPEND_POLL_S = 1.0          # how often an event stream looks for new spend alerts (one small SQLite read)
SPEND_REPLAY_S = 300.0      # a new stream first gets the spend alerts of the last few minutes (a pause still on)


class SpendAlerts:
    """New rows of repairos.spend's alert table as SSE `spend_alert` chunks, for one event stream. Alerts carry no
    `id:` field, so a client's Last-Event-ID (the session's row cursor) is never moved by them; `alert_id` in the
    payload lets a client drop one it has already shown."""

    def __init__(self):
        self.cursor: int | None = None
        self.next_at = 0.0

    async def poll(self) -> list[bytes]:
        if time.monotonic() < self.next_at:
            return []
        self.next_at = time.monotonic() + SPEND_POLL_S
        try:
            from repairos import spend
            # Indexed reads of a few rows (well under a millisecond), so they run inline: no thread hop that a busy
            # pool could delay, which would hold back the row events behind them.
            if self.cursor is None:
                rows = spend.alerts_since(0, within_s=SPEND_REPLAY_S)
                self.cursor = spend.last_alert_id()
            else:
                rows = spend.alerts_since(self.cursor)
        except Exception:  # noqa: BLE001 - alerts are a side channel: a broken spend file never cuts the stream
            return []
        if rows:
            self.cursor = max(self.cursor or 0, rows[-1]["alert_id"])
        return [f"event: spend_alert\ndata: {json.dumps(spend.event_payload(a))}\n\n".encode() for a in rows]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class NewSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str | None = None
    make_model: str | None = None
    symptom: str | None = None


class Story(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    source: Literal["typed", "voice"] = "typed"
    scan_id: str | None = None


class IdentifyPhoto(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: str = Field(pattern=r"^blob://sha256:[0-9a-f]{64}$")


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

    async def _jobs(request: Request, kind: str, data: dict, rev: int | None = None) -> list[str]:
        """Queue the stages for a new object. Job ids are derived from the object, so a retried upload queues
        nothing new (enqueue is idempotent per job_id)."""
        oid = data.get("still_id") or data["scan_id"]
        key = f"{kind}:{oid}" + (f":rev{rev}" if rev and rev > 1 else "")   # a re-posted package is new work
        out = []
        for job_kind in JOBS_ON.get(kind, []):
            if kind == "still" and data.get("kind") == "label":
                job_kind = "ocr"
            job_id = f"job_{job_kind}_{hashlib.sha256(key.encode()).hexdigest()[:16]}"
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

    @app.post("/v1/sessions/{session_id}/story", status_code=202)
    async def post_story(session_id: str, body: Story, request: Request, _d: Device):
        """What the person thinks is wrong, typed or spoken (the voice agent posts each utterance). Kept on the
        session as a lead, never a fact: the worker reads it into a hypothesis, sends the live event `lead` per
        place worth a close look, and the report's `user_lead` says what the photos showed. 202: queued."""
        from repairos.pipeline.story import job_key, record_story

        await _session(request, session_id)
        rev, row = await record_story(store(request), session_id, body.text, body.source, body.scan_id)
        n = len(row.get("said") or [])
        job_id = f"job_story_{hashlib.sha256(job_key(session_id, row).encode()).hexdigest()[:16]}"
        job = await store(request).enqueue(Job(kind="story", job_id=job_id, session_id=session_id,
                                               scan_id=body.scan_id, inputs={"kind": "story", "id": session_id}))
        return {"session_id": session_id, "rev": rev, "said": n, "job": job}

    @app.post("/v1/sessions/{session_id}/identify", status_code=202)
    async def post_identify(session_id: str, body: IdentifyPhoto, request: Request, _d: Device):
        """One photo of the product or its label, before scanning (upload it first: PUT /v1/blobs/{sha256}). The
        worker reads the label to identify the product, researches it and sends the live event `product_brief`.
        No pose or scan is needed. 202: queued."""
        from repairos.pipeline.story import job_key, record_photo

        await _session(request, session_id)
        if not blob_store(request).exists(body.image):
            raise HTTPException(422, f"no blob {body.image}: PUT it to /v1/blobs/{{sha256}} first")
        rev, row = await record_photo(store(request), session_id, body.image)
        job_id = f"job_story_{hashlib.sha256(job_key(session_id, row).encode()).hexdigest()[:16]}"
        job = await store(request).enqueue(Job(kind="story", job_id=job_id, session_id=session_id,
                                               inputs={"kind": "story", "id": session_id}))
        return {"session_id": session_id, "rev": rev, "photos": len(row.get("photos") or []), "job": job}

    @app.post("/v1/scans", status_code=201)
    async def post_scan(request: Request, _d: Device, body: Annotated[dict[str, Any], Body()]):
        """The scan package (201, `updated: false`). Posting it again for the same scan_id (more photos of the same
        object, new keyframes and segments) replaces it (200, `updated: true`, rev + 1); the same content again is
        an idempotent retry (201, the same rev and jobs). Stills already inspected keep their answers; POST .../complete then re-runs the
        report over all stills."""
        contracts.validate("scan-package", body)
        await _session(request, body["session_id"])
        b = blob_store(request)
        _refuse(await asyncio.to_thread(_check_scan, b, body))
        early = [r.data for r in await store(request).list("still", scan_id=body["scan_id"])]
        _refuse([f"still {st['still_id']} was posted for this scan under session {st['session_id']}, not "
                 f"{body['session_id']}" for st in early if st["session_id"] != body["session_id"]])
        existing = await store(request).get("scan-package", body["scan_id"])
        if existing is not None and existing.data == body:
            existing = None   # an identical retry: the same row and jobs, answered like the first post
        action = "scan.update" if existing is not None else "scan.create"
        row = await store(request).transition("scan-package", body["scan_id"], action, body)
        jobs = await _jobs(request, "scan-package", body, row.rev)
        w = recorder(request, body["session_id"])
        await asyncio.to_thread(w.write_scan, body, _fetch(b))
        w.write_session({"device": body["device"], "symptom": body.get("symptom")})
        out = {"scan_id": row.id, "rev": row.rev, "jobs": jobs, "updated": existing is not None}
        return JSONResponse(status_code=200, content=out) if existing is not None else out

    @app.post("/v1/stills", status_code=201)
    async def post_still(request: Request, _d: Device, body: Annotated[dict[str, Any], Body()]):
        """A still, validated in full (contract + its files). It may arrive before its scan package: the scan is
        then pending (`scan_pending: true`) and the still is inspected at once from its own image, pose and
        intrinsics; the session must exist. The still is linked by its scan_id when the scan package is posted,
        and fuse, locate and the report run only after that (POST /v1/scans/{scan_id}/complete)."""
        contracts.validate("still", body)
        await _session(request, body["session_id"])
        b = blob_store(request)
        _refuse(await asyncio.to_thread(_check_still, b, body))
        pending = await store(request).get("scan-package", body["scan_id"]) is None
        row = await store(request).transition("still", body["still_id"], "still.create", body)
        jobs = await _jobs(request, "still", body)
        await asyncio.to_thread(recorder(request, body["session_id"]).write_still, body, _fetch(b))
        return {"still_id": row.id, "rev": row.rev, "jobs": jobs, "scan_pending": pending}

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

    @app.get("/v1/sessions")
    async def list_sessions(request: Request, _d: Device, limit: Annotated[int, Query(ge=1, le=100)] = 20):
        """Sessions, the most recently active first (newest live event): `{sessions: [{session_id, created_at,
        last_event_id, last_event_at}]}`. The live mirror (repairos.live.spacetime_bridge) finds sessions here."""
        return {"sessions": await store(request).recent_sessions(limit)}

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
        out = {k: run.get(k) for k in ("status", "attempt", "started_at", "finished_at", "report_id", "error",
                                       "continuation")
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
        """The run's status, `report_rev` (latest finished report), `rebuilding` and `pending` (_pipeline_view). A
        re-made report is ready when report_rev goes up; status `closed` = a continuation nobody completed in time
        (the latest report stands; its stills are folded in on the next complete)."""
        await _get(request, "scan-package", scan_id)
        return await _pipeline_view(request, scan_id)

    async def _pipeline_view(request: Request, scan_id: str) -> dict:
        """The run view plus the report state: `report_rev` = the latest finished report's revision (absent before
        the first); `rebuilding` = a newer report is being made or new stills are waiting for it; `pending` = the
        stills added since that report (`new_stills`, `inspected` of them) while it is not current."""
        run = await _run(request, scan_id)
        view = _run_view(run)
        mesh = (run or {}).get("mesh") or {}
        if mesh.get("status") == "done":
            p = mesh_payload(scan_id, mesh)
            view.update(mesh_url=p["mesh_url"], mesh_rev=p["mesh_rev"], hero=p["hero"])
            if p.get("cloud_url"):
                view["cloud_url"] = p["cloud_url"]
        elif mesh.get("status") == "unavailable":
            view["mesh_unavailable"] = mesh.get("reason")
        if ((run or {}).get("identity") or {}).get("status") == "done":
            view["identity"] = {k: v for k, v in identified_payload(scan_id, run["identity"]).items() if k != "scan_id"}
        if (run or {}).get("progress"):
            view["progress"] = {k: v for k, v in run["progress"].items() if k != "at"}
        if ((run or {}).get("fix") or {}).get("status"):
            view["fix_status"] = run["fix"]["status"]   # searching | done | none | timeout | failed | stale
        row = await store(request).get(REPORT_KIND, f"rep_{scan_id}")
        status = (run or {}).get("status")
        if row is None:
            view["rebuilding"] = status in ("inspecting", "running")
            return view
        view["report_rev"] = row.rev
        view["rebuilding"] = status in ("inspecting", "running")
        stills = (run or {}).get("stills") or {}
        in_report = int((row.data.get("capture") or {}).get("stills") or 0)
        claimed = await store(request).list("still", scan_id=scan_id)
        new = max(0, len(claimed) - in_report)
        if status == "done" and new:
            # a continuation's stills posted (and complete called) before a worker picked them up: the run row is
            # still the finished one, but a newer report is coming
            view["rebuilding"] = True
        if new or view["rebuilding"]:
            view["pending"] = {"new_stills": new,
                               "inspected": max(0, sum(1 for st in stills.values() if st.get("status") == "done")
                                                - in_report),
                               "closed": status == "closed"}
        return view

    async def _report(request: Request, scan_id: str) -> tuple[dict, int, dict]:
        """(the latest finished report, its revision, the pipeline view). A newer report being made never hides
        the finished one; 404 only when no report was ever finished, 409 when the only run failed."""
        await _get(request, "scan-package", scan_id)
        view = await _pipeline_view(request, scan_id)
        row = await store(request).get(REPORT_KIND, f"rep_{scan_id}")
        if row is None:
            if view.get("status") == "failed":
                raise HTTPException(409, {"error": "pipeline_failed", "pipeline": view})
            raise HTTPException(404, {"error": "no_report", "pipeline": view})
        return row.data, row.rev, view

    async def _ply(request: Request, scan_id: str, rev: int | None, field: str, what: str):
        await _get(request, "scan-package", scan_id)
        run = await _run(request, scan_id)
        mesh = (run or {}).get("mesh") or {}
        if mesh.get("status") != "done" or not mesh.get(field):
            raise HTTPException(404, {"error": f"no_{what}", "reason": mesh.get("reason") or
                                      ("not built yet" if mesh.get("status") != "done" else f"the scan has no {what}")})
        b = blob_store(request)
        f = b.open(mesh[field])

        def chunks():
            with f:
                while data := f.read(1 << 20):
                    yield data
        return StreamingResponse(chunks(), media_type="application/x-ply", headers={
            "Content-Length": str(b.size(mesh[field])), "X-Mesh-Rev": str(mesh["rev"]),
            "X-Hero": str(mesh.get("hero") or "mesh"),
            "Content-Disposition": f'inline; filename="{scan_id}_{what}_rev{mesh["rev"]}.ply"',
            "Cache-Control": "private, max-age=31536000" if rev == mesh["rev"] else "no-cache"})

    @app.get("/v1/scans/{scan_id}/cloud", responses={200: {"content": {"application/x-ply": {}}}})
    async def scan_cloud(scan_id: str, request: Request, _d: Device, rev: int | None = None):
        """The object's part of the phone's LiDAR cloud: binary PLY points (float x y z, uchar red green blue), the
        same frame and revision as the mesh. `X-Hero` says which 3D view reads best (points, mesh or photos)."""
        return await _ply(request, scan_id, rev, "cloud_ref", "cloud")

    @app.get("/v1/scans/{scan_id}/mesh", responses={200: {"content": {"application/x-ply": {}}}})
    async def scan_mesh(scan_id: str, request: Request, _d: Device, rev: int | None = None):
        """The scan's 3D mesh (binary PLY: float x y z + uchar red green blue per vertex, triangle lists; metres,
        arkit_world, the frame of every finding's location). `X-Mesh-Rev` = its revision (up when more photos are
        added). 404 while there is none (not built yet, or the scan has no usable depth: `reason`). `rev` is a
        cache key only: the latest mesh is always served."""
        return await _ply(request, scan_id, rev, "ref", "mesh")

    @app.get("/v1/scans/{scan_id}/recreation", responses={200: {"content": {"model/vnd.usdz+zip": {},
                                                                          "application/octet-stream": {}}}})
    async def scan_recreation(scan_id: str, request: Request, _d: Device, rev: int | None = None):
        """The photo-real recreation of the scanned object (repairos.recreation): a USDZ textured mesh
        (`X-Recreation-Kind: textured_mesh`) or a `.splat` Gaussian splat (`splat`), in the scan's ARKit world frame
        (metres, Y up: the frame of every finding's location). `X-Recreation-Rev` = its revision (up when the scan
        gains photos); `X-Recreation-Focus` = the object's centre x,y,z and radius; `X-Recreation-Bottom` = how many
        registered flipped segments close the model underneath (0: open below). 404 while there is none (`reason`).
        The live event `recreation_ready` announces each revision. `rev` is a cache key only."""
        await _get(request, "scan-package", scan_id)
        rec = ((await _run(request, scan_id)) or {}).get("recreation") or {}
        if rec.get("status") != "done" or not rec.get("ref"):
            raise HTTPException(404, {"error": "no_recreation", "reason": rec.get("reason") or "not built yet"})
        b = blob_store(request)
        f = b.open(rec["ref"])

        def chunks():
            with f:
                while data := f.read(1 << 20):
                    yield data
        focus = ",".join(str(x) for x in [*(rec.get("focus_center") or []), rec.get("focus_radius") or 0])
        media = "model/vnd.usdz+zip" if rec.get("format") == "usdz" else "application/octet-stream"
        return StreamingResponse(chunks(), media_type=media, headers={
            "Content-Length": str(b.size(rec["ref"])), "X-Recreation-Rev": str(rec["rev"]),
            "X-Recreation-Kind": str(rec.get("kind")), "X-Recreation-Format": str(rec.get("format")),
            "X-Recreation-Focus": focus, "X-Recreation-Bottom": str(len(rec.get("bottom_segments") or [])),
            "Content-Disposition": f'inline; filename="{scan_id}_recreation_rev{rec["rev"]}.{rec.get("format")}"',
            "Cache-Control": "private, max-age=31536000" if rev == rec["rev"] else "no-cache"})

    @app.get("/v1/scans/{scan_id}/report")
    async def scan_report(scan_id: str, request: Request, _d: Device):
        """The scan's latest finished report (spec/contracts/report.schema.json), 200, with its revision in
        `X-Report-Rev` (= `report_rev` of GET .../pipeline; it goes up each time the report is re-made with more
        photos) and `X-Report-Rebuilding: 1` while a newer one is pending (0 otherwise). 404 only when no report was
        ever finished (with the pipeline status); 409 when the first run failed (with the stage and the error)."""
        report, rev, view = await _report(request, scan_id)
        return JSONResponse(content=finite_json(report), headers={"X-Report-Rev": str(rev),
                                                     "X-Report-Rebuilding": "1" if view.get("rebuilding") else "0"})

    @app.get("/v1/scans/{scan_id}/report/export", responses={200: {"content": {"text/html": {}, "text/markdown": {}}}})
    async def scan_report_export(scan_id: str, request: Request, _d: Device,
                                 format: Annotated[str, Query(pattern="^(html|md)$")] = "html"):
        """A printable copy of the report for sharing: self-contained HTML (crops embedded) or Markdown."""
        from repairos.report.export import crop_thumbnail, to_html, to_markdown

        report, _rev, _view = await _report(request, scan_id)
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
            alerts = SpendAlerts()
            yield b"retry: 1000\n\n"
            while not await request.is_disconnected():
                for chunk in await alerts.poll():   # spend_alert: global, no `id:` (the row cursor is untouched)
                    yield chunk
                    last_sent = time.monotonic()
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

    @app.get("/v1/events", response_class=StreamingResponse, responses={200: {"content": {"text/event-stream": {}}}})
    async def global_events(request: Request, _d: Device):
        """Server-sent events that belong to no session: `spend_alert` (repairos.spend), the alerts of the last
        few minutes first, then each new one as it is stored."""
        async def stream() -> AsyncIterator[bytes]:
            alerts, last_sent = SpendAlerts(), time.monotonic()
            yield b"retry: 1000\n\n"
            while not await request.is_disconnected():
                chunks = await alerts.poll()
                for chunk in chunks:
                    yield chunk
                    last_sent = time.monotonic()
                if not chunks:
                    if time.monotonic() - last_sent > SSE_KEEPALIVE_S:
                        yield b": keepalive\n\n"
                        last_sent = time.monotonic()
                    await asyncio.sleep(SPEND_POLL_S)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # --------------------------------------------------------------- usage (repairos.spend)
    @app.get("/v1/usage")
    async def usage_view(_d: Device):
        """Paid-service usage: per service the calls in the last minute, hour and day, the estimated USD for the
        day, the caps, the breaker and the last alert (repairos.spend.usage)."""
        from repairos import spend
        return await asyncio.to_thread(spend.usage)

    # --------------------------------------------------------------- voice (repairos/voice: TTS, STT, Ask Lea)
    from repairos.voice import register_voice_routes
    register_voice_routes(app, Device)

    return app

