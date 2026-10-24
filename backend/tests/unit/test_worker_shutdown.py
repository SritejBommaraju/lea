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


# ------------------------------------------------------------------- Greptile round 2 (PR #7, after ffb2d32)
def test_a_stale_claim_is_taken_over_by_one_waiter_only(env, tmp_path):  # noqa: F811
    from repairos.slots.state import AlreadyClaimed

    state, blobs, scan = env
    _claim_as(state, scan, "s_label", "gone-host:9", datetime.now(UTC) - timedelta(seconds=CLAIM_STALE_S + 5))
    seen = dict(_stills(state, scan)["s_label"])
    expect = {k: seen.get(k) for k in ("worker", "started_at", "heartbeat_at")}
    take = {"still_id": "s_label", "at": _stamp(datetime.now(UTC)), "expect": expect}
    run(state.transition("pipeline-run", run_id_for(scan["scan_id"]), "pipeline.claim_still", {**take, "worker": "a"}))
    with pytest.raises(AlreadyClaimed):    # the same stale snapshot no longer matches: the second waiter loses
        run(state.transition("pipeline-run", run_id_for(scan["scan_id"]), "pipeline.claim_still",
                             {**take, "worker": "b"}))
    assert _stills(state, scan)["s_label"]["worker"] == "a"
    # and through the pipeline: two runs waiting on one dead claim, concurrently; both end done, one inspected it
    _claim_as(state, scan, "s_skip", "gone-host:10", datetime.now(UTC) - timedelta(seconds=CLAIM_STALE_S + 5))
    one = Context(state=state, blobs=blobs, root=tmp_path / "1", worker="one")
    two = Context(state=state, blobs=blobs, root=tmp_path / "2", worker="two")

    async def both():
        return await asyncio.gather(inspect_still_stage(one, "s_skip"), inspect_still_stage(two, "s_skip"))
    a, b = run(asyncio.wait_for(both(), 20))
    assert a["status"] == b["status"] == "done" and _stills(state, scan)["s_skip"]["worker"] in ("one", "two")


def test_release_leaves_a_claim_that_changed_since_it_was_seen(env, tmp_path):  # noqa: F811
    state, blobs, scan = env
    old = datetime.now(UTC) - timedelta(seconds=CLAIM_STALE_S + 30)
    _claim_as(state, scan, "s_label", "slow-host:3", old)
    seen = dict(_stills(state, scan)["s_label"])
    # the owner beats between the sweep's read and its write: the release must not happen
    run(state.transition("pipeline-run", run_id_for(scan["scan_id"]), "pipeline.beat_still",
                         {"still_id": "s_label", "worker": "slow-host:3", "at": _stamp(datetime.now(UTC))}))
    from repairos.slots.state import AlreadyClaimed

    with pytest.raises(AlreadyClaimed):
        run(state.transition("pipeline-run", run_id_for(scan["scan_id"]), "pipeline.release_still", {
            "still_id": "s_label", "expect": {k: seen.get(k) for k in ("worker", "started_at", "heartbeat_at")}}))
    assert _stills(state, scan)["s_label"]["status"] == "running"


def test_a_released_claim_is_never_written_over_by_the_old_inspection(env, tmp_path):  # noqa: F811
    from repairos.pipeline.run import ClaimLost, _write_still

    state, blobs, scan = env
    me = Context(state=state, blobs=blobs, root=tmp_path / "p", worker="old")

    async def go():
        assert (await _claim(me, scan, "s_label"))[0] == "mine"
        await release_claims(me, worker="old")                    # shutdown released it
        nxt = Context(state=state, blobs=blobs, root=tmp_path / "q", worker="new")
        assert (await _claim(nxt, scan, "s_label"))[0] == "mine"  # another worker took the still
        with pytest.raises(ClaimLost):                            # the old inspection finishing late
            await _write_still(me, scan, "s_label", {"status": "done", "finding_ids": ["f_x"]})
    run(go())
    st = _stills(state, scan)["s_label"]
    assert st["status"] == "running" and st["worker"] == "new" and "finding_ids" not in st


def test_a_stopping_worker_starts_no_further_model_call():
    import threading

    from PIL import Image

    from repairos.slots.detector import Detection
    from repairos.vision.inspect import inspect_still

    stop = threading.Event()
    stop.set()
    dets = [Detection(box=(10, 10, 60, 60), label="connector", score=0.5)]
    with pytest.raises(RuntimeError, match="stopping"):
        inspect_still(Image.new("RGB", (400, 300)), still_id="s1", scan_id="sc", detector=object(),
                      judge=object(), classifier=False, detections=dets, stop=stop)   # judge is never reached


