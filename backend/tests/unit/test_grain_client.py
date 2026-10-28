"""The design engine client and what the gateway does with it (repairos.grain).

No network and no invented service: the transport below only RECORDS each request and returns the hand-written answer
for its URL. The answers are the documented API shape written by hand, not recorded responses. What these tests prove
is the request side (URL, headers, body), the pure functions (zip of a session folder, the readings sent, the message
composed for a fix, the chat answer read) and the gateway's wiring: complete -> upload -> judgement -> report ->
reading -> new report -> fix.
"""
from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from pathlib import Path

import httpx2
import pytest

from repairos.api.app import create_app
from repairos.grain import service
from repairos.grain.bundle import session_files, zip_session
from repairos.grain.client import GrainClient, GrainError
from repairos.sessions.validate import validate_session
from repairos.slots.blob_local import LocalBlobStore
from repairos.slots.state_sqlite import SqliteState

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api_gateway import CODE, Client, ex, make_scan, make_still, serve  # noqa: E402
from test_report_from_grain import CALL, PLAN  # noqa: E402

BASE = "http://design.test"


class Recorder(httpx2.BaseTransport):
    """Records every request; answers from `answers` by (method, path), 404 when there is none."""

    def __init__(self, answers: dict):
        self.answers, self.requests = answers, []

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        got = self.answers.get((request.method, request.url.path))
        if got is None:
            return httpx2.Response(404, json={"detail": "not found"})
        status, body = got if isinstance(got, tuple) else (200, got)
        return httpx2.Response(status, json=body)

    def of(self, method: str, path: str) -> list[httpx2.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]


SCAN_RECORD = {"id": "scan_g1", "points": 4, "bbox_mm": [10, 10, 10], "photos": [], "readings": [],
               "lea": {"session_id": "sess_x", "scan_id": "scan_x", "frame": "arkit_world",
                       "recommended_align": "icp", "transform_arkit_m_to_design_mm": [1000.0] + [0.0] * 14 + [1.0]}}
CHAT = {"text": "I moved the cover seat 1 mm and rebuilt.", "finished": True, "waiting": False, "question": None,
        "last_build": {"ok": True, "errors": [], "warnings": [], "seconds": 4.2}, "usage": None, "model": "m", "errors": []}


def answers() -> dict:
    return {("POST", "/projects/demo_product/scans"): SCAN_RECORD, ("POST", "/call"): CALL,
            ("GET", "/projects/demo_product/electrical/plan"): PLAN,
            ("POST", "/projects/demo_product/chat"): CHAT}


# ---------------------------------------------------------------------------------------------- request shapes
def test_disabled_without_a_url_and_nothing_is_called():
    c = GrainClient(None, "k")
    assert not c.enabled
    with pytest.raises(GrainError, match="no design engine configured"):
        c.call("p", "s", question=None, scan_id="scan_x")


def test_the_scan_upload_is_a_zip_post_with_the_name_units_and_bearer_key():
    t = Recorder(answers())
    c = GrainClient(BASE + "/", "grain_secret", transport=t)
    out = c.post_scan("demo_product", "scan_x.zip", b"PK-bytes", scan_id="scan_x")
    (req,) = t.requests
    assert out == SCAN_RECORD
    assert req.method == "POST" and req.url.path == "/projects/demo_product/scans"
    assert dict(req.url.params) == {"name": "scan_x.zip", "units": "m"}
    assert req.headers["authorization"] == "Bearer grain_secret" and req.headers["content-type"] == "application/zip"
    assert req.content == b"PK-bytes"


def test_the_project_is_quoted_in_the_path():
    t = Recorder({})
    GrainClient(BASE, "k", transport=t).plan("a b/c", None, scan_id="s")
    assert t.requests[0].url.raw_path.startswith(b"/projects/a%20b%2Fc/electrical/plan")


