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
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from repairos.voice.audio import TTSCache, line_key, wav
from repairos.voice.cartesia import Cartesia, CartesiaError
from repairos.voice.conversation import (ACTIONS, MAX_LOOK_IMAGES, NEEDS_REPORT, ConversationStore, SentenceSplitter,
                                         Talker, context_block, history_view, limit_reached, live_note, look_content,
                                         messages_for, parse_live, part_rows, searches_made, views_content,
                                         web_search_tool)
from repairos.voice.evidence import FINDING_RADIUS_M, POINT_RADIUS_M
from repairos.voice.settings import SAMPLE_RATE, VoiceSettings, get_voice_settings
from repairos.voice.spend_guard import VoicePaused, charge, key_of
from repairos.voice.views import point_at

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
    _evidence: Any = None

    def evidence(self, request: Request):
        """A scan's photos, 3D surface and part index for report turns (repairos.voice.evidence)."""
        if self._evidence is None:
            from repairos.voice.evidence import Evidence

            self._evidence = Evidence(request.app, self.conversations.root.parent / "voice_index")
        return self._evidence

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
        charge("cartesia", key=key_of("stream", kind, d.get("device_id", ""), time.time()))
        return {"ok": True}

    # ------------------------------------------------------------------ conversation
    @app.get("/v1/voice/conversation")
    async def voice_conversation(request: Request, d: device_dep,
                                 scan_id: Annotated[str | None, Query(max_length=128)] = None):
        """The conversation so far (to show it again when the report reopens)."""
        rt = _runtime(request)
        if scan_id:   # the report is opening: build its part index in the background (once per scan)
            await _start_index(request, rt, scan_id)
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
                             held: bool = False,
                             commit: Annotated[str | None, Query(max_length=64)] = None,
                             live: Annotated[str | None, Query(max_length=2000)] = None,
                             session_id: Annotated[str | None, Query(max_length=128)] = None,
                             image: Annotated[list[str] | None, Query(max_length=MAX_LOOK_IMAGES)] = None):
        """One turn of the conversation. Body: the recording (audio/m4a, ...) or JSON {text} (the phone's own
        recognizer, e.g. after "Hey Lea"). The answer streams back as frames (`frame()`): J events
        {type: transcript | sentence | action | done}, and A frames of PCM (s16le, 24 kHz) for each sentence right
        after its `sentence` event (`audio: true`). A sentence with `audio: false` (no Cartesia, the spend guard
        paused it, or `audio=none`) is for the phone to say in its own voice. `held=1`: an answer started before the
        person finished (Ink-2's eager end); it stays out of the conversation (later answers, the caps) until the
        phone commits the `held_id` its `done` event carries. `commit=<held_id>`: that confirmed early answer joins
        before this one is answered (the phone's commit request failed). `live`: JSON of what the screen shows
        now (a scan's stage, cue, progress), read for this turn only; during a scan no scan is offered. `session_id`:
        before a scan exists, the session whose story row holds what the label photo identified and the product
        brief, so Lea can talk about that product. `image` (repeated, up to MAX_LOOK_IMAGES, oldest first): blobs (JPEG,
        PUT /v1/blobs first, uploaded while the person talks) of what the camera showed, the last one now; Lea sees
        them and can point (J events {type: mark, x, y, label}) or ask the phone to measure (J event {type: measure,
        x1, y1, x2, y2}), fractions of the last photo. On a report (not during a scan) Lea can move its 3D model: J
        events {type: show, part} (a located finding id: fly there, light its ring, zoom in), {type: view, side}
        (top, bottom, front, back, left, right, around, reset) and {type: zoom, dir} (in, out), each sent right after
        the sentence it was written with. A turn with photos uses `lea_voice_look_model`; after
        `lea_voice_look_turns_per_scan` such turns in one conversation, photos are dropped (words only)."""
        rt = _runtime(request)
        t0 = time.perf_counter()
        scope = _scope(scan_id, d)
        shown = parse_live(live)
        seen = [p for p in (_look_image(request, ref) for ref in _look_refs(image)) if p]
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
        report, story, identity = await _grounding(request, scan_id, session_id)
        convo = rt.conversations.load(scope)
        if commit and (convo.get("held") or {}).get("id") == commit:
            # An early answer the phone confirmed but could not commit (its request failed): it joins first, so this
            # answer has it in context (Greptile P1 on #26). A stale or repeated id is ignored.
            _remember(convo, convo["held"].get("turns") or [], rt.settings, time.time())
            rt.conversations.save(scope, convo)
        talker = rt.talker()
        if paused_line is None and question:
            paused_line = limit_reached(convo, rt.settings, time.time())
            if talker is None:
                paused_line = "Answers are off on the Mac right now."
        if seen:
            if int(convo.get("looks") or 0) >= rt.settings.lea_voice_look_turns_per_scan:
                seen = []   # the per-scan cap on looking turns: this one goes on words alone, and Lea is told why
                shown["camera"] = "not sent this turn (the looking limit for this scan is reached): answer from the report, the \
parts and what you know about the product"
            elif question and paused_line is None:
                convo["looks"] = int(convo.get("looks") or 0) + 1
                rt.conversations.save(scope, convo)
                for k, v in (await _inspected(request, scan_id) if scan_id and not report else {}).items():
                    shown.setdefault(k, v)
        speak = audio == "stream" and rt.cartesia_on
        # On a report (not during a scan) Lea sees the scan's own photos and renders of its 3D reconstruction, knows
        # the located parts (the report's, the part index's, spots pointed out earlier) and can show them on the 3D
        # model (<show>, <point>, <view>, <zoom>); an unknown id or a spot off the model is dropped.
        on_model = report is not None and shown.get("screen") != "scan" and bool(scan_id)
        ev = rt.evidence(request) if on_model else None
        rows = part_rows(report) if on_model else []
        extra: list[dict] = []
        if ev is not None:
            extra = ev.index(scan_id) + list(convo.get("located") or [])
            await _start_index(request, rt, scan_id, identity)
        known = {r["id"]: r for r in rows + extra}
        finding_ids = {r["id"] for r in rows}
        report_views: list = []
        answering = bool(question) and paused_line is None
        if ev is not None and answering and not seen and rt.settings.lea_voice_report_views > 0 \
                and int(convo.get("looks") or 0) < rt.settings.lea_voice_look_turns_per_scan:
            try:
                report_views = (await ev.views(scan_id, question, rows + extra))[:rt.settings.lea_voice_report_views]
            except Exception as e:  # noqa: BLE001 - the answer goes ahead on words
                log.warning("report views for %s failed: %s", scan_id, e)
            if report_views:
                convo["looks"] = int(convo.get("looks") or 0) + 1
                rt.conversations.save(scope, convo)
        turn_model = rt.settings.lea_voice_look_model if (seen or report_views) else rt.settings.lea_voice_answer_model
        tools = None
        named = identity and any(identity.get(k) and identity.get(k) != "unknown" for k in ("name", "brand", "model"))
        if answering and named and int(convo.get("searches") or 0) < rt.settings.lea_voice_searches_per_scan:
            try:   # one unit per turn that may search, before the call (the spend guard's caps and breaker)
                charge("anthropic_web_search", key=key_of("search_offer", scope, question))
                tools = [web_search_tool(turn_model)]
            except VoicePaused:
                tools = None
        used: dict[str, int] = {"searches": 0}
        new_located: list[dict] = []

        def resolve(mv: dict) -> tuple[dict, list[float] | None, float]:
            """A marker as the phone's event, with the 3D point and ring size to photograph (None: no photo)."""
            if mv["type"] == "show":
                r = known.get(mv["part"])
                if r is None:
                    return {}, None, 0.0
                if r["id"] in finding_ids:
                    return {"type": "show", "part": r["id"]}, r.get("point"), FINDING_RADIUS_M
                radius = float(r.get("radius") or POINT_RADIUS_M)
                return _point_event(r, radius), r.get("point"), radius
            if mv["type"] == "point":
                # A spot she points at that a placed part already names (the part index read "B button"): that
                # part's refined place, not a fresh guess from the whole photo.
                named = _named_part(mv["label"], extra)
                if named is not None:
                    radius = float(named.get("radius") or POINT_RADIUS_M)
                    return _point_event(named, radius), named.get("point"), radius
                view = next((v for v in report_views if v.key == mv["photo"]), None)
                pt = point_at(view, mv["x"], mv["y"]) if view is not None else None
                if pt is None:
                    return {}, None, 0.0
                label = mv["label"] or "this spot"
                r = next((x for x in extra + new_located if str(x.get("label", "")).lower() == label.lower()
                          and str(x["id"]).startswith("pt_")), None)
                if r is None:
                    n = 1 + max([int(str(x["id"])[3:]) for x in extra + new_located
                                 if str(x["id"]).startswith("pt_") and str(x["id"])[3:].isdigit()] or [0])
                    r = {"id": f"pt_{n}", "label": label, "part": label, "where": "pointed out earlier in this talk",
                         "point": [round(float(c), 5) for c in pt], "radius": POINT_RADIUS_M,
                         "still_id": view.still_id}
                    new_located.append(r)
                    known[r["id"]] = r
                    # Refined on a full-resolution crop after the sentence is out (never before it): the phone gets
                    # the same id again with the tighter spot.
                    item = {"label": label, "point": r["point"], "still_id": view.still_id,
                            "x": mv["x"] if view.still_id else None, "y": mv["y"] if view.still_id else None}
                    refines.append((r, asyncio.create_task(ev.refine_live(
                        scan_id, item, rt.claude_key(), rt.settings.lea_voice_index_model,
                        http_client=rt.anthropic_http_client))))
                radius = float(r.get("radius") or POINT_RADIUS_M)
                return _point_event(r, radius), r["point"], radius
            return mv, None, 0.0

        photos: list[tuple[str, asyncio.Task]] = []
        refines: list[tuple[dict, asyncio.Task]] = []

        def stage_events(splitter: SentenceSplitter, sent: list[dict], through: int | None = None):
            for mv in splitter.stage if through is None else splitter.moves_through(through):
                if mv in sent:
                    continue
                sent.append(mv)
                if not on_model:
                    continue
                event, point, radius = resolve(mv)
                if not event:
                    continue
                yield jframe(event)
                if point is not None:   # the real photo of the spot, ringed: sent when it is ready
                    fid = event.get("part") or event.get("id")
                    prefer = (known.get(fid) or {}).get("still_id")
                    photos.append((fid, asyncio.create_task(ev.spot_photo(scan_id, point, radius, prefer))))

        async def photo_events(wait: bool):
            deadline = time.perf_counter() + 8
            for item in list(refines):
                r, task = item
                if not task.done() and wait:
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout=max(0.1, deadline - time.perf_counter()))
                    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                        pass
                if task.done():
                    refines.remove(item)
                    got = None if task.cancelled() or task.exception() else task.result()
                    if got:
                        r.update(point=[round(float(c), 5) for c in got["point"]], radius=got["radius"])
                        yield jframe(_point_event(r, float(got["radius"])))
                        photos.append((r["id"], asyncio.create_task(ev.spot_photo(scan_id, r["point"], r["radius"],
                                                                                  got.get("still_id")))))
                        r["still_id"] = got.get("still_id")
            for item in list(photos):
                fid, task = item
                if not task.done() and wait:
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout=6)
                    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                        pass
                if task.done():
                    photos.remove(item)
                    sha = None if task.cancelled() or task.exception() else task.result()
                    if sha:
                        yield jframe({"type": "photo", "for": fid, "crop": sha})

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
            moved: list[dict] = []
            if not question and paused_line is None:
                lines = ["I didn't catch that. Say it again?"]
            elif paused_line is not None:
                lines = [paused_line]
            else:
                lines = None
            queue: asyncio.Queue = asyncio.Queue()

            async def produce():
                try:
                    msgs = messages_for(convo, context_block(report, identity, story, parts=on_model,
                                                             extra_parts=extra), live_note(shown) + question)
                    if seen:   # the camera, for this turn only (the history keeps words)
                        msgs[-1]["content"] = look_content(seen, msgs[-1]["content"])
                    elif report_views:   # the scan's photos and 3D views, for this turn only
                        msgs[-1]["content"] = views_content(report_views, msgs[-1]["content"])

                    def on_usage(_m, usage):
                        used["searches"] = searches_made(usage)
                    async for delta in talker.stream(scope, msgs, model=turn_model, tools=tools, on_usage=on_usage):
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
                cut = -1
                while (sentence := await queue.get()) is not None:
                    timings.setdefault("first_sentence", round((time.perf_counter() - t0) * 1000))
                    cut += 1
                    if lines is None and repeats(sentence, spoken):
                        # A sentence that only says again what she already said (a model sometimes re-states its
                        # answer to place markers): not spoken, but its moves still happen.
                        for f in stage_events(splitter, moved, cut):
                            yield f
                        continue
                    spoken.append(sentence)
                    yield jframe({"type": "sentence", "text": sentence, "audio": speak})
                    for f in stage_events(splitter, moved, cut):   # moves as she says that sentence
                        yield f
                    async for f in photo_events(wait=False):
                        yield f
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
            for f in stage_events(splitter, moved):   # a marker written after the last sentence
                yield f
            async for f in photo_events(wait=True):
                yield f
            action = next((a for a in splitter.actions if report or a not in NEEDS_REPORT), None)
            if shown.get("screen") == "scan" and action == "start_scan":   # a scan is open: never a second one
                action = None
            for mark in splitter.marks if seen else []:   # only a turn that came with a photo can point at it
                yield jframe({"type": "mark", **mark})
            if seen and splitter.measure:   # the phone measures it with LiDAR and says the number
                yield jframe({"type": "measure", **splitter.measure})
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
                if new_located:   # spots pointed out this turn: a repeat question is a lookup (<show part="pt_1"/>)
                    latest["located"] = (list(latest.get("located") or []) + new_located)[-20:]
                if used["searches"]:
                    latest["searches"] = int(latest.get("searches") or 0) + used["searches"]
                    log.info("voice converse %s: %d web search(es)", scope, used["searches"])
                if held:   # kept aside until the phone commits it (POST /v1/voice/conversation/commit)
                    held_id = uuid.uuid4().hex
                    latest["held"] = {"id": held_id, "turns": exchange}
                else:
                    _remember(latest, exchange, rt.settings, now)
                rt.conversations.save(scope, latest)
                turns = _user_turns(latest)
            else:
                turns = _user_turns(convo)
            log.info("voice converse %s timings %s photos %d", scope, timings, len(seen))
            yield jframe({"type": "done", "answer_text": answer, "action": action, "timings_ms": timings,
                          "turns": turns, **({"held_id": held_id} if held_id else {})})

        return StreamingResponse(stream(), media_type="application/x-lea-voice",
                                 headers={"Cache-Control": "no-cache", "X-Sample-Rate": str(SAMPLE_RATE)})


