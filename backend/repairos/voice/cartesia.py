"""Cartesia over plain HTTP (httpx2, BSD-3-Clause, already in the lock file via anthropic). No Cartesia SDK.

Endpoints (docs.cartesia.ai, read 2026-10-24):
- TTS: POST /tts/bytes, JSON {model_id, transcript, voice, output_format, language}; the audio is streamed back
  with chunked encoding (api-reference/tts/bytes.md, get-started/authenticate-your-client-applications.md).
  output_format raw = {container: raw, encoding: pcm_s16le, sample_rate}
  (build-with-cartesia/capability-guides/tts-output-audio-format.md).
- STT: POST /stt, multipart/form-data {file, model, language}; returns {text, language, duration, ...}
  (api-reference/stt/transcribe.md). The file is decoded from its container (wav, m4a, mp3, ...).
- Auth: `Authorization: Bearer <key>` (or X-Api-Key) plus the required `Cartesia-Version` header.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx2

from repairos.voice.settings import SAMPLE_RATE, VoiceSettings
from repairos.voice.spend_guard import charge, key_of


class CartesiaError(RuntimeError):
    """Cartesia answered with an error (status and a short body; never the key)."""

    def __init__(self, status: int, body: str):
        self.status = status
        super().__init__(f"Cartesia answered {status}: {body[:300]}")


@dataclass
class Transcript:
    text: str
    language: str | None
    duration_s: float | None


class Cartesia:
    def __init__(self, settings: VoiceSettings, *, transport: httpx2.AsyncBaseTransport | None = None):
        key = settings.cartesia_key
        if not key:
            raise RuntimeError("CARTESIA_API_KEY is not set")
        self.settings = settings
        self._http = httpx2.AsyncClient(
            base_url=settings.cartesia_base_url, transport=transport,
            headers={"Authorization": f"Bearer {key}", "Cartesia-Version": settings.cartesia_version},
            timeout=httpx2.Timeout(20.0, connect=5.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    def tts_body(self, text: str, sample_rate: int = SAMPLE_RATE) -> dict:
        s = self.settings
        gen: dict = {"speed": s.cartesia_speed}
        if s.cartesia_emotion:
            gen["emotion"] = s.cartesia_emotion
        return {"model_id": s.cartesia_tts_model, "transcript": text, "voice": s.cartesia_voice_id, "language": "en",
                "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": sample_rate},
                "generation_config": gen}

    async def tts_stream(self, text: str, sample_rate: int = SAMPLE_RATE) -> AsyncIterator[bytes]:
        """Raw 16-bit little-endian mono PCM, as Cartesia streams it (an even number of bytes per chunk is not
        guaranteed; the caller joins them). Checks the spend guard first (VoicePaused)."""
        charge("cartesia", key=key_of("tts", self.settings.cartesia_voice_id, text))
        async with self._http.stream("POST", "/tts/bytes", json=self.tts_body(text, sample_rate)) as r:
            if r.status_code != 200:
                raise CartesiaError(r.status_code, (await r.aread()).decode("utf-8", "replace"))
            async for chunk in r.aiter_bytes():
                if chunk:
                    yield chunk

    async def tts(self, text: str, sample_rate: int = SAMPLE_RATE) -> bytes:
        return b"".join([c async for c in self.tts_stream(text, sample_rate)])

    async def stt(self, audio: bytes, filename: str, content_type: str, language: str = "en",
                  scope: str = "") -> Transcript:
        charge("cartesia", key=key_of("stt", scope, len(audio)))
        r = await self._http.post("/stt", files={"file": (filename, audio, content_type)},
                                  data={"model": self.settings.cartesia_stt_model, "language": language})
        if r.status_code != 200:
            raise CartesiaError(r.status_code, r.text)
        out = r.json()
        return Transcript(text=(out.get("text") or "").strip(), language=out.get("language"),
                          duration_s=out.get("duration"))
