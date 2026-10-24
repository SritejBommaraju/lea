"""Voice mode live: a real back-and-forth with Claude through the real gateway.

    pytest -m live -s backend/tests/integration/test_voice_live.py     # needs ANTHROPIC_API_KEY in backend/.env

The person explains a fault and their theory over several turns, as in voice mode. Lea must keep the theory in
context, ask at most one question per turn, offer a scan with a symptom in their words, start it on a plain "Yes.",
never call the theory proven, and keep one conversation's history out of another. `-s` prints the conversation.
A missing key skips with HUMAN ACTION NEEDED. Nothing is faked.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from test_api_gateway import gw  # noqa: E402,F401  (the gateway fixture)

from repairos.config import MissingSettingError, get_settings  # noqa: E402

pytestmark = pytest.mark.live
PROOF = re.compile(r"\b(definitely|certainly|confirmed|for sure|is the cause)\b", re.I)


def need(key: str) -> None:
    try:
        get_settings().require(key)
    except MissingSettingError:
        pytest.skip(f"HUMAN ACTION NEEDED: {key} in backend/.env")


def say(gw, conversation: str, n: int, text: str) -> dict:
    status, out, _ = gw.req("POST", "/v1/voice/turns", {"id": f"{conversation}-{n}", "transcript": text,
                                                        "mode": "wake", "conversation_id": conversation})
    assert status == 200, out
    print(f"\n  you: {text}\n  lea: {out['reply']}\n       {out['actions']}")
    reply = out["reply"]
    assert reply and len(re.findall(r"[.!?](\s|$)", reply)) <= 4, "spoken replies stay short"
    assert reply.count("?") <= 1, "one question at a time"
    assert not PROOF.search(reply), "the person's theory is a lead, not a finding"
    return out


def test_conversation_keeps_the_theory_and_leads_into_a_scan(gw):
    need("ANTHROPIC_API_KEY")
    c = "vc_live_1"
    turns = ["My desk fan stopped spinning after I knocked it off the table. I think a wire inside came loose.",
             "It still hums when I switch it on, the blades just don't move.",
             "No, I haven't opened it up yet."]
    offer = None
    for n, text in enumerate(turns):
        out = say(gw, c, n, text)
        offer = next((a for a in out["actions"] if a["type"] in ("offer_scan", "scan")), None)
        if offer:
            break
    assert offer, "with no scan yet, Lea asks for one within three turns"
    assert offer.get("symptom"), "the scan starts with what they said"
    print(f"  symptom: {offer['symptom']}")

    recall = say(gw, c, 10, "Wait, what did I say I think the problem is?")["reply"].lower()
    assert "wire" in recall or "loose" in recall, "the theory from the first turn is still in context"

    if offer["type"] == "offer_scan":
        accepted = say(gw, c, 11, "Yes.")
        scan = next((a for a in accepted["actions"] if a["type"] == "scan"), None)
        assert scan, "a plain yes to the offer starts the scan"
        assert scan.get("symptom")

    other = say(gw, "vc_live_2", 0, "What did I tell you is wrong with it?")["reply"].lower()
    assert "wire" not in other and "table" not in other, "a new conversation starts without the old one"
