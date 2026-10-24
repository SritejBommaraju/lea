"""Pipeline workers on the STATE job queue (task B10): `make run-workers`.

    python -m repairos.pipeline.worker [--once] [--poll 0.5] [--name w1]

Jobs (queued by the gateway, repairos.api.app):
  detect   on a scan package: record the capture facts; on a still: decide (still selection against the stills of
           its scan kept so far) and inspect it now, while the scan goes on, also before the scan package exists
  ocr      on a label still: the inspect stage (label stills are not_inspected until OCR exists, M8)
  pipeline on `POST /v1/scans/{scan_id}/complete` (the upload is complete): the whole run, then the report
Every SWEEP_S the worker also closes continuations nobody finished (run.close_abandoned).
Shutdown (SIGTERM from the pod, SIGINT): no new job is claimed; running jobs get GRACE_S to finish; what is still
running is cancelled, its still claims are released to pending (their stills queued again) and its jobs go back to
the queue untried. Startup and every sweep release claims whose worker is gone (run.release_claims), and every still
claim carries a heartbeat (run.beat_claims), so a crash or a kill never leaves a still `running` for good.
One worker runs up to `worker_jobs` (config/pipeline.yaml) jobs at once in one event loop, sharing one Context: one
detector model, one Claude call pool. A pipeline job is claimed before any other kind, so a finished scan's report
is not queued behind the next scan's stills.
A failed job is retried by the queue (up to its attempt limit) when the error is transient (Jev or Claude
unreachable); the run row shows `failed` with the stage and the error either way. Never a silent default.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
from typing import Any

from repairos.pipeline.run import (CLAIM_BEAT_S, Context, PipelineFailed, beat_claims, close_abandoned,
                                   inspect_still_stage, record_capture, release_claims, run_scan)
from repairos.slots.state import Job

KINDS = ["detect", "ocr", "pipeline"]
HEARTBEAT_S = 30.0
SWEEP_S = 30.0
GRACE_S = 20.0   # on SIGTERM / SIGINT: how long running jobs may finish before their claims are released   # how often an idle or busy worker closes continuations nobody finished (close_abandoned)
log = logging.getLogger("repairos.pipeline.worker")


def transient(e: BaseException) -> bool:
    """Worth retrying: the service was unreachable or busy, not our request or our data."""
    from repairos.slots.fusion import FusionUnavailable

    cause = e.__cause__ or e
    if isinstance(cause, (FusionUnavailable, TimeoutError, ConnectionError)):
        return True
    try:
        import anthropic
    except ImportError:
        return False
    return isinstance(cause, (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError))


async def handle(ctx: Context, job: Job) -> dict[str, Any]:
    kind, inputs = job.kind, job.inputs or {}
    if kind in ("detect", "ocr") and inputs.get("kind") == "still":
        stage = await inspect_still_stage(ctx, inputs["id"], decide=kind == "detect")
        return {"stage": "inspect", "still_id": inputs["id"], "findings": len(stage.get("finding_ids", []))}
    if kind == "detect" and inputs.get("kind") == "scan-package":
        return await record_capture(ctx, inputs["id"])
    if kind == "pipeline":
        report = await run_scan(ctx, inputs.get("scan_id") or job.scan_id)
        return {"stage": "report", "report_id": report["report_id"], "verdict": report["verdict"]}
    raise ValueError(f"job {job.job_id}: no handler for kind {kind!r} with inputs {inputs!r}")


class Worker:
    def __init__(self, state: Any, blobs: Any, registry: Any = None, name: str | None = None,
                 kinds: list[str] | None = None, poll_s: float = 0.5, jobs_at_once: int | None = None):
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"
        self.ctx = Context(state=state, blobs=blobs, registry=registry, worker=self.name)
        self.state, self.kinds, self.poll_s = state, kinds or KINDS, poll_s
        self.jobs_at_once = jobs_at_once or self.ctx.config["worker_jobs"]
        self.active: dict[asyncio.Task, Job] = {}

    async def _claim(self) -> Job | None:
        """Pipeline jobs first (a report waiting is worth more than one more still), then the rest in queue order."""
        first = [k for k in self.kinds if k == "pipeline"]
        job = await self.state.claim(self.name, first) if first else None
        rest = [k for k in self.kinds if k != "pipeline"]
        return job or (await self.state.claim(self.name, rest) if rest else None)

    async def _beat(self, job_id: str) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            await self.state.heartbeat(job_id)

    async def run_once(self) -> Job | None:
        """Claim and run one job; None when the queue is empty."""
        job = await self._claim()
        if job is None:
            return None
        return await self._run(job)

    async def _run(self, job: Job) -> Job | None:
        beat = asyncio.create_task(self._beat(job.job_id))
        try:
            result = await handle(self.ctx, job)
        except Exception as e:
            retry = transient(e)
            log.error("job %s (%s) failed%s: %s", job.job_id, job.kind, ", will retry" if retry else "", e)
            await self.state.fail(job.job_id, f"{type(e).__name__}: {e}"[:4000], retry=retry)
            if not isinstance(e, PipelineFailed):
                log.debug("traceback", exc_info=True)
        else:
            await self.state.complete(job.job_id, result)
            log.info("job %s (%s) done: %s", job.job_id, job.kind, result)
        finally:
            beat.cancel()
        return await self.state.get_job(job.job_id)

    async def drain(self) -> list[Job]:
        done = []
        while (job := await self.run_once()) is not None:
            done.append(job)
        return done

    async def run_forever(self, stop: asyncio.Event | None = None, grace_s: float = GRACE_S) -> None:
        """Up to `jobs_at_once` jobs at a time; an idle worker polls every `poll_s`."""
        stop = stop or asyncio.Event()
        running: set[asyncio.Task] = set()
        swept = beat = 0.0
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            if loop.time() - swept >= SWEEP_S:
                swept = loop.time()
                try:
                    await release_claims(self.ctx)          # claims of workers that are gone (also at startup)
                    await close_abandoned(self.ctx)
                except Exception as e:  # noqa: BLE001 - the sweep never stops the worker; it is retried next time
                    log.error("sweep failed: %s", e)
            if loop.time() - beat >= CLAIM_BEAT_S:
                beat = loop.time()
                try:
                    await beat_claims(self.ctx)
                except Exception as e:  # noqa: BLE001
                    log.error("claim heartbeat failed: %s", e)
            while len(running) < self.jobs_at_once and not stop.is_set() and (job := await self._claim()) is not None:
                task = asyncio.create_task(self._run(job))
                self.active[task] = job
                task.add_done_callback(lambda t: self.active.pop(t, None))
                running.add(task)
            waiters = [asyncio.create_task(stop.wait())]
            try:
                await asyncio.wait(running | set(waiters), timeout=self.poll_s, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waiters:
                    w.cancel()
            running = {t for t in running if not t.done()}
        await self.shutdown(running, grace_s)

    async def shutdown(self, running: set[asyncio.Task], grace_s: float = GRACE_S) -> None:
        """Let running jobs finish for `grace_s`, then cancel the rest, release their still claims and put their
        jobs back in the queue."""
        if running:
            log.info("stopping: %d job(s) running, %.0f s to finish", len(running), grace_s)
            _, late = await asyncio.wait(running, timeout=grace_s)
            unfinished = [self.active.get(t) for t in late]
            for t in late:
                t.cancel()
            await asyncio.gather(*late, return_exceptions=True)
            for job in unfinished:
                if job is not None:
                    await self.state.release(job.job_id)
                    log.info("job %s (%s) back in the queue", job.job_id, job.kind)
        released = await release_claims(self.ctx, worker=self.name)
        if released:
            log.info("released %d still claim(s) on shutdown", len(released))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--once", action="store_true", help="run every queued job, then exit")
    ap.add_argument("--poll", type=float, default=0.5, help="seconds between polls of an empty queue")
    ap.add_argument("--name", default=None, help="worker name recorded on claimed jobs")
    ap.add_argument("--jobs", type=int, default=None, help="jobs at once (default: worker_jobs in config/pipeline.yaml)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from repairos.registry import default_registry

    reg = default_registry()
    w = Worker(reg.slot("STATE"), reg.slot("BLOB"), reg, name=a.name, poll_s=a.poll, jobs_at_once=a.jobs)
    log.info("worker %s on kinds %s, %d jobs at once (STATE=%s, BLOB=%s)", w.name, w.kinds, w.jobs_at_once,
             reg.selected("STATE"), reg.selected("BLOB"))
    if a.once:
        jobs = asyncio.run(w.drain())
        failed = [j for j in jobs if j and j.status == "failed"]
        print(f"{len(jobs)} job(s) run, {len(failed)} failed")
        return 1 if failed else 0
    asyncio.run(serve(w))
    return 0


async def serve(w: Worker, grace_s: float = GRACE_S) -> None:
    """run_forever until SIGTERM or SIGINT, then a graceful shutdown (Worker.shutdown)."""
    import signal

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await w.run_forever(stop, grace_s)


if __name__ == "__main__":
    sys.exit(main())
