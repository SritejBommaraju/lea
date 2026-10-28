"""What the customer does after the report: the outcome tap, the design's picture, the part file, a typed reading.

Same rules as test_grain_client.py: the transport only RECORDS requests and returns hand-written answers in the
documented shape; nothing here is a recorded response from the real design engine. What is proved is the request side
(URL, body, key), the gateway's saving and retrying, and that the phone never sees the design engine's address or key.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx2
import pytest

from repairos.grain import service
from repairos.grain.client import GrainClient, GrainError, safe_name

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api_gateway import ex, make_scan, serve  # noqa: E402
from test_grain_client import BASE, CALL, Recorder, answers, gateway, paired, report_rev, wait_for  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n-design-view"
STL = b"solid hinge\nendsolid hinge\n"


class FilesRecorder(Recorder):
    """The JSON recorder plus byte answers: a value `(status, bytes, content type)` is returned as it is."""

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        got = self.answers.get((request.method, request.url.path))
        if isinstance(got, tuple) and len(got) == 3:
            self.requests.append(request)
            status, content, ctype = got
            return httpx2.Response(status, content=content, headers={"content-type": ctype})
        return super().handle_request(request)


def files_answers() -> dict:
    return {**answers(),
            ("GET", "/projects/demo_product/files/out/views/iso.png"): (200, PNG, "image/png"),
            ("GET", "/projects/demo_product/files/out/views/sheet.png"): (200, PNG + b"sheet", "image/png"),
            ("GET", "/projects/demo_product/bom/part/hinge/file"): (200, STL, "model/stl"),
            ("POST", "/projects/demo_product/scans/scan_g1/outcome"): {"ok": True}}


OUTCOME_PATH = "/projects/demo_product/scans/scan_g1/outcome"


def judged(c, t, *, tmp_path=None):
    """Pair, make a scan, complete it, wait for the report. Returns (scan id, session id, report)."""
    paired(c)
    _, session, _ = c.req("POST", "/v1/sessions", {"symptom": "no output", "product": "demo_product"})
    pkg = make_scan(c, session["session_id"])
    assert c.req("POST", "/v1/scans", pkg)[0] == 201
    assert c.req("POST", f"/v1/scans/{pkg['scan_id']}/complete")[0] == 202
    report, _rev = report_rev(c, pkg["scan_id"], 1)
    return pkg["scan_id"], session["session_id"], report


def row_of(report, part):
    return next(f for f in report["findings"] if f["subject"]["ref"] == part)


# ---------------------------------------------------------------------------------------------- request shapes
def test_outcome_body_names_the_part_the_state_the_person_and_the_note():
    t = FilesRecorder(files_answers())
    GrainClient(BASE, "grain_secret", transport=t).outcome(
        "demo_product", "scan_g1", part="rear_wheel", state="damaged", note="It spins now.", scan_id="scan_x")
    (req,) = t.of("POST", OUTCOME_PATH)
    assert json.loads(req.content) == {"part": "rear_wheel", "state": "damaged", "confirmed_by": "person",
                                       "note": "It spins now."}
    assert req.headers["authorization"] == "Bearer grain_secret"


def test_views_and_part_files_use_the_documented_paths_and_refuse_other_names():
    t = FilesRecorder(files_answers())
    c = GrainClient(BASE, "k", transport=t)
    assert c.design_view("demo_product", "iso") == (PNG, "image/png")
    assert c.part_file("demo_product", "hinge", "step")[0] == STL
    assert dict(t.of("GET", "/projects/demo_product/bom/part/hinge/file")[0].url.params) == {"format": "step"}
    for bad in ("..", ".", "a/b", "a\\b", ""):
        assert not safe_name(bad)
        with pytest.raises(GrainError):
            c.part_file("demo_product", bad, "stl")
    with pytest.raises(GrainError):
        c.part_file("demo_product", "hinge", "exe")
    with pytest.raises(GrainError):
        c.design_view("demo_product", "../../.env")
    assert len(t.requests) == 2   # the refused ones never left the gateway


# ---------------------------------------------------------------------------------------------- pure functions
def test_a_saved_answer_is_put_on_the_row_by_what_the_row_is_about():
    row = {"finding_id": "g_wheel_1", "subject": {"kind": "part", "ref": "rear_wheel"}, "issue": "missing"}
    entry = service.outcome_entry(row, "fixed", "  Works.  ")
    assert entry["note"] == "Works." and entry["confirmed_by"] == "person" and entry["sent"] is False
    rebuilt = {"findings": [{"finding_id": "g_wheel_2", "subject": {"kind": "part", "ref": "rear_wheel"}},
                            {"finding_id": "g_other", "subject": {"kind": "part", "ref": "cover"}}]}
    out = service.with_outcomes(rebuilt, {service.row_key(row): entry})
    assert out["findings"][0]["outcome"] == {"state": "fixed", "recorded_at": entry["recorded_at"],
                                             "confirmed_by": "person", "note": "Works."}
    assert "outcome" not in out["findings"][1] and "outcome" not in rebuilt["findings"][0]   # the stored one is untouched


def test_not_tried_is_never_sent_and_a_scan_never_uploaded_waits(tmp_path):
    client = GrainClient(BASE, "k", transport=FilesRecorder(files_answers()))
    scan = {"scan_id": "scan_x", "session_id": "sess_x"}
    session = {"product": "demo_product"}
    row = {"finding_id": "g_a", "subject": {"kind": "part", "ref": "a"}, "issue": "loose"}
    assert service.forward_outcome(client, root=tmp_path, session=session, scan=scan,
                                   entry=service.outcome_entry(row, "not_tried", None)) == "not_applicable"
    assert service.forward_outcome(client, root=tmp_path, session=session, scan=scan,
                                   entry=service.outcome_entry(row, "fixed", None)) == "waiting"
    assert client._transport.requests == []


def test_the_design_picture_is_cached_and_served_stale_when_the_design_engine_is_down(tmp_path):
    t = FilesRecorder(files_answers())
    client = GrainClient(BASE, "k", transport=t)
    assert service.design_view(client, root=tmp_path, project="demo_product", now=1000.0)[0] == PNG
    service.design_view(client, root=tmp_path, project="demo_product")
    assert len(t.requests) == 1
    t.answers.clear()   # now every request is a 404
    later = 10 ** 10
    assert service.design_view(client, root=tmp_path, project="demo_product", now=later)[0] == PNG
    with pytest.raises(GrainError):
        service.design_view(client, root=tmp_path, project="other", now=later)


# -------------------------------------------------------------------------------------------------- the gateway
def test_the_outcome_is_saved_sent_as_a_person_label_and_shown_on_the_row(tmp_path):
    t = FilesRecorder(files_answers())
    with serve(gateway(tmp_path, t)) as c:
        scan_id, session_id, report = judged(c, t)
        wheel = row_of(report, "rear_wheel")
        assert "outcome" not in wheel
        status, out, _ = c.req("POST", f"/v1/scans/{scan_id}/outcome",
                               {"row_id": wheel["finding_id"], "outcome": "fixed", "note": "Clipped on, spins."})
        assert status == 200 and out == {"row_id": wheel["finding_id"], "outcome": "fixed", "saved": True,
                                         "forwarded": "sent"}
        (req,) = t.of("POST", OUTCOME_PATH)
        assert json.loads(req.content) == {
            "part": "rear_wheel", "state": "missing", "confirmed_by": "person",
            "note": "The person tried the suggested fix and it fixed the problem. Clipped on, spins."}
        shown = row_of(c.req("GET", f"/v1/scans/{scan_id}/report")[1], "rear_wheel")
        assert shown["outcome"]["state"] == "fixed" and shown["outcome"]["confirmed_by"] == "person"
        assert shown["outcome"]["note"] == "Clipped on, spins."

        # changing the answer replaces it, and the same answer again is sent once more at most (idempotent save)
        status, out, _ = c.req("POST", f"/v1/scans/{scan_id}/outcome",
                               {"row_id": wheel["finding_id"], "outcome": "not_fixed"})
        assert status == 200 and out["forwarded"] == "sent"
        assert json.loads(t.of("POST", OUTCOME_PATH)[-1].content)["note"].startswith("The person tried the suggested fix and it did not")
        assert row_of(c.req("GET", f"/v1/scans/{scan_id}/report")[1], "rear_wheel")["outcome"]["state"] == "not_fixed"
        # not tried is saved and shown but says nothing about the part, so nothing is sent
        n = len(t.of("POST", OUTCOME_PATH))
        status, out, _ = c.req("POST", f"/v1/scans/{scan_id}/outcome",
                               {"row_id": wheel["finding_id"], "outcome": "not_tried"})
        assert out["forwarded"] == "not_applicable" and len(t.of("POST", OUTCOME_PATH)) == n
        saved = json.loads((tmp_path / "sessions" / session_id / "outcomes.json").read_text(encoding="utf-8"))
        assert saved[scan_id]["part:rear_wheel"]["state"] == "not_tried"


def test_outcome_refuses_a_row_the_report_does_not_have_and_a_state_that_is_not_a_label(tmp_path):
    t = FilesRecorder(files_answers())
    with serve(gateway(tmp_path, t)) as c:
        scan_id, _, report = judged(c, t)
        assert c.req("POST", f"/v1/scans/{scan_id}/outcome", {"row_id": "nope", "outcome": "fixed"})[0] == 404
        row = row_of(report, "cover")["finding_id"]
        assert c.req("POST", f"/v1/scans/{scan_id}/outcome", {"row_id": row, "outcome": "maybe"})[0] == 422
        assert c.req("POST", f"/v1/scans/{scan_id}/outcome", {"row_id": row, "outcome": "fixed", "x": 1})[0] == 422
        assert c.req("POST", "/v1/scans/scan_unknown/outcome", {"row_id": row, "outcome": "fixed"}, auth=False)[0] == 401
        assert t.of("POST", OUTCOME_PATH) == []


def test_an_answer_the_design_engine_could_not_take_is_kept_and_sent_when_the_report_is_read_again(tmp_path):
    t = FilesRecorder(files_answers())
    with serve(gateway(tmp_path, t)) as c:
        scan_id, _, report = judged(c, t)
        t.answers[("POST", OUTCOME_PATH)] = (503, {"detail": "restarting"})
        wheel = row_of(report, "rear_wheel")["finding_id"]
        status, out, _ = c.req("POST", f"/v1/scans/{scan_id}/outcome", {"row_id": wheel, "outcome": "fixed"})
        assert status == 200 and out["saved"] is True and out["forwarded"] == "failed"
        assert row_of(c.req("GET", f"/v1/scans/{scan_id}/report")[1], "rear_wheel")["outcome"]["state"] == "fixed"
        t.answers[("POST", OUTCOME_PATH)] = {"ok": True}
        before = len(t.of("POST", OUTCOME_PATH))

        def resent():
            c.req("GET", f"/v1/scans/{scan_id}/report")
            return len(t.of("POST", OUTCOME_PATH)) > before
        wait_for(resent, "the saved answer to be sent again")
        time_sent = len(t.of("POST", OUTCOME_PATH))
        c.req("GET", f"/v1/scans/{scan_id}/report")
        assert len(t.of("POST", OUTCOME_PATH)) == time_sent   # sent once it was taken


def test_the_design_picture_comes_through_the_gateway_without_the_address_or_key(tmp_path):
    t = FilesRecorder(files_answers())
    with serve(gateway(tmp_path, t)) as c:
        scan_id, _, _ = judged(c, t)
        status, body, headers = c.req("GET", f"/v1/scans/{scan_id}/design-view")
        assert status == 200 and body == PNG and headers["Content-Type"] == "image/png"
        assert "grain_secret" not in str(dict(headers)) and "design.test" not in str(dict(headers))
        assert c.req("GET", f"/v1/scans/{scan_id}/design-view")[1] == PNG
        assert len(t.of("GET", "/projects/demo_product/files/out/views/iso.png")) == 1   # cached
        assert t.of("GET", "/projects/demo_product/files/out/views/iso.png")[0].headers["authorization"] == "Bearer grain_secret"
        assert c.req("GET", f"/v1/scans/{scan_id}/design-view?view=sheet")[1].endswith(b"sheet")
        assert c.req("GET", f"/v1/scans/{scan_id}/design-view?view=../x")[0] == 422
        status, body, _ = c.req("GET", "/v1/products/demo_product/view")
        assert status == 200 and body == PNG
        assert c.req("GET", "/v1/products/demo_product/view", auth=False)[0] == 401
        assert c.req("GET", "/v1/products/unknown_thing/view")[0] == 404   # the design engine has none: honest 404


def test_a_part_to_remake_is_downloaded_through_the_gateway(tmp_path):
    t = FilesRecorder(files_answers())
    with serve(gateway(tmp_path, t)) as c:
        scan_id, _, report = judged(c, t)
        hinge = row_of(report, "hinge")
        assert hinge["fix_plan"]["part_file"] == {"formats": ["stl", "step"]}
        status, body, headers = c.req("GET", f"/v1/scans/{scan_id}/fixes/{hinge['finding_id']}/file")
        assert status == 200 and body == STL and 'filename="hinge.stl"' in headers["Content-Disposition"]
        assert dict(t.of("GET", "/projects/demo_product/bom/part/hinge/file")[0].url.params) == {"format": "stl"}
        assert c.req("GET", f"/v1/scans/{scan_id}/fixes/{hinge['finding_id']}/file?format=step")[0] == 200
        assert dict(t.of("GET", "/projects/demo_product/bom/part/hinge/file")[-1].url.params) == {"format": "step"}
        assert c.req("GET", f"/v1/scans/{scan_id}/fixes/{hinge['finding_id']}/file?format=exe")[0] == 422
        wheel = row_of(report, "rear_wheel")["finding_id"]   # a part to buy has no file
        assert c.req("GET", f"/v1/scans/{scan_id}/fixes/{wheel}/file")[0] == 404
        assert c.req("GET", f"/v1/scans/{scan_id}/fixes/nope/file")[0] == 404


def test_a_hand_typed_reading_with_a_check_id_is_judged_and_its_verdict_can_be_read_back(tmp_path):
    t = FilesRecorder(files_answers())
    with serve(gateway(tmp_path, t)) as c:
        scan_id, session_id, report = judged(c, t)
        step = report["next_step"]
        assert step["action_id"] == "chk_vin" and step["target"]["net"] == "VIN" and step["power_state"] == "on"
        reading = {**ex("meter-reading"), "session_id": session_id, "scan_id": scan_id, "reading_id": "rd_hand",
                   "kind": "voltage_dc", "net": "VIN", "value": {"volts": 3.1}, "power_state": "on",
                   "action_id": step["action_id"], "source": "typed", "raw_text": "3.1"}
        reading.pop("pins", None)
        reading.pop("meter", None)
        t.answers[("POST", "/call")] = {**CALL, "electrical": {
            "readings": [{"net": "VIN", "verdict": "off", "findings": [0]}],
            "findings": [{"severity": "error", "text": "VIN reads 3.1 V; the design expects 4.75 to 5.25 V.",
                          "subject": {"net": "VIN"}}]}}
        assert c.req("POST", "/v1/readings", reading)[0] == 201
        got = wait_for(lambda: (lambda b: b if b["verdict"]["state"] == "judged" else None)(
            c.req("GET", "/v1/readings/rd_hand")[1]), "the verdict")
        assert got["verdict"]["verdict"] == "off" and "4.75" in got["verdict"]["notes"][0]
        assert got["reading"]["source"] == "typed" and got["reading"]["action_id"] == "chk_vin"
        sent = json.loads(t.of("POST", "/call")[-1].content)
        assert sent["readings"] == [{"net": "VIN", "id": "rd_hand", "power_state": "on", "volts": 3.1, "check": "chk_vin"}]
        assert c.req("GET", "/v1/readings/nope")[0] == 404
