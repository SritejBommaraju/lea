"""Voice routes (repairos/voice): Cartesia TTS/STT and the grounded "Ask Lea" answer. Cartesia and the Claude API
are replaced at the HTTP boundary only (httpx2.MockTransport); everything above it runs for real (gateway, auth,
cache, WAV, report lookup, prompt). One live call per endpoint runs when CARTESIA_API_KEY is set (test_live_*)."""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import hashlib
import struct
import sys
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from repairos.api.app import create_app
from repairos.slots.blob_local import LocalBlobStore
from repairos.slots.state_sqlite import SqliteState
from repairos.voice.answer import SYSTEM, report_facts
from repairos.voice.conversation import PERSONA, ConversationStore, SentenceSplitter
from repairos.voice.audio import TTSCache, wav
from repairos.voice.routes import VoiceRuntime
from repairos.voice.settings import CARTESIA_VERSION, SAMPLE_RATE, VoiceSettings

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api_gateway import make_scan, make_still  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[3] / "spec" / "contracts" / "examples"
CODE = "pairing-code-for-voice-tests"
FAKE_KEY = "sk_car_test_only"


class Cartesia:
    """Records what the gateway sent to Cartesia and answers like the documented API."""

    def __init__(self, pcm: bytes = b"\x01\x00" * 2400, transcript: str = "is it safe to open"):
        self.pcm, self.transcript, self.calls = pcm, transcript, []
        self.fail: int | None = None

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        self.calls.append(req)
        if self.fail:
            return httpx2.Response(self.fail, json={"error": "nope"})
        if req.url.path == "/tts/bytes":
            return httpx2.Response(200, content=self.pcm, headers={"Content-Type": "audio/pcm"})
        if req.url.path == "/access-token":
            return httpx2.Response(200, json={"token": "short-lived-token"})
        if req.url.path == "/stt":
            return httpx2.Response(200, json={"type": "transcript", "text": f" {self.transcript} ", "language": "en",
                                              "duration": 1.4})
        return httpx2.Response(404)


class Claude:
    def __init__(self, text: str = "It may be loose at the connector. Unplug it before you open it."):
        self.text, self.bodies = text, []
        # Streamed turns (POST /v1/messages with "stream": true) answer with these deltas, one SSE event each.
        self.deltas = ["The photos suggest the connector ", "may be loose. Unplug it ", "before you open it. ",
                       "Did it stop right after a drop?"]

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        if req.method == "GET":   # the warm-up: GET /v1/models/{id}
            self.warms = getattr(self, "warms", 0) + 1
            return httpx2.Response(200, json={"type": "model", "id": req.url.path.rsplit("/", 1)[-1],
                                              "display_name": "test", "created_at": "2026-01-01T00:00:00Z"})
        self.bodies.append(json.loads(req.content))
        if self.bodies[-1].get("stream"):
            if getattr(self, "on_stream", None):   # something that happens while an answer streams
                self.on_stream()
            return httpx2.Response(200, content=sse(self.bodies[-1]["model"], self.deltas),
                                   headers={"Content-Type": "text/event-stream"})
        return httpx2.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": self.bodies[-1]["model"],
            "content": [{"type": "text", "text": self.text}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 20}})


def sse(model: str, deltas: list[str]) -> bytes:
    """A Messages API event stream carrying `deltas` as text (the documented event sequence)."""
    events = [("message_start", {"type": "message_start", "message": {
        "id": "msg_s", "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None,
        "stop_sequence": None, "usage": {"input_tokens": 50, "output_tokens": 1}}}),
              ("content_block_start", {"type": "content_block_start", "index": 0,
                                       "content_block": {"type": "text", "text": ""}})]
    events += [("content_block_delta", {"type": "content_block_delta", "index": 0,
                                        "delta": {"type": "text_delta", "text": d}}) for d in deltas]
    events += [("content_block_stop", {"type": "content_block_stop", "index": 0}),
               ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                  "usage": {"output_tokens": 30}}),
               ("message_stop", {"type": "message_stop"})]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


def frames(data: bytes) -> list[tuple[str, object]]:
    out, i = [], 0
    while i < len(data):
        kind, n = data[i:i + 1].decode(), int.from_bytes(data[i + 1:i + 5], "big")
        payload = data[i + 5:i + 5 + n]
        out.append((kind, json.loads(payload) if kind == "J" else payload))
        i += 5 + n
    assert i == len(data)
    return out


def report_example(scan_id: str, session_id: str) -> dict:
    r = copy.deepcopy(json.loads((EXAMPLES / "report.json").read_text(encoding="utf-8")))
    r["scan_id"], r["session_id"], r["report_id"] = scan_id, session_id, f"rep_{scan_id}"
    r["safety"] = [{"code": "unplug_first", "text": "Unplug it before opening."}]
    return r


@pytest.fixture
def gw(tmp_path):
    def make(key: str | None = FAKE_KEY, claude_key: str | None = "sk-ant-test", **settings):
        state, blobs = SqliteState(tmp_path / "state.sqlite3"), LocalBlobStore(tmp_path / "blobs")
        app = create_app(state, blobs, pairing_code=CODE, recordings=tmp_path / "sessions")
        cart, claude = Cartesia(), Claude()
        app.state.voice = VoiceRuntime(
            settings=VoiceSettings(_env_file=None, cartesia_api_key=key, **settings), cache=TTSCache(tmp_path / "vc"),
            conversations=ConversationStore(tmp_path / "convos"),
            cartesia_transport=httpx2.MockTransport(cart),
            anthropic_http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(claude)), anthropic_key=claude_key)
        c = TestClient(app)
        tok = c.post("/v1/devices/pair", json={"code": CODE}).json()["token"]
        c.headers["Authorization"] = f"Bearer {tok}"
        c.cart, c.claude, c.state = cart, claude, state
        return c
    return make


def publish_report(c, story="it stopped charging") -> str:
    """A real chain of rows through the gateway and the state store: session, scan package, still, finding, then
    the published report that names that finding (the store's REF/I3/I4 rules all apply)."""
    sid = c.post("/v1/sessions", json={"symptom": story}).json()["session_id"]
    up = Uploader(c)
    pkg = make_scan(up, sid)
    assert c.post("/v1/scans", json=pkg).status_code == 201
    st = make_still(up, pkg)
    assert c.post("/v1/stills", json=st).status_code == 201
    scan_id = pkg["scan_id"]
    f = json.loads((EXAMPLES / "finding.json").read_text(encoding="utf-8"))
    f.update(finding_id="f_example_a", scan_id=scan_id, state="loose")
    f["evidence_refs"]["still_ids"] = [st["still_id"]]
    rep = report_example(scan_id, sid)
    rep["findings"] = [x for x in rep["findings"] if x["finding_id"] == "f_example_a"]
    rep["not_inspected"], rep["insufficient_evidence"] = [], []

    async def rows():
        await c.state.transition("finding", f["finding_id"], "finding.create", f)
        await c.state.transition("report", f"rep_{scan_id}", "report.publish", rep)
    asyncio.run(rows())
    return scan_id


class Uploader:
    """The gateway test helpers' upload(), over the TestClient."""

    def __init__(self, c):
        self.c = c

    def upload(self, data: bytes, content_type="application/octet-stream") -> str:
        r = self.c.put(f"/v1/blobs/{hashlib.sha256(data).hexdigest()}", content=data,
                       headers={"Content-Type": content_type})
        assert r.status_code in (200, 201), r.text
        return r.json()["ref"]


# ------------------------------------------------------------------ status, auth, fallback
def test_needs_device_token(gw):
    c = gw()
    del c.headers["Authorization"]
    assert c.get("/v1/voice/status").status_code == 401
    assert c.post("/v1/voice/tts", json={"text": "hi"}).status_code == 401


def test_status_says_what_the_phone_can_use(gw):
    on = gw().get("/v1/voice/status").json()
    assert on["cartesia"] is True and on["answers"] is True and on["sample_rate"] == SAMPLE_RATE
    assert on["tts_model"] == "sonic-3.6" and on["stt_model"] == "ink-whisper"
    assert FAKE_KEY not in json.dumps(on)
    off = gw(key=None).get("/v1/voice/status").json()
    assert off["cartesia"] is False and off["tts_model"] is None


