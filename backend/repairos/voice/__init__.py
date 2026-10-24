"""Spoken turns from the phone: a final transcript in, a short spoken reply and phone actions out.

With ANTHROPIC_API_KEY the reply comes from a short Claude tool loop over the session's scans, findings, suspects and
next actions; without it, plain rules answer. The phone sends final transcripts only (partials never arrive here),
streams its own speech with a short-lived Cartesia token from `cartesia_token`, and holds no key (A6).
Suspects stay suspects: replies use the suspect's own probability word and never call a fault proven.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.request
from typing import Any

from repairos.config import get_settings

MODEL = "claude-opus-5-5"
MAX_TOOL_ROUNDS = 4

SCAN = re.compile(r"\b(make a (render|scan|model)|start (a |the )?scan|scan (this|it)|new scan|capture (this|it))\b", re.I)
REPORT = re.compile(r"\b(show|open|see|read)\b.{0,20}\breport\b", re.I)
STATUS = re.compile(r"\b(what did you find|what'?s wrong|what seems to be|status|findings|suspects?|what next|next step|did it work)\b", re.I)
WAKE = re.compile(r"^\s*(hey|hi|ok)?\s*,?\s*(leah|lea)\b[\s,.:]*", re.I)


def _subject(s: dict) -> str:
    sub = s.get("subject") or {}
    return str(sub.get("ref") or sub.get("kind") or "a part")


def status_reply(snap: dict) -> str:
    """Where the session stands, in a sentence or two."""
    suspects = sorted(snap.get("suspects", []), key=lambda s: s.get("rank") or 99)
    actions = snap.get("next_actions", [])
    if not snap.get("scans"):
        return "There's no scan yet. Say “make a scan” or press Start."
    if not suspects:
        return "The scan is in, and nothing is suspected so far." if snap.get("findings") else "The scan is in; I'm still looking at it."
    top = suspects[0]
    kind = ((top.get("fault_kind") or {}).get("choice") or "a fault").replace("_", " ")
    line = f"The most likely problem is {_subject(top)}: {top.get('p_fault_word', 'possibly')} {kind}. That's a suspect, not proven yet."
    if actions:
        line += f" Next: {actions[0].get('instruction', '')}"
    return line


def route(transcript: str, snap: dict) -> tuple[str, list[dict]]:
    """The rule-based answer, used when no assistant key is set or the assistant fails."""
    text = WAKE.sub("", transcript).strip()
    if SCAN.search(text):
        return "Opening a new scan. Walk slowly around the object.", [{"type": "scan"}]
    if REPORT.search(text):
        scans = snap.get("scans", [])
        return ("Opening the report.", [{"type": "report", "scan_id": scans[-1]["scan_id"]}]) if scans else ("There's no report yet.", [])
    if STATUS.search(text):
        return status_reply(snap), []
    return "I can start a scan, tell you what I found, or open the report. Which do you want?", []


SYSTEM = """You are Lea, a repair copilot talking with someone through their iPhone while they look at a physical product.
Your reply is spoken aloud: one to three short sentences, plain words, no lists, no markdown.
Latency-sensitive; begin your visible answer immediately.
Findings and suspects come from the vision pipeline and are candidates, never proof; only a meter reading proves a fault.
Use the suspect's own probability word. Never state voltages, polarity or ratings from appearance.
Safety: only low-voltage products; continuity checks are done unplugged.
Use tools to read what the scan found and to act on the phone (start a scan, open the report)."""

TOOLS = [
    {"name": "session_state", "description": "What this session has: scans, findings, suspects (ranked) and next actions.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "start_scan", "description": "Open a new guided scan on the phone (\"make a render of this\").",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
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


def agent_turn(transcript: str, snap: dict, history: list[tuple[str, str]], client=None) -> tuple[str, list[dict]]:
    """One spoken turn through Claude with tools. Raises on API errors; the caller falls back to `route`."""
    client = client or _anthropic()
    actions: list[dict] = []
    messages: list[dict] = []
    for said, replied in history[-12:]:
        messages += [{"role": "user", "content": said}, {"role": "assistant", "content": replied or "(no reply)"}]
    messages.append({"role": "user", "content": transcript})
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
            elif u.name == "start_scan":
                actions.append({"type": "scan"})
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
    voice = s.require("CARTESIA_VOICE_ID", "voice")
    version = s.cartesia_version
    req = urllib.request.Request("https://api.cartesia.ai/access-token", method="POST",
                                 data=json.dumps({"grants": {"stt": True, "tts": True}, "expires_in": seconds}).encode(),
                                 headers={"Cartesia-Version": version, "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read())
    return {"token": body["token"], "expiresInSeconds": body.get("expires_in", seconds), "version": version, "voiceId": voice}