def test_call_body_has_the_project_scan_symptom_and_judge_and_readings_only_when_sent():
    t = Recorder(answers())
    c = GrainClient(BASE, "k", transport=t)
    c.call("demo_product", "scan_g1", question="no output", scan_id="scan_x")
    c.call("demo_product", "scan_g1", question=None, readings=[{"net": "VIN", "volts": 4.98}], scan_id="scan_x")
    first, second = (json.loads(r.content) for r in t.of("POST", "/call"))
    assert first == {"project": "demo_product", "scan": {"id": "scan_g1"}, "question": "no output", "judge": True}
    assert second["readings"] == [{"net": "VIN", "volts": 4.98}] and "question" not in second


def test_plan_404_leaves_the_plan_empty_and_other_errors_are_raised():
    t = Recorder({})
    c = GrainClient(BASE, "k", transport=t)
    assert c.plan("demo_product", ["wheel", "cover"], scan_id="s") == {}
    assert dict(t.requests[0].url.params) == {"suspects": "wheel,cover"}
    with pytest.raises(GrainError) as e:
        GrainClient(BASE, "k", transport=Recorder({("GET", "/projects/p/electrical/plan"): (500, {"detail": "boom"})})
                    ).plan("p", None, scan_id="s")
    assert e.value.status == 500 and e.value.detail == "boom"
    assert c.bom_row("demo_product", "R9", scan_id="s") is None


def test_chat_waits_and_queues():
    t = Recorder(answers())
    GrainClient(BASE, "k", transport=t).chat("demo_product", "hello", scan_id="s")
    assert json.loads(t.requests[0].content) == {"message": "hello", "wait": True, "queue": True}


def test_every_answer_is_saved_as_it_came(tmp_path):
    c = GrainClient(BASE, "k", transport=Recorder(answers()))
    c.post_scan("demo_product", "scan_x.zip", b"z", scan_id="scan_x", record_to=tmp_path / "grain")
    c.call("demo_product", "scan_g1", question=None, scan_id="scan_x", record_to=tmp_path / "grain")
    names = sorted(p.name for p in (tmp_path / "grain").iterdir())
    assert [n.split("-")[0] for n in names] == ["call", "scan"] and all("-scan_x-" in n for n in names)
    assert service.latest(tmp_path / "grain", "call", "scan_x") == CALL
    assert service.latest(tmp_path / "grain", "call", "scan_other") is None


def test_the_key_never_appears_in_an_error():
    t = Recorder({("POST", "/call"): (401, {"detail": "bad key"})})
    with pytest.raises(GrainError) as e:
        GrainClient(BASE, "grain_secret", transport=t).call("p", "s", question=None, scan_id="s")
    assert "grain_secret" not in str(e.value) and e.value.status == 401


# -------------------------------------------------------------------------------------------- session folder -> zip
def make_session_folder(root: Path) -> Path:
    s = root / "sess_x"
    for rel, data in {
        "session.json": b'{"session_id": "sess_x"}',
        "scans/scan_x/scan_package.json": b"{}", "scans/scan_x/points_arkit_world.ply": b"ply\n",
        "scans/scan_x/kf_000.jpg": b"jpeg", "scans/scan_y/scan_package.json": b"{}",
        "stills/s1.json": b'{"scan_id": "scan_x"}', "stills/s1.jpg": b"j", "stills/s1_depth.png": b"d",
        "stills/s10.json": b'{"scan_id": "scan_y"}', "stills/s10.jpg": b"j",
        "readings/rd_1.json": b'{"scan_id": "scan_x"}', "readings/rd_2.json": b'{"scan_id": "scan_y"}',
        "grain/call-scan_x-1.json": b"{}",
    }.items():
        (s / rel).parent.mkdir(parents=True, exist_ok=True)
        (s / rel).write_bytes(data)
    return s


def test_zip_holds_one_scan_its_stills_and_readings_but_not_the_engines_own_answers(tmp_path):
    s = make_session_folder(tmp_path)
    data = zip_session(s, "scan_x")
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert names == ["session.json", "scans/scan_x/kf_000.jpg", "scans/scan_x/points_arkit_world.ply",
                     "scans/scan_x/scan_package.json", "stills/s1.jpg", "stills/s1.json", "stills/s1_depth.png",
                     "readings/rd_1.json"]
    assert zip_session(s, "scan_x") == data   # the same folder always zips to the same bytes
    assert [p.as_posix() for p in session_files(s, "scan_x")] == names
    with pytest.raises(ValueError, match="no scan scan_z"):
        zip_session(s, "scan_z")


