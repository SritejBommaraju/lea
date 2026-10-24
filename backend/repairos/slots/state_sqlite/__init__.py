"""STATE=sqlite (task B4): one SQLite file (WAL) under <data>/state.sqlite3, for the single-laptop build.

- `rows`: every contract object as validated JSON with lifted columns (kind, id, rev, session_id, scan_id, status).
- `events`: append-only row changes; LiveEvents polls it (SSE resumes from Last-Event-ID).
- `job`: the job queue; claim is one `UPDATE ... RETURNING` inside BEGIN IMMEDIATE, so claims are exclusive.

<data> is REPAIROS_DATA_DIR, else the repo's data/ (gitignored). Each call opens its own connection in a worker
thread, so the store is safe across threads and processes.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import jsonschema

from repairos import contracts
from repairos.config import ROOT
from repairos.registry import register
from repairos.slots.state import (ID_FIELDS, INTERNAL_KINDS, TRANSITIONS, AlreadyClaimed, InvariantViolation, Job,
                                  NotFound, RevConflict, Row, RowEvent, StateError)
from repairos.slots.state_sqlite import rules

T = TypeVar("T")
POLL_S = 0.25
MAX_ATTEMPTS = 3
STALE_HEARTBEAT_S = 120

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rows (
  kind TEXT NOT NULL, id TEXT NOT NULL, rev INTEGER NOT NULL, session_id TEXT, scan_id TEXT, status TEXT,
  updated_at TEXT NOT NULL, json TEXT NOT NULL, PRIMARY KEY (kind, id));
CREATE INDEX IF NOT EXISTS rows_session ON rows (kind, session_id);
CREATE INDEX IF NOT EXISTS rows_scan ON rows (kind, scan_id);
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, kind TEXT NOT NULL, id TEXT NOT NULL,
  op TEXT NOT NULL, rev INTEGER NOT NULL, json TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_session ON events (session_id, event_id);
CREATE TABLE IF NOT EXISTS job (
  job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, session_id TEXT, scan_id TEXT, inputs TEXT NOT NULL,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, worker TEXT, result TEXT, error TEXT,
  created_at TEXT NOT NULL, heartbeat_at TEXT);
CREATE INDEX IF NOT EXISTS job_ready ON job (status, kind, created_at);
"""


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def data_dir() -> Path:
    return Path(os.environ.get("REPAIROS_DATA_DIR") or ROOT / "data")


def id_field(kind: str) -> str:
    try:
        return ID_FIELDS.get(kind) or INTERNAL_KINDS[kind]
    except KeyError:
        raise StateError(f"unknown kind {kind!r}") from None


