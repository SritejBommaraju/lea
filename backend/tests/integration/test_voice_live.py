"""Voice live: a real back-and-forth with Lea (real Claude) through the real gateway's `/v1/voice/converse`.

    pytest -m live -s backend/tests/integration/test_voice_live.py     # needs ANTHROPIC_API_KEY in backend/.env

The person explains a fault and their theory over a few turns on the home screen. Lea keeps the theory in context,
asks at most one question a turn, never calls the theory proven, and offers a scan (the start_scan action) within
three turns. During the scan (`live` says so) she answers without offering another one. A different scan's
conversation starts without the first one's history. `-s` prints the conversation. Text answers only (audio=none):
Cartesia is not needed. A missing key skips with HUMAN ACTION NEEDED. Nothing is faked.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from test_api_gateway import gw  # noqa: E402,F401  (the gateway fixture: the real app, real voice runtime)

from repairos.config import MissingSettingError, get_settings  # noqa: E402

pytestmark = pytest.mark.live
PROOF = re.compile(r"\b(definitely|certainly|confirmed|for sure|is the cause)\b", re.I)


def need(key: str) -> None:
    try:
        get_settings().require(key)
    except MissingSettingError:
        pytest.skip(f"HUMAN ACTION NEEDED: {key} in backend/.env")


def frames(raw: bytes) -> list[dict]:
    """The J events of a converse answer (1 byte kind, 4 bytes length, payload)."""
    out, i = [], 0
    while i + 5 <= len(raw):
        kind, n = raw[i:i + 1], int.from_bytes(raw[i + 1:i + 5], "big")
        if kind == b"J":
            out.append(json.loads(raw[i + 5:i + 5 + n]))
        i += 5 + n
    return out


def say(gw, scan_id: str, text: str, live: dict | None = None) -> tuple[str, list[str]]:  # noqa: F811
    q = f"/v1/voice/converse?audio=none&scan_id={scan_id}"
    if live:
        q += "&live=" + urllib.parse.quote(json.dumps(live))
    status, raw, _ = gw.req("POST", q, {"text": text})
    assert status == 200, raw
    events = frames(raw)
    reply = " ".join(e["text"] for e in events if e["type"] == "sentence")
    actions = [e["action"] for e in events if e["type"] == "action"]
    print(f"\n  you: {text}\n  lea: {reply}\n       {actions}")
    assert reply and len(re.findall(r"[.!?](\s|$)", reply)) <= 4, "spoken answers stay short"
    assert reply.count("?") <= 1, "one question at a time"
    assert not PROOF.search(reply), "the person's theory is a lead, not a finding"
    return reply, actions


def test_a_conversation_keeps_the_theory_offers_a_scan_and_coaches_during_it(gw, tmp_path, monkeypatch):  # noqa: F811
    need("ANTHROPIC_API_KEY")
    monkeypatch.setenv("REPAIROS_DATA_DIR", str(tmp_path))   # conversations are saved there, not in the repo
    home = "scan_live_home"
    turns = ["My desk fan stopped spinning after I knocked it off the table. I think a wire inside came loose.",
             "It still hums when I switch it on, the blades just don't move.",
             "No, I haven't opened it up yet."]
    offered = False
    for text in turns:
        _, actions = say(gw, home, text)
        if "start_scan" in actions:
            offered = True
            break
    assert offered, "with no scan yet, Lea offers one within three turns"

    recall, _ = say(gw, home, "Wait, what did I say I think the problem is?")
    assert "wire" in recall.lower() or "loose" in recall.lower(), "the theory from the first turn is still in context"

    live = {"screen": "scan", "stage": "scanning: walking around it", "progress": "35%", "close-ups taken": "1",
            "on-screen cue": "Move closer"}
    _, during = say(gw, home, "Should I get the back where the cord goes in?", live)
    assert "start_scan" not in during, "no second scan while one is open"

    other, _ = say(gw, "scan_live_other", "What did I tell you is wrong with it?")
    assert "wire" not in other.lower() and "table" not in other.lower(), "another scan's conversation starts fresh"
