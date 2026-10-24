"""Voice turns on the gateway: rules answer without a key, turns are idempotent, partial/empty turns are refused,
devices must be paired, and the speech token says which setting is missing. Real uvicorn server and SQLite state
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
    keyless = lambda: Settings(_env_file=None, anthropic_api_key=None, cartesia_api_key=None, cartesia_voice_id=None)  # noqa: E731
    monkeypatch.setattr(gateway, "get_settings", keyless)
    monkeypatch.setattr(voice, "get_settings", keyless)


def test_rules_answer_and_act(gw):
    status, out, _ = gw.req("POST", "/v1/voice/turns", {"id": "t1", "transcript": "Hey Leah, make a render of this for me"})
    assert status == 200 and out["actions"] == [{"type": "scan"}] and out["by"] == "rules"
    status, out, _ = gw.req("POST", "/v1/voice/turns", {"id": "t2", "transcript": "make it nicer"})
    assert out["actions"] == [] and "Which do you want" in out["reply"]


def test_status_reads_the_session(gw):
    status, made, _ = gw.req("POST", "/v1/sessions", {"symptom": "fan does not spin"})
    assert status == 201
    sid = made["session_id"]
    status, out, _ = gw.req("POST", "/v1/voice/turns", {"id": "t3", "transcript": "what seems to be the issue here?", "session_id": sid})
    assert status == 200 and "no scan yet" in out["reply"]
    status, out, _ = gw.req("POST", "/v1/voice/turns", {"id": "t4", "transcript": "what did you find", "session_id": "nope"})
    assert status == 404


def test_idempotent_and_final_only(gw):
    status, first, _ = gw.req("POST", "/v1/voice/turns", {"id": "t5", "transcript": "open the report"})
    assert status == 200 and first["reply"] == "There's no report yet."
    again = gw.req("POST", "/v1/voice/turns", {"id": "t5", "transcript": "something else entirely"})[1]
    assert again == first
    assert gw.req("POST", "/v1/voice/turns", {"id": "t6", "transcript": "   "})[0] == 422


def test_paired_devices_only_and_token_names_the_missing_setting(gw):
    token, gw.token = gw.token, None
    assert gw.req("POST", "/v1/voice/turns", {"id": "t7", "transcript": "hi"})[0] == 401
    gw.token = token
    status, out, _ = gw.req("POST", "/v1/voice/token", {})
    assert status == 503 and "CARTESIA_API_KEY" in str(out)


def test_status_words_stay_suspects():
    snap = {"scans": [{"scan_id": "s1"}], "findings": [{}],
            "suspects": [{"rank": 1, "subject": {"ref": "J1"}, "p_fault_word": "likely", "fault_kind": {"choice": "unplugged"}}],
            "next_actions": [{"instruction": "Power OFF. Probe J1 pin 1 to LOAD1 pin 1."}]}
    line = voice.status_reply(snap)
    assert "J1: likely unplugged" in line and "not proven" in line and "Power OFF" in line
