"""Voice turns on the gateway: answered by Claude only (a missing key is a 503 naming it, never a canned reply),
partial/empty turns are refused, failures are not cached, devices must be paired, and the speech token names what is missing. Real uvicorn server and SQLite state
(the B5 test fixture). Keys are forced off so a developer's backend/.env never makes these tests call a paid API."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api_gateway import gw  # noqa: E402,F401  (the gateway fixture)

import repairos.api.app as gateway  # noqa: E402
import repairos.voice as voice  # noqa: E402
from repairos.config import Settings  # noqa: E402


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    keyless = lambda: Settings(_env_file=None, anthropic_api_key=None, cartesia_api_key=None)  # noqa: E731
    monkeypatch.setattr(gateway, "get_settings", keyless)
    monkeypatch.setattr(voice, "get_settings", keyless)


def test_needs_the_assistant_key_and_says_so(gw):
    status, out, _ = gw.req("POST", "/v1/voice/turns", {"id": "t1", "transcript": "Hey Leah, what seems to be the issue here?"})
    assert status == 503 and "ANTHROPIC_API_KEY" in str(out)
    status, made, _ = gw.req("POST", "/v1/sessions", {"symptom": "fan does not spin"})
    assert gw.req("POST", "/v1/voice/turns", {"id": "t2", "transcript": "what did you find", "session_id": "nope"})[0] == 404


def test_final_turns_only_and_failures_are_not_cached(gw):
    assert gw.req("POST", "/v1/voice/turns", {"id": "t3", "transcript": "   "})[0] == 422
    assert gw.req("POST", "/v1/voice/turns", {"id": "t4", "transcript": "hello"})[0] == 503
    assert gw.req("POST", "/v1/voice/turns", {"id": "t4", "transcript": "hello"})[0] == 503   # retried, not a stale answer


def test_turns_carry_their_conversation(gw):
    turn = {"id": "t5", "transcript": "I think the fuse blew", "conversation_id": "c1"}
    assert gw.req("POST", "/v1/voice/turns", turn)[0] == 503   # accepted; only the missing key stops it


def test_live_scan_state_rides_along(gw):
    live = {"screen": "scan", "stage": "scanning", "cue": "Walk slowly around it", "progress": "40%"}
    assert gw.req("POST", "/v1/voice/turns", {"id": "t6", "transcript": "is this enough?", "live": live})[0] == 503
    assert voice._live_note(live) == "On the phone right now: screen: scan; stage: scanning; cue: Walk slowly around it; progress: 40%"
    assert voice._live_note(None) is None and voice._live_note({}) is None
    too_much = {f"k{i}": "v" for i in range(13)}
    assert gw.req("POST", "/v1/voice/turns", {"id": "t8", "transcript": "hi", "live": too_much})[0] == 422


def test_scan_symptoms_are_trimmed_and_optional():
    assert voice._symptom({"symptom": "  won't turn on, maybe the switch  "}) == {"symptom": "won't turn on, maybe the switch"}
    assert voice._symptom({}) == {} and voice._symptom({"symptom": " "}) == {} and voice._symptom(None) == {}


def test_paired_devices_only_and_token_names_the_missing_setting(gw):
    token, gw.token = gw.token, None
    assert gw.req("POST", "/v1/voice/turns", {"id": "t7", "transcript": "hi"})[0] == 401
    gw.token = token
    status, out, _ = gw.req("POST", "/v1/voice/token", {})
    assert status == 503 and "CARTESIA_API_KEY" in str(out)