def test_without_cartesia_key_the_phone_falls_back(gw):
    c = gw(key=None)
    r = c.post("/v1/voice/tts", json={"text": "Move closer here."})
    assert r.status_code == 503 and r.json()["detail"]["error"] == "voice_unavailable"
    assert c.post("/v1/voice/stt", content=b"x", headers={"Content-Type": "audio/m4a"}).status_code == 503
    assert c.cart.calls == []


# ------------------------------------------------------------------ TTS
def test_tts_wav_request_shape_and_cache(gw):
    c = gw()
    r = c.post("/v1/voice/tts", json={"text": "  Move   closer here. "})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert r.headers["x-voice-cache"] == "miss"
    data = r.content
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    assert struct.unpack("<I", data[24:28])[0] == SAMPLE_RATE and struct.unpack("<I", data[40:44])[0] == len(c.cart.pcm)
    req = c.cart.calls[0]
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}" and req.headers["cartesia-version"] == CARTESIA_VERSION
    body = json.loads(req.content)
    assert body["model_id"] == "sonic-3.6" and body["transcript"] == "Move closer here."
    assert body["output_format"] == {"container": "raw", "encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE}
    assert isinstance(body["voice"], str) and body["voice"]
    # Same words again: from the cache, no Cartesia call; the pcm stream also hits it.
    again = c.post("/v1/voice/tts", json={"text": "Move closer here."})
    assert again.content == data and again.headers["x-voice-cache"] == "hit"
    pcm = c.post("/v1/voice/tts", json={"text": "Move closer here.", "format": "pcm"})
    assert pcm.content == c.cart.pcm and pcm.headers["x-sample-rate"] == str(SAMPLE_RATE)
    assert len(c.cart.calls) == 1


def test_tts_pcm_stream_is_whole_samples_and_fills_the_cache(gw):
    c = gw()
    c.cart.pcm = bytes(range(256)) * 37 + b"\x07"   # odd length: the last half sample is dropped
    r = c.post("/v1/voice/tts", json={"text": "All sides covered.", "format": "pcm"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/pcm"
    assert len(r.content) % 2 == 0 and r.content == c.cart.pcm[:-1]
    assert c.post("/v1/voice/tts", json={"text": "All sides covered.", "format": "pcm"}).headers["x-voice-cache"] == "hit"


def test_tts_errors_are_http_errors_without_the_key(gw):
    c = gw()
    c.cart.fail = 401
    for fmt in ("wav", "pcm"):
        r = c.post("/v1/voice/tts", json={"text": "Hello", "format": fmt})
        assert r.status_code == 502 and r.json()["detail"]["status"] == 401
        assert FAKE_KEY not in r.text
    assert c.post("/v1/voice/tts", json={"text": ""}).status_code == 422
    assert c.post("/v1/voice/tts", json={"text": "x" * 601}).status_code == 422


# ------------------------------------------------------------------ STT
def test_stt_sends_the_recording_as_multipart(gw):
    c = gw()
    r = c.post("/v1/voice/stt", content=b"m4a-bytes", headers={"Content-Type": "audio/m4a"})
    assert r.status_code == 200 and r.json()["transcript"] == "is it safe to open"
    req = c.cart.calls[0]
    assert req.url.path == "/stt" and req.headers["content-type"].startswith("multipart/form-data")
    body = req.content
    assert b'name="model"' in body and b"ink-whisper" in body and b'filename="speech.m4a"' in body
    assert b"m4a-bytes" in body
    assert c.post("/v1/voice/stt", content=b"x", headers={"Content-Type": "text/plain"}).status_code == 415
    assert c.post("/v1/voice/stt", content=b"", headers={"Content-Type": "audio/wav"}).status_code == 400


# ------------------------------------------------------------------ Ask Lea
def test_ask_by_voice_is_grounded_in_the_report_and_story(gw):
    c = gw()
    scan_id = publish_report(c)
    r = c.post(f"/v1/voice/ask?scan_id={scan_id}", content=b"rec", headers={"Content-Type": "audio/m4a"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["transcript"] == "is it safe to open" and out["answer_text"] == c.claude.text
    audio = base64.b64decode(out["audio"])
    assert audio[:4] == b"RIFF" and out["audio_format"] == "wav"
    assert set(out["timings_ms"]) == {"stt", "answer", "tts", "total"}
    sent = c.claude.bodies[0]
    assert sent["system"] == SYSTEM and sent["output_config"] == {"effort": "low"}
    assert sent["model"] == "claude-haiku-5-5" and "fallbacks" not in sent
    user = sent["messages"][0]["content"]
    assert "<question>is it safe to open</question>" in user and "<story>it stopped charging</story>" in user
    assert "Unplug it before opening." in user and '"verdict":"potential_issues"' in user
    assert "blob://" not in user and "box_px" not in user
    # STT, then the answer's own TTS: two Cartesia calls.
    assert [q.url.path for q in c.cart.calls] == ["/stt", "/tts/bytes"]


def test_ask_without_inline_audio_answers_text_only(gw):
    c = gw()
    scan_id = publish_report(c)
    out = c.post(f"/v1/voice/ask?scan_id={scan_id}&audio=none", content=b"rec",
                 headers={"Content-Type": "audio/m4a"}).json()
    assert out["audio"] is None and out["answer_text"] == c.claude.text
    assert [q.url.path for q in c.cart.calls] == ["/stt"]


def test_status_warms_the_answer_connection_once(gw):
    c = gw()
    c.get("/v1/voice/status")
    c.get("/v1/voice/status")
    assert getattr(c.claude, "warms", 0) == 1 and c.claude.bodies == []


def test_ask_with_text_and_no_cartesia_returns_text_for_the_phone_to_speak(gw):
    c = gw(key=None)
    scan_id = publish_report(c)
    r = c.post("/v1/voice/ask", json={"scan_id": scan_id, "question": "what's wrong with it"})
    assert r.status_code == 200
    out = r.json()
    assert out["audio"] is None and out["answer_text"] == c.claude.text and out["transcript"] == "what's wrong with it"


def test_ask_errors(gw):
    c = gw()
    assert c.post("/v1/voice/ask", json={"scan_id": "scan_none", "question": "hi"}).status_code == 404
    assert c.post("/v1/voice/ask", json={"question": "hi"}).status_code == 422
    assert c.post("/v1/voice/ask", json={"scan_id": "x", "question": " "}).status_code == 422
    scan_id = publish_report(c)
    c.cart.transcript = ""
    r = c.post(f"/v1/voice/ask?scan_id={scan_id}", content=b"rec", headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200 and "didn't catch" in r.json()["answer_text"] and c.claude.bodies == []
    no_claude = gw(claude_key="")
    no_claude.state = c.state
    r = no_claude.post("/v1/voice/ask", json={"scan_id": scan_id, "question": "hi"})
    assert r.status_code == 503 and r.json()["detail"]["error"] == "answer_unavailable"


def test_report_facts_keep_words_and_drop_refs():
    r = report_example("scan_x", "ses_x")
    facts = report_facts(r)
    text = json.dumps(facts)
    assert "blob://" not in text and "box_px" not in text and "still s1" not in text
    assert facts["verdict"] == "potential_issues" and facts["safety_notes"] == ["Unplug it before opening."]
    top = facts["most_likely_problems"][0]
    assert top["part"] == "connector" and top["issue"] == "loose" and top["proof"].startswith("not measured")
    assert any(e.startswith("what the photo shows:") for e in top["evidence"])


def test_the_persons_guess_reaches_the_prompt_as_words_only():
    r = report_example("scan_x", "ses_x")
    r["user_lead"] = {"area": "the power cord", "status": "not_supported", "lead_id": "lead_1",
                      "seen": "the photos show the cord intact", "crop_ref": "blob://sha256:" + "0" * 64}
    facts = report_facts(r)["what_the_person_suspected"]
    assert facts == {"area": "the power cord", "status": "not_supported", "seen": "the photos show the cord intact"}
    assert "never agree just to please" in PERSONA.replace("\n", " ") and "never agree just to please" in SYSTEM


def test_no_problems_seen_keeps_its_caveat():
    r = report_example("scan_x", "ses_x")
    r["verdict"] = "no_problems_seen"
    r["verdict_reason"] = {"text": "Nothing looked wrong.", "caveat": "The inside was not seen in still s3."}
    facts = report_facts(r)
    assert facts["verdict"] == "no_problems_seen" and facts["verdict_caveat"] == "The inside was not seen in a photo."
    assert "no_problems_seen" in PERSONA and "no_problems_seen" in SYSTEM


def test_wav_header_matches_samples():
    w = wav(b"\x00\x01" * 10 + b"\x05", 16000)
    assert len(w) == 44 + 20 and struct.unpack("<I", w[40:44])[0] == 20 and struct.unpack("<I", w[24:28])[0] == 16000


# ------------------------------------------------------------------ conversation
def converse(c, scan_id=None, audio=True, mode="stream"):
    q = f"?audio={mode}" + (f"&scan_id={scan_id}" if scan_id else "")
    if audio:
        r = c.post("/v1/voice/converse" + q, content=b"rec", headers={"Content-Type": "audio/m4a"})
    else:
        r = c.post("/v1/voice/converse" + q, json={"text": "what should I check first"})
    assert r.status_code == 200, r.text
    return frames(r.content)


def test_converse_streams_sentences_then_their_audio_and_keeps_history(gw):
    c = gw()
    scan_id = publish_report(c)
    fr = converse(c, scan_id)
    assert fr[0] == ("J", {"type": "transcript", "text": "is it safe to open"})
    events = [p for k, p in fr if k == "J"]
    sentences = [e["text"] for e in events if e["type"] == "sentence"]
    assert sentences == ["The photos suggest the connector may be loose.", "Unplug it before you open it.",
                         "Did it stop right after a drop?"]
    # Each sentence event is followed by its audio frames before the next sentence.
    kinds = "".join(k for k, _ in fr)
    assert kinds.startswith("JJA") and kinds.count("A") == 3 and kinds.endswith("J")
    done = events[-1]
    assert done["type"] == "done" and done["answer_text"] == " ".join(sentences) and done["turns"] == 1
    assert {"stt", "first_sentence", "first_audio", "total"} <= set(done["timings_ms"])
    tts = [json.loads(q.content)["transcript"] for q in c.cart.calls if q.url.path == "/tts/bytes"]
    assert tts == sentences
    sent = c.claude.bodies[0]
    assert sent["system"] == [{"type": "text", "text": PERSONA, "cache_control": {"type": "ephemeral"}}]
    assert sent["stream"] is True
    first = sent["messages"][0]["content"]
    assert "<scan>" in first and "<story>it stopped charging</story>" in first and first.endswith("is it safe to open")
    # Second turn: the history goes back with it, grounded once at the start.
    c.cart.transcript = "no it just stopped"
    converse(c, scan_id)
    msgs = c.claude.bodies[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"] and msgs[2]["content"] == "no it just stopped"
    assert msgs[1]["content"] == done["answer_text"] and "<scan>" in msgs[0]["content"]
    hist = c.get(f"/v1/voice/conversation?scan_id={scan_id}").json()["turns"]
    assert [t["role"] for t in hist] == ["user", "assistant", "user", "assistant"]
    assert c.delete(f"/v1/voice/conversation?scan_id={scan_id}").json() == {"turns": []}


def test_converse_action_marker_is_an_event_not_speech(gw):
    c = gw()
    scan_id = publish_report(c)
    c.claude.deltas = ["I can guide you to a closer photo of that spot. <continue", "_scan/>"]
    fr = converse(c, scan_id, audio=False, mode="none")
    events = [p for k, p in fr if k == "J"]
    assert [e["text"] for e in events if e["type"] == "sentence"] == ["I can guide you to a closer photo of that spot."]
    assert {"type": "action", "action": "continue_scan", "label": "Take a closer photo"} in events
    assert all(k == "J" for k, _ in fr) and not any(q.url.path == "/tts/bytes" for q in c.cart.calls)
    assert all(e.get("audio") is False for e in events if e["type"] == "sentence")


def test_converse_walkthrough_marker_starts_the_tour_on_the_phone(gw):
    c = gw()
    scan_id = publish_report(c)
    c.claude.deltas = ["Sure, I'll walk you through it now. <walk", "through/>"]
    events = [p for k, p in converse(c, scan_id, audio=False, mode="none") if k == "J"]
    assert [e["text"] for e in events if e["type"] == "sentence"] == ["Sure, I'll walk you through it now."]
    assert {"type": "action", "action": "walkthrough", "label": "Walk me through it"} in events
    assert events[-1]["type"] == "done" and events[-1]["action"] == "walkthrough"
    # The prompt says when to use it (a walkthrough, a breakdown, the full analysis) and that the phone tours it.
    persona = " ".join(PERSONA.split())
    assert "<walkthrough/>" in persona and "breakdown" in persona and "full analysis" in persona


def test_converse_walkthrough_needs_a_report(gw):
    c = gw()
    c.claude.deltas = ["I'll walk you through it. <walkthrough/>"]
    events = [p for k, p in converse(c, None, audio=False, mode="none") if k == "J"]
    assert not any(e["type"] == "action" for e in events)
    assert events[-1]["action"] is None


def test_converse_without_a_scan_is_a_home_conversation(gw):
    c = gw()
    converse(c, None, audio=False)
    first = c.claude.bodies[0]["messages"][0]["content"]
    assert "no report yet" in first and "<story>not given</story>" in first
    assert c.get("/v1/voice/conversation").json()["turns"][0]["text"] == "what should I check first"


def test_converse_during_a_scan_sees_the_screen_and_never_offers_a_scan(gw):
    import urllib.parse

    from repairos.voice.conversation import parse_live
    c = gw()
    live = {"screen": "scan", "stage": "scanning", "on-screen cue": "Move closer", "progress": "40%"}
    c.claude.deltas = ["Get the back where the cord goes in. <start", "_scan/>"]
    q = "?audio=none&live=" + urllib.parse.quote(json.dumps(live))
    r = c.post("/v1/voice/converse" + q, json={"text": "should I get the back"})
    events = [p for k, p in frames(r.content) if k == "J"]
    assert not [e for e in events if e["type"] == "action"]                    # a scan is open: no second one
    asked = c.claude.bodies[0]["messages"][-1]["content"]
    assert "<phone>screen: scan; stage: scanning; on-screen cue: Move closer; progress: 40%</phone>" in asked
    assert c.get("/v1/voice/conversation").json()["turns"][0]["text"] == "should I get the back"   # not stored
    # On the home screen the same marker is still an offer.
    home = c.post("/v1/voice/converse?audio=none", json={"text": "it stopped spinning"})
    assert {"type": "action", "action": "start_scan", "label": "Start a scan"} in [p for k, p in frames(home.content) if k == "J"]
    assert parse_live("not json") == {} and parse_live('["a"]') == {}
    assert len(parse_live(json.dumps({f"k{i}": "v" for i in range(20)}))) == 16


def test_before_a_scan_lea_knows_the_product_the_label_photo_identified(gw):
    import asyncio

    c = gw()
    sid = c.post("/v1/sessions", json={"symptom": "it stopped spinning"}).json()["session_id"]
    asyncio.run(c.state.put("story", {"session_id": sid, "said": [], "brief": {
        "status": "done", "name": "Desk fan 40", "brand": "Breeze", "model": "DF-40",
        "known_issues": [{"area": "speed switch", "why": "Owners report it wears out."}]}}))
    c.post(f"/v1/voice/converse?audio=none&scan_id=scan_future&session_id={sid}", json={"text": "what is it again"})
    first = c.claude.bodies[0]["messages"][0]["content"]
    assert '"name": "Desk fan 40"' in first and '"brand": "Breeze"' in first and "speed switch" in first
    assert "<story>it stopped spinning</story>" in first
    # Without the session id the future scan has nothing to ground on.
    c.post("/v1/voice/converse?audio=none&scan_id=scan_other", json={"text": "what is it"})
    assert "not identified" in c.claude.bodies[1]["messages"][0]["content"]


def test_marks_are_parsed_out_of_speech_and_never_split():
    from repairos.voice.conversation import SentenceSplitter
    sp = SentenceSplitter()
    out = sp.feed("The cord enters at the back. Look at the strain relief there. <mark x=\"0.4")
    out += sp.feed('2" y="0.61" label="cord entry. back"/><mark x="1.7" y="0.2" label="off photo"/>')
    out += sp.flush()
    assert out == ["The cord enters at the back.", "Look at the strain relief there."]
    assert sp.marks == [{"x": 0.42, "y": 0.61, "label": "cord entry. back"}]   # 1.7 is outside the photo


def test_a_look_turn_sends_the_photo_and_returns_marks_but_keeps_words(gw):
    import urllib.parse
    c = gw()
    jpeg = b"\xff\xd8\xff\xe0" + b"look" * 50
    ref = c.app.state.blobs.put(jpeg, "image/jpeg")
    c.claude.deltas = ["That looks like the cord entry. ", 'Get a close-up there. <mark x="0.42" y="0.61" label="cord entry"/>']
    live = urllib.parse.quote(json.dumps({"screen": "scan"}))
    r = c.post(f"/v1/voice/converse?audio=none&live={live}&image={urllib.parse.quote(ref)}", json={"text": "what is this"})
    events = [p for k, p in frames(r.content) if k == "J"]
    assert {"type": "mark", "x": 0.42, "y": 0.61, "label": "cord entry"} in events
    assert [e["text"] for e in events if e["type"] == "sentence"] == ["That looks like the cord entry.", "Get a close-up there."]
    last = c.claude.bodies[0]["messages"][-1]["content"]
    assert last[0]["type"] == "image" and last[0]["source"]["media_type"] == "image/jpeg"
    assert last[1]["type"] == "text" and last[1]["text"].endswith("what is this")
    turns = c.get("/v1/voice/conversation").json()["turns"]
    assert turns[0]["text"] == "what is this" and "mark" not in turns[1]["text"]   # words only
    # No photo: markers are ignored (nothing to point at), and a non-JPEG blob is not sent.
    png = c.app.state.blobs.put(b"\x89PNG" + b"x" * 40, "image/png")
    r = c.post(f"/v1/voice/converse?audio=none&image={urllib.parse.quote(png)}", json={"text": "and this"})
    assert not [p for k, p in frames(r.content) if k == "J" and p["type"] == "mark"]
    assert isinstance(c.claude.bodies[1]["messages"][-1]["content"], str)


def _jpeg(c, tag: bytes) -> str:
    return c.app.state.blobs.put(b"\xff\xd8\xff\xe0" + tag * 40, "image/jpeg")


def test_a_look_turn_takes_up_to_three_photos_the_last_is_now_and_uses_the_look_model(gw):
    import base64
    import urllib.parse
    c = gw(lea_voice_look_model="claude-sonnet-5-5")
    refs = [_jpeg(c, t) for t in (b"old1", b"old2", b"now!")]
    q = "&".join(f"image={urllib.parse.quote(r)}" for r in refs)
    c.post(f"/v1/voice/converse?audio=none&{q}", json={"text": "which one is the fuse"})
    body = c.claude.bodies[0]
    assert body["model"] == "claude-sonnet-5-5"
    last = body["messages"][-1]["content"]
    assert [b["type"] for b in last] == ["text", "image", "text", "image", "text", "image", "text"]
    assert last[4]["text"].startswith("Now") and base64.b64decode(last[5]["source"]["data"]).endswith(b"now!")
    assert last[0]["text"].startswith("A few seconds ago") and last[-1]["text"].endswith("which one is the fuse")
    # A turn without photos keeps the answer model; more than three photos is refused.
    c.post("/v1/voice/converse?audio=none", json={"text": "thanks"})
    assert c.claude.bodies[1]["model"] == "claude-haiku-5-5"
    four = "&".join(f"image={urllib.parse.quote(r)}" for r in refs + [_jpeg(c, b"more")])
    assert c.post(f"/v1/voice/converse?audio=none&{four}", json={"text": "and"}).status_code == 422
    # Refs that are not blob refs are ignored (the turn goes on with the good ones).
    c.post(f"/v1/voice/converse?audio=none&image=file%3A%2F%2Fx&image={urllib.parse.quote(refs[2])}", json={"text": "x"})
    assert [b["type"] for b in c.claude.bodies[2]["messages"][-1]["content"]] == ["image", "text"]


def test_looking_turns_are_capped_per_scan(gw):
    import urllib.parse
    c = gw(lea_voice_look_turns_per_scan=1)
    ref = urllib.parse.quote(_jpeg(c, b"cap"))
    c.post(f"/v1/voice/converse?audio=none&scan_id=scan_cap&image={ref}", json={"text": "what is this"})
    c.post(f"/v1/voice/converse?audio=none&scan_id=scan_cap&image={ref}", json={"text": "and this"})
    assert isinstance(c.claude.bodies[0]["messages"][-1]["content"], list)
    second = c.claude.bodies[1]["messages"][-1]["content"]
    assert isinstance(second, str) and "looking limit" in second and second.endswith("and this")
    # Another scan has its own allowance.
    c.post(f"/v1/voice/converse?audio=none&scan_id=scan_other&image={ref}", json={"text": "what is this"})
    assert isinstance(c.claude.bodies[2]["messages"][-1]["content"], list)


def test_measure_marker_asks_the_phone_to_measure_only_on_a_look_turn(gw):
    import urllib.parse
    from repairos.voice.conversation import SentenceSplitter
    sp = SentenceSplitter()
    out = sp.feed('I\'ll measure that for you. <measure x1="0.3" y1="0.4')
    out += sp.feed('" x2="0.62" y2="0.44"/><measure x1="2" y1="0" x2="0" y2="0"/>') + sp.flush()
    assert out == ["I'll measure that for you."] and sp.measure == {"x1": 0.3, "y1": 0.4, "x2": 0.62, "y2": 0.44}
    c = gw()
    c.claude.deltas = ['I\'ll measure it. <measure x1="0.1" y1="0.2" x2="0.3" y2="0.4"/>']
    ref = urllib.parse.quote(_jpeg(c, b"ms"))
    events = [p for k, p in frames(c.post(f"/v1/voice/converse?audio=none&image={ref}",
                                          json={"text": "how wide is it"}).content) if k == "J"]
    assert {"type": "measure", "x1": 0.1, "y1": 0.2, "x2": 0.3, "y2": 0.4} in events
    events = [p for k, p in frames(c.post("/v1/voice/converse?audio=none", json={"text": "how wide"}).content)
              if k == "J"]
    assert not [e for e in events if e["type"] == "measure"]   # no photo: nothing to measure on


def test_a_look_turn_during_a_scan_hears_what_was_inspected_so_far(gw):
    import urllib.parse
    c = gw()
    sid = c.post("/v1/sessions", json={"symptom": "it rattles"}).json()["session_id"]
    up = Uploader(c)
    pkg = make_scan(up, sid)
    assert c.post("/v1/scans", json=pkg).status_code == 201
    st = make_still(up, pkg)
    assert c.post("/v1/stills", json=st).status_code == 201
    f = json.loads((EXAMPLES / "finding.json").read_text(encoding="utf-8"))
    f.update(finding_id="f_live_a", scan_id=pkg["scan_id"], state="loose", part_words="the cord's strain relief")
    f["evidence_refs"]["still_ids"] = [st["still_id"]]
    asyncio.run(c.state.transition("finding", f["finding_id"], "finding.create", f))
    ref = urllib.parse.quote(_jpeg(c, b"insp"))
    c.post(f"/v1/voice/converse?audio=none&scan_id={pkg['scan_id']}&image={ref}", json={"text": "anything yet"})
    asked = c.claude.bodies[0]["messages"][-1]["content"][-1]["text"]
    assert "inspected so far: the cord's strain relief: loose" in asked
    # Words-only turns stay lean (no store read for them).
    c.post(f"/v1/voice/converse?audio=none&scan_id={pkg['scan_id']}", json={"text": "ok"})
    assert "inspected so far" not in c.claude.bodies[1]["messages"][-1]["content"]


def test_persona_keeps_looking_turns_honest():
    p = " ".join(PERSONA.split())
    for words in ("blurry, too dark", "move closer", "never guess", "polarity", "from its colour",
                  "Repeat a number only as given", "never work out, estimate or compare", "the last one is what it shows now",
                  "<measure", "Count only what you can clearly see"):
        assert words in p, words


def test_converse_turn_caps(gw):
    c = gw(lea_voice_turns_per_minute=1)
    scan_id = publish_report(c)
    converse(c, scan_id)
    events = [p for k, p in converse(c, scan_id) if k == "J"]
    assert [e["text"] for e in events if e["type"] == "sentence"] == ["Give me a moment, then ask again."]
    assert len(c.claude.bodies) == 1
    c2 = gw(lea_voice_turns_per_scan=1)
    c2.state = c.state
    converse(c2, scan_id)
    events = [p for k, p in converse(c2, scan_id) if k == "J"]
    assert "Start a new scan" in events[-1]["answer_text"]


def test_converse_held_answer_is_remembered_only_once_committed(gw):
    """An early answer (Ink-2's eager end, `held=1`) is kept aside (Greptile, PR #21): one the phone drops
    (`turn.resume`) never reaches later answers or the caps; one the person's final words confirm is committed."""
    c = gw(lea_voice_turns_per_minute=2)   # the dropped answer counting would refuse the confirmed one below
    scan_id = publish_report(c)

    def held(text):
        r = c.post(f"/v1/voice/converse?audio=none&held=1&scan_id={scan_id}", json={"text": text})
        assert r.status_code == 200, r.text
        done = [p for k, p in frames(r.content) if k == "J"][-1]
        assert done["type"] == "done" and done["held_id"]
        return done["held_id"]

    def commit(held_id):
        r = c.post(f"/v1/voice/conversation/commit?scan_id={scan_id}", json={"held_id": held_id})
        assert r.status_code == 200, r.text
        return r.json()

    def history():
        return [t["text"] for t in c.get(f"/v1/voice/conversation?scan_id={scan_id}").json()["turns"]
                if t["role"] == "user"]

    dropped = held("is it the fuse")
    assert history() == []
    converse(c, scan_id, audio=False, mode="none")   # the words the person actually finished with
    assert len(c.claude.bodies[1]["messages"]) == 1   # the dropped exchange is not in the answer's context...
    assert history() == ["what should I check first"]   # ...nor in the history, nor the per-minute cap
    assert commit(dropped) == {"committed": False, "turns": 1}   # replaced: it can no longer be committed

    confirmed = held("is it the fuse")
    assert commit(confirmed) == {"committed": True, "turns": 2}
    assert history() == ["what should I check first", "is it the fuse"]
    assert commit(confirmed) == {"committed": False, "turns": 2}   # once


def test_a_commit_while_another_answer_streams_is_kept(gw):
    """The phone commits a confirmed early answer while the person's next question is already streaming (Greptile
    P1 on #21): the streaming answer is saved onto the conversation as it is then, so the committed exchange stays."""
    c = gw()
    scan_id = publish_report(c)
    r = c.post(f"/v1/voice/converse?audio=none&held=1&scan_id={scan_id}", json={"text": "is it the fuse"})
    held_id = [p for k, p in frames(r.content) if k == "J"][-1]["held_id"]

    def commit_now():   # the commit lands between this answer's start and its save
        c.claude.on_stream = None
        assert c.post(f"/v1/voice/conversation/commit?scan_id={scan_id}", json={"held_id": held_id}).json()["committed"]
    c.claude.on_stream = commit_now
    converse(c, scan_id, audio=False, mode="none")
    hist = [t["text"] for t in c.get(f"/v1/voice/conversation?scan_id={scan_id}").json()["turns"] if t["role"] == "user"]
    assert hist == ["is it the fuse", "what should I check first"]


def test_the_next_turn_carries_a_commit_that_failed(gw):
    """The phone's commit request failed (Greptile P1 on #26): the next turn carries the id (`commit=`), the confirmed
    exchange joins before that answer, so it is in the answer's context and kept; a repeated id does nothing."""
    c = gw()
    scan_id = publish_report(c)
    r = c.post(f"/v1/voice/converse?audio=none&held=1&scan_id={scan_id}", json={"text": "is it the fuse"})
    held_id = [p for k, p in frames(r.content) if k == "J"][-1]["held_id"]
    r = c.post(f"/v1/voice/converse?audio=none&commit={held_id}&scan_id={scan_id}", json={"text": "what should I check first"})
    assert r.status_code == 200, r.text
    assert [m["role"] for m in c.claude.bodies[-1]["messages"]] == ["user", "assistant", "user"]
    r = c.post(f"/v1/voice/converse?audio=none&commit={held_id}&scan_id={scan_id}", json={"text": "and then"})
    hist = [t["text"] for t in c.get(f"/v1/voice/conversation?scan_id={scan_id}").json()["turns"] if t["role"] == "user"]
    assert hist == ["is it the fuse", "what should I check first", "and then"]


def test_sentence_splitter():
    sp = SentenceSplitter()
    assert sp.feed("Hi. It looks") == []          # "Hi." is too short to stand alone
    assert sp.feed(" bent at 1.5 cm. Unplug") == ["Hi. It looks bent at 1.5 cm."]
    assert sp.feed(" it first!") == []            # the end of the text so far may continue
    assert sp.feed(" Then") == ["Unplug it first!"]
    assert sp.flush() == ["Then"]


# ------------------------------------------------------------------ realtime token
def test_token_is_short_lived_and_keeps_the_key_on_the_gateway(gw):
    c = gw()
    r = c.post("/v1/voice/token")
    assert r.status_code == 200
    out = r.json()
    assert out["token"] == "short-lived-token" and out["expires_in"] == 300 and out["stt_model"] == "ink-2"
    assert out["keyterms"] == ["Lea", "Leah"] and FAKE_KEY not in r.text
    req = c.cart.calls[-1]
    assert req.url.path == "/access-token" and req.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert json.loads(req.content) == {"grants": {"stt": True, "tts": True}, "expires_in": 300}
    assert c.post("/v1/voice/stream?kind=stt").json() == {"ok": True}
    assert gw(key=None).post("/v1/voice/token").status_code == 503


def test_token_and_streams_go_through_the_spend_guard(gw, spend):
    c = gw()
    c.post("/v1/voice/token")
    c.post("/v1/voice/stream?kind=tts")
    assert [s for s, _ in spend.calls] == ["cartesia", "cartesia"]
    spend.blocked.add("cartesia")
    assert c.post("/v1/voice/token").json()["error"] == "voice_paused"
    assert c.post("/v1/voice/stream?kind=stt").json()["error"] == "voice_paused"
    assert sum(1 for q in c.cart.calls if q.url.path == "/access-token") == 1


# ------------------------------------------------------------------ spend guard
class SpendLimit(RuntimeError):
    def __init__(self, service):
        self.service, self.reason, self.retry_after_s = service, "over the limit", 120.0
        super().__init__(service)


@pytest.fixture
def spend(monkeypatch):
    """repairos.spend (owned elsewhere) with its documented API: charge(service, units, *, usd, key) raises
    SpendLimit before a call that would go over."""
    import types
    mod = types.ModuleType("repairos.spend")
    mod.SpendLimit, mod.blocked, mod.calls = SpendLimit, set(), []

    def charge(service, units=1.0, *, usd=None, key=None):
        mod.calls.append((service, key))
        if service in mod.blocked:
            raise SpendLimit(service)
    mod.charge = charge
    monkeypatch.setitem(sys.modules, "repairos.spend", mod)
    import repairos
    monkeypatch.setattr(repairos, "spend", mod, raising=False)
    return mod


def test_spend_guard_is_checked_before_paid_calls_and_cache_hits_are_free(gw, spend):
    c = gw()
    c.post("/v1/voice/tts", json={"text": "Move closer here."})
    c.post("/v1/voice/tts", json={"text": "Move closer here."})
    assert [s for s, _ in spend.calls] == ["cartesia"]
    spend.blocked.add("cartesia")
    r = c.post("/v1/voice/tts", json={"text": "Something new.", "format": "pcm"})
    assert r.status_code == 503 and r.json()["error"] == "voice_paused" and r.headers["retry-after"] == "120"
    assert c.post("/v1/voice/stt", content=b"x", headers={"Content-Type": "audio/m4a"}).json()["error"] == "voice_paused"
    assert c.post("/v1/voice/tts", json={"text": "Move closer here."}).status_code == 200   # cached: still free
    assert not any(q.url.path == "/tts/bytes" and json.loads(q.content)["transcript"] == "Something new."
                   for q in c.cart.calls)


def test_paused_services_fall_back_to_spoken_lines(gw, spend):
    c = gw()
    scan_id = publish_report(c)
    spend.blocked.add("anthropic")
    out = c.post("/v1/voice/ask", json={"scan_id": scan_id, "question": "hi"}).json()
    assert out["answer_text"].startswith("Lea is paused")
    events = [p for k, p in converse(c, scan_id) if k == "J"]
    assert events[-1]["answer_text"].startswith("Lea is paused") and c.claude.bodies == []
    spend.blocked = {"cartesia"}
    fr = converse(c, scan_id, audio=False)
    events = [p for k, p in fr if k == "J"]
    assert {"type": "audio_off"} in events and not any(k == "A" for k, _ in fr)
    assert len([e for e in events if e["type"] == "sentence"]) == 3


def test_voice_claude_answers_record_their_measured_token_cost(gw):
    """Greptile (PR #13): voice answers were charged at the shared estimate only. Each one now replaces it with the
    cost of its own token counts (repairos.spend, the real module with this test's own counter file), priced by the
    model that answered."""
    from repairos import spend as real_spend

    def usd_day() -> float:   # unrounded (usage() rounds to 4 places; one short answer is about 2e-5 USD)
        c, _ = real_spend._conn()
        return real_spend._window(c, "anthropic", real_spend._now())["usd_day"]

    c = gw()
    scan_id = publish_report(c)
    price = real_spend.caps("anthropic")["usd_per_mtok"]["claude-haiku-5-5"]
    assert usd_day() == 0
    c.post("/v1/voice/ask", json={"scan_id": scan_id, "question": "is it safe to open"})
    ask = (100 * price["input"] + 20 * price["output"]) / 1e6     # the Claude mock's usage for a message
    assert usd_day() == pytest.approx(ask)            # not the 0.006 USD estimate
    n = len(c.claude.bodies)
    converse(c, scan_id, audio=False)
    streams = len([b for b in c.claude.bodies[n:] if b.get("stream")])
    turn = (50 * price["input"] + 30 * price["output"]) / 1e6    # a streamed turn: message_start + message_delta
    assert streams >= 1
    assert usd_day() == pytest.approx(ask + streams * turn)
    assert real_spend.estimate_usd("anthropic") < real_spend.caps("anthropic")["usd_per_call"]   # learnt, not stuck


# ------------------------------------------------------------------ live (only with a real key)
LIVE_KEY = VoiceSettings().cartesia_key
live = pytest.mark.skipif(not LIVE_KEY or os.environ.get("REPAIROS_VOICE_LIVE") == "0",
                          reason="CARTESIA_API_KEY not set (backend/.env or environment)")


@live
def test_live_tts_and_stt(tmp_path):
    from repairos.voice.cartesia import Cartesia as Real

    async def go():
        cart = Real(VoiceSettings())
        try:
            pcm = await cart.tts("Move closer here.")
            assert len(pcm) > SAMPLE_RATE // 2   # more than a quarter second of audio
            tr = await cart.stt(wav(pcm, SAMPLE_RATE), "speech.wav", "audio/wav")
            assert "closer" in tr.text.lower()
        finally:
            await cart.aclose()
    asyncio.run(go())


# ------------------------------------------------------------------ report turns: evidence, the 3D model, search
def publish_scan_with_model(c, *, object_words=None, story="the X button sticks", scan_id: str | None = None) -> str:
    """A published report on a scan Lea can look at: a dense coloured cloud (a plane 30 cm in front of the still's
    camera), a decodable still with that pose, and two findings: f_example_a (not located) and f_example_b (located,
    no issue)."""
    import io

    import numpy as np
    from PIL import Image

    from repairos.importers.ply import ply_bytes
    from repairos.locate.camera import Pose

    sid = c.post("/v1/sessions", json={"symptom": story}).json()["session_id"]
    up = Uploader(c)
    st_example = json.loads((EXAMPLES / "still.json").read_text(encoding="utf-8"))
    pose = Pose.from_contract(st_example["camera"])
    g = np.linspace(-0.08, 0.08, 120)
    xx, yy = np.meshgrid(g, g)
    local = np.stack([xx.ravel(), yy.ravel(), np.full(xx.size, -0.3)], axis=1)      # camera frame: 30 cm ahead
    xyz = pose.to_world(local).astype(np.float32)
    rgb = np.tile(np.array([[60, 160, 90]], np.uint8), (len(xyz), 1))
    pkg = make_scan(up, sid)
    if scan_id:
        pkg["scan_id"] = scan_id
    pkg["points_file"] = up.upload(ply_bytes(xyz, rgb))
    pkg["point_count"] = len(xyz)
    pkg.pop("crop_box", None)
    assert c.post("/v1/scans", json=pkg).status_code == 201
    st = make_still(up, pkg)
    buf = io.BytesIO()
    Image.new("RGB", (st["width_px"], st["height_px"]), (90, 90, 100)).save(buf, "JPEG")
    st["image_file"] = up.upload(buf.getvalue(), "image/jpeg")
    st.pop("segment", None)
    if scan_id:
        st["still_id"] = f"{scan_id}_s7"
    assert c.post("/v1/stills", json=st).status_code == 201
    scan_id = pkg["scan_id"]
    f = json.loads((EXAMPLES / "finding.json").read_text(encoding="utf-8"))
    tag = "" if scan_id == "scan_7f3a" else "_" + scan_id[-6:]
    f.update(finding_id="f_example_a" + tag, scan_id=scan_id, state="loose")
    f["evidence_refs"]["still_ids"] = [st["still_id"]]
    fb = {**copy.deepcopy(f), "finding_id": "f_example_b" + tag, "state": "ok"}
    rep = report_example(scan_id, sid)
    rep["findings"][1]["location"]["point_m"] = [float(v) for v in xyz[len(xyz) // 2 + 60]]
    for row, fid in zip(rep["findings"], (f["finding_id"], fb["finding_id"])):
        row["finding_id"] = fid
    rep["most_likely"] = [x if not isinstance(x, str) else f["finding_id"] for x in rep.get("most_likely") or []]
    rep["not_inspected"], rep["insufficient_evidence"] = [], []
    if object_words:
        rep["object"] = object_words

    async def rows():
        await c.state.transition("finding", f["finding_id"], "finding.create", f)
        await c.state.transition("finding", fb["finding_id"], "finding.create", fb)
        await c.state.transition("report", f"rep_{scan_id}", "report.publish", rep)
    asyncio.run(rows())
    return scan_id


def streamed(c) -> list[dict]:
    """The streamed answer requests (the part index is a separate, non-streamed call)."""
    return [b for b in c.claude.bodies if b.get("stream")]


def ask(c, scan_id, text, live=None):
    import urllib.parse

    q = f"?audio=none&scan_id={scan_id}" + (f"&live={urllib.parse.quote(json.dumps(live))}" if live else "")
    r = c.post("/v1/voice/converse" + q, json={"text": text})
    assert r.status_code == 200, r.text
    return [p for k, p in frames(r.content) if k == "J"]


def test_stage_markers_are_parsed_out_of_speech():
    sp = SentenceSplitter()
    out = sp.feed('<show part="f_ab12"/>The battery cover is on the back. <view side="Top"/>Here is the top. ')
    out += sp.feed('<zoom dir="in"/><point photo="s4" x="0.41" y="0.55" label="X button"/>The X button is here. ')
    out += sp.feed('<view side="sideways"/><zoom dir="far"/><point photo="s4" x="1.4" y="0.5"/><show part="a b"/>Done.')
    out += sp.flush()
    assert out == ["The battery cover is on the back.", "Here is the top.", "The X button is here.", "Done."]
    assert sp.stage == [{"type": "show", "part": "f_ab12"}, {"type": "view", "side": "top"},
                        {"type": "zoom", "dir": "in"},
                        {"type": "point", "photo": "s4", "x": 0.41, "y": 0.55, "label": "X button"}]


def test_voice_facts_leave_out_counts_and_coverage():
    from repairos.voice.conversation import voice_facts
    r = report_example("scan_x", "ses_x")
    r["verdict_reason"] = {"text": "Nothing looked wrong.", "caveat": "19 areas need a closer photo."}
    r["capture"] = {"stills": 20, "stills_inspected": 7, "coverage_words": "70 percent"}
    r["limits"] = ["the bottom was not scanned"]
    facts = voice_facts(r)
    assert "photos" not in facts and "limits" not in facts and "verdict_caveat" not in facts
    assert "summary" not in facts and "verdict_reason" not in facts and facts["verdict"] == r["verdict"]
    r["insufficient_evidence"] = [{"part_class": "face button", "reason": "the photo was too blurry to judge"}]
    assert voice_facts(r)["could_not_judge"] == ["face button"]          # which part, not the photo complaint
    assert "blurry" not in json.dumps(voice_facts(r))


def test_located_parts_list_every_located_finding_without_numbers():
    from repairos.voice.conversation import located_parts, part_rows
    r = report_example("scan_x", "ses_x")
    r["findings"].append({**copy.deepcopy(r["findings"][1]), "finding_id": "f_old"})
    r["findings"][1]["superseded_ids"] = ["f_old"]
    rows = part_rows(r)
    assert [x["id"] for x in rows] == ["f_example_b"] and rows[0]["photos_showed"] == "no issue"
    assert rows[0]["point"] == [0.1, 0.02, -0.3] and rows[0]["still_id"] == "s1"
    seen = located_parts(r)
    assert seen == [{"id": "f_example_b", "part": "capacitor", "where": rows[0].get("where"),
                     "photos_showed": "no issue"}] or (seen[0]["id"] == "f_example_b" and "point" not in seen[0])
    assert "0.02" not in json.dumps(seen) and "s1" not in json.dumps(seen)


def test_report_turn_sees_photos_and_views_and_moves_the_model(gw):
    c = gw()
    scan_id = publish_scan_with_model(c)
    c.claude.deltas = ['<show part="f_example_b"/>The capacitor sits ', 'on the board. <view side="top"/>Here is the top. ',
                       '<show part="f_nope"/><zoom dir="in"/>Closer now.']
    events = ask(c, scan_id, "where is the capacitor")
    kinds = [(e["type"], e.get("text") or e.get("part") or e.get("side") or e.get("dir")) for e in events
             if e["type"] in ("sentence", "show", "view", "zoom")]
    assert kinds == [("sentence", "The capacitor sits on the board."), ("show", "f_example_b"),
                     ("sentence", "Here is the top."), ("view", "top"), ("sentence", "Closer now."), ("zoom", "in")]
    photo = next(e for e in events if e["type"] == "photo")      # the real photo of the spot, ringed
    assert photo["for"] == "f_example_b"
    got = c.get(f"/v1/blobs/{photo['crop']}")
    assert got.status_code == 200 and got.content[:3] == b"\xff\xd8\xff"
    body = streamed(c)[0]
    first = body["messages"][0]["content"]
    first = first if isinstance(first, str) else next(b["text"] for b in first if b["type"] == "text" and "<scan>" in b["text"])
    parts = json.loads(first.split("<parts>")[1].split("</parts>")[0])
    assert [p["id"] for p in parts] == ["f_example_b"] and "point" not in parts[0]
    last = body["messages"][-1]["content"]
    captions = [b["text"] for b in last if b["type"] == "text"]
    assert any(t.startswith("Photo s7") for t in captions) and any(t.startswith("View v1") for t in captions)
    assert sum(1 for b in last if b["type"] == "image") <= 3 and body["model"] == "claude-haiku-5-5"
    hist = c.get(f"/v1/voice/conversation?scan_id={scan_id}").json()["turns"]
    assert "<" not in hist[1]["text"]                             # markers never reach the words


def test_a_point_marker_lands_on_the_model_and_a_repeat_is_a_lookup(gw):
    c = gw()
    scan_id = publish_scan_with_model(c)
    c.claude.deltas = ['<point photo="s7" x="0.5" y="0.5" label="X button"/>The X button is right here. ',
                       '<point photo="s9" x="0.5" y="0.5" label="ghost"/>That one is not in the photos.']
    events = ask(c, scan_id, "where is the X button")
    pts = [e for e in events if e["type"] == "point"]
    assert len(pts) == 1 and pts[0]["id"] == "pt_1" and pts[0]["label"] == "X button"
    assert all(isinstance(pts[0][k], float) for k in ("x", "y", "z")) and pts[0]["radius"] > 0
    assert any(e["type"] == "photo" and e["for"] == "pt_1" for e in events)
    c.claude.deltas = ['<show part="pt_1"/>Same spot as before.']
    events = ask(c, scan_id, "show me the X button again")
    again = [e for e in events if e["type"] == "point"]
    assert again and again[0]["id"] == "pt_1" and again[0]["x"] == pts[0]["x"]
    first = streamed(c)[-1]["messages"][0]["content"]
    first = first if isinstance(first, str) else next(b["text"] for b in first if b["type"] == "text" and "<parts>" in b["text"])
    assert '"id":"pt_1"' in first and "X button" in first


def test_during_a_scan_the_report_model_is_not_moved(gw):
    c = gw()
    scan_id = publish_scan_with_model(c)
    c.claude.deltas = ['<show part="f_example_b"/>Keep going round it.']
    events = ask(c, scan_id, "where is the capacitor", live={"screen": "scan"})
    assert not [e for e in events if e["type"] in ("show", "point", "view", "zoom", "photo")]
    sent = streamed(c)[0]["messages"][0]["content"]
    assert "<parts>" not in (sent if isinstance(sent, str) else json.dumps(sent))


def test_web_search_is_offered_once_the_object_is_named_and_capped(gw):
    c = gw()
    named = publish_scan_with_model(c, object_words={"category": "game controller", "brand": "Acme", "model": "X1", "confidence_word": "high", "label_visible": True})
    c.claude.deltas = ["It takes two AA batteries."]
    ask(c, named, "which batteries does it take")
    assert streamed(c)[-1]["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}]
    c2 = gw(lea_voice_searches_per_scan=0)
    capped = publish_scan_with_model(c2, object_words={"category": "game controller", "brand": "Acme", "confidence_word": "high", "label_visible": True},
                                     scan_id="scan_capped01")
    ask(c2, capped, "which batteries does it take")
    assert "tools" not in streamed(c2)[-1]
    c3 = gw()
    unnamed = publish_scan_with_model(c3, scan_id="scan_unnamed01")
    ask(c3, unnamed, "which batteries does it take")
    assert "tools" not in streamed(c3)[-1]


def test_searches_are_counted_from_usage():
    from types import SimpleNamespace

    from repairos.voice.conversation import searches_made, web_search_tool
    assert searches_made(SimpleNamespace(server_tool_use=SimpleNamespace(web_search_requests=2))) == 2
    assert searches_made(SimpleNamespace(server_tool_use=None)) == 0 and searches_made(None) == 0
    assert web_search_tool("claude-sonnet-5-5")["type"] == "web_search_20260209"


def test_the_part_index_is_built_once_per_scan_in_the_background(gw, tmp_path):
    import time as _t
    c = gw()
    c.claude.text = json.dumps({"parts": [{"label": "A2", "kind": "pin", "photo": "s7", "x": 0.5, "y": 0.5,
                                           "note": "analog input 2"}]})
    scan_id = publish_scan_with_model(c)
    c.get(f"/v1/voice/conversation?scan_id={scan_id}")     # the report opens
    c.get(f"/v1/voice/conversation?scan_id={scan_id}")
    index = tmp_path / "voice_index" / f"{scan_id}.json"
    for _ in range(100):
        if index.exists():
            break
        _t.sleep(0.05)
    out = json.loads(index.read_text())
    assert out["status"] == "done" and [e["label"] for e in out["entries"]] == ["A2"]
    calls = [b for b in c.claude.bodies if not b.get("stream") and b.get("output_config", {}).get("format")]
    listing = [b for b in calls if "parts" in b["output_config"]["format"]["schema"]["properties"]]
    refine = [b for b in calls if "items" in b["output_config"]["format"]["schema"]["properties"]]
    assert len(listing) == 1 and listing[0]["model"] == "claude-sonnet-5-5"
    assert len(refine) == 2 and out["calls"] == 3          # two zoom passes on the one photo, in the same job
    # The fake answers no refine: the spot stays a region, never a tight ring on a guess.
    assert out["entries"][0]["precision"] == "coarse" and out["entries"][0]["radius"] == 0.03
    c.claude.deltas = ['<show part="ix_1"/>A2 is on the analog header, third from the left.']
    events = ask(c, scan_id, "where is A2")
    assert any(e["type"] == "point" and e["id"] == "ix_1" and e["label"] == "A2" for e in events)
    # Pointing in a photo at a part the index already names: the index's place, not a new guess.
    c.claude.deltas = ['<point photo="s7" x="0.9" y="0.9" label="a2"/>That is A2.']
    events = ask(c, scan_id, "and where is A2 again")
    assert [e["id"] for e in events if e["type"] == "point"] == ["ix_1"]
    assert len([b for b in c.claude.bodies if not b.get("stream") and b.get("output_config", {}).get("format")]) == 3


def test_persona_answers_confidently_and_keeps_the_hard_rules():
    p = " ".join(PERSONA.split())
    for words in ("Answer the question they asked first", "Partial evidence is enough",
                  "Never mention photo counts, coverage, blur", "Never invent a defect",
                  "Never say a part was checked or is fine when nothing shows it", "Partial evidence never clears a part",
                  "Say where a wire goes only when you can see it attached at both ends", "from its colour",
                  "<show part=", "<point photo=", '<view side="top"/>', '<zoom dir="in"/>',
                  "a render of the 3D reconstruction of this real object", "name it in a few words",
                  "nothing you have shows that spot at all", "never add a second pass", "even in the middle of a sentence"):
        assert words in p, words
    assert "but that photo was dark" not in p


def test_a_repeated_sentence_is_not_spoken_but_its_move_happens(gw):
    from repairos.voice.routes import repeats
    assert repeats("A is the green one at the bottom.", ["A is the green one at the bottom, and it confirms."])
    assert not repeats("B is the red one.", ["A is the green one at the bottom."])
    assert not repeats("Yes it is.", ["Yes it is, I think."])          # too short to call a repeat
    c = gw()
    scan_id = publish_scan_with_model(c)
    c.claude.deltas = ["The capacitor is the round part on the board, near the edge. ",
                       '<show part="f_example_b"/>The capacitor is the round part on the board.']
    events = ask(c, scan_id, "where is the capacitor")
    assert [e["text"] for e in events if e["type"] == "sentence"] == [
        "The capacitor is the round part on the board, near the edge."]
    assert {"type": "show", "part": "f_example_b"} in events


def test_ring_size_follows_how_sure_the_spot_is():
    from repairos.voice.evidence import ring_for
    p, r, word = ring_for([0, 0, 0], 0.004, True)
    assert word == "confirmed in a second photo" and r == 0.012
    assert ring_for([0, 0, 0], 0.03, True)[1:] == (0.03, "second photo disagrees")
    assert ring_for([0, 0, 0], 0.2, True)[1] == 0.04
    assert ring_for([0, 0, 0], None, True)[1:] == (0.014, "refined in one photo")
    assert ring_for([0, 0, 0], None, False)[1:] == (0.03, "coarse")


def test_refine_maps_a_crop_answer_back_to_the_model():
    """refine_points with a real crop pipeline and a recorded-shape answer: the crop centre maps back onto the
    surface point under it (synthetic plane, known pose)."""
    import asyncio as _a
    import io

    import numpy as np
    from PIL import Image

    from repairos.voice import views as V
    from repairos.voice.evidence import refine_points
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_voice_views import plane, still

    s = plane()
    st = still()
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), (100, 100, 100)).save(buf, "JPEG")
    img = buf.getvalue()

    class Fake:   # answers every crop with its centre
        class beta:
            class messages:
                @staticmethod
                async def create(**kw):
                    n = sum(1 for b in kw["messages"][0]["content"] if b["type"] == "image")
                    from types import SimpleNamespace as NS
                    text = json.dumps({"items": [{"crop": k + 1, "seen": True, "x": 0.5, "y": 0.5} for k in range(n)]})
                    return NS(stop_reason="end_turn", content=[NS(type="text", text=text)], usage=None, model="m")
    target = np.array([0.03, 0.02, -0.5])
    x, y, _ = V.project_upright(st, target)
    out, calls = _a.run(refine_points(s, [st], lambda _s: img, [{"label": "A2", "point": [0, 0, -0.5],
                                                                 "still_id": st["still_id"], "x": x, "y": y}],
                                      client=Fake(), model="claude-sonnet-5-5", scan_id="t", verify=False))
    assert calls == 2 and out[0]["precision"] == "refined in one photo"
    assert np.linalg.norm(np.asarray(out[0]["point"]) - target) < 0.004


def test_image_names_and_ids_are_never_spoken():
    from repairos.voice.conversation import unspeakable
    assert unspeakable("In photo s2 it's the red button on the left.") == "It's the red button on the left."
    assert unspeakable("The B button is the red one, as shown in view v1.") == "The B button is the red one."
    assert unspeakable("That's ix_3, the X button, see f_25c6159f83e6 too.") == "That's, the X button, see too."
    assert unspeakable("It's from still scan_75033ad7_s4.") == "It's from still."
    assert unspeakable("The Galaxy S2 and a V8 engine stay.") == "The Galaxy S2 and a V8 engine stay."
    sp = SentenceSplitter()
    out = sp.feed("In photo s2 the X button is the blue one. ") + sp.flush()
    assert out == ["The X button is the blue one."]


def test_photo_only_parts_show_their_photo_and_are_placed_when_the_surface_changes(gw, tmp_path):
    import asyncio as _a
    c = gw()
    scan_id = publish_scan_with_model(c)
    still_id = c.state and _a.run(c.state.list("still", scan_id=scan_id))[0].data["still_id"]
    index = tmp_path / "voice_index" / f"{scan_id}.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    entry = {"id": "ix_1", "label": "A2", "part": "A2 pin", "kind": "pin", "point": None, "radius": 0.006,
             "precision": "pin read in a close-up", "still_id": still_id, "x": 0.5, "y": 0.5}
    index.write_text(json.dumps({"status": "done", "entries": [entry], "surface_key": "an older surface"}))
    c.claude.deltas = ['<show part="ix_1"/>A2 is on the analog header.']
    events = ask(c, scan_id, "where is A2")
    # The surface changed since the index was written: A2 got its 3D point from its photo and pixel.
    placed = json.loads(index.read_text())
    assert placed["surface_key"] != "an older surface" and placed["entries"][0]["point"] is not None
    assert any(e["type"] == "point" and e["id"] == "ix_1" for e in events)
    assert any(e["type"] == "photo" and e["for"] == "ix_1" for e in events)
    # On a surface that can't take it, the spot stays photo-only: its photo, the whole object, no ring.
    off = {**entry, "x": 0.01, "y": 0.01}
    index.write_text(json.dumps({"status": "done", "entries": [off], "surface_key": "older again"}))
    events = ask(c, scan_id, "and where is A2 now")
    assert {"type": "view", "side": "reset"} in events and not [e for e in events if e["type"] == "point"]
    assert any(e["type"] == "photo" and e["for"] == "ix_1" for e in events)
