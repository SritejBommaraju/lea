"""Phase 1 pipeline orchestrator (PLAN.md task B10): inspect -> fuse -> locate -> report.

    await run_session(session_id=..., state=..., blobs=...)    # tools/replay.py, `make replay SESSION=...`
    python -m repairos.pipeline.worker                          # `make run-workers`: the STATE job queue

run.py holds the stages, worker.py the job worker, locate_iface.py the seam to the 3D localization module.
"""
from __future__ import annotations

from repairos.pipeline.run import (Context, PipelineError, PipelineFailed, inspect_still_stage, record_capture,
                                   run_id_for, run_scan, run_session)

__all__ = ["Context", "PipelineError", "PipelineFailed", "inspect_still_stage", "record_capture", "run_id_for",
           "run_scan", "run_session"]
