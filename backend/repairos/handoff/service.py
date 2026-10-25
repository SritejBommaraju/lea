"""The durable Lea -> Grain handoff: one STATE row `grain-handoff` per design objective of a scan, advanced by the
worker job `grain` (repairos.pipeline.worker) and read by the gateway (GET /v1/scans/{id}/grain).

Stages, each persisted before the next call:
    queued -> measured -> project -> uploaded -> capture -> designing -> asking | done | stopped | failed
- measured: repairos.measure on the scan's stills when the request carries marks (else skipped, said so).
- project: one Grain project per objective, named "Lea <handoff_id>"; a retry first looks for that name.
- uploaded: the scan (mesh, else cloud, else none) named "lea-<handoff>.ply", then the chosen photos; a retry counts
  what Grain already has before sending more.
- capture: src/LEA_CAPTURE.json (handoff.manifest), written again whenever it changes (PUT is idempotent).
- designing: one detached turn; the turn and the message count it starts from are saved BEFORE the POST, and an
  ambiguous failure is resolved by asking Grain whether a turn began, never by posting again blindly. A busy project
  (409) waits for its turn to end.
- asking: Grain's agent asked (its question is on the row; the live event `grain` carries it). POST .../grain/answer
  queues the answer as the next turn.
- done / stopped: the turn ended; when it built, the turn's final build is saved as a revision (out/model.step,
  out/model.glb, out/report.json and every src/ file, each a blob) with its fit check. One revision per turn at most
  (keyed by the turn, so a recovery never saves a build twice), and a revision is never changed. Grain keeps only its
  latest files, so a build the agent replaced later in the same turn is not kept.
- failed: what went wrong is on the row with the stage to resume from (`resume_stage`); asking again for the same
  objective resumes there in the same project. A transient Grain error is retried by the queue up to
  MAX_TRANSIENT times per attempt, then the row fails (resumable) instead of looking busy forever.

One worker at a time: a run takes a lease on the row (a compare-and-set write) before any Grain call and renews it
while it works, so two jobs for one handoff never both call Grain. Jobs are keyed by the row's `attempt`, which goes
up only when a new run is wanted (a new objective, a resume, an answer, or a design whose worker is gone).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from repairos.handoff import manifest as mf
from repairos.slots.grain import GrainError

log = logging.getLogger(__name__)

KIND = "grain-handoff"
POLL_S = 5.0
TURN_TIMEOUT_S = 60 * 60          # a design turn with many builds; past it the row says so and the next job polls on
MAX_SRC_FILE = 2 * 1024 * 1024
OUT_FILES = ("out/model.step", "out/model.glb", "out/report.json")
STAGES = ("queued", "measured", "project", "uploaded", "capture", "designing", "asking", "done", "stopped", "failed")
WORKING = ("queued", "measured", "project", "uploaded", "capture", "designing")
LEASE_S = 90.0                    # a run renews its lease at least this often (every save, and while polling)
MAX_TRANSIENT = 3                 # the queue's own attempt limit (state_sqlite.MAX_ATTEMPTS)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def handoff_id(scan_id: str, n: int) -> str:
    return f"gh_{scan_id}_{n}"


async def latest(state, scan_id: str):
    rows = await state.list(KIND, scan_id=scan_id)
    return max(rows, key=lambda r: int(r.data.get("objective_n") or 0)) if rows else None


def _live(row: Mapping[str, Any]) -> bool:
    """A worker holds the row's lease now."""
    lease = row.get("lease") or {}
    return float(lease.get("until") or 0) > time.time()


async def create(state, scan: Mapping[str, Any], objective: str, measure_request: Mapping | None,
                 unknowns: list[str]) -> dict:
    """The design for this objective: a new one (a new objective), the open one (the same objective while it is
    working or done), or the failed one resumed from the step that failed, in the same Grain project. A working design
    whose worker is gone (no live lease, nothing written for LEASE_S) gets a new attempt."""
    prev = await latest(state, scan["scan_id"])
    if prev and prev.data.get("objective") == objective:
        row = prev.data
        stale = row.get("stage") in WORKING and not _live(row) and \
            time.time() - _epoch(row.get("updated_at") or row.get("created_at")) > LEASE_S
        if row.get("stage") == "failed":
            row = {**row, "stage": row.get("resume_stage") or "queued", "resume_stage": None, "error": None}
        elif not stale:
            return row
        row = {**row, "attempt": int(row.get("attempt") or 1) + 1, "transient_failures": 0, "updated_at": _now()}
        await state.put(KIND, row, expect_rev=prev.rev)
        return row
    n = int(prev.data.get("objective_n") or 0) + 1 if prev else 1
    row = {"handoff_id": handoff_id(scan["scan_id"], n), "scan_id": scan["scan_id"],
           "session_id": scan.get("session_id"), "objective": objective, "objective_n": n,
           "measure_request": dict(measure_request) if measure_request else None, "unknowns": list(unknowns),
           "stage": "queued", "created_at": _now(), "grain": {}, "turns": [], "answers": [], "revisions": [],
           "question": None, "error": None, "announce": "grain", "attempt": 1, "transient_failures": 0, "lease": None}
    await state.put(KIND, row, expect_rev=0)
    return row


