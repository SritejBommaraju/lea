"""Two report runs of one scan at once (Greptile, PR #13): /complete, another still, /complete again queue two
pipeline jobs that one worker (worker_jobs > 1) or two workers run together. The run that started with fewer stills
can finish last; its draft must never replace the report built from more stills. report.publish compares the still
counts inside its write transaction (StaleReport), and the older run then ends quietly: no failure, no write to the
run row, and it returns the report that stands."""
from __future__ import annotations

import asyncio

import pytest

from repairos import spend
from repairos.slots.state import StaleReport
from test_pipeline import _not_inspected, _report_for, _stored, env, run  # noqa: F401 (fixture)
from test_spend import caps  # noqa: F401 (fixture)


def _draft(scan, stills: int, created_at: str):
    ni = _not_inspected(scan)
    rep = _report_for(scan, [ni], {ni["finding_id"]: {"part_class": "label", "inspected_by": []}})
    rep["capture"]["stills"] = stills
    return {**rep, "created_at": created_at}


def test_publish_never_replaces_a_report_built_from_more_stills(env):
    state, _, scan = env
    _stored(state, scan, _not_inspected(scan))
    newer = _draft(scan, 3, "2026-10-24T22:00:02Z")
    rid = newer["report_id"]
    run(state.transition("report", rid, "report.publish", newer))
    with pytest.raises(StaleReport) as e:
        run(state.transition("report", rid, "report.publish", _draft(scan, 2, "2026-10-24T22:00:03Z")))
    assert e.value.draft_stills == 2 and e.value.published_stills == 3
    row = run(state.get("report", rid))
    assert row.rev == 1 and row.data["created_at"] == newer["created_at"]   # nothing written
    run(state.transition("report", rid, "report.publish", _draft(scan, 3, "2026-10-24T22:00:04Z")))   # a re-run
    run(state.transition("report", rid, "report.publish", _draft(scan, 4, "2026-10-24T22:00:05Z")))   # more stills
    assert run(state.get("report", rid)).rev == 3


def test_an_older_run_finishing_last_leaves_the_newer_report(env, caps, tmp_path, monkeypatch):  # noqa: F811
    """The whole run (label and skipped stills: no model needed; Claude's cap is used up so the summary writer is
    never called): a newer report with more stills is published while this run builds its draft."""
    from repairos.pipeline import run as R
    from repairos.slots.llm_anthropic import AnthropicTextModel

    state, blobs, scan = env
    caps(anthropic={"per_minute": 1, "repeat_per_minute": None})
    spend.charge("anthropic")   # the minute's cap is used up: the summary writer is refused, never called
    _stored(state, scan, _not_inspected(scan))
    newer = _draft(scan, 5, "2026-10-24T22:00:09Z")   # the other run, started after two more stills arrived

    real_start_fix = R.start_fix

    async def newer_publishes_first(ctx, sc, draft):
        await state.transition("report", draft["report_id"], "report.publish", newer)
        R.log.info("the newer run published first")
        return await real_start_fix(ctx, sc, draft)
    monkeypatch.setattr(R, "start_fix", newer_publishes_first)
    monkeypatch.setattr(R, "maybe_identify", lambda *a, **k: asyncio.sleep(0))
    ctx = R.Context(state=state, blobs=blobs, worker="w-old")
    ctx._slots.update(LLM=AnthropicTextModel())
    ctx._locator, ctx._locator_loaded = None, True

    got = asyncio.run(R.run_scan(ctx, scan["scan_id"]))
    asyncio.run(ctx.settle())
    assert got["created_at"] == newer["created_at"] and got["capture"]["stills"] == 5
    row = run(state.get("report", newer["report_id"]))
    assert row.rev == 1 and row.data["created_at"] == newer["created_at"]
    runrow = run(state.get("pipeline-run", R.run_id_for(scan["scan_id"]))).data
    assert runrow["status"] != "failed" and not runrow.get("error")
    assert runrow["stages"]["report"]["status"] == "running"   # this run wrote nothing after the refused publish