def _named_part(label: str, parts: list[dict]) -> dict | None:
    """The placed part (index entry or earlier spot) whose label is `label`, ignoring case and spacing."""
    want = " ".join(str(label or "").lower().split())
    if not want:
        return None
    return next((r for r in parts if r.get("point") and " ".join(str(r.get("label") or "").lower().split()) == want),
                None)


def repeats(sentence: str, said: list[str]) -> bool:
    """Whether a sentence of four or more words only repeats words of one sentence already said in this answer."""
    w = set(re.findall(r"[a-z0-9']+", sentence.lower()))
    return len(w) >= 4 and any(w <= set(re.findall(r"[a-z0-9']+", s.lower())) for s in said)


def _point_event(r: dict, radius: float) -> dict:
    """A spot on the 3D model (not one of the report's findings): where (scan frame, metres), what, how big a ring."""
    x, y, z = (round(float(c), 5) for c in r["point"])
    return {"type": "point", "id": r["id"], "x": x, "y": y, "z": z, "label": r.get("label") or r.get("part") or "",
            "radius": round(radius, 4)}


async def _start_index(request: Request, rt: VoiceRuntime, scan_id: str, identity: dict | None = None) -> None:
    """The scan's part index in the background, once its report exists (never blocks the answer)."""
    try:
        from repairos.report import report_id_for

        if await request.app.state.store.get("report", report_id_for(scan_id)) is None:
            return
        if identity is None:
            run = await request.app.state.store.get("pipeline-run", f"run_{scan_id}")
            identity = ((run.data if run else {}) or {}).get("identity")
        await rt.evidence(request).ensure_index(scan_id, rt.claude_key(), rt.settings.lea_voice_index_model, identity,
                                                http_client=rt.anthropic_http_client)
    except Exception as e:  # noqa: BLE001
        log.warning("voice index for %s not started: %s", scan_id, e)


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