def _epoch(stamp: str | None) -> float:
    try:
        return datetime.fromisoformat(stamp).timestamp() if stamp else 0.0
    except ValueError:
        return 0.0


def job_key(row: Mapping[str, Any]) -> str:
    """The job id of the row's current attempt (the queue ignores an id it already has)."""
    return f"job_grain_{hashlib.sha256(f'{row['handoff_id']}:{row.get('attempt') or 1}'.encode()).hexdigest()[:16]}"


async def add_answer(state, hid: str, text: str) -> dict:
    r = await state.get(KIND, hid)
    if r is None:
        raise KeyError(hid)
    if r.data.get("stage") != "asking":
        raise ValueError(f"the Grain design is {r.data.get('stage')}, not waiting for an answer")
    row = dict(r.data)
    row["answers"] = [*row.get("answers", []), {"text": text, "at": _now(), "sent": False}]
    row["stage"] = "capture"           # the next job starts the answer turn
    row["attempt"] = int(row.get("attempt") or 1) + 1
    row["transient_failures"] = 0
    await state.put(KIND, row, expect_rev=r.rev)
    return row


def payload(row: Mapping[str, Any]) -> dict:
    """What the app sees (live event `grain` and GET .../grain): stage, question, revisions without blob internals."""
    revs = [{k: v for k, v in r.items() if k != "files"} | {"files": sorted(r.get("files") or {})}
            for r in row.get("revisions") or []]
    return {"scan_id": row["scan_id"], "handoff_id": row["handoff_id"], "objective": row.get("objective"),
            "stage": row.get("stage"), "question": row.get("question"), "error": row.get("error"),
            "grain_project_id": (row.get("grain") or {}).get("project_id"), "revisions": revs,
            "measurement": _measurement_view(row.get("measurement")), "notes": row.get("notes") or []}


def _measurement_view(m):
    if not m:
        return None
    return {"dimensions": m.get("dimensions"), "failed": m.get("failed"), "check": m.get("check"),
            "scale": {k: (m.get("scale") or {}).get(k) for k in ("s", "sigma_s", "motion_scale", "motion_agrees")},
            "adjustment": m.get("adjustment")}


