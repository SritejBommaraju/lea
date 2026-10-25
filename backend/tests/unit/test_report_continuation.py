"""An abandoned "Continue scanning" (2026-10-24, seen on the phone): new stills after a finished report, then no
package re-post and no complete. The finished report must stay readable (GET /report 200 with its revision while a
newer one is pending), GET /pipeline must say a rebuild is pending, and after `continuation_timeout_s` the
continuation is closed: its stills keep their inspections, nothing stays `inspecting`. Offline: label and skipped
stills (no model needed); the report is built from the stored findings as test_pipeline.py does."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from repairos.pipeline.run import Context, close_abandoned, inspect_still_stage, run_id_for
from test_api_gateway import CODE, serve
from test_pipeline import _report_for, env, ex, run, upd  # noqa: F401 (fixture)


def _publish(state, scan):
    runrow = run(state.get("pipeline-run", run_id_for(scan["scan_id"]))).data
    regions = {k: v for st in runrow["stills"].values() for k, v in st["regions"].items()}
    findings = [r.data for r in run(state.list("finding", scan_id=scan["scan_id"])) if r.data["finding_id"] in regions]
    stills = [r.data for r in run(state.list("still", scan_id=scan["scan_id"]))]
    rep = _report_for(scan, findings, regions)
    rep["capture"]["stills"] = len(stills)
    row = run(state.transition("report", rep["report_id"], "report.publish", rep))
    upd(state, scan, {"status": "done", "report_id": rep["report_id"], "error": None})
    return row


def _new_still(state, scan, still_id):
    st = {**ex("still"), "still_id": still_id, "kind": "label", "targets": [], "segment": 1}
    run(state.transition("still", still_id, "still.create", st))


def test_an_abandoned_continuation_never_hides_the_finished_report(env, tmp_path):  # noqa: F811
    from repairos.api.app import create_app

    state, blobs, scan = env
    sid = scan["scan_id"]
    ctx = Context(state=state, blobs=blobs, root=tmp_path / "pipeline")
    with serve(create_app(state, blobs, pairing_code=CODE)) as c:
        c.token = c.req("POST", "/v1/devices/pair", {"code": CODE}, auth=False)[1]["token"]
        assert c.req("GET", f"/v1/scans/{sid}/report")[0] == 404          # never finished: the only 404
        for s in ("s_label", "s_skip"):
            run(inspect_still_stage(ctx, s))
        first = _publish(state, scan)
        status, rep, h = c.req("GET", f"/v1/scans/{sid}/report")
        assert status == 200 and h["x-report-rev"] == str(first.rev) and h["x-report-rebuilding"] == "0"
        view = c.req("GET", f"/v1/scans/{sid}/pipeline")[1]
        assert view["report_rev"] == first.rev and view["rebuilding"] is False and "pending" not in view

        # Continue scanning: one still posted and inspected, then the app goes away.
        _new_still(state, scan, "s_more")
        run(inspect_still_stage(ctx, "s_more", decide=True))
        _new_still(state, scan, "s_stuck")
        run(state.transition("pipeline-run", run_id_for(sid), "pipeline.claim_still",
                             {"still_id": "s_stuck", "worker": "a worker that stopped", "scan_id": sid,
                              "session_id": scan["session_id"]}))
        status, again, h = c.req("GET", f"/v1/scans/{sid}/report")
        assert status == 200 and again == rep and h["x-report-rev"] == str(first.rev)
        assert h["x-report-rebuilding"] == "1"
        view = c.req("GET", f"/v1/scans/{sid}/pipeline")[1]
        assert view["status"] == "inspecting" and view["report_rev"] == first.rev and view["rebuilding"] is True
        assert view["pending"]["new_stills"] == 2 and view["pending"]["closed"] is False

        # Not yet timed out: nothing is closed. After the timeout: closed, the finished report stands.
        assert run(close_abandoned(ctx)) == []
        later = datetime.now(UTC) + timedelta(seconds=ctx.config["continuation_timeout_s"] + 1)
        assert run(close_abandoned(ctx, now=later)) == [sid]
        view = c.req("GET", f"/v1/scans/{sid}/pipeline")[1]
        assert view["status"] == "closed" and view["rebuilding"] is False and view["report_rev"] == first.rev
        assert view["pending"] == {"new_stills": 2, "inspected": 1, "closed": True}
        assert view["stills"]["s_more"]["status"] == "done"                 # its inspection is kept
        assert view["stills"]["s_stuck"]["status"] == "failed"              # re-inspected on the next complete
        assert "stopped" in view["stills"]["s_stuck"]["error"] and view["continuation"]["report_rev"] == first.rev
        status, _, h = c.req("GET", f"/v1/scans/{sid}/report")
        assert status == 200 and h["x-report-rev"] == str(first.rev) and h["x-report-rebuilding"] == "0"

        # The next complete folds the stills in: the stuck one is claimed again, the kept one is not redone.
        assert c.req("POST", f"/v1/scans/{sid}/complete")[0] == 202
        stage = run(inspect_still_stage(ctx, "s_stuck"))
        assert stage["status"] == "done"
        kept = run(state.get("pipeline-run", run_id_for(sid))).data["stills"]["s_more"]["finished_at"]
        assert run(inspect_still_stage(ctx, "s_more"))["finished_at"] == kept
        second = _publish(state, scan)
        assert second.rev == first.rev + 1
        status, _, h = c.req("GET", f"/v1/scans/{sid}/report")
        assert status == 200 and h["x-report-rev"] == str(second.rev) and h["x-report-rebuilding"] == "0"
        assert c.req("GET", f"/v1/scans/{sid}/pipeline")[1]["status"] == "done"


def test_a_failed_rebuild_keeps_serving_the_finished_report(env, tmp_path):  # noqa: F811
    from repairos.api.app import create_app

    state, blobs, scan = env
    sid = scan["scan_id"]
    ctx = Context(state=state, blobs=blobs, root=tmp_path / "pipeline")
    for s in ("s_label", "s_skip"):
        run(inspect_still_stage(ctx, s))
    first = _publish(state, scan)
    upd(state, scan, {"status": "failed", "error": {"stage": "fuse", "message": "Jev unreachable"}})
    with serve(create_app(state, blobs, pairing_code=CODE)) as c:
        c.token = c.req("POST", "/v1/devices/pair", {"code": CODE}, auth=False)[1]["token"]
        status, _, h = c.req("GET", f"/v1/scans/{sid}/report")
        assert status == 200 and h["x-report-rev"] == str(first.rev)
        view = c.req("GET", f"/v1/scans/{sid}/pipeline")[1]
        assert view["status"] == "failed" and view["error"]["stage"] == "fuse" and view["rebuilding"] is False


def test_a_scan_without_any_report_is_not_closed(env, tmp_path):  # noqa: F811
    state, blobs, scan = env
    ctx = Context(state=state, blobs=blobs, root=tmp_path / "pipeline")
    run(inspect_still_stage(ctx, "s_label"))
    later = datetime.now(UTC) + timedelta(hours=1)
    assert run(close_abandoned(ctx, now=later)) == []
    assert run(state.get("pipeline-run", run_id_for(scan["scan_id"]))).data["status"] == "inspecting"


def test_stills_posted_after_a_finished_report_mean_a_rebuild_is_pending(env, tmp_path):  # noqa: F811
    """Greptile PR #7: a continuation's stills are posted and /complete queues the run, but until a worker picks
    either up the run row is still `done`. The phone polls in that window: it must be told a rebuild is pending,
    or it settles on the old revision and never asks again."""
    from repairos.api.app import create_app

    state, blobs, scan = env
    sid = scan["scan_id"]
    ctx = Context(state=state, blobs=blobs, root=tmp_path / "pipeline")
    for s in ("s_label", "s_skip"):
        run(inspect_still_stage(ctx, s))
    first = _publish(state, scan)
    with serve(create_app(state, blobs, pairing_code=CODE)) as c:
        c.token = c.req("POST", "/v1/devices/pair", {"code": CODE}, auth=False)[1]["token"]
        _new_still(state, scan, "s_more")                   # posted, nothing has run yet
        assert c.req("POST", f"/v1/scans/{sid}/complete")[0] == 202
        view = c.req("GET", f"/v1/scans/{sid}/pipeline")[1]
        assert view["status"] == "done" and view["report_rev"] == first.rev
        assert view["rebuilding"] is True
        assert view["pending"] == {"new_stills": 1, "inspected": 0, "closed": False}
        assert c.req("GET", f"/v1/scans/{sid}/report")[2]["x-report-rebuilding"] == "1"