async def _grounding(request: Request, scan_id: str | None,
                     session_id: str | None = None) -> tuple[dict | None, str | None, dict | None]:
    """(the scan's report or None, the person's story, the identified object) for a conversation turn. Before a scan
    exists (the home conversation), the session's story row says what the label photo identified and what public
    sources report about the product (its brief)."""
    if not scan_id and not session_id:
        return None, None, None
    from repairos.report import report_id_for

    st = request.app.state.store
    row = await st.get("report", report_id_for(scan_id)) if scan_id else None
    report = row.data if row else None
    run = await st.get("pipeline-run", f"run_{scan_id}") if scan_id else None
    identity = ((run.data if run else {}) or {}).get("identity") or (report or {}).get("object")
    story = None
    pkg = await st.get("scan-package", scan_id) if scan_id else None
    sid = (report or {}).get("session_id") or (pkg.data.get("session_id") if pkg else None) or session_id
    if sid and not identity:
        from repairos.pipeline.story import story_identity

        identity = await story_identity(st, sid, detail=True)
    if sid:
        sess = await st.get("session", sid)
        story = (sess.data.get("symptom") if sess else None) or None
    if not story and pkg:
        story = pkg.data.get("symptom") or None
    return report, story, identity


MAX_LOOK_BYTES = 2_000_000
_BLOB_REF = re.compile(r"^blob://sha256:[0-9a-f]{64}$")