class Handoff:
    """One run of the job: load the row, advance it, write after every step."""

    def __init__(self, ctx, hid: str):
        self.ctx, self.hid = ctx, hid
        self.rev: int | None = None
        self.row: dict = {}
        self.token = uuid.uuid4().hex

    async def load(self) -> dict:
        r = await self.ctx.state.get(KIND, self.hid)
        if r is None:
            raise KeyError(f"no grain handoff {self.hid}")
        self.rev, self.row = r.rev, json.loads(json.dumps(r.data))
        return self.row

    async def save(self, **changes) -> None:
        self.row.update(changes, updated_at=_now())
        if (self.row.get("lease") or {}).get("token") == self.token:
            self.row["lease"] = {"token": self.token, "until": time.time() + LEASE_S}
        self.rev = await self.ctx.state.put(KIND, self.row, expect_rev=self.rev)

    async def acquire(self) -> bool:
        """Take the row's lease, or False when another run holds it (or took it first)."""
        from repairos.slots.state import RevConflict
        if _live(self.row) and self.row["lease"].get("token") != self.token:
            return False
        self.row["lease"] = {"token": self.token, "until": time.time() + LEASE_S}
        try:
            await self.save()
        except RevConflict:
            return False
        return True

    @property
    def grain(self):
        return self.ctx.slot("GRAIN")

    async def call(self, fn, *a):
        return await asyncio.to_thread(fn, *a)

    # ------------------------------------------------------------------------------------------------- steps
    async def run(self) -> dict:
        await self.load()
        if self.row.get("stage") not in WORKING:
            return self._result()
        if not await self.acquire():
            return self._result() | {"skipped": "another run holds this design"}
        try:
            await self._advance()
        except GrainError as e:
            if not e.transient:
                await self.fail(str(e))
            elif int(self.row.get("transient_failures") or 0) + 1 >= MAX_TRANSIENT:
                await self.fail(f"Grain kept failing ({e}); ask again to resume")
            else:
                await self.save(error=str(e), transient_failures=int(self.row.get("transient_failures") or 0) + 1,
                                lease=None, notes=[*(self.row.get("notes") or []), f"{_now()}: {e}; retrying"])
                raise
        except Exception as e:  # noqa: BLE001 - anything else ends this attempt visibly, never a silent busy stage
            log.exception("grain handoff %s", self.hid)
            await self.fail(f"{type(e).__name__}: {e}")
        else:
            if self.row.get("lease"):
                await self.save(lease=None)
        return self._result()

    async def _advance(self) -> None:
        if self.row["stage"] == "queued":
            await self.measure()
        if self.row["stage"] == "measured":
            await self.project()
        if self.row["stage"] == "project":
            await self.upload()
        if self.row["stage"] in ("uploaded", "capture"):
            await self.capture()
            await self.turn()
        if self.row["stage"] == "designing":
            await self.poll()

    async def fail(self, error: str, resume: str | None = None) -> None:
        """Stop this attempt: the stage it stopped in is where asking again resumes."""
        stage = resume or self.row.get("stage")
        await self.save(stage="failed", resume_stage=stage if stage in WORKING else "capture", error=error,
                        lease=None)

    def _result(self) -> dict:
        return {"stage": self.row["stage"], "handoff_id": self.hid, "revisions": len(self.row.get("revisions") or []),
                "error": self.row.get("error")}

    async def measure(self) -> None:
        req = self.row.get("measure_request")
        if not req or not req.get("points"):
            await self.save(stage="measured", measurement=None,
                            notes=[*(self.row.get("notes") or []), "no marks: nothing measured, nothing verified"])
            return
        stills = [r.data for r in await self.ctx.state.list("still", scan_id=self.row["scan_id"])]
        rec = await asyncio.to_thread(measure_stills, self.ctx.blobs, stills, req)
        await self.save(stage="measured", measurement=rec)

    async def project(self) -> None:
        g = dict(self.row.get("grain") or {})
        if not g.get("project_id"):
            name = f"Lea {self.hid}"
            found = await self.call(self._find_project, name)
            pid = found or (await self.call(self.grain.create_project, name))["id"]
            g["project_id"] = pid
            await self.save(grain=g)
        await self.save(stage="project")

    def _find_project(self, name: str) -> str | None:
        return next((p["id"] for p in self.grain.projects() if p.get("name") == name), None)

    async def upload(self) -> None:
        g, pid = dict(self.row["grain"]), self.row["grain"]["project_id"]
        run = await self.ctx.state.get("pipeline-run", f"run_{self.row['scan_id']}")
        mesh = (run.data if run else {}).get("mesh")
        geo = mf.geometry_choice(mesh, self.ctx.blobs.size)
        scan_name = f"lea-{self.hid}.ply"
        have = {s.get("name"): s for s in await self.call(self.grain.scans, pid)}
        if geo and not g.get("scan_id"):
            ref, kind, source = geo
            if scan_name in have:
                g["scan_id"] = have[scan_name]["id"]
            else:
                data = await self.call(self.ctx.blobs.get, ref)
                g["scan_id"] = (await self.call(self.grain.upload_scan, pid, scan_name, data, "m"))["id"]
            g["geometry"] = {"file": scan_name, "kind": kind, "units": "m", "frame": "arkit_world",
                             "sha256": ref.rsplit(":", 1)[-1], "source": source,
                             "quality": {"triangles": (mesh or {}).get("triangles"),
                                         "points": (mesh or {}).get("cloud_points")}}
            await self.save(grain=g)
        await self._photos(g, pid)
        await self.save(stage="uploaded")

    async def _photos(self, g: dict, pid: str) -> None:
        """Upload the chosen photos as lea-<handoff>-<n>.jpg. Before each POST, what Grain already holds under those
        names is taken as sent (an upload whose answer was lost); for a photo-only handoff that is also how the record
        the first photo made is found again."""
        report = await self._report()
        stills = [r.data for r in await self.ctx.state.list("still", scan_id=self.row["scan_id"])]
        picked = mf.choose_photos(mf.findings(report), stills, self.row.get("measurement"))
        by = {s["still_id"]: s for s in stills}
        sent = {p["n"]: p for p in g.get("photos") or []}
        for n, sid in enumerate(picked, start=1):
            if n in sent:
                continue
            original = await self.call(self.ctx.blobs.get, by[sid]["image_file"])
            jpeg = await asyncio.to_thread(mf.photo_derivative, original)
            name = f"lea-{self.hid}-{n:02d}.jpg"
            held = await self.call(self._held_photo, pid, g.get("scan_id"), name)
            if held is None:
                got = await self.call(self.grain.upload_photo, pid, g.get("scan_id"), name, jpeg)
                held = got["id"]
            g["scan_id"] = g.get("scan_id") or held
            sent[n] = {"n": n, "still_id": sid, "sha256": mf.sha256(jpeg), "original_sha256": mf.sha256(original)}
            g["photos"] = [sent[k] for k in sorted(sent)]
            await self.save(grain=g)

    def _held_photo(self, pid: str, scan_id: str | None, name: str) -> str | None:
        """The id of the Grain record that already holds a photo named `name` (in `scan_id` when it is known)."""
        for rec in self.grain.scans(pid):
            if scan_id and rec.get("id") != scan_id:
                continue
            if any(p.get("name") == name for p in rec.get("photos") or []):
                return rec["id"]
        return None

    async def _report(self) -> dict | None:
        rows = await self.ctx.state.list("report", scan_id=self.row["scan_id"])
        return max(rows, key=lambda r: r.rev).data if rows else None

    async def capture(self) -> None:
        g, pid = self.row["grain"], self.row["grain"]["project_id"]
        report = await self._report()
        scan = await self.ctx.state.get("scan-package", self.row["scan_id"])
        m = mf.build(handoff_id=self.hid, scan=scan.data if scan else {"scan_id": self.row["scan_id"]},
                     report=report, revision=len(self.row.get("revisions") or []) + 1,
                     objective=self.row["objective"], geometry=g.get("geometry"), photos=g.get("photos") or [],
                     measurement=self.row.get("measurement"), grain_project_id=pid,
                     grain_scan_id=g.get("scan_id"), unknowns=self.row.get("unknowns") or [])
        text = json.dumps(m, indent=1)
        sha = hashlib.sha256(text.encode()).hexdigest()
        if g.get("capture_sha256") != sha:
            await self.call(self.grain.write_file, pid, mf.CAPTURE_PATH, text)
            await self.save(grain={**g, "capture_sha256": sha}, manifest=m)
        await self.save(stage="capture")

    async def turn(self) -> None:
        pid = self.row["grain"]["project_id"]
        turns = list(self.row.get("turns") or [])
        pending = [a for a in self.row.get("answers") or [] if not a.get("sent")]
        last = turns[-1] if turns else None
        if last and last["state"] == "started" and not pending:
            await self.save(stage="designing")
            return
        if last and last["state"] == "requested":      # saved, then the POST's outcome was lost: did it start?
            if await self.call(self._turn_began, pid, last["log_from"], last["message"]):
                await self._started(turns[:-1], last)
                return
            message, t = last["message"], last          # it did not: send the same turn again
            turns = turns[:-1]
        else:
            message = mf.first_message(self.row["manifest"]) if not turns else "\n".join(a["text"] for a in pending)
            msgs = await self.call(self.grain.messages, pid)
            t = {"n": len(turns) + 1, "state": "requested", "log_from": len(msgs), "at": _now(), "message": message}
        await self.save(turns=[*turns, t])
        try:
            await self.call(self.grain.start_turn, pid, message)
        except GrainError as e:
            if e.status == 503:                         # Grain has no model to design with: a person must act
                await self.fail(f"HUMAN ACTION NEEDED: Grain cannot start a design turn ({e})", resume="capture")
                return
            if e.busy or e.status is not None:          # busy: ours waits for the running turn (the job retries)
                raise
            if not await self.call(self._turn_began, pid, t["log_from"], message):
                raise
        await self._started(turns, t)

    async def _started(self, turns: list, t: dict) -> None:
        answers = [dict(a, sent=True) for a in self.row.get("answers") or []]
        await self.save(turns=[*turns, dict(t, state="started")], answers=answers, stage="designing",
                        question=None, error=None)

    def _turn_began(self, pid: str, log_from: int, message: str) -> bool:
        """Our turn began only if Grain logged our message after `log_from`; a busy project may be someone else's
        turn (then ours is still unsent and is sent once that one ends)."""
        return any(m.get("role") == "user" and str(m.get("text") or "").startswith(message[:200])
                   for m in self.grain.messages(pid)[log_from:])

    async def poll(self) -> None:
        pid = self.row["grain"]["project_id"]
        t = self.row["turns"][-1]
        deadline = time.monotonic() + TURN_TIMEOUT_S
        while True:
            p = await self.call(self.grain.project, pid)
            if not p.get("busy"):
                break
            if time.monotonic() > deadline:
                await self.save(notes=[*(self.row.get("notes") or []), f"{_now()}: turn {t['n']} still running"])
                raise GrainError(None, "the design turn is still running; polling again")
            if self.row["lease"]["until"] - time.time() < LEASE_S / 2:
                await self.save()                    # renew the lease
            await asyncio.sleep(POLL_S)
        msgs = (await self.call(self.grain.messages, pid))[t["log_from"]:]
        question = next((m for m in reversed(msgs) if m.get("type") == "question"), None)
        finished = next((m for m in reversed(msgs) if m.get("type") == "tool_use" and m.get("name") == "finish"), None)
        said = [m.get("text") for m in msgs if m.get("role") == "assistant" and m.get("type") == "text"]
        turns = [*self.row["turns"][:-1], dict(t, state="ended", ended_at=_now(), finished=bool(finished))]

        await self.save(turns=turns)
        rep = await self.call(self.grain.report, pid)
        built_this_turn = any(m.get("type") == "tool_use" and m.get("name") == "run_build" for m in msgs)
        if rep is not None and built_this_turn:
            await self.revision(pid, rep, t["n"], (finished or {}).get("input", {}).get("summary"))
        if p.get("waiting") and question:
            await self.save(stage="asking", question={k: question.get(k) for k in ("id", "why", "questions")})
        else:
            last = said[-1] if said else None
            await self.save(stage="done" if finished else "stopped", question=None,
                            summary=(finished or {}).get("input", {}).get("summary") or last)

    async def revision(self, pid: str, rep: Mapping[str, Any], turn_n: int, summary: str | None) -> None:
        if any(r.get("turn") == turn_n for r in self.row.get("revisions") or []):
            return                                   # this turn's build is already kept (a recovered run)
        files: dict[str, str] = {}
        listing = {f["path"]: f for f in await self.call(self.grain.files, pid)}
        for path in [*OUT_FILES, *sorted(p for p in listing if p.startswith("src/") and p != mf.CAPTURE_PATH)]:
            if path not in listing or (path.startswith("src/") and listing[path].get("size", 0) > MAX_SRC_FILE):
                continue
            data = await self.call(self.grain.read_file, pid, path)
            files[path] = await asyncio.to_thread(self.ctx.blobs.put, data, _ctype(path))
        fit = (rep.get("domains") or {}).get("fit")
        rev = {"n": len(self.row.get("revisions") or []) + 1, "turn": turn_n, "at": _now(),
               "capture_sha256": self.row["grain"].get("capture_sha256"),
               "build_ok": bool(rep.get("ok")), "errors": list(rep.get("errors") or [])[:20],
               "fit": None if fit is None else {k: fit.get(k) for k in ("status", "errors", "spacings", "holes_found")},
               "has_step": "out/model.step" in files, "summary": summary, "files": files}
        await self.save(revisions=[*(self.row.get("revisions") or []), rev])


