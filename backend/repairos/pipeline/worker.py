"""Pipeline workers on the STATE job queue (task B10): `make run-workers`.

    python -m repairos.pipeline.worker [--once] [--poll 0.5] [--name w1]

Jobs (queued by the gateway, repairos.api.app):
  detect   on a scan package: record the capture facts; on a still: inspect it now, while the upload goes on
  ocr      on a label still: the inspect stage (label stills are not_inspected until OCR exists, M8)
  pipeline on `POST /v1/scans/{scan_id}/complete` (the upload is complete): the whole run, then the report
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

from repairos.pipeline.run import Context, PipelineFailed, inspect_still_stage, record_capture, run_scan
from repairos.slots.state import Job

KINDS = ["detect", "ocr", "pipeline"]
HEARTBEAT_S = 30.0
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
        stage = await inspect_still_stage(ctx, inputs["id"])
        return {"stage": "inspect", "still_id": inputs["id"], "findings": len(stage.get("finding_ids", []))}
    if kind == "detect" and inputs.get("kind") == "scan-package":
        return await record_capture(ctx, inputs["id"])
    if kind == "pipeline":
        report = await run_scan(ctx, inputs.get("scan_id") or job.scan_id)
        return {"stage": "report", "report_id": report["report_id"], "verdict": report["verdict"]}
    raise ValueError(f"job {job.job_id}: no handler for kind {kind!r} with inputs {inputs!r}")


class Worker:
    def __init__(self, state: Any, blobs: Any, registry: Any = None, name: str | None = None,
                 kinds: list[str] | None = None, poll_s: float = 0.5):
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"
        self.ctx = Context(state=state, blobs=blobs, registry=registry, worker=self.name)
        self.state, self.kinds, self.poll_s = state, kinds or KINDS, poll_s

    async def _beat(self, job_id: str) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            await self.state.heartbeat(job_id)

    async def run_once(self) -> Job | None:
        """Claim and run one job; None when the queue is empty."""
        job = await self.state.claim(self.name, self.kinds)
        if job is None:
            return None
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

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            if await self.run_once() is None:
                try:
                    await asyncio.wait_for(stop.wait(), self.poll_s)
                except TimeoutError:
                    pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--once", action="store_true", help="run every queued job, then exit")
    ap.add_argument("--poll", type=float, default=0.5, help="seconds between polls of an empty queue")
    ap.add_argument("--name", default=None, help="worker name recorded on claimed jobs")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from repairos.registry import default_registry

    reg = default_registry()
    w = Worker(reg.slot("STATE"), reg.slot("BLOB"), reg, name=a.name, poll_s=a.poll)
    log.info("worker %s on kinds %s (STATE=%s, BLOB=%s)", w.name, w.kinds, reg.selected("STATE"),
             reg.selected("BLOB"))
    if a.once:
        jobs = asyncio.run(w.drain())
        failed = [j for j in jobs if j and j.status == "failed"]
        print(f"{len(jobs)} job(s) run, {len(failed)} failed")
        return 1 if failed else 0
    try:
        asyncio.run(w.run_forever())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