def test_claims_beat_in_every_mode_not_only_run_forever(env, tmp_path, monkeypatch):  # noqa: F811
    from repairos.pipeline import run as R

    state, blobs, scan = env
    monkeypatch.setattr(R, "CLAIM_BEAT_S", 0.05)

    async def held_job(ctx, job):   # a --once / drain job holding a claim for a while
        assert (await _claim(ctx, scan, "s_label"))[0] == "mine"
        await asyncio.sleep(0.4)
        return {"held": True}

    monkeypatch.setattr(W, "handle", held_job)
    run(state.enqueue(Job(kind="detect", job_id="job_held", inputs={"kind": "still", "id": "s_label"})))
    w = W.Worker(state, blobs, name="drainer")
    jobs = run(w.drain())
    assert [j.status for j in jobs] == ["done"]
    assert _stills(state, scan)["s_label"].get("heartbeat_at")      # beaten while held, without run_forever


# ----------------------------------------- Greptile round 3: stopping is "release, not fail" (WorkerStopping)
def _stop_aware_inspection(R, monkeypatch, ignore_stop: bool = False, error: Exception | None = None):
    """Replace only the per-still inspection body: it waits for the worker's stop flag and then runs the real
    inspect_still, whose real stop check raises WorkerStopping (no model is reached: the judge is a bare object).
    `ignore_stop`: a model call still in flight past the grace period. `error`: a real inspection error."""
    from PIL import Image

    from repairos.slots.detector import Detection
    from repairos.vision.inspect import inspect_still

    async def inspect_one(ctx, scan, still, covered=None):
        if error is not None:
            raise error
        while not ctx.stopping.is_set():
            await asyncio.sleep(0.02)
        if ignore_stop:
            await asyncio.sleep(3600)
        await asyncio.to_thread(inspect_still, Image.new("RGB", (400, 300)), still_id=still["still_id"],
                                scan_id=scan["scan_id"], detector=object(), judge=object(), classifier=False,
                                detections=[Detection(box=(10, 10, 60, 60), label="connector", score=0.5)],
                                stop=ctx.stopping)
    monkeypatch.setattr(R, "_inspect_one", inspect_one)


def _serve_until_claimed_then_sigterm(state, scan, w, grace_s):
    async def go():
        server = asyncio.create_task(W.serve(w, grace_s=grace_s))
        for _ in range(500):
            row = await state.get("pipeline-run", run_id_for(scan["scan_id"]))
            if row and (row.data.get("stills") or {}).get("s_label", {}).get("status") == "running":
                break
            await asyncio.sleep(0.02)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(server, 20)
    run(go())


@pytest.mark.parametrize("within_grace", [True, False])
def test_stopping_mid_inspection_releases_and_never_fails(env, monkeypatch, within_grace):  # noqa: F811
    from repairos.pipeline import run as R

    state, blobs, scan = env
    _stop_aware_inspection(R, monkeypatch, ignore_stop=not within_grace)
    run(state.enqueue(Job(kind="ocr", job_id="job_stop", inputs={"kind": "still", "id": "s_label"})))
    w = W.Worker(state, blobs, name=f"{socket.gethostname()}:{os.getpid()}", jobs_at_once=1, poll_s=0.02)
    _serve_until_claimed_then_sigterm(state, scan, w, grace_s=5.0 if within_grace else 0.2)
    stills = _stills(state, scan)
    assert "s_label" not in stills                                  # pending, never "failed"
    job = run(state.get_job("job_stop"))
    assert job.status == "queued" and job.attempts == 0 and job.worker is None and not job.error
    if not within_grace:                                            # cancelled: released and queued again
        assert _queued_for(state, "s_label")


def test_a_real_inspection_error_still_fails(env, monkeypatch):  # noqa: F811
    from repairos.pipeline import run as R

    state, blobs, scan = env
    _stop_aware_inspection(R, monkeypatch, error=ValueError("a real bug"))
    run(state.enqueue(Job(kind="ocr", job_id="job_bug", inputs={"kind": "still", "id": "s_label"})))
    jobs = run(W.Worker(state, blobs, name="w").drain())
    assert [j.status for j in jobs] == ["failed"] and "a real bug" in jobs[0].error
    st = _stills(state, scan)["s_label"]
    assert st["status"] == "failed" and "a real bug" in st["error"]


def test_the_stop_signal_has_one_import_path():
    from repairos.pipeline.run import WorkerStopping as from_run
    from repairos.pipeline.stopping import WorkerStopping, is_stopping

    assert from_run is WorkerStopping
    try:
        try:
            raise WorkerStopping("stop")
        except WorkerStopping as e:
            raise RuntimeError("wrapped") from e
    except RuntimeError as wrapped:
        assert is_stopping(wrapped) and not is_stopping(ValueError("x"))
