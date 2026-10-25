"""Voice routes on the gateway (device token required, like every other /v1 route). The Cartesia key stays here.

GET  /v1/voice/status            {cartesia, answers, sample_rate, ...}: what the phone can use; cartesia false =
                                 speak and listen on the phone.
POST /v1/voice/tts {text, format} format "wav" (default): one complete WAV file (16-bit mono, 24 kHz), cached by
                                 text; "pcm": the same samples as raw s16le, streamed while Cartesia makes them
                                 (`X-Sample-Rate`), so the phone starts playing on the first chunk.
POST /v1/voice/stt               body = the recording (Content-Type audio/m4a, audio/wav, ...) -> {transcript}.
POST /v1/voice/ask?scan_id=      body = the recording -> Cartesia STT -> a Claude answer grounded only in that
                                 scan's report and the person's story -> {transcript, answer_text, audio (base64
                                 WAV), timings_ms}; `audio=none` returns the texts only, for the phone to stream
                                 the answer from /v1/voice/tts {format: pcm}. A JSON body {scan_id, question} skips STT (the phone's own
                                 recognizer); without Cartesia `audio` is null and the phone speaks the text.
Without CARTESIA_API_KEY the Cartesia routes answer 503 {"error": "voice_unavailable"}.
"""
# No `from __future__ import annotations` here: FastAPI must see `device_dep` (a local) as a real annotation.
import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from repairos.voice.audio import TTSCache, line_key, wav
from repairos.voice.cartesia import Cartesia, CartesiaError
from repairos.voice.conversation import (ACTIONS, ConversationStore, SentenceSplitter, Talker, context_block,
                                         history_view, limit_reached, messages_for)
from repairos.voice.settings import SAMPLE_RATE, VoiceSettings, get_voice_settings
from repairos.voice.spend_guard import VoicePaused

log = logging.getLogger("repairos.voice")
MAX_TEXT = 600
MAX_AUDIO_BYTES = 10 << 20
AUDIO_EXT = {"audio/m4a": "m4a", "audio/x-m4a": "m4a", "audio/mp4": "m4a", "audio/aac": "m4a", "audio/wav": "wav",
             "audio/x-wav": "wav", "audio/wave": "wav", "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/flac": "flac",
             "audio/ogg": "ogg", "audio/webm": "webm"}


class TTSBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    format: str = Field(default="wav", pattern="^(wav|pcm)$")


class CommitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    held_id: str = Field(min_length=1, max_length=64)


@dataclass
class VoiceRuntime:
    """Per-app voice clients, made on first use. Tests pass transports at the HTTP boundary."""
    settings: VoiceSettings = field(default_factory=get_voice_settings)
    cache: TTSCache = field(default_factory=TTSCache)
    cartesia_transport: Any = None
    anthropic_http_client: Any = None
    anthropic_key: str | None = None
    _cartesia: Cartesia | None = None
    _answerer: Any = None
    warmed: bool = False
    tasks: set = field(default_factory=set)
    conversations: ConversationStore = field(default_factory=ConversationStore)
    _talker: Any = None

    def talker(self) -> Talker | None:
        if self._talker is None and (key := self.claude_key()):
            self._talker = Talker(self.settings, key, http_client=self.anthropic_http_client)
        return self._talker

    @property
    def cartesia_on(self) -> bool:
        return bool(self.settings.cartesia_key)

    def cartesia(self) -> Cartesia:
        if not self.cartesia_on:
            raise HTTPException(503, {"error": "voice_unavailable",
                                      "message": "CARTESIA_API_KEY is not set: use the phone's own voice"})
        if self._cartesia is None:
            self._cartesia = Cartesia(self.settings, transport=self.cartesia_transport)
        return self._cartesia

    def claude_key(self) -> str | None:
        if self.anthropic_key is not None:   # given explicitly ("" = none)
            return self.anthropic_key or None
        from repairos.config import get_settings
        k = get_settings().anthropic_api_key
        return k.get_secret_value() if k is not None and k.get_secret_value() else None

    def answerer(self):
        if self._answerer is None:
            key = self.claude_key()
            if not key:
                raise HTTPException(503, {"error": "answer_unavailable", "message": "ANTHROPIC_API_KEY is not set"})
            from repairos.voice.answer import Answerer
            self._answerer = Answerer(self.settings, key, http_client=self.anthropic_http_client)
        return self._answerer

    def key(self, text: str) -> str:
        s = self.settings
        return line_key(text, s.cartesia_tts_model, f"{s.cartesia_voice_id}|{s.cartesia_speed}|{s.cartesia_emotion}",
                        SAMPLE_RATE)

    async def speak(self, text: str) -> tuple[bytes, bool]:
        """(pcm, cache hit)."""
        k = self.key(text)
        if (pcm := self.cache.get(k)) is not None:
            return pcm, True
        pcm = await self.cartesia().tts(text)
        self.cache.put(k, pcm)
        return pcm, False