def _merge(target: Any, patch: Any) -> Any:
    """RFC 7386 JSON merge patch."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = _merge(out.get(k), v)
    return out


def _row(r: sqlite3.Row) -> Row:
    return Row(kind=r["kind"], id=r["id"], rev=r["rev"], session_id=r["session_id"], data=json.loads(r["json"]),
               updated_at=r["updated_at"], status=r["status"])


def _job(r: sqlite3.Row) -> Job:
    return Job(job_id=r["job_id"], kind=r["kind"], session_id=r["session_id"], scan_id=r["scan_id"],
               inputs=json.loads(r["inputs"]), status=r["status"], attempts=r["attempts"], worker=r["worker"],
               result=json.loads(r["result"]) if r["result"] else None, error=r["error"],
               created_at=r["created_at"], heartbeat_at=r["heartbeat_at"])


class SqliteState:
    """StateStore + JobQueue + LiveEvents on one SQLite file."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else data_dir() / "state.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    async def _run(self, fn: Callable[[sqlite3.Connection], T], write: bool = False) -> T:
        def work() -> T:
            with closing(self._connect()) as c:
                if not write:
                    return fn(c)
                c.execute("BEGIN IMMEDIATE")
                try:
                    out = fn(c)
                except BaseException:
                    c.execute("ROLLBACK")
                    raise
                c.execute("COMMIT")
                return out
        return await asyncio.to_thread(work)

    # ------------------------------------------------------------------ rows
    @staticmethod
    def _get(c: sqlite3.Connection, kind: str, id: str) -> Row | None:
        r = c.execute("SELECT * FROM rows WHERE kind=? AND id=?", (kind, id)).fetchone()
        return _row(r) if r else None

    def _write(self, c: sqlite3.Connection, kind: str, data: Mapping[str, Any], expect_rev: int | None,
               status: str | None = None) -> Row:
        data = dict(data)
        idf = id_field(kind)
        if not data.get(idf):
            raise InvariantViolation("SCHEMA", f"{kind} needs {idf}")
        if kind in ID_FIELDS:
            try:
                contracts.validate(kind, data)
            except jsonschema.ValidationError as e:
                path = "/".join(str(p) for p in e.absolute_path)
                raise InvariantViolation("SCHEMA", f"{kind} at /{path}: {e.message}") from None
        id = str(data[idf])
        prev = self._get(c, kind, id)
        actual = prev.rev if prev else 0
        if expect_rev is not None and expect_rev != actual:
            raise RevConflict(kind, id, expect_rev, actual)

        def lookup(k: str, i: str):
            r = self._get(c, k, i)
            return r.data if r else None

        rules.check(kind, data, prev.data if prev else None, lookup)
        scan_id = data.get("scan_id") if kind != "scan-package" else id
        session_id = data.get("session_id")
        if session_id is None and scan_id:
            scan = self._get(c, "scan-package", scan_id)
            session_id = scan.session_id if scan else None
        if kind == "session":
            session_id = id
        rev, ts = actual + 1, now()
        status = status if status is not None else (prev.status if prev else None)
        blob = json.dumps(data, separators=(",", ":"), sort_keys=True)
        c.execute("INSERT INTO rows (kind,id,rev,session_id,scan_id,status,updated_at,json) VALUES (?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(kind,id) DO UPDATE SET rev=excluded.rev, session_id=excluded.session_id, "
                  "scan_id=excluded.scan_id, status=excluded.status, updated_at=excluded.updated_at, json=excluded.json",
                  (kind, id, rev, session_id, scan_id, status, ts, blob))
        if kind != "device":  # device rows hold token hashes; they never go on a live stream
            c.execute("INSERT INTO events (session_id,kind,id,op,rev,json,at) VALUES (?,?,?,?,?,?,?)",
                      (session_id, kind, id, "update" if prev else "insert", rev, blob, ts))
        return Row(kind, id, rev, session_id, data, ts, status)

    async def put(self, kind: str, data: Mapping[str, Any], *, expect_rev: int | None = None) -> int:
        return (await self._run(lambda c: self._write(c, kind, data, expect_rev), write=True)).rev

    async def get(self, kind: str, id: str) -> Row | None:
        return await self._run(lambda c: self._get(c, kind, id))

    async def list(self, kind: str, *, session_id: str | None = None, scan_id: str | None = None) -> list[Row]:
        sql, args = "SELECT * FROM rows WHERE kind=?", [kind]
        if session_id is not None:
            sql, args = sql + " AND session_id=?", args + [session_id]
        if scan_id is not None:
            sql, args = sql + " AND scan_id=?", args + [scan_id]
        return await self._run(lambda c: [_row(r) for r in c.execute(sql + " ORDER BY updated_at, id", args)])

    # ------------------------------------------------------------ transitions
    async def transition(self, kind: str, id: str, action: str, payload: Mapping[str, Any]) -> Row:
        if action not in TRANSITIONS:
            raise StateError(f"unknown transition {action!r}; known: {', '.join(TRANSITIONS)}")
        return await self._run(lambda c: self._transition(c, kind, id, action, dict(payload)), write=True)

    def _create(self, c, kind: str, id: str, data: dict, status: str | None = None) -> Row:
        """Insert once; posting identical content again returns the existing row (idempotent retry)."""
        idf = id_field(kind)
        data.setdefault(idf, id)
        if data[idf] != id:
            raise StateError(f"{kind}: payload {idf} {data[idf]!r} != {id!r}")
        prev = self._get(c, kind, id)
        if prev is not None:
            if prev.data == data:
                return prev
            raise RevConflict(kind, id, 0, prev.rev)
        return self._write(c, kind, data, 0, status)

    def _existing(self, c, kind: str, id: str) -> Row:
        row = self._get(c, kind, id)
        if row is None:
            raise NotFound(f"{kind} {id}")
        return row

    def _transition(self, c, kind: str, id: str, action: str, p: dict) -> Row:
        expect = {"session": "session", "device": "device", "scan": "scan-package", "still": "still",
                  "finding": "finding", "suspect": "suspect", "next_action": "next-action",
                  "reading": "meter-reading", "part": "part-on-hand", "plan": "repair-plan",
                  "pipeline": "pipeline-run", "report": "report"}[action.split(".")[0]]
        if kind != expect:
            raise StateError(f"transition {action} applies to {expect}, not {kind}")
        if action in ("session.create", "device.pair", "scan.create", "still.create", "finding.create",
                      "next_action.issue", "reading.record", "part.add"):
            return self._create(c, kind, id, p)
        if action == "plan.create":
            return self._create(c, kind, id, p, status="open")
        if action in ("pipeline.update", "pipeline.claim_still"):
            return self._pipeline(c, id, action, p)
        if action == "report.publish":   # a re-run replaces the scan's report (new rev, one event)
            prev = self._get(c, kind, id)
            p.setdefault("report_id", id)
            return self._write(c, kind, p, prev.rev if prev else 0)
        row = self._existing(c, kind, id) if action != "suspect.update_from_fusion" else self._get(c, kind, id)
        if action == "scan.set_alignment":
            return self._write(c, kind, {**row.data, "alignment": p["alignment"]}, row.rev)
        if action == "still.mark_not_inspected":
            return self._write(c, kind, {**row.data, "user_skipped": True}, row.rev)
        if action == "plan.confirm":
            return self._write(c, kind, row.data, row.rev, status="closed")
        if action == "plan.reopen":
            return self._write(c, kind, row.data, row.rev, status="open")
        if action == "suspect.update_from_fusion":
            p.setdefault("suspect_id", id)
            if p["verdict"] in ("confirmed", "cleared") and p["subject"]["kind"] in rules.ELECTRICAL:
                raise InvariantViolation("I2", f"suspect {id}: fusion cannot {p['verdict']} an electrical subject; "
                                               "only suspect.confirm/clear with a meter reading can")
            if row is not None and row.data.get("proof_reading_id"):
                # A meter reading overrides any engine answer (I2): keep the proven verdict and proof.
                p = {**p, "verdict": row.data["verdict"], "proof_reading_id": row.data["proof_reading_id"]}
            return self._write(c, kind, p, row.rev if row else 0)
        if action in ("suspect.confirm", "suspect.clear"):
            reading = self._existing(c, "meter-reading", p["reading_id"]).data
            want = "disagrees" if action == "suspect.confirm" else "agrees"
            if reading.get("verdict") != want:
                raise InvariantViolation("I4" if reading.get("verdict") in (None, "unknown") else "I2",
                                         f"suspect {id}: {action} needs a reading whose verdict is {want!r}; "
                                         f"reading {p['reading_id']} is {reading.get('verdict')!r}")
            verdict = "confirmed" if action == "suspect.confirm" else "cleared"
            data = {**row.data, "verdict": verdict, "proof_reading_id": p["reading_id"],
                    "updated_at": p.get("updated_at") or now()}
            return self._write(c, kind, data, row.rev)
        raise StateError(f"transition {action} not implemented")  # unreachable: TRANSITIONS is checked

    def _pipeline(self, c, id: str, action: str, p: dict) -> Row:
        """pipeline.update: an RFC 7386 merge patch on the run row (created on first use; None deletes a key).
        pipeline.claim_still: {still_id, worker, at[, force]} marks one still `running` for one worker, atomically;
        AlreadyClaimed when it is running or done (unless force)."""
        prev = self._get(c, "pipeline-run", id)
        base = prev.data if prev else {"run_id": id, "scan_id": p.get("scan_id"), "session_id": p.get("session_id"),
                                       "status": "inspecting", "attempt": 0, "stages": {}, "stills": {}}
        if action == "pipeline.update":
            data = _merge(base, p)
        else:
            sid = p["still_id"]
            cur = (base.get("stills") or {}).get(sid) or {}
            if cur.get("status") in ("running", "done") and not p.get("force"):
                raise AlreadyClaimed(sid, cur["status"], cur.get("worker"))
            data = _merge(base, {"stills": {sid: {"status": "running", "worker": p.get("worker"),
                                                  "started_at": p.get("at") or now(), "error": None}}})
            if data.get("status") in ("done", "failed"):   # a new still after a report: the report is out of date
                data["status"] = "inspecting"
        data["run_id"] = id
        return self._write(c, "pipeline-run", data, prev.rev if prev else 0, status=data.get("status"))

    # ------------------------------------------------------------------ jobs
    async def enqueue(self, job: Job) -> str:
        job_id = job.job_id or f"job_{uuid.uuid4().hex[:12]}"

        def fn(c):
            c.execute("INSERT INTO job (job_id,kind,session_id,scan_id,inputs,status,attempts,created_at) "
                      "VALUES (?,?,?,?,?,'queued',0,?) ON CONFLICT(job_id) DO NOTHING",
                      (job_id, job.kind, job.session_id, job.scan_id, json.dumps(job.inputs), now()))
            return job_id
        return await self._run(fn, write=True)

    async def claim(self, worker: str, kinds: list[str]) -> Job | None:
        if not kinds:
            return None
        marks = ",".join("?" * len(kinds))
        stale = (datetime.now(UTC) - timedelta(seconds=STALE_HEARTBEAT_S)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        def fn(c):
            r = c.execute(
                "UPDATE job SET status='running', worker=?, attempts=attempts+1, heartbeat_at=? WHERE job_id = ("
                f"SELECT job_id FROM job WHERE kind IN ({marks}) AND (status='queued' OR "
                "(status='running' AND heartbeat_at < ?)) ORDER BY created_at, rowid LIMIT 1) RETURNING *",
                (worker, now(), *kinds, stale)).fetchone()
            return _job(r) if r else None
        return await self._run(fn, write=True)

    async def _update_job(self, job_id: str, sql: str, args: tuple) -> None:
        def fn(c):
            if c.execute(f"UPDATE job SET {sql} WHERE job_id=?", (*args, job_id)).rowcount == 0:
                raise NotFound(f"job {job_id}")
        await self._run(fn, write=True)

    async def heartbeat(self, job_id: str) -> None:
        await self._update_job(job_id, "heartbeat_at=?", (now(),))

    async def complete(self, job_id: str, result: dict[str, Any]) -> None:
        await self._update_job(job_id, "status='done', result=?, error=NULL", (json.dumps(result),))

    async def fail(self, job_id: str, error: str, retry: bool) -> None:
        await self._update_job(job_id, "error=?, worker=NULL, status=CASE WHEN ? AND attempts < ? THEN 'queued' "
                                       "ELSE 'failed' END", (error, int(retry), MAX_ATTEMPTS))

    async def get_job(self, job_id: str) -> Job | None:
        def fn(c):
            r = c.execute("SELECT * FROM job WHERE job_id=?", (job_id,)).fetchone()
            return _job(r) if r else None
        return await self._run(fn)

    # ---------------------------------------------------------------- events
    async def events_since(self, session_id: str, after: int = 0, limit: int = 500) -> list[RowEvent]:
        def fn(c):
            return [RowEvent(event_id=r["event_id"], session_id=r["session_id"], kind=r["kind"], id=r["id"],
                             op=r["op"], rev=r["rev"], row=json.loads(r["json"]), at=r["at"])
                    for r in c.execute("SELECT * FROM events WHERE session_id=? AND event_id>? ORDER BY event_id "
                                       "LIMIT ?", (session_id, after, limit))]
        return await self._run(fn)

    async def subscribe(self, session_id: str, after: int = 0) -> AsyncIterator[RowEvent]:
        while True:
            batch = await self.events_since(session_id, after)
            for ev in batch:
                after = ev.event_id
                yield ev
            if not batch:
                await asyncio.sleep(POLL_S)


@register("STATE", "sqlite")
def _factory() -> SqliteState:
    return SqliteState()