def _decode(data: bytes, mode: str):
    import io

    import numpy as np
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        if mode == "rgb":
            return np.asarray(im.convert("RGB"))
        arr = np.asarray(im)
    if mode == "depth":       # 16-bit millimetres, as the app writes them (importers/images.read_depth_mm)
        if arr.ndim != 2 or arr.dtype.kind not in "ui" or arr.dtype.itemsize < 2:
            raise ValueError("depth is not a 16-bit single-channel PNG of millimetres")
        return arr.astype(np.float32) / 1000.0
    return arr.astype(np.uint8)


def measure_stills(blobs, stills: list[dict], request: Mapping[str, Any]) -> dict:
    """repairos.measure over a scan's stills straight from the blob store (gateway or worker)."""
    from repairos.measure.run import measure

    def opt(still, key, mode):
        return _decode(blobs.get(still[key]), mode) if still.get(key) else None
    return measure(stills, lambda s: _decode(blobs.get(s["image_file"]), "rgb"), lambda s: opt(s, "depth_file", "depth"),
                   request, lambda s: opt(s, "confidence_file", "conf"))


def _ctype(path: str) -> str:
    return {"step": "model/step", "glb": "model/gltf-binary", "json": "application/json"}.get(
        path.rsplit(".", 1)[-1].lower(), "text/plain")


async def run_handoff(ctx, hid: str) -> dict:
    return await Handoff(ctx, hid).run()