def _runtime(request: Request) -> VoiceRuntime:
    rt = getattr(request.app.state, "voice", None)
    if rt is None:
        rt = request.app.state.voice = VoiceRuntime()
    return rt


def _cartesia_failed(e: CartesiaError) -> HTTPException:
    log.warning("cartesia error %s", e)
    return HTTPException(502, {"error": "cartesia", "status": e.status, "message": str(e)})


async def _audio(request: Request) -> tuple[bytes, str, str]:
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype not in AUDIO_EXT:
        raise HTTPException(415, {"error": "audio_type", "message": f"send audio ({', '.join(sorted(AUDIO_EXT))})"})
    data = await request.body()
    if not data:
        raise HTTPException(400, {"error": "empty_audio"})
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, {"error": "audio_too_large"})
    return data, f"speech.{AUDIO_EXT[ctype]}", ctype


PAUSED_LINE = "Lea is paused for a few minutes. Try again soon."


def frame(kind: bytes, payload: bytes) -> bytes:
    """One frame of the conversation stream: 1 byte kind (J = JSON event, A = PCM audio), 4 bytes big-endian
    length, the payload."""
    return kind + len(payload).to_bytes(4, "big") + payload


def jframe(event: dict) -> bytes:
    return frame(b"J", json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode())