# ------------------------------------------------------------------------------------------ pure message helpers
def rd(rid, net, kind, value, **kw):
    return {"reading_id": rid, "scan_id": "scan_x", "session_id": "sess_x", "kind": kind, "net": net, "value": value,
            "power_state": kw.pop("power", "on"), "source": "typed", "recorded_at": "2026-10-28T12:00:00Z", **kw}


def test_readings_are_sent_in_the_shapes_the_design_engine_takes():
    meter = {"model": "Example Meter", "mode": "dc_voltage"}
    sent = service.readings_payload([
        rd("a", "VIN", "voltage_dc", {"volts": 4.98}, meter=meter, action_id="chk_vin"),
        rd("b", "GND", "continuity", {"continuity": False}, pins=["J1.2", "U1.4"], power="off"),
        rd("c", "R", "resistance", {"ohms": 0.3}, pins=["A.1", "B.1"], power="off")])
    assert sent == [
        {"net": "VIN", "id": "a", "power_state": "on", "volts": 4.98, "meter": meter, "check": "chk_vin"},
        {"net": "GND", "id": "b", "power_state": "off", "pins": ["J1.2", "U1.4"], "continuity": False},
        {"net": "R", "id": "c", "power_state": "off", "pins": ["A.1", "B.1"], "ohms": 0.3}]


def test_fix_message_carries_the_context_then_the_customers_words_last():
    call = {**CALL, "electrical": {"readings": [{"net": "VIN", "verdict": "off", "findings": []}], "findings": []}}
    msg = service.fix_message(
        project="demo_product", session={"symptom": "no output"}, scan={"scan_id": "scan_x"}, call=call,
        readings=[rd("a", "VIN", "voltage_dc", {"volts": 3.1}, pins=["J1.1", "J1.2"])],
        words="make the cover seat deeper so it stops popping off")
    assert "Design (project): demo_product" in msg and "Symptom the customer reported: no output" in msg
    assert "- rear_wheel: not_seen, coverage 0.00 (no scan points on the wheel's surface)" in msg
    assert "- cover: shifted, coverage 0.90" in msg and "front_wheel" not in msg and "board" not in msg
    assert "VIN between J1.1 and J1.2 read 3.1 V with the product on: off" in msg
    assert "- buy rear_wheel: not in the scan where the design puts it" in msg
    assert msg.rstrip().endswith("make the cover seat deeper so it stops popping off")
    assert msg.index("Fixes suggested so far") < msg.index("The customer says:")


def test_fix_message_without_a_judgement_still_says_what_it_knows():
    msg = service.fix_message(project="p", session={}, scan={"scan_id": "s"}, call=None, readings=[], words="help")
    assert "reported no symptom" in msg and msg.endswith("help")


def test_chat_answer_is_read_for_its_words_build_and_state():
    assert service.chat_result(CHAT) == {"reply": "I moved the cover seat 1 mm and rebuilt.", "finished": True,
                                          "waiting": False, "errors": [], "build": "ok"}
    failed = service.chat_result({**CHAT, "last_build": {"ok": False, "errors": ["x"]}, "errors": ["e"]})
    assert failed["build"] == "failed" and failed["errors"] == ["e"]
    asking = service.chat_result({"text": "", "finished": False, "waiting": True, "last_build": None,
                                  "question": {"why": "Which cover do you mean?", "questions": []}, "errors": []})
    assert asking == {"reply": "Which cover do you mean?", "finished": False, "waiting": True, "errors": [],
                      "build": "none"}


# ------------------------------------------------------------------------------------------------- the gateway
def gateway(tmp_path, transport, *, project=None):
    state, blobs = SqliteState(tmp_path / "state.sqlite3"), LocalBlobStore(tmp_path / "blobs")
    client = GrainClient(BASE, "grain_secret", project, transport=transport)
    return create_app(state, blobs, pairing_code=CODE, recordings=tmp_path / "sessions", grain=client)


