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
    """Write a spend config whose `spend:` caps are the shipped ones with `overrides` per service; returns it."""
    base = yaml.safe_load((ROOT / "config" / "spend.yaml").read_text())

    def make(**overrides):
        cfg = json.loads(json.dumps(base))
        for service, over in overrides.items():
            cfg["spend"].setdefault(service, dict(cfg["spend"]["default"])).update(over)
        p = tmp_path / f"spend_{time.monotonic_ns()}.yaml"
        p.write_text(yaml.safe_dump(cfg))
        monkeypatch.setenv("REPAIROS_SPEND_CONFIG", str(p))
        return p
    return make


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 1_800_000_000.0}
    monkeypatch.setattr(spend, "_now", lambda: t["now"])
    return t


def test_shipped_caps_parse_and_name_every_paid_service():
    cfg = spend.load_config()
    assert {"anthropic", "anthropic_web_search", "cartesia", "default"} == set(cfg["services"])
    a = spend.caps("anthropic")
    assert (a["per_minute"], a["per_day"], a["usd_per_day"]) == (400, 4000, 25)
    assert spend.caps("anthropic_web_search")["per_minute"] == 10 and spend.caps("cartesia")["per_day"] == 2000
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
    caps(anthropic_web_search={"per_day": 2})
    spend.charge("anthropic_web_search", key="q1")
    spend.charge("anthropic_web_search", key="q2")
    spend._local.conns = {}          # a new process opens the file afresh
    with pytest.raises(spend.SpendLimit) as e:
        spend.charge("anthropic_web_search", key="q3")
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
    caps(cartesia={"per_day": 10, "per_minute": 100, "repeat_per_minute": None})
    with caplog.at_level(logging.ERROR, logger="repairos.spend"):
        for _ in range(8):
            spend.charge("cartesia")
        for _ in range(2):
            spend.charge("cartesia")
        with pytest.raises(spend.SpendLimit):
            spend.charge("cartesia")
    alerts = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(alerts) == 2 and all(a.startswith("SPEND ALERT cartesia") for a in alerts)
    assert "80 %" in alerts[0] and "per_day cap reached" in alerts[1]
    stored = spend.alerts_since(0)
    assert [a["reason"] for a in stored] == ["near_per_day", "per_day"]
    assert stored[1]["used"] == 10 and stored[1]["cap"] == 10 and stored[1]["label"] == "Voice"
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
    assert out.returncode == 0 and "service" in out.stdout and "cartesia" in out.stdout


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

    caps(anthropic_web_search={"per_minute": 1})
    state, blobs = SqliteState(tmp_path / "state.sqlite3"), LocalBlobStore(tmp_path / "blobs")
    with serve(create_app(state, blobs, pairing_code=CODE, recordings=tmp_path / "sessions")) as c:
        assert c.req("GET", "/v1/usage", auth=False)[0] == 401
        assert c.req("GET", "/v1/usage", headers={"Authorization": "Bearer not-a-device"}, auth=False)[0] == 403
        c.token = c.req("POST", "/v1/devices/pair", {"code": CODE, "name": "test phone"})[1]["token"]
        status, out, _ = c.req("GET", "/v1/usage")
        assert status == 200 and out["services"]["anthropic_web_search"]["label"] == "Lea web search" and out["last_alert"] is None
        sid = c.req("POST", "/v1/sessions", {})[1]["session_id"]

        got: dict[str, list] = {}

        def listen(name, path):
            got[name] = _sse(c.port, path, c.token, "spend_alert")
        threads = [threading.Thread(target=listen, args=a) for a in
                   (("session", f"/v1/sessions/{sid}/events"), ("global", "/v1/events"))]
        for t in threads:
            t.start()
        time.sleep(1.5)
        spend.charge("anthropic_web_search")
        with pytest.raises(spend.SpendLimit):
            spend.charge("anthropic_web_search")
        for t in threads:
            t.join(timeout=10)
        for name in ("session", "global"):
            ev = got[name][0]
            assert ev["service"] == "anthropic_web_search" and ev["reason"] == "per_minute" and ev["cap"] == 1, name
            assert ev["retry_after_s"] > 0 and ev["label"] == "Lea web search"
        status, out, _ = c.req("GET", "/v1/usage")
        assert out["services"]["anthropic_web_search"]["breaker"]["open"] and out["last_alert"]["reason"] == "per_minute"
        assert c.req("GET", "/v1/events", auth=False)[0] == 401
