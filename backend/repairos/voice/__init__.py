"""Spoken turns from the phone: a final transcript in, a short spoken reply and phone actions out.

Every reply comes from a short Claude tool loop over the session's scans, findings, suspects and next actions
(ANTHROPIC_API_KEY, backend only). The phone sends final transcripts only (partials never arrive here), streams its
own speech with a short-lived Cartesia token from `cartesia_token`, and holds no key (A6).
Suspects stay suspects: replies use the suspect's own probability word and never call a fault proven.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from typing import Any

from repairos.config import get_settings

MODEL = "claude-opus-5-5"
MAX_TOOL_ROUNDS = 4

def _subject(s: dict) -> str:
    sub = s.get("subject") or {}
    return str(sub.get("ref") or sub.get("kind") or "a part")


SYSTEM = """You are Lea, a repair copilot talking with someone through their iPhone while they look at a physical product.
Your reply is spoken aloud: one to three short sentences, plain words, no lists, no markdown.
Latency-sensitive; begin your visible answer immediately.
Findings and suspects come from the vision pipeline and are candidates, never proof; only a meter reading proves a fault.
Use the suspect's own probability word. Never state voltages, polarity or ratings from appearance.
Safety: only low-voltage products; continuity checks are done unplugged.
This is a back-and-forth conversation: build on what they told you earlier instead of starting over.
When they explain what they think is wrong, take their theory seriously and say whether the scan supports it, but treat
it as a lead, not a finding. Ask at most one short follow-up question at a time when it would narrow things down (what
happened, when it started, what they already tried) and stop asking once you know enough to look.
Use tools to read what the scan found and to act on the phone.
When the person describes a problem and there is no scan yet, ask them to scan the object and call offer_scan (it shows
a scan button they can accept). Call start_scan only when they ask for a scan or say yes to one you offered.
Pass both a symptom that sums up, in their words, what is wrong and what they suspect, so the scan starts with it.
Each turn may begin with what the phone shows right now (the screen, the scan's stage, the on-screen cue, progress,
close-ups taken). While they scan, keep talking with them: answer, coach from the cue in your own words, suggest close-ups
of what they are worried about, and keep replies to a sentence or two. While a scan is open, never offer or start one."""

_SYMPTOM = {"type": "object", "additionalProperties": False, "properties": {"symptom": {
    "type": "string", "description": "One sentence, in their words: what is wrong and what they think the cause is."}}}

TOOLS = [
    {"name": "session_state", "description": "What this session has: scans, findings, suspects (ranked) and next actions.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "offer_scan", "description": "Show a scan button the person can accept, after asking them to scan the object.",
     "input_schema": _SYMPTOM},
    {"name": "start_scan", "description": "Open a new guided scan now: they asked for one, or said yes to the offer.",
     "input_schema": _SYMPTOM},
    {"name": "open_report", "description": "Show the report for the latest scan on the phone.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
]

_client = None
_lock = threading.Lock()


def _anthropic():
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                import anthropic

                _client = anthropic.Anthropic(api_key=get_settings().require("ANTHROPIC_API_KEY", "voice agent"))
    return _client


def _brief(snap: dict) -> dict:
    return {
        "scans": len(snap.get("scans", [])),
        "findings": [{"subject": _subject(f), "state": f.get("state"), "is_proof": f.get("is_proof")} for f in snap.get("findings", [])][:10],
        "suspects": [{"subject": _subject(s), "rank": s.get("rank"), "likelihood": s.get("p_fault_word"), "verdict": s.get("verdict"),
                      "fault": (s.get("fault_kind") or {}).get("choice")} for s in snap.get("suspects", [])][:10],
        "next_actions": [{"instruction": a.get("instruction"), "power_state": a.get("power_state")} for a in snap.get("next_actions", [])][:3],
    }


def _symptom(given: Any) -> dict:
    s = given.get("symptom") if isinstance(given, dict) else None
    return {"symptom": s.strip()[:300]} if isinstance(s, str) and s.strip() else {}


def _live_note(live: dict[str, str] | None) -> str | None:
    """What the phone shows right now, as one line ahead of the transcript (not kept in the history)."""
    if not live:
        return None
    return "On the phone right now: " + "; ".join(f"{k}: {v}" for k, v in list(live.items())[:12])


def agent_turn(transcript: str, snap: dict, history: list[tuple[str, str]], client=None,
               live: dict[str, str] | None = None) -> tuple[str, list[dict]]:
    """One spoken turn through Claude with tools. Raises MissingSettingError without a key, and on API errors."""
    client = client or _anthropic()
    actions: list[dict] = []
    messages: list[dict] = []
    for said, replied in history[-12:]:
        messages += [{"role": "user", "content": said}, {"role": "assistant", "content": replied or "(no reply)"}]
    note = _live_note(live)
    scanning = bool(live) and live.get("screen") == "scan"
    messages.append({"role": "user", "content": [{"type": "text", "text": note}, {"type": "text", "text": transcript}]
                     if note else transcript})
    for _ in range(MAX_TOOL_ROUNDS):
        resp = client.beta.messages.create(
            model=MODEL, max_tokens=4000, system=SYSTEM, tools=TOOLS, messages=messages,
            output_config={"effort": "low"}, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            cache_control={"type": "ephemeral"},
        )
        if resp.stop_reason == "refusal":
            return "I can't help with that one.", actions
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            reply = " ".join(b.text for b in resp.content if b.type == "text").strip()
            return reply or "Okay.", actions
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for u in uses:
            if u.name == "session_state":
                out = json.dumps(_brief(snap))
            elif u.name in ("offer_scan", "start_scan") and scanning:
                out = "A scan is already open on the phone; help them with it."
            elif u.name == "offer_scan":
                actions.append({"type": "offer_scan", **_symptom(u.input)})
                out = "The scan button is showing; it opens the scan when they tap it or say yes."
            elif u.name == "start_scan":
                actions.append({"type": "scan", **_symptom(u.input)})
                out = "The scan screen is opening on the phone."
            elif u.name == "open_report" and snap.get("scans"):
                actions.append({"type": "report", "scan_id": snap["scans"][-1]["scan_id"]})
                out = "The report is opening."
            else:
                out = "There is no scan yet, so there is no report."
            results.append({"type": "tool_result", "tool_use_id": u.id, "content": out})
        messages.append({"role": "user", "content": results})
    return "Sorry, that took too long. Could you ask again?", actions


def cartesia_token(seconds: int = 300) -> dict[str, Any]:
    """A short-lived Cartesia token for the phone's speech (STT and TTS). Raises MissingSettingError when not set up."""
    s = get_settings()
    key = s.require("CARTESIA_API_KEY", "voice")
    voice = s.cartesia_voice_id
    version = s.cartesia_version
    req = urllib.request.Request("https://api.cartesia.ai/access-token", method="POST",
                                 data=json.dumps({"grants": {"stt": True, "tts": True}, "expires_in": seconds}).encode(),
                                 headers={"Cartesia-Version": version, "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read())
    return {"token": body["token"], "expiresInSeconds": body.get("expires_in", seconds), "version": version, "voiceId": voice}