def register_voice_routes(app: FastAPI, device_dep) -> None:
    """Adds the /v1/voice routes. `device_dep` is the gateway's device-token dependency (Annotated)."""

    @app.exception_handler(VoicePaused)
    async def _paused(_req, e: VoicePaused):
        # The spend guard said no: the phone uses its own voice / recognizer; nothing retries here.
        headers = {"Retry-After": str(int(e.retry_after_s))} if e.retry_after_s else {}
        return JSONResponse(status_code=503, headers=headers, content={
            "error": "voice_paused", "service": e.service, "reason": e.reason, "retry_after_s": e.retry_after_s})

    @app.get("/v1/voice/status")
    async def voice_status(request: Request, _d: device_dep):
        """The phone asks at launch; that also warms the answer model's connection for the first question."""
        rt = _runtime(request)
        s = rt.settings
        if rt.claude_key() and not rt.warmed:
            rt.warmed = True
            rt.tasks.add(t := asyncio.create_task(rt.answerer().warm()))
            t.add_done_callback(rt.tasks.discard)
        return {"cartesia": rt.cartesia_on, "answers": bool(rt.claude_key()), "sample_rate": SAMPLE_RATE,
                "tts_model": s.cartesia_tts_model if rt.cartesia_on else None,
                "stt_model": s.cartesia_stt_model if rt.cartesia_on else None,
                "voice_id": s.cartesia_voice_id if rt.cartesia_on else None}

    @app.post("/v1/voice/tts", responses={200: {"content": {"audio/wav": {}, "audio/pcm": {}}}})
    async def voice_tts(body: TTSBody, request: Request, _d: device_dep):
        rt = _runtime(request)
        text = " ".join(body.text.split())
        k = rt.key(text)
        cached = rt.cache.get(k)
        cart = rt.cartesia() if cached is None else None
        headers = {"X-Sample-Rate": str(SAMPLE_RATE), "X-Voice-Cache": "hit" if cached is not None else "miss",
                   "Cache-Control": "private, max-age=86400"}
        if body.format == "wav":
            if cached is None:
                try:
                    cached = await cart.tts(text)
                except CartesiaError as e:
                    raise _cartesia_failed(e) from None
                rt.cache.put(k, cached)
            return Response(wav(cached, SAMPLE_RATE), media_type="audio/wav", headers=headers)
        if cached is not None:
            return Response(cached, media_type="audio/pcm", headers=headers)

        # Stream: open Cartesia first so an error is still an HTTP error, then pass chunks through on even sample
        # boundaries and keep the whole line for the cache.
        it = cart.tts_stream(text)
        try:
            first = await anext(it)
        except StopAsyncIteration:
            first = b""
        except CartesiaError as e:
            raise _cartesia_failed(e) from None

        async def chunks():
            got, carry = [first], first
            out_len = len(carry) - len(carry) % 2
            if out_len:
                yield carry[:out_len]
            carry = carry[out_len:]
            try:
                async for c in it:
                    got.append(c)
                    carry += c
                    n = len(carry) - len(carry) % 2
                    if n:
                        yield carry[:n]
                        carry = carry[n:]
            except CartesiaError as e:   # mid-stream: the phone gets a short line and falls back
                log.warning("cartesia stream cut: %s", e)
                return
            rt.cache.put(k, b"".join(got))
        return StreamingResponse(chunks(), media_type="audio/pcm", headers=headers)

    @app.post("/v1/voice/stt")
    async def voice_stt(request: Request, _d: device_dep):
        rt = _runtime(request)
        cart = rt.cartesia()
        audio, name, ctype = await _audio(request)
        t0 = time.perf_counter()
        try:
            tr = await cart.stt(audio, name, ctype)
        except CartesiaError as e:
            raise _cartesia_failed(e) from None
        return {"transcript": tr.text, "language": tr.language, "duration_s": tr.duration_s,
                "ms": round((time.perf_counter() - t0) * 1000)}

    @app.post("/v1/voice/ask")
    async def voice_ask(request: Request, _d: device_dep,
                        scan_id: Annotated[str | None, Query(max_length=128)] = None,
                        audio: Annotated[str, Query(pattern="^(inline|none)$")] = "inline"):
        """`audio=none`: answer with the texts only, at once; the phone then streams the answer's speech from
        POST /v1/voice/tts {format: pcm} and starts playing on its first chunk (faster than waiting for the whole
        inline WAV)."""
        rt = _runtime(request)
        timings: dict[str, int] = {}
        t0 = time.perf_counter()

        def lap(name: str, since: float) -> float:
            now = time.perf_counter()
            timings[name] = round((now - since) * 1000)
            return now

        paused = False
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype == "application/json":
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(422, {"error": "body", "message": "send {scan_id, question}"})
            scan_id = scan_id or body.get("scan_id")
            question = " ".join(str(body.get("question") or "").split())[:MAX_TEXT]
            if not question:
                raise HTTPException(422, {"error": "no_question"})
        else:
            cart = rt.cartesia()
            rec, name, actype = await _audio(request)
            try:
                question = (await cart.stt(rec, name, actype, scope=scan_id or "")).text
            except CartesiaError as e:
                raise _cartesia_failed(e) from None
            except VoicePaused:
                question, paused = "", True
        t = lap("stt", t0)
        if not scan_id:
            raise HTTPException(422, {"error": "no_scan_id", "message": "pass ?scan_id="})
        report, story = await _report_and_story(request, scan_id)
        if paused:
            answer = PAUSED_LINE
        elif not question:
            answer = "I didn't catch that. Hold the button and ask again."
        else:
            try:
                answer = await rt.answerer().answer(question, report, story)
            except VoicePaused:
                answer = PAUSED_LINE
        t = lap("answer", t)
        audio_b64 = None
        if rt.cartesia_on and audio == "inline":
            try:
                pcm, _hit = await rt.speak(answer)
                audio_b64 = base64.b64encode(wav(pcm, SAMPLE_RATE)).decode()
            except (CartesiaError, VoicePaused) as e:   # the text still goes back; the phone speaks it itself
                log.warning("answer audio failed: %s", e)
        lap("tts", t)
        timings["total"] = round((time.perf_counter() - t0) * 1000)
        log.info("voice ask %s timings %s", scan_id, timings)
        return JSONResponse({"scan_id": scan_id, "transcript": question, "answer_text": answer, "audio": audio_b64,
                             "audio_format": "wav" if audio_b64 else None, "sample_rate": SAMPLE_RATE,
                             "timings_ms": timings})


    # ------------------------------------------------------------------ realtime (phone -> Cartesia sockets)
    @app.post("/v1/voice/token")
    async def voice_token(request: Request, d: device_dep):
        """A short-lived Cartesia access token (5 min) for the phone's realtime sockets: Ink-2 turn detection
        (`wss://api.cartesia.ai/stt/turns/websocket`) and Sonic (`wss://api.cartesia.ai/tts/websocket`). The key
        never leaves the gateway; every mint is spend-checked (503 `voice_paused` when the guard says no)."""
        rt = _runtime(request)
        s = rt.settings
        try:
            token = await rt.cartesia().access_token(300, scope=d.get("device_id", ""))
        except CartesiaError as e:
            raise _cartesia_failed(e) from None
        return {"token": token, "expires_in": 300, "version": s.cartesia_version, "voice_id": s.cartesia_voice_id,
                "tts_model": s.cartesia_tts_model, "stt_model": "ink-2", "speed": s.cartesia_speed,
                "emotion": s.cartesia_emotion or None, "sample_rate": SAMPLE_RATE, "stt_sample_rate": 16000,
                "keyterms": ["Lea", "Leah"]}

    @app.post("/v1/voice/stream")
    async def voice_stream(request: Request, d: device_dep,
                           kind: Annotated[str, Query(pattern="^(stt|tts)$")] = "stt"):
        """The phone opens one realtime socket (a listening turn or one spoken answer): counted by the spend guard
        before it opens (503 `voice_paused` = use the gateway or the phone's own voice instead)."""
        rt = _runtime(request)
        if not rt.cartesia_on:
            raise HTTPException(503, {"error": "voice_unavailable"})
        from repairos.voice.spend_guard import charge, key_of
        charge("cartesia", key=key_of("stream", kind, d.get("device_id", ""), time.time()))
        return {"ok": True}

    # ------------------------------------------------------------------ conversation
    @app.get("/v1/voice/conversation")
    async def voice_conversation(request: Request, d: device_dep,
                                 scan_id: Annotated[str | None, Query(max_length=128)] = None):
        """The conversation so far (to show it again when the report reopens)."""
        rt = _runtime(request)
        return {"turns": history_view(rt.conversations.load(_scope(scan_id, d)))}

    @app.delete("/v1/voice/conversation")
    async def voice_conversation_reset(request: Request, d: device_dep,
                                       scan_id: Annotated[str | None, Query(max_length=128)] = None):
        rt = _runtime(request)
        rt.conversations.save(_scope(scan_id, d), {"turns": [], "starts": []})
        return {"turns": []}

    @app.post("/v1/voice/conversation/commit")
    async def voice_conversation_commit(body: CommitBody, request: Request, d: device_dep,
                                        scan_id: Annotated[str | None, Query(max_length=128)] = None):
        """An early answer (`converse?held=1`) that the person's final words confirmed: into the conversation.
        `committed` false when it was replaced (another turn came first) or already committed."""
        rt = _runtime(request)
        scope = _scope(scan_id, d)
        convo = rt.conversations.load(scope)
        held = convo.get("held") or {}
        if held.get("id") != body.held_id:
            return {"committed": False, "turns": _user_turns(convo)}
        _remember(convo, held.get("turns") or [], rt.settings, time.time())
        rt.conversations.save(scope, convo)
        return {"committed": True, "turns": _user_turns(convo)}

    @app.post("/v1/voice/converse", responses={200: {"content": {"application/x-lea-voice": {}}}})
    async def voice_converse(request: Request, d: device_dep,
                             scan_id: Annotated[str | None, Query(max_length=128)] = None,
                             audio: Annotated[str, Query(pattern="^(stream|none)$")] = "stream",
                             held: bool = False):
        """One turn of the conversation. Body: the recording (audio/m4a, ...) or JSON {text} (the phone's own
        recognizer, e.g. after "Hey Lea"). The answer streams back as frames (`frame()`): J events
        {type: transcript | sentence | action | done}, and A frames of PCM (s16le, 24 kHz) for each sentence right
        after its `sentence` event (`audio: true`). A sentence with `audio: false` (no Cartesia, the spend guard
        paused it, or `audio=none`) is for the phone to say in its own voice. `held=1`: an answer started before the
        person finished (Ink-2's eager end); it stays out of the conversation (later answers, the caps) until the
        phone commits the `held_id` its `done` event carries."""
        rt = _runtime(request)
        t0 = time.perf_counter()
        scope = _scope(scan_id, d)
        question, stt_ms, paused_line = "", 0, None
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype == "application/json":
            body = await request.json()
            question = " ".join(str((body or {}).get("text") or "").split())[:MAX_TEXT] if isinstance(body, dict) else ""
        else:
            cart = rt.cartesia()
            rec, name, actype = await _audio(request)
            try:
                question = (await cart.stt(rec, name, actype, scope=scope)).text
            except CartesiaError as e:
                raise _cartesia_failed(e) from None
            except VoicePaused:
                paused_line = PAUSED_LINE
            stt_ms = round((time.perf_counter() - t0) * 1000)
        report, story, identity = await _grounding(request, scan_id)
        convo = rt.conversations.load(scope)
        talker = rt.talker()
        if paused_line is None and question:
            paused_line = limit_reached(convo, rt.settings, time.time())
            if talker is None:
                paused_line = "Answers are off on the Mac right now."
        speak = audio == "stream" and rt.cartesia_on

        async def tts_frames(sentence: str):
            """PCM frames for one sentence; on any failure, nothing (the phone says it in its own voice)."""
            k = rt.key(sentence)
            if (pcm := rt.cache.get(k)) is not None:
                yield frame(b"A", pcm)
                return
            got = []
            try:
                async for chunk in rt.cartesia().tts_stream(sentence):
                    got.append(chunk)
                    yield frame(b"A", chunk)
            except (CartesiaError, VoicePaused) as e:
                log.warning("converse tts stopped: %s", e)
                return
            rt.cache.put(k, b"".join(got))

        async def stream():
            nonlocal speak
            timings: dict[str, int] = {"stt": stt_ms}
            yield jframe({"type": "transcript", "text": question})
            spoken: list[str] = []
            splitter = SentenceSplitter()
            if not question and paused_line is None:
                lines = ["I didn't catch that. Say it again?"]
            elif paused_line is not None:
                lines = [paused_line]
            else:
                lines = None
            queue: asyncio.Queue = asyncio.Queue()

            async def produce():
                try:
                    msgs = messages_for(convo, context_block(report, identity, story), question)
                    async for delta in talker.stream(scope, msgs):
                        for sentence in splitter.feed(delta):
                            await queue.put(sentence)
                    for sentence in splitter.flush():
                        await queue.put(sentence)
                except VoicePaused:
                    await queue.put(PAUSED_LINE)
                except Exception as e:   # the turn still ends with a spoken line
                    log.warning("converse answer failed: %s", e)
                    await queue.put("Sorry, I lost that. Ask me again?")
                await queue.put(None)

            if lines is None:
                producer = asyncio.create_task(produce())
            else:
                for line in lines:
                    await queue.put(line)
                await queue.put(None)
                producer = None
            try:
                while (sentence := await queue.get()) is not None:
                    timings.setdefault("first_sentence", round((time.perf_counter() - t0) * 1000))
                    spoken.append(sentence)
                    yield jframe({"type": "sentence", "text": sentence, "audio": speak})
                    if speak:
                        n = 0
                        async for f in tts_frames(sentence):
                            if n == 0:
                                timings.setdefault("first_audio", round((time.perf_counter() - t0) * 1000))
                            n += 1
                            yield f
                        if n == 0:   # Cartesia stopped: the rest is the phone's voice
                            speak = False
                            yield jframe({"type": "audio_off"})
            finally:
                if producer is not None and not producer.done():
                    producer.cancel()
            action = splitter.actions[0] if splitter.actions else None
            if action and action in ACTIONS:
                yield jframe({"type": "action", "action": action, "label": ACTIONS[action]})
            answer = " ".join(spoken)
            timings["total"] = round((time.perf_counter() - t0) * 1000)
            held_id = None
            if question and lines is None:
                now = time.time()
                exchange = [{"role": "user", "text": question, "at": now},
                            {"role": "assistant", "text": answer, "at": now, **({"action": action} if action else {})}]
                # Onto the conversation as stored NOW, not the copy loaded when this answer started: a held answer
                # committed while this one streamed must not be written over (Greptile P1 on #21).
                latest = rt.conversations.load(scope)
                if held:   # kept aside until the phone commits it (POST /v1/voice/conversation/commit)
                    held_id = uuid.uuid4().hex
                    latest["held"] = {"id": held_id, "turns": exchange}
                else:
                    _remember(latest, exchange, rt.settings, now)
                rt.conversations.save(scope, latest)
                turns = _user_turns(latest)
            else:
                turns = _user_turns(convo)
            log.info("voice converse %s timings %s", scope, timings)
            yield jframe({"type": "done", "answer_text": answer, "action": action, "timings_ms": timings,
                          "turns": turns, **({"held_id": held_id} if held_id else {})})

        return StreamingResponse(stream(), media_type="application/x-lea-voice",
                                 headers={"Cache-Control": "no-cache", "X-Sample-Rate": str(SAMPLE_RATE)})


