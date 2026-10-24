"""Worker restarts never leave a still claim `running` for good (Greptile on PR #9: the pod's start.sh sends SIGTERM,
the worker had no handler, and a still claimed by the old worker made every later run wait STILL_WAIT_S and fail).

- SIGTERM mid-job: no new job is claimed, the job is cancelled after the grace period, its still claim goes back to
  pending (the still queued again) and the job back to the queue untried.
- At startup (and every sweep) a claim whose worker is gone (its pid exited on this host, or no heartbeat for
  CLAIM_STALE_S) is released and its still queued again; a live claim is untouched.
- A run waiting on a stale claim takes it over instead of waiting out STILL_WAIT_S.
Offline: label stills (no model) from the public contract examples; the long job is our own handler held open."""
from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from repairos.pipeline import worker as W
from repairos.pipeline.run import (CLAIM_STALE_S, Context, _claim, beat_claims, inspect_still_stage,
                                   release_claims, run_id_for)
from repairos.slots.state import Job
from test_pipeline import env, ex, run  # noqa: F401 (fixture)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _claim_as(state, scan, still_id, worker, at, heartbeat=None):
    patch = {"still_id": still_id, "worker": worker, "at": _stamp(at), "scan_id": scan["scan_id"],
             "session_id": scan["session_id"]}
    run(state.transition("pipeline-run", run_id_for(scan["scan_id"]), "pipeline.claim_still", patch))
    if heartbeat is not None:
        run(state.transition("pipeline-run", run_id_for(scan["scan_id"]), "pipeline.update", {
            "scan_id": scan["scan_id"], "session_id": scan["session_id"],
            "stills": {still_id: {"heartbeat_at": _stamp(heartbeat)}}}))


def _stills(state, scan):
    return run(state.get("pipeline-run", run_id_for(scan["scan_id"]))).data["stills"]


def _queued_for(state, still_id):
    import sqlite3

    with sqlite3.connect(state.path) as c:
        return [r[0] for r in c.execute("SELECT job_id FROM job WHERE status='queued' AND inputs LIKE ?",
                                        (f'%"{still_id}"%',))]


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_sigterm_mid_job_releases_the_claim_and_the_job(env, tmp_path, monkeypatch):  # noqa: F811
    state, blobs, scan = env
    claimed = asyncio.Event()

    async def long_job(ctx, job):   # our own handler, held open: a still claimed and being inspected
        how, _ = await _claim(ctx, scan, "s_label")
        assert how == "mine"
        claimed.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(W, "handle", long_job)
    run(state.enqueue(Job(kind="detect", job_id="job_long", inputs={"kind": "still", "id": "s_label"})))
    w = W.Worker(state, blobs, name=f"{socket.gethostname()}:{os.getpid()}", jobs_at_once=2, poll_s=0.05)

    async def go():
        server = asyncio.create_task(W.serve(w, grace_s=0.2))
        await asyncio.wait_for(claimed.wait(), 10)
        row = await state.get("pipeline-run", run_id_for(scan["scan_id"]))
        assert row.data["stills"]["s_label"]["status"] == "running"
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(server, 10)
    run(go())
    assert "s_label" not in _stills(state, scan)                    # back to pending
    assert _queued_for(state, "s_label")                            # and queued again
    job = run(state.get_job("job_long"))
    assert job.status == "queued" and job.worker is None and job.attempts == 0


def test_a_stale_claim_found_at_startup_is_requeued(env, tmp_path):  # noqa: F811
    state, blobs, scan = env
    now = datetime.now(UTC)
    _claim_as(state, scan, "s_label", "another-host:41", now - timedelta(seconds=CLAIM_STALE_S + 30))
    _claim_as(state, scan, "s_skip", f"{socket.gethostname()}:{_dead_pid()}", now)   # exited process, fresh claim
    ctx = Context(state=state, blobs=blobs, root=tmp_path / "p", worker="new-worker")
    released = run(release_claims(ctx))
    assert sorted(sid for _, sid in released) == ["s_label", "s_skip"]
    assert _stills(state, scan) == {}
    assert _queued_for(state, "s_label") and _queued_for(state, "s_skip")


def test_a_live_claim_is_untouched(env, tmp_path):  # noqa: F811
    state, blobs, scan = env
    now = datetime.now(UTC)
    _claim_as(state, scan, "s_label", "another-host:42", now - timedelta(minutes=10), heartbeat=now)
    _claim_as(state, scan, "s_skip", f"{socket.gethostname()}:{os.getpid()}", now - timedelta(minutes=10),
              heartbeat=now)
    ctx = Context(state=state, blobs=blobs, root=tmp_path / "p", worker="new-worker")
    assert run(release_claims(ctx)) == []
    st = _stills(state, scan)
    assert st["s_label"]["status"] == "running" and st["s_skip"]["status"] == "running"
    assert not _queued_for(state, "s_label")


def test_a_worker_beats_its_claims_and_a_run_takes_over_a_dead_one(env, tmp_path):  # noqa: F811
    state, blobs, scan = env
    ctx = Context(state=state, blobs=blobs, root=tmp_path / "p", worker="me")
    run(_claim(ctx, scan, "s_label"))
    assert run(beat_claims(ctx)) == 1 and _stills(state, scan)["s_label"].get("heartbeat_at")
    # a claim on s_skip by a worker that is gone: a run needing that still inspects it itself, at once
    _claim_as(state, scan, "s_skip", "gone-host:7", datetime.now(UTC) - timedelta(seconds=CLAIM_STALE_S + 5))
    other = Context(state=state, blobs=blobs, root=tmp_path / "q", worker="runner")
    stage = run(asyncio.wait_for(inspect_still_stage(other, "s_skip"), 10))
    assert stage["status"] == "done" and _stills(state, scan)["s_skip"]["worker"] == "runner"


@pytest.mark.parametrize("name,alive", [("elsewhere:1", None), ("bad-name", None)])
def test_worker_liveness_is_only_judged_on_this_host(name, alive):
    from repairos.pipeline.run import worker_alive

    assert worker_alive(name) is alive
    assert worker_alive(f"{socket.gethostname()}:{os.getpid()}") is True
    assert worker_alive(f"{socket.gethostname()}:{_dead_pid()}") is False
    assert ex("still")["still_id"]