def paired(c: Client) -> Client:
    status, out, _ = c.req("POST", "/v1/devices/pair", {"code": CODE, "name": "test phone"})
    assert status == 201
    c.token = out["token"]
    return c


def wait_for(fn, what, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = fn()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def report_rev(c: Client, scan_id: str, at_least: int):
    def get():
        status, body, h = c.req("GET", f"/v1/scans/{scan_id}/report")
        return (body, int(h["X-Report-Rev"])) if status == 200 and int(h["X-Report-Rev"]) >= at_least else None
    return wait_for(get, f"report revision {at_least}")


def test_complete_uploads_the_session_judges_it_and_publishes_the_report(tmp_path):
    t = Recorder(answers())
    with serve(gateway(tmp_path, t)) as c:
        paired(c)
        status, session, _ = c.req("POST", "/v1/sessions", {"symptom": "no output", "product": "demo_product"})
        assert status == 201 and session["product"] == "demo_product"
        pkg = make_scan(c, session["session_id"])
        assert c.req("POST", "/v1/scans", pkg)[0] == 201
        status, out, _ = c.req("POST", f"/v1/scans/{pkg['scan_id']}/complete")
        assert status == 202 and out["job_id"].startswith("grain_")
        report, rev = report_rev(c, pkg["scan_id"], 1)
        assert report["design"] == {"engine": "grain", "project": "demo_product"} and report["verdict"] == "potential_issues"
        assert report["next_step"]["action_id"] == "chk_vin"
        # the report is visible a moment before the run is marked done
        run = wait_for(lambda: (lambda r: r if r["status"] == "done" else None)(
            c.req("GET", f"/v1/scans/{pkg['scan_id']}/pipeline")[1]), "the run to be done")
        assert run["report_id"] == report["report_id"]

        (up,) = t.of("POST", "/projects/demo_product/scans")
        assert up.headers["authorization"] == "Bearer grain_secret" and dict(up.url.params)["name"] == f"{pkg['scan_id']}.zip"
        names = zipfile.ZipFile(io.BytesIO(up.content)).namelist()
        assert "session.json" in names and f"scans/{pkg['scan_id']}/scan_package.json" in names
        assert json.loads(t.of("POST", "/call")[0].content) == {
            "project": "demo_product", "scan": {"id": "scan_g1"}, "question": "no output", "judge": True}
        assert dict(t.of("GET", "/projects/demo_product/electrical/plan")[0].url.params)["suspects"].startswith("rear_wheel")
        saved = sorted(p.name.split("-")[0] for p in (tmp_path / "sessions" / session["session_id"] / "grain").iterdir())
        assert {"scan", "call", "plan"} <= set(saved)
        # the session folder, with its product and the grain/ answers, is still a valid replayable session
        assert validate_session(tmp_path / "sessions" / session["session_id"]) == []

        # a reading is forwarded through /call with the scan's readings, and the next check moves on
        reading = {**ex("meter-reading"), "session_id": session["session_id"], "scan_id": pkg["scan_id"],
                   "reading_id": "rd_vin", "kind": "voltage_dc", "net": "VIN", "value": {"volts": 4.98},
                   "power_state": "on", "action_id": "chk_vin", "source": "typed"}
        reading.pop("pins", None)
        t.answers[("POST", "/call")] = {**CALL, "electrical": {"readings": [{"net": "VIN", "verdict": "ok", "findings": []}],
                                                               "findings": []}}
        assert c.req("POST", "/v1/readings", reading)[0] == 201
        report2, rev2 = report_rev(c, pkg["scan_id"], rev + 1)
        sent = json.loads(t.of("POST", "/call")[-1].content)
        assert sent["scan"] == {"id": "scan_g1"} and sent["readings"] == [
            {"net": "VIN", "id": "rd_vin", "power_state": "on", "volts": 4.98, "check": "chk_vin"}]
        assert report2["next_step"]["action_id"] == "chk_gnd"
        vin = next(f for f in report2["findings"] if f["subject"] == {"kind": "net", "ref": "VIN"})
        assert vin["proof"] == {"status": "ruled_out_by_meter", "reading_id": "rd_vin"}
        ledger = sorted((tmp_path / "sessions" / session["session_id"] / "grain").glob("reading-rd_vin-*.json"))
        assert json.loads(ledger[-1].read_text(encoding="utf-8"))["grain_verdict"] == "ok"

        # fix it from the phone
        status, fixed, _ = c.req("POST", f"/v1/scans/{pkg['scan_id']}/fix", {"message": "make the cover seat deeper"})
        assert status == 200 and fixed == {"reply": CHAT["text"], "finished": True, "waiting": False, "errors": [],
                                            "build": "ok"}
        sent_chat = json.loads(t.of("POST", "/projects/demo_product/chat")[0].content)
        assert sent_chat["wait"] is True and sent_chat["queue"] is True
        assert "no output" in sent_chat["message"] and "VIN" in sent_chat["message"]
        assert sent_chat["message"].rstrip().endswith("make the cover seat deeper")
        assert c.req("POST", f"/v1/scans/{pkg['scan_id']}/fix", {"message": ""})[0] == 422


def test_a_reading_before_the_upload_waits_for_complete(tmp_path):
    t = Recorder(answers())
    with serve(gateway(tmp_path, t)) as c:
        paired(c)
        _, session, _ = c.req("POST", "/v1/sessions", {"product": "demo_product"})
        pkg = make_scan(c, session["session_id"])
        c.req("POST", "/v1/scans", pkg)
        reading = {**ex("meter-reading"), "session_id": session["session_id"], "scan_id": pkg["scan_id"]}
        assert c.req("POST", "/v1/readings", reading)[0] == 201
        time.sleep(0.3)
        assert t.requests == []   # nothing was uploaded yet, so nothing to forward to


def test_no_product_and_no_default_means_the_report_says_the_design_is_unknown(tmp_path):
    t = Recorder(answers())
    with serve(gateway(tmp_path, t)) as c:
        paired(c)
        _, session, _ = c.req("POST", "/v1/sessions", {"symptom": "dead"})
        pkg = make_scan(c, session["session_id"])
        c.req("POST", "/v1/scans", pkg)
        assert c.req("POST", f"/v1/scans/{pkg['scan_id']}/complete")[0] == 202
        report, _ = report_rev(c, pkg["scan_id"], 1)
        assert report["verdict"] == "insufficient_evidence" and "design" not in report
        assert t.requests == []   # nothing was called
        assert c.req("POST", f"/v1/scans/{pkg['scan_id']}/fix", {"message": "help"})[0] == 409


def test_the_default_project_is_used_when_a_session_names_none(tmp_path):
    t = Recorder(answers())
    with serve(gateway(tmp_path, t, project="demo_product")) as c:
        paired(c)
        _, session, _ = c.req("POST", "/v1/sessions", {})
        pkg = make_scan(c, session["session_id"])
        c.req("POST", "/v1/scans", pkg)
        c.req("POST", f"/v1/scans/{pkg['scan_id']}/complete")
        report, _ = report_rev(c, pkg["scan_id"], 1)
        assert report["design"]["project"] == "demo_product"


def test_a_design_engine_error_is_the_runs_error_and_never_a_report(tmp_path):
    t = Recorder({("POST", "/projects/demo_product/scans"): (400, {"detail": "this scan has no LiDAR points"})})
    with serve(gateway(tmp_path, t)) as c:
        paired(c)
        _, session, _ = c.req("POST", "/v1/sessions", {"product": "demo_product"})
        pkg = make_scan(c, session["session_id"])
        c.req("POST", "/v1/scans", pkg)
        c.req("POST", f"/v1/scans/{pkg['scan_id']}/complete")
        run = wait_for(lambda: (lambda r: r if r.get("status") == "failed" else None)(
            c.req("GET", f"/v1/scans/{pkg['scan_id']}/pipeline")[1]), "a failed run")
        assert "no LiDAR points" in run["error"]["message"] and run["error"]["stage"] == "design_engine"
        assert c.req("GET", f"/v1/scans/{pkg['scan_id']}/report")[0] == 409


def test_without_a_design_engine_complete_says_so_and_nothing_else_judges(tmp_path):
    state, blobs = SqliteState(tmp_path / "state.sqlite3"), LocalBlobStore(tmp_path / "blobs")
    app = create_app(state, blobs, pairing_code=CODE, recordings=tmp_path / "sessions", grain=GrainClient(None))
    with serve(app) as c:
        paired(c)
        _, session, _ = c.req("POST", "/v1/sessions", {})
        pkg = make_scan(c, session["session_id"])
        c.req("POST", "/v1/scans", pkg)
        status, out, _ = c.req("POST", f"/v1/scans/{pkg['scan_id']}/complete")
        assert status == 409 and "no design engine configured" in out["detail"]
        assert c.req("GET", f"/v1/scans/{pkg['scan_id']}/report")[0] == 404   # no run, no report
        assert c.req("POST", f"/v1/scans/{pkg['scan_id']}/fix", {"message": "help"})[0] == 409


def test_a_retried_complete_judges_once_and_changed_inputs_judge_again(tmp_path):
    t = Recorder(answers())
    with serve(gateway(tmp_path, t)) as c:
        paired(c)
        _, session, _ = c.req("POST", "/v1/sessions", {"product": "demo_product"})
        pkg = make_scan(c, session["session_id"])
        c.req("POST", "/v1/scans", pkg)
        path = f"/v1/scans/{pkg['scan_id']}/complete"
        assert c.req("POST", path)[0] == 202 and c.req("POST", path)[0] == 202   # a double tap, or a lost answer
        report_rev(c, pkg["scan_id"], 1)
        assert c.req("POST", path)[0] == 202   # and again after it finished: same inputs
        time.sleep(0.3)
        assert len(t.of("POST", "/projects/demo_product/scans")) == 1 and len(t.of("POST", "/call")) == 1
        # a reading is judged on its own; the judgement then covers it, so complete does not repeat it
        reading = {**ex("meter-reading"), "session_id": session["session_id"], "scan_id": pkg["scan_id"],
                   "reading_id": "rd_x", "kind": "voltage_dc", "net": "VIN", "value": {"volts": 4.9}, "power_state": "on"}
        reading.pop("pins", None)
        c.req("POST", "/v1/readings", reading)
        report_rev(c, pkg["scan_id"], 2)
        assert c.req("POST", path)[0] == 202
        time.sleep(0.3)
        assert len(t.of("POST", "/projects/demo_product/scans")) == 1 and len(t.of("POST", "/call")) == 2
        # a new still changes what the judgement is made from: complete judges again
        assert c.req("POST", "/v1/stills", make_still(c, pkg))[0] == 201
        assert c.req("POST", path)[0] == 202
        wait_for(lambda: len(t.of("POST", "/projects/demo_product/scans")) == 2, "a second upload")


def test_runs_the_gateway_lost_in_a_restart_are_failed_with_a_reason(tmp_path):
    from repairos.api.app import INTERRUPTED, fail_interrupted_runs

    t = Recorder(answers())
    app = gateway(tmp_path, t)
    state = app.state.store
    with serve(app) as c:
        paired(c)
        _, session, _ = c.req("POST", "/v1/sessions", {"product": "demo_product"})
        pkg = make_scan(c, session["session_id"])
        c.req("POST", "/v1/scans", pkg)
        import asyncio

        async def interrupted():
            await state.transition("pipeline-run", f"run_{pkg['scan_id']}", "pipeline.update", {
                "scan_id": pkg["scan_id"], "session_id": session["session_id"], "status": "running",
                "stages": {"design_engine": {"status": "running"}}})
        asyncio.run(interrupted())
        assert asyncio.run(fail_interrupted_runs(state)) == 1
        status, run, _ = c.req("GET", f"/v1/scans/{pkg['scan_id']}/pipeline")
        assert run["status"] == "failed" and run["error"] == {"message": INTERRUPTED, "stage": "design_engine"}
        assert asyncio.run(fail_interrupted_runs(state)) == 0   # only runs that were running
