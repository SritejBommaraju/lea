"""B10: the pipeline's state transitions and rules, the worker on the job queue, a stage failure as an error state,
the report and pipeline routes, and the locate seam. Offline: rows are the public contract examples; the only
engine used is the real Jev client pointed at a closed port (an outage, the way test_jev.py tests it). The full run
with live Jev, live Claude and a real photo is in tests/integration (-m live)."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from repairos.contracts import validate
from repairos.pipeline import locate_iface as li
from repairos.pipeline.run import Context, regions_for, run_id_for
from repairos.pipeline.worker import Worker
from repairos.report import build_report
from repairos.slots.blob_local import LocalBlobStore
from repairos.slots.fusion_jev import JevEngine
from repairos.slots.state import AlreadyClaimed, InvariantViolation, Job, RowEvent
from repairos.slots.state_sqlite import SqliteState

EXAMPLES = Path(__file__).resolve().parents[3] / "spec" / "contracts" / "examples"
TEST_SUMMARY = {"text": "Test text, not written by a model.", "written_by": "unit test", "from_words_only": True}


def ex(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("REPAIROS_DATA_DIR", str(tmp_path))
    state, blobs = SqliteState(tmp_path / "state.sqlite3"), LocalBlobStore(tmp_path / "blobs")
    scan = ex("scan-package")

    async def seed():
        await state.transition("session", scan["session_id"], "session.create", {"session_id": scan["session_id"]})
        await state.transition("scan-package", scan["scan_id"], "scan.create", scan)
        label = {**ex("still"), "still_id": "s_label", "kind": "label", "targets": []}
        skipped = {**ex("still"), "still_id": "s_skip", "user_skipped": True}
        for st in (label, skipped):
            await state.transition("still", st["still_id"], "still.create", st)
    run(seed())
    return state, blobs, scan


def upd(state, scan, patch):
    return run(state.transition("pipeline-run", run_id_for(scan["scan_id"]), "pipeline.update",
                                {"scan_id": scan["scan_id"], "session_id": scan["session_id"], **patch}))


# ------------------------------------------------------------------------------------------- state rules
def test_run_row_is_a_merge_patch_and_claims_are_exclusive(env):
    state, _, scan = env
    upd(state, scan, {"stages": {"capture": {"status": "done"}}})
    rid = run_id_for(scan["scan_id"])
    claim = {"still_id": "s_label", "worker": "w1", "scan_id": scan["scan_id"], "session_id": scan["session_id"]}
    run(state.transition("pipeline-run", rid, "pipeline.claim_still", claim))
    with pytest.raises(AlreadyClaimed) as e:
        run(state.transition("pipeline-run", rid, "pipeline.claim_still", {**claim, "worker": "w2"}))
    assert e.value.status == "running" and e.value.worker == "w1"
    row = upd(state, scan, {"stages": {"inspect": {"status": "running"}}})
    assert row.data["stages"] == {"capture": {"status": "done"}, "inspect": {"status": "running"}}
    assert row.data["stills"]["s_label"]["status"] == "running" and row.status == "inspecting"


def test_a_failed_run_names_its_stage_and_error(env):
    state, _, scan = env
    with pytest.raises(InvariantViolation, match="I4"):
        upd(state, scan, {"status": "failed"})
    with pytest.raises(InvariantViolation, match="I4"):
        upd(state, scan, {"status": "failed", "error": {"stage": "fuse", "message": " "}})
    assert upd(state, scan, {"status": "failed", "error": {"stage": "fuse", "message": "x"}}).status == "failed"


def test_a_done_still_points_at_stored_findings(env):
    state, _, scan = env
    with pytest.raises(InvariantViolation, match="I3"):
        upd(state, scan, {"stills": {"s_label": {"status": "done", "finding_ids": []}}})
    with pytest.raises(InvariantViolation, match="REF"):
        upd(state, scan, {"stills": {"s_label": {"status": "done", "finding_ids": ["f_nowhere"]}}})


def test_done_needs_the_report(env):
    state, _, scan = env
    with pytest.raises(InvariantViolation, match="REF"):
        upd(state, scan, {"status": "done", "report_id": "rep_missing"})


def _stored(state, scan, *findings):
    for f in findings:
        run(state.transition("finding", f["finding_id"], "finding.create", f))


def _not_inspected(scan):
    from repairos.pipeline.run import not_inspected_finding
    return not_inspected_finding(scan["scan_id"], "s_label", "label", "no OCR yet", "repairos.ocr")


def _report_for(scan, findings, regions):
    rep = build_report(scan=scan, stills=[], findings=findings, suspects=[], next_action=None, regions=regions,
                       locations={f["finding_id"]: li.not_computed(li.NOT_INSTALLED) for f in findings},
                       models={"detector": "d", "classifier": "c", "vlm": "v", "summary_writer": "s", "jev": "j",
                               "locate": "l"})
    return {**rep, "summary": TEST_SUMMARY}


def test_report_rules_check_buckets_against_stored_findings(env):
    state, _, scan = env
    ni = _not_inspected(scan)
    _stored(state, scan, ni)
    rep = _report_for(scan, [ni], {ni["finding_id"]: {"part_class": "label", "inspected_by": []}})
    run(state.transition("report", rep["report_id"], "report.publish", rep))
    # Schema-valid, but the stored finding was never looked at: it cannot be listed as judged ok (I3).
    entry = {"finding_id": ni["finding_id"], "subject": ni["subject"], "part_class": "label", "issue": "none",
             "confidence_word": "low", "evidence": ["x"], "inspected_by": ["vlm"],
             "crop": {"still_id": "s_label", "box_px": [0, 0, 1, 1]}, "location": li.not_computed("x"),
             "proof": {"status": "not_measured"}}
    bad = {**rep, "findings": [entry], "not_inspected": [], "verdict": "looks_good",
           "verdict_reason": {"code": "all_inspected_ok", "rule": "V4", "text": "t"}}
    validate("report", bad)
    with pytest.raises(InvariantViolation, match="I3"):
        run(state.transition("report", rep["report_id"], "report.publish", bad))
    assert run(state.get("report", rep["report_id"])).rev == 1


def test_publishing_again_replaces_the_report(env):
    state, _, scan = env
    ni = _not_inspected(scan)
    _stored(state, scan, ni)
    rep = _report_for(scan, [ni], {ni["finding_id"]: {"part_class": "label", "inspected_by": []}})
    run(state.transition("report", rep["report_id"], "report.publish", rep))
    run(state.transition("report", rep["report_id"], "report.publish", {**rep, "created_at": "2026-10-25T00:00:00Z"}))
    assert run(state.get("report", rep["report_id"])).rev == 2


# --------------------------------------------------------------------------- worker, stages, error state
def _queue(state, scan):
    async def go():
        for kind, inputs in (("detect", {"kind": "scan-package", "id": scan["scan_id"]}),
                             ("ocr", {"kind": "still", "id": "s_label"}),
                             ("detect", {"kind": "still", "id": "s_skip"}),
                             ("pipeline", {"kind": "scan-package", "scan_id": scan["scan_id"]})):
            await state.enqueue(Job(kind=kind, job_id=f"job_{kind}_{inputs.get('id', 'run')}",
                                    session_id=scan["session_id"], scan_id=scan["scan_id"], inputs=inputs))
    run(go())


def _jev_down() -> JevEngine:
    return JevEngine(key="unused", url="http://127.0.0.1:9/v1/systemone", record_dir=None, timeout_s=1.0)


def test_worker_runs_stages_and_a_jev_outage_is_a_clear_error_state(env):
    state, blobs, scan = env
    _queue(state, scan)
    w = Worker(state, blobs, name="w-test")
    w.ctx._slots["FUSION"] = _jev_down()
    jobs = run(w.drain())
    by_id = {j.job_id: j for j in jobs}
    assert by_id["job_detect_" + scan["scan_id"]].status == "done"
    assert by_id["job_ocr_s_label"].status == "done" and by_id["job_detect_s_skip"].status == "done"
    pipe = run(state.get_job("job_pipeline_run"))
    assert pipe.status == "failed" and pipe.attempts == 3 and "FusionUnavailable" in pipe.error   # retried, then failed

    runrow = run(state.get("pipeline-run", run_id_for(scan["scan_id"]))).data
    assert runrow["status"] == "failed" and runrow["error"]["stage"] == "fuse"
    assert "FusionUnavailable" in runrow["error"]["message"] and runrow["stages"]["fuse"]["status"] == "failed"
    assert runrow["stages"]["inspect"]["status"] == "done" and runrow["attempt"] == 3
    assert runrow["stages"]["capture"]["depth_source"] == "lidar"
    for sid, part in (("s_label", "label"), ("s_skip", "whole photo")):
        st = runrow["stills"][sid]
        assert st["status"] == "done" and st["regions"][st["finding_ids"][0]]["part_class"] == part
    findings = [r.data for r in run(state.list("finding", scan_id=scan["scan_id"]))]
    assert sorted(f["state"] for f in findings) == ["not_inspected", "not_inspected"]   # never ok (I3)
    assert {f["source"] for f in findings} == {"repairos.ocr", "repairos.vision"}
    assert run(state.list("report", scan_id=scan["scan_id"])) == []                      # no report made up
    assert run(state.list("suspect", scan_id=scan["scan_id"])) == []


def test_a_still_is_inspected_once(env):
    state, blobs, scan = env
    from repairos.pipeline.run import inspect_still_stage

    ctx = Context(state=state, blobs=blobs, worker="w1")
    first = run(inspect_still_stage(ctx, "s_label"))
    again = run(inspect_still_stage(Context(state=state, blobs=blobs, worker="w2"), "s_label"))
    assert first["finding_ids"] == again["finding_ids"]
    assert len(run(state.list("finding", scan_id=scan["scan_id"]))) == 1


def test_selected_ocr_without_a_stage_is_an_error_not_a_skip(env, monkeypatch):
    state, blobs, scan = env
    from repairos.pipeline.run import inspect_still_stage
    from repairos.registry import Registry

    monkeypatch.setenv("REPAIROS_SLOT_OCRB", "something")
    ctx = Context(state=state, blobs=blobs, registry=Registry())
    with pytest.raises(Exception, match="no OCR stage"):
        run(inspect_still_stage(ctx, "s_label"))
    st = run(state.get("pipeline-run", run_id_for(scan["scan_id"]))).data["stills"]["s_label"]
    assert st["status"] == "failed" and "OCR" in st["error"]


def test_regions_map_detections_to_generic_classes():
    from repairos.slots.detector import Detection
    from repairos.vision import inspect as insp

    det = Detection(box=(1, 1, 5, 5), label="ribbon cable", score=0.3)
    f = insp.finding_not_inspected(scan_id="scan_x", still_id="s2", index=0, det=det, reason="budget")
    n = insp.finding_nothing_detected(scan_id="scan_x", still_id="s2")
    out = regions_for([f, n], [{"index": 0, "label": "ribbon cable", "box_px": [1, 1, 5, 5]}], "whole photo")
    assert out[f["finding_id"]] == {"part_class": "ribbon cable", "inspected_by": ["detector"]}
    assert out[n["finding_id"]] == {"part_class": "whole photo", "inspected_by": ["detector"]}
    with pytest.raises(Exception, match="did not return"):
        regions_for([f], [], "whole photo")


@pytest.mark.parametrize("state", ["ok", "burnt", "cannot_tell"])
def test_judged_regions_use_the_vlm_part_name(state):
    """Tied to the sentence vision.inspect.finding_from_crop writes: a format change there fails here."""
    from repairos.slots.detector import Detection
    from repairos.slots.vlm import CropState
    from repairos.vision import inspect as insp
    from repairos.vision.crops import Crop

    det = Detection(box=(10, 10, 50, 50), label="melted plastic", score=0.4)
    crop = Crop(image=None, region_px=(0, 0, 256, 256), scale=1.0, box_px=det.box, still_id="s3")
    cs = CropState(part_class="toroidal inductor", state=state, evidence_text="e", confidence_word="low",
                   model="m", latency_ms=1)
    f = insp.finding_from_crop(scan_id="scan_x", still_id="s3", index=0, det=det, crop=crop, crop_state=cs)
    out = regions_for([f], [{"index": 0, "label": "melted plastic", "box_px": [10, 10, 50, 50]}], "whole photo")
    assert out[f["finding_id"]] == {"part_class": "toroidal inductor", "inspected_by": ["detector", "vlm"]}


# ------------------------------------------------------------------------------------------ gateway routes
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api_gateway import CODE, serve  # noqa: E402


def test_routes_complete_pipeline_report_and_export(env):
    from repairos.api.app import _named_event, create_app

    state, blobs, scan = env
    sid = scan["scan_id"]
    with serve(create_app(state, blobs, pairing_code=CODE)) as c:
        status, out, _ = c.req("POST", "/v1/devices/pair", {"code": CODE}, auth=False)
        c.token = out["token"]
        status, out, _ = c.req("GET", f"/v1/scans/{sid}/report")
        assert status == 404 and out["detail"]["pipeline"]["status"] == "not_started"
        status, out, _ = c.req("POST", f"/v1/scans/{sid}/complete")
        assert status == 202 and out["stills"] == 2
        assert c.req("POST", f"/v1/scans/{sid}/complete")[1]["job_id"] == out["job_id"]   # idempotent
        job = run(state.get_job(out["job_id"]))
        assert job.kind == "pipeline" and job.scan_id == sid

        w = Worker(state, blobs, name="w-test")
        w.ctx._slots["FUSION"] = _jev_down()
        run(w.drain())
        status, out, _ = c.req("GET", f"/v1/scans/{sid}/report")
        assert status == 409 and out["detail"]["error"] == "pipeline_failed"
        assert out["detail"]["pipeline"]["error"]["stage"] == "fuse"
        status, out, _ = c.req("GET", f"/v1/scans/{sid}/pipeline")
        assert status == 200 and out["status"] == "failed" and out["stills"]["s_label"]["status"] == "done"

        # The route serves whatever report the pipeline publishes; publish one built from the stored findings.
        findings = [r.data for r in run(state.list("finding", scan_id=sid))]
        runrow = run(state.get("pipeline-run", run_id_for(sid))).data
        regions = {k: v for st in runrow["stills"].values() for k, v in st["regions"].items()}
        rep = _report_for(scan, findings, regions)
        run(state.transition("report", rep["report_id"], "report.publish", rep))
        upd(state, scan, {"status": "done", "report_id": rep["report_id"], "error": None})
        status, out, _ = c.req("GET", f"/v1/scans/{sid}/report")
        assert status == 200 and out == rep and out["verdict"] == "insufficient_evidence"
        status, page, headers = c.req("GET", f"/v1/scans/{sid}/report/export?format=html")
        assert status == 200 and headers["Content-Type"].startswith("text/html") and b"Not inspected" in page
        status, md, _ = c.req("GET", f"/v1/scans/{sid}/report/export?format=md")
        assert status == 200 and md.startswith(b"# Inspection report")

    events = run(state.events_since(scan["session_id"]))
    named = [_named_event(e) for e in events]
    assert ("report_ready", {"scan_id": sid, "report_id": rep["report_id"], "verdict": rep["verdict"]}) in named
    assert any(n and n[0] == "pipeline_failed" and n[1]["error"]["stage"] == "fuse" for n in named)
    assert _named_event(RowEvent(1, "s", "finding", "f", "insert", 1, {}, "t")) is None


# ------------------------------------------------------------------------------------------------- locate
def test_locate_module_absent_means_none():
    assert li.load_locator("repairos.locate_not_a_module") is None


def test_default_locator_absent_until_the_locate_task_lands():
    try:
        import repairos.locate  # noqa: F401
    except ModuleNotFoundError:
        assert li.load_locator() is None
    else:
        pytest.skip("repairos.locate is installed; its own tests cover it")


def test_locations_are_checked_never_made_up():
    loc = li.as_report_location(li.Location(point_m=(0.1, 0.2, -0.3), method="ray to mesh"), "repairos.locate 1")
    assert loc == {"status": "computed", "frame": "arkit_world", "point_m": [0.1, 0.2, -0.3],
                   "method": "repairos.locate 1: ray to mesh"}
    validate("report", {**ex("report"), "findings": [{**ex("report")["findings"][0], "location": loc}]})
    assert li.as_report_location({"point_m": [0, 0, 0], "extent_m": [0.01, 0.02, 0]}, "v")["extent_m"] == [0.01,
                                                                                                     0.02, 0]
    for bad in ({"point_m": [0, 0]}, {"point_m": [0, float("nan"), 0]}, {"point_m": None},
                {"point_m": [0, 0, 0], "frame": "design"}, {"point_m": [0, 0, 0], "extent_m": [-1, 0, 0]}):
        with pytest.raises(li.LocatorError):
            li.as_report_location(bad, "v")


def test_pose_reliability():
    still = ex("still")
    assert li.pose_reliable(still)
    assert not li.pose_reliable({**still, "kind": "macro_detail"})
    assert not li.pose_reliable({**still, "pose_reliable": False})
    assert not li.pose_reliable({**still, "tracking_state": "limited"})
