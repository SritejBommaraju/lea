#!/usr/bin/env python3
"""Replay one recorded session offline (BUILD.md B0.7). Format: samples/README.md.

    make replay SESSION=<session_id | path>          # = backend/.venv/bin/python tools/replay.py --session ...
    python tools/replay.py --check [--all | --session X]  # validate only (CI: every non-held-out session)

Steps: validate the folder; load it into a fresh STATE + BLOB (the configured slots, data dir
data/replay/<session_id>/) through the gateway's transitions; run `repairos.pipeline.run_session` when the pipeline
exists (card F4.8; until then the run says `not_built` and nothing stands in for it); score the findings against
truth.json; write data/replay/<session_id>/summary.json. Phase 1 makes no Grain calls.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _check(paths: list[Path]) -> int:
    from repairos.sessions.validate import validate_session

    bad = 0
    for p in paths:
        problems = validate_session(p)
        print(f"{'FAIL' if problems else 'ok  '} {p}")
        for msg in problems:
            print(f"     - {msg}")
        bad += bool(problems)
    print(f"{len(paths)} session(s), {bad} invalid")
    return 1 if bad else 0


async def _replay(path: Path, out_root: Path, allow_heldout: bool) -> dict:
    from repairos.sessions.ingest import ingest, score
    from repairos.sessions.reader import open_session
    from repairos.sessions.validate import validate_session

    stages: list[dict] = []

    def stage(name: str, t0: float, status: str, **extra) -> None:
        stages.append({"stage": name, "seconds": round(time.monotonic() - t0, 3), "status": status, **extra})

    t0 = time.monotonic()
    session = open_session(path, allow_heldout=allow_heldout)
    problems = validate_session(session.dir)
    stage("validate", t0, "failed" if problems else "ok", problems=problems)
    out = out_root / session.session_id
    summary = {"session_id": session.session_id, "source": str(session.dir), "out": str(out), "stages": stages,
               "grain": "not called (Phase 1)"}
    if problems:
        return summary

    if out.exists():
        shutil.rmtree(out)
    os.environ["REPAIROS_DATA_DIR"] = str(out)  # the slots below keep their state in this run's folder
    from repairos.registry import Registry
    registry = Registry()
    state, blobs = registry.slot("STATE"), registry.slot("BLOB")
    summary["slots"] = {"STATE": registry.selected("STATE"), "BLOB": registry.selected("BLOB")}

    t0 = time.monotonic()
    summary["counts"] = await ingest(session, state, blobs)
    stage("ingest", t0, "ok")

    t0 = time.monotonic()
    try:
        pipeline = importlib.import_module("repairos.pipeline")
    except ModuleNotFoundError as e:
        if e.name not in ("repairos.pipeline",):
            raise
        pipeline = None
    if pipeline is None or not hasattr(pipeline, "run_session"):
        stage("pipeline", t0, "not_built", card="F4.8")
    else:
        result = pipeline.run_session(session_id=session.session_id, state=state, blobs=blobs)
        if inspect.isawaitable(result):
            await result
        stage("pipeline", t0, "ok")

    findings = [r.data for r in await state.list("finding", session_id=session.session_id)]
    summary["findings"] = [{"finding_id": f["finding_id"], "subject": f["subject"], "state": f["state"],
                            "source": f["source"], "is_proof": f["is_proof"]} for f in findings]
    summary["outcome"] = score(session.truth, findings)
    if session.truth is None:
        summary["outcome_note"] = "no truth.json: nothing to score against"
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", help="session id under samples/sessions/, or a session folder path")
    ap.add_argument("--all", action="store_true", help="with --check: every non-held-out session in --root")
    ap.add_argument("--check", action="store_true", help="validate only")
    ap.add_argument("--root", type=Path, default=ROOT / "samples" / "sessions")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "replay")
    ap.add_argument("--allow-heldout", action="store_true", help="evaluation only (E8.3)")
    args = ap.parse_args(argv)

    from repairos.sessions.reader import list_sessions, session_path

    if args.check:
        if args.all:
            return _check(list_sessions(args.root, include_heldout=args.allow_heldout))
        if not args.session:
            ap.error("--check needs --session or --all")
        return _check([session_path(args.session, args.root)])
    if not args.session:
        ap.error("--session is required (make replay SESSION=<id or path>)")
    summary = asyncio.run(_replay(session_path(args.session, args.root), args.out, args.allow_heldout))
    if "counts" in summary:
        Path(summary["out"]).mkdir(parents=True, exist_ok=True)
        (Path(summary["out"]) / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if all(s["status"] != "failed" for s in summary["stages"]) else 1


if __name__ == "__main__":
    sys.exit(main())
