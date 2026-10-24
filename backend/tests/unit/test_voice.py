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
    assert sent["system"] == PERSONA and sent["stream"] is True
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
    assert len(parse_live(json.dumps({f"k{i}": "v" for i in range(20)}))) == 12


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


def test_sentence_splitter():
    sp = SentenceSplitter()
    assert sp.feed("Hi. It looks") == []          # "Hi." is too short to stand alone
    assert sp.feed(" bent at 1.5 cm. Unplug") == ["Hi. It looks bent at 1.5 cm."]
    assert sp.feed(" it first!") == []            # the end of the text so far may continue
    assert sp.feed(" Then") == ["Unplug it first!"]
    assert sp.flush() == ["Then"]


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
