"""Spend guards (repairos.spend): caps trip before anything is spent, the counters are shared by processes, a repeated
request is caught, the breaker holds for its cooldown, alerts are logged, stored and streamed, and a scan whose
Claude cap trips still gets a report that says so. Every test has its own spend file (backend/tests/conftest.py)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from repairos import spend
from repairos.config import ROOT

BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture
def caps(tmp_path, monkeypatch):
    """Write a pipeline config whose `spend:` caps are the shipped ones with `overrides` per service; returns it."""
    base = yaml.safe_load((ROOT / "config" / "pipeline.yaml").read_text())

    def make(**overrides):
        cfg = json.loads(json.dumps(base))
        for service, over in overrides.items():
            cfg["spend"].setdefault(service, dict(cfg["spend"]["default"])).update(over)
        p = tmp_path / f"pipeline_{time.monotonic_ns()}.yaml"
        p.write_text(yaml.safe_dump(cfg))
        monkeypatch.setenv("REPAIROS_PIPELINE_CONFIG", str(p))
        return p
    return make


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 1_800_000_000.0}
    monkeypatch.setattr(spend, "_now", lambda: t["now"])
    return t


def test_shipped_caps_parse_and_name_every_paid_service():
    cfg = spend.load_config()
    assert {"anthropic", "typesafe", "brightdata", "cartesia", "spacetime", "default"} <= set(cfg["services"])
    a = spend.caps("anthropic")
    assert (a["per_minute"], a["per_day"], a["usd_per_day"]) == (400, 4000, 25)
    assert spend.caps("brightdata")["per_minute"] == 30 and spend.caps("cartesia")["per_day"] == 2000
    assert spend.caps("spacetime")["per_minute"] / 60 == 50
    assert spend.caps("some_new_service")["per_day"] == cfg["services"]["default"]["per_day"]


def test_the_cap_trips_before_the_call_and_the_open_breaker_refuses_at_once(caps, clock):
    caps(anthropic={"per_minute": 3, "breaker_cooldown_s": 120})
    for _ in range(3):
        spend.charge("anthropic")
    with pytest.raises(spend.SpendLimit) as e:
        spend.charge("anthropic")
    assert e.value.service == "anthropic" and e.value.reason == "per_minute" and e.value.retry_after_s == 120
    clock["now"] += 61   # the minute has passed, the breaker has not
    with pytest.raises(spend.SpendLimit) as e:
        spend.charge("anthropic")
    assert e.value.reason == "breaker_open" and e.value.tripped == "per_minute" and 0 < e.value.retry_after_s <= 59
    u = spend.usage()["services"]["anthropic"]
    assert u["last_day"] == 3 and u["blocked_day"] == 2 and u["breaker"]["open"]   # refused calls are not spent
    clock["now"] += 60   # cooldown over: calls go again
    spend.charge("anthropic")
    assert spend.usage()["services"]["anthropic"]["breaker"]["open"] is False


def test_the_per_day_cap_holds_across_processes_sharing_the_file(caps, tmp_path):
    cfg = caps(cartesia={"per_minute": 1000, "per_day": 40, "repeat_per_minute": None})
    code = ("import sys\nfrom repairos import spend\nok = 0\n"
            "for _ in range(30):\n"
            "    try:\n        spend.charge('cartesia'); ok += 1\n"
            "    except spend.SpendLimit as e:\n        assert e.reason in ('per_day', 'breaker_open'), e.reason\n"
            "print(ok)\n")
    env = {**os.environ, "PYTHONPATH": str(BACKEND), "REPAIROS_PIPELINE_CONFIG": str(cfg),
           "REPAIROS_SPEND_DB": os.environ["REPAIROS_SPEND_DB"]}
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for _ in range(3)]
    outs = [p.communicate(timeout=60) for p in procs]
    assert not any("unusable" in err for _, err in outs), outs   # each counted in the shared file, not in memory
    counts = [int(out.strip()) for out, _ in outs]
    assert all(p.returncode == 0 for p in procs)
    assert sum(counts) == 40 and spend.usage()["services"]["cartesia"]["last_day"] == 40
    with pytest.raises(spend.SpendLimit):   # and a third process (this one) is refused too
        spend.charge("cartesia")


def test_counters_survive_a_restart(caps):
    caps(brightdata={"per_day": 2})
    spend.charge("brightdata", key="q1")
    spend.charge("brightdata", key="q2")
    spend._local.conns = {}          # a new process opens the file afresh
    with pytest.raises(spend.SpendLimit) as e:
        spend.charge("brightdata", key="q3")
    assert e.value.reason == "per_day" and e.value.retry_after_s > 300   # until the day's window frees up


def test_the_same_request_again_and_again_is_repeat_spam(caps, clock):
    caps(anthropic={"repeat_per_minute": 2, "breaker_cooldown_s": 30})
    spend.charge("anthropic", key="same")
    spend.charge("anthropic", key="other")
    spend.charge("anthropic", key="same")
    with pytest.raises(spend.SpendLimit) as e:
        spend.charge("anthropic", key="same")
    assert e.value.reason == "repeat_spam"
    with pytest.raises(spend.SpendLimit) as e:      # the whole service is paused, not just that request
        spend.charge("anthropic", key="different")
    assert e.value.reason == "breaker_open"
    clock["now"] += 61
    spend.charge("anthropic", key="same")            # a minute later the key is fresh again


def test_measured_cost_refines_the_estimate_and_the_usd_cap_trips(caps, clock):
    caps(anthropic={"usd_per_day": 1.0, "usd_per_call": 0.01})
    assert spend.estimate_usd("anthropic") == 0.01
    spend.charge("anthropic", usd=0.01)
    spend.record_cost("anthropic", 0.6, estimate=0.01)   # the answer was long
    assert spend.usage()["services"]["anthropic"]["usd_day"] == pytest.approx(0.6)
    assert spend.estimate_usd("anthropic") == pytest.approx(0.6)
    with pytest.raises(spend.SpendLimit) as e:            # 0.6 so far + 0.6 expected > 1.0: refused first
        spend.charge("anthropic")
    assert e.value.reason == "usd_per_day"
    assert spend.usage()["services"]["anthropic"]["usd_day"] == pytest.approx(0.6)


def test_usd_estimate_before_the_call_trips_the_cap(caps, clock):
    caps(anthropic={"usd_per_day": 1.0})
    spend.charge("anthropic", usd=0.7)
    with pytest.raises(spend.SpendLimit) as e:
        spend.charge("anthropic", usd=0.7)
    assert e.value.reason == "usd_per_day"


def test_a_measured_cost_over_the_cap_opens_the_breaker_for_the_next_call(caps, clock):
    caps(anthropic={"usd_per_day": 1.0})
    spend.charge("anthropic", usd=0.1)
    spend.record_cost("anthropic", 1.5, estimate=0.1)
    with pytest.raises(spend.SpendLimit) as e:
        spend.charge("anthropic", usd=0.0001)
    assert e.value.reason == "breaker_open" and e.value.tripped == "usd_per_day"


def test_claude_token_counts_price_each_answer():
    usd = spend.anthropic_usd("claude-sonnet-5-5", {"input_tokens": 1000, "output_tokens": 500,
                                                    "cache_creation_input_tokens": 0,
                                                    "cache_read_input_tokens": 3000})
    assert usd == pytest.approx((1000 * 2 + 500 * 10 + 3000 * 0.2) / 1e6)
    assert spend.anthropic_usd("a-model-without-a-price", {"input_tokens": 1}) is None


def test_trips_and_near_cap_warnings_are_logged_stored_and_reported(caps, clock, caplog):
    caps(typesafe={"per_day": 10, "per_minute": 100, "repeat_per_minute": None})
    with caplog.at_level(logging.ERROR, logger="repairos.spend"):
        for _ in range(8):
            spend.charge("typesafe")
        for _ in range(2):
            spend.charge("typesafe")
        with pytest.raises(spend.SpendLimit):
            spend.charge("typesafe")
    alerts = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(alerts) == 2 and all(a.startswith("SPEND ALERT typesafe") for a in alerts)
    assert "80 %" in alerts[0] and "per_day cap reached" in alerts[1]
    stored = spend.alerts_since(0)
    assert [a["reason"] for a in stored] == ["near_per_day", "per_day"]
    assert stored[1]["used"] == 10 and stored[1]["cap"] == 10 and stored[1]["label"] == "Jev"
    assert spend.usage()["last_alert"]["alert_id"] == stored[1]["alert_id"] == spend.last_alert_id()
    ev = spend.event_payload(stored[1])
    assert {"service", "reason", "used", "cap", "retry_after_s"} <= set(ev)


def test_cli_status_prints_a_table_and_reset_closes_a_breaker(caps, capsys):
    caps(cartesia={"per_minute": 1})
    spend.charge("cartesia")
    with pytest.raises(spend.SpendLimit):
        spend.charge("cartesia")
    assert spend.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "cartesia" in out and "open per_minute" in out and "anthropic" in out
    assert spend.main(["reset", "cartesia"]) == 0
    assert "breaker closed: cartesia" in capsys.readouterr().out
    assert spend.usage()["services"]["cartesia"]["breaker"]["open"] is False


def test_the_module_runs_as_a_cli(caps):
    out = subprocess.run([sys.executable, "-m", "repairos.spend", "status"], capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": str(BACKEND)}, timeout=60)
    assert out.returncode == 0 and "service" in out.stdout and "spacetime" in out.stdout


def test_charging_is_thread_safe(caps):
    caps(cartesia={"per_minute": 10_000, "per_day": 300, "repeat_per_minute": None})
    ok = []

    def go():
        for _ in range(50):
            try:
                spend.charge("cartesia")
                ok.append(1)
            except spend.SpendLimit:
                pass
    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 300


# ------------------------------------------------------------------------------------------- call sites
def _response(text: str, **usage):
    u = {"input_tokens": 1200, "output_tokens": 300, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
         **usage}
    return SimpleNamespace(id="msg_test", stop_reason="end_turn", usage=SimpleNamespace(**u),
                           content=[SimpleNamespace(type="text", text=text)])


class RecordedClient:
    """Answers messages.create with one recorded-shape response; counts the calls that reached it."""

    def __init__(self, text: str):
        self.calls, self.text = 0, text
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        return _response(self.text)


def test_every_claude_crop_call_is_charged_first_and_priced_from_its_tokens(caps):
    from repairos.slots.vlm_anthropic import AnthropicVisionJudge

    caps(anthropic={"per_minute": 1})
    j = AnthropicVisionJudge(models=["claude-sonnet-5-5"], effort={"claude-sonnet-5-5": "medium"}, checklists=False)
    j._client = RecordedClient(json.dumps({"category": "kettle", "brand": "", "model": "", "confidence": "low",
                                           "label_visible": False, "label_text": ""}))
    from PIL import Image

    assert j.identify([Image.new("RGB", (64, 64))])["category"] == "kettle"
    u = spend.usage()["services"]["anthropic"]
    assert u["last_minute"] == 1 and u["usd_day"] == pytest.approx((1200 * 2 + 300 * 10) / 1e6)
    with pytest.raises(spend.SpendLimit):
        j.identify([Image.new("RGB", (64, 64))])
    assert j._client.calls == 1   # the refused call never reached Claude


def test_the_summary_writer_is_charged_too(caps):
    from repairos.slots.llm_anthropic import AnthropicTextModel

    caps(anthropic={"per_minute": 1})
    m = AnthropicTextModel()
    m._client = RecordedClient("A plain summary.")
    assert m.complete("system", [{"role": "user", "content": "hi"}]) == "A plain summary."
    with pytest.raises(spend.SpendLimit):
        m.complete("system", [{"role": "user", "content": "hi again"}])
    assert m._client.calls == 1


def test_a_jev_call_over_its_cap_is_a_clear_outage_never_retried(caps):
    from repairos.pipeline.worker import transient
    from repairos.slots.fusion import FusionUnavailable, Question
    from repairos.slots.fusion_jev import JevEngine

    caps(typesafe={"per_minute": 1})
    spend.charge("typesafe")
    posts = []
    eng = JevEngine(key="unused", url="http://127.0.0.1:9/v1/systemone", record_dir=None, timeout_s=1.0)
    eng._post = lambda payload: posts.append(payload) or (None, "", "unreachable")
    q = Question(id="q1", type="noul", instructions="Is the part damaged?")
    with pytest.raises(FusionUnavailable) as e:
        eng.ask({"part": "a part"}, [q])
    assert posts == [] and isinstance(e.value.__cause__, spend.SpendLimit) and spend.PAUSED in str(e.value)
    assert transient(RuntimeError("stage failed")) is False
    wrapped = RuntimeError("fuse failed")
    wrapped.__cause__ = e.value
    assert transient(wrapped) is False   # a spend cap is not worth a job retry


def test_spacetime_writes_wait_coalesced_while_the_breaker_is_open(caps, monkeypatch):
    from repairos.live import spacetime_bridge as sb

    caps(spacetime={"per_minute": 1, "breaker_cooldown_s": 30})
    spend.charge("spacetime")   # the minute's one call is used: the next trips the breaker

    class Store:
        def __init__(self):
            self.calls = []

        def call(self, reducer, args):
            self.calls.append(reducer)
            return 200, ""

    st, outbox, stop = Store(), sb.Outbox(), threading.Event()
    outbox.put("row1", "upsert_scan", [1])
    t = threading.Thread(target=sb.writer_loop, args=(st, outbox, stop), daemon=True)
    t.start()
    time.sleep(0.5)
    outbox.put("row1", "upsert_scan", [2])   # a newer write of the same row replaces the waiting one
    time.sleep(0.3)
    stop.set()
    t.join(timeout=5)
    assert st.calls == [] and len(outbox) == 1 and outbox.take(0.1)[2] == [2]
    assert spend.usage()["services"]["spacetime"]["breaker"]["open"]


# ------------------------------------------------------------------------------------------- degradation
def test_claude_blocked_mid_still_leaves_the_rest_not_inspected_never_ok(caps):
    from PIL import Image

    from repairos.contracts import validate
    from repairos.slots.detector import Detection
    from repairos.slots.vlm import CropState
    from repairos.vision import inspect as insp

    caps(anthropic={"per_minute": 2})

    class ChargedJudge:   # charges like the Claude slot, answers from code (no network in unit tests)
        def inspect_crop(self, crop, context=None, part_hint=None, family=None, **kw):
            spend.charge("anthropic")
            return CropState(part_class=part_hint or "part", state="ok", evidence_text="looks intact",
                             confidence_word="medium", model="test", latency_ms=1, visibility="clear")

    dets = [Detection(box=(100 + 700 * i, 200, 260 + 700 * i, 360), label=f"part {i}", score=0.9 - i / 10)
            for i in range(5)]   # far apart: five separate regions, no merging
    res = insp.inspect_still(Image.new("RGB", (3600, 600), "gray"), still_id="s1", scan_id="sc", detector=object(),
                             judge=ChargedJudge(), classifier=False, detections=dets, workers=1, max_crops=5)
    states = [f.state.value for f in res.findings if f.subject.ref.startswith("s1/det")]
    paused = [f for f in res.findings if f.state.value == "not_inspected" and spend.PAUSED in f.evidence[0]]
    assert states == ["ok", "ok", "not_inspected", "not_inspected", "not_inspected"] and len(paused) == 3
    for f in paused:
        validate("finding", f.model_dump(mode="json", exclude_none=True))


def test_a_scan_paused_by_the_claude_cap_still_gets_a_report_that_says_so(caps, tmp_path, monkeypatch):
    """The whole run: Claude's cap is used up when a photo still with five regions comes in (the partial case is the
    test above); every region is not_inspected with the reason, the summary writer is refused too, and the report is
    still published and says so. (Judged regions would need live Jev, which unit tests never fake.)"""
    import io

    from PIL import Image

    from repairos.pipeline.run import PAUSED_LIMIT, Context, run_id_for, run_scan
    from repairos.slots.blob_local import LocalBlobStore
    from repairos.slots.detector import Detection
    from repairos.slots.fusion_jev import JevEngine
    from repairos.slots.llm_anthropic import AnthropicTextModel
    from repairos.slots.state_sqlite import SqliteState
    from repairos.slots.vlm import CropState
    from test_pipeline import ex

    monkeypatch.setenv("REPAIROS_DATA_DIR", str(tmp_path))
    caps(anthropic={"per_minute": 1, "repeat_per_minute": None})
    spend.charge("anthropic")   # the minute's cap is used up before the scan's crops
    state, blobs = SqliteState(tmp_path / "state.sqlite3"), LocalBlobStore(tmp_path / "blobs")
    buf = io.BytesIO()
    Image.new("RGB", (3600, 600), "gray").save(buf, "JPEG")
    scan = ex("scan-package")
    still = {**ex("still"), "still_id": "s_photo", "image_file": blobs.put(buf.getvalue(), "image/jpeg")}
    still.pop("depth_file", None)

    async def seed():
        await state.transition("session", scan["session_id"], "session.create", {"session_id": scan["session_id"]})
        await state.transition("scan-package", scan["scan_id"], "scan.create", scan)
        await state.transition("still", still["still_id"], "still.create", still)
    asyncio.run(seed())

    class Detector:
        model_id = "test detector"

        def detect(self, im, *a, **k):
            return [Detection(box=(100 + 700 * i, 200, 260 + 700 * i, 360), label=f"part {i}", score=0.9 - i / 10)
                    for i in range(5)]

    class ChargedJudge:   # charges exactly as the Claude slot does, then answers from code
        models = ("test judge",)

        def inspect_crop(self, crop, context=None, part_hint=None, family=None, **kw):
            spend.charge("anthropic")
            return CropState(part_class=part_hint or "part", state="cannot_tell", evidence_text="too blurred",
                             confidence_word="low", model="test", latency_ms=1, visibility="poor")

        def identify(self, images):
            spend.charge("anthropic")
            raise AssertionError("not reached: the cap is used up by then")

    llm = AnthropicTextModel()
    llm._client = RecordedClient("never asked")
    ctx = Context(state=state, blobs=blobs, worker="w-test")
    ctx._slots.update(DET=Detector(), VLM=ChargedJudge(), LLM=llm,
                      FUSION=JevEngine(key="unused", url="http://127.0.0.1:9/v1/systemone", record_dir=None,
                                       timeout_s=1.0))
    ctx._locator, ctx._locator_loaded = None, True   # no locate module: every location says "not computed"
    monkeypatch.setattr("repairos.pipeline.run.maybe_identify", lambda *a, **k: asyncio.sleep(0))
    report = asyncio.run(run_scan(ctx, scan["scan_id"]))
    asyncio.run(ctx.settle())
    assert llm._client.calls == 0
    assert report["summary"]["written_by"] == "repairos (usage limit reached)"
    assert "usage limit" in report["summary"]["text"] and not any(ch.isdigit() for ch in report["summary"]["text"])
    assert PAUSED_LIMIT in report["limits"]
    findings = [r.data for r in asyncio.run(state.list("finding", scan_id=scan["scan_id"]))]
    paused = [f for f in findings if f["state"] == "not_inspected" and spend.PAUSED in f["evidence"][0]]
    assert len(paused) == 5 and not any(f["state"] == "ok" for f in findings)
    assert {f["finding_id"] for f in paused} <= set(json.dumps(report["not_inspected"]).split('"'))
    run = asyncio.run(state.get("pipeline-run", run_id_for(scan["scan_id"]))).data
    assert run["status"] == "done" and report["verdict"]


# ------------------------------------------------------------------------------------------- gateway
def _sse(port: int, path: str, token: str, want: str, timeout: float = 6.0) -> list[dict]:
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\nAccept: text/event-stream\r\n\r\n"
              .encode())
    buf, out, deadline = b"", [], time.time() + timeout
    while not out and time.time() < deadline:
        try:
            chunk = s.recv(65536)
        except TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            lines = block.decode(errors="replace").splitlines()
            fields = dict(line.split(": ", 1) for line in lines if ": " in line and not line.startswith(":"))
            if fields.get("event") == want:
                assert "id" not in fields   # never moves the client's Last-Event-ID
                out.append(json.loads(fields["data"]))
    s.close()
    return out


def test_usage_needs_a_paired_device_and_alerts_reach_the_event_streams(caps, tmp_path):
    from test_api_gateway import CODE, serve

    from repairos.api.app import create_app
    from repairos.slots.blob_local import LocalBlobStore
    from repairos.slots.state_sqlite import SqliteState

    caps(brightdata={"per_minute": 1})
    state, blobs = SqliteState(tmp_path / "state.sqlite3"), LocalBlobStore(tmp_path / "blobs")
    with serve(create_app(state, blobs, pairing_code=CODE, recordings=tmp_path / "sessions")) as c:
        assert c.req("GET", "/v1/usage", auth=False)[0] == 401
        assert c.req("GET", "/v1/usage", headers={"Authorization": "Bearer not-a-device"}, auth=False)[0] == 403
        c.token = c.req("POST", "/v1/devices/pair", {"code": CODE, "name": "test phone"})[1]["token"]
        status, out, _ = c.req("GET", "/v1/usage")
        assert status == 200 and out["services"]["brightdata"]["label"] == "Web search" and out["last_alert"] is None
        sid = c.req("POST", "/v1/sessions", {})[1]["session_id"]

        got: dict[str, list] = {}

        def listen(name, path):
            got[name] = _sse(c.port, path, c.token, "spend_alert")
        threads = [threading.Thread(target=listen, args=a) for a in
                   (("session", f"/v1/sessions/{sid}/events"), ("global", "/v1/events"))]
        for t in threads:
            t.start()
        time.sleep(1.5)
        spend.charge("brightdata")
        with pytest.raises(spend.SpendLimit):
            spend.charge("brightdata")
        for t in threads:
            t.join(timeout=10)
        for name in ("session", "global"):
            ev = got[name][0]
            assert ev["service"] == "brightdata" and ev["reason"] == "per_minute" and ev["cap"] == 1, name
            assert ev["retry_after_s"] > 0 and ev["label"] == "Web search"
        status, out, _ = c.req("GET", "/v1/usage")
        assert out["services"]["brightdata"]["breaker"]["open"] and out["last_alert"]["reason"] == "per_minute"
        assert c.req("GET", "/v1/events", auth=False)[0] == 401