def _look_refs(refs: list[str] | None) -> list[str]:
    """The turn's photo refs, oldest first: well-formed blob refs only, the last MAX_LOOK_IMAGES (the newest)."""
    return [r for r in refs or [] if _BLOB_REF.match(r)][-MAX_LOOK_IMAGES:]


async def _inspected(request: Request, scan_id: str) -> dict[str, str]:
    """While a scan has no report yet: the last few parts the pipeline looked at, in its own words, for a looking
    turn's <phone> line (nothing when none were looked at yet)."""
    rows = await request.app.state.store.list("finding", scan_id=scan_id)
    looked = sorted((r.data for r in rows if (r.data or {}).get("state") not in (None, "not_inspected")),
                    key=lambda f: str(f.get("created_at") or ""), reverse=True)[:3]
    words = [f"{f.get('part_words') or (f.get('subject') or {}).get('kind') or 'a part'}: "
             f"{str(f.get('state')).replace('_', ' ')}" for f in looked]
    return {"inspected so far": "; ".join(words)[:200]} if words else {}


def _look_image(request: Request, ref: str | None) -> str | None:
    """The look photo as base64 JPEG, or None (no photo, not uploaded, too big, or not a JPEG): the turn goes ahead
    on words alone."""
    import base64

    blobs = request.app.state.blobs
    if not ref or not blobs.exists(ref) or blobs.size(ref) > MAX_LOOK_BYTES:
        return None
    data = blobs.get(ref)
    return base64.b64encode(data).decode() if data[:3] == b"\xff\xd8\xff" else None


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