def _scope(scan_id: str | None, device: dict) -> str:
    return scan_id or f"home_{device.get('device_id', 'unknown')}"


def _remember(convo: dict, exchange: list[dict], settings: VoiceSettings, now: float) -> None:
    """One question and its answer into the conversation: what later answers see and the caps count. An early answer
    still held is dropped (the person said something else)."""
    convo.setdefault("turns", []).extend(exchange)
    convo["turns"] = convo["turns"][-2 * settings.lea_voice_turns_per_scan:]
    convo["starts"] = [t for t in convo.get("starts", []) if now - t < 60] + [now]
    convo.pop("held", None)


def _user_turns(convo: dict) -> int:
    return sum(1 for t in convo.get("turns", []) if t.get("role") == "user")


async def _grounding(request: Request, scan_id: str | None) -> tuple[dict | None, str | None, dict | None]:
    """(the scan's report or None, the person's story, the identified object) for a conversation turn."""
    if not scan_id:
        return None, None, None
    from repairos.report import report_id_for

    st = request.app.state.store
    row = await st.get("report", report_id_for(scan_id))
    report = row.data if row else None
    run = await st.get("pipeline-run", f"run_{scan_id}")
    identity = ((run.data if run else {}) or {}).get("identity") or (report or {}).get("object")
    story = None
    pkg = await st.get("scan-package", scan_id)
    sid = (report or {}).get("session_id") or (pkg.data.get("session_id") if pkg else None)
    if sid:
        sess = await st.get("session", sid)
        story = (sess.data.get("symptom") if sess else None) or None
    if not story and pkg:
        story = pkg.data.get("symptom") or None
    return report, story, identity


async def _report_and_story(request: Request, scan_id: str) -> tuple[dict, str | None]:
    from repairos.report import report_id_for

    st = request.app.state.store
    row = await st.get("report", report_id_for(scan_id))
    if row is None:
        raise HTTPException(404, {"error": "no_report", "message": f"scan {scan_id} has no finished report yet"})
    report = row.data
    story = None
    sid = report.get("session_id")
    if sid:
        sess = await st.get("session", sid)
        story = (sess.data.get("symptom") if sess else None) or None
    if not story:
        pkg = await st.get("scan-package", scan_id)
        story = (pkg.data.get("symptom") if pkg else None) or None
    return report, story
