"""Voice settings, read like repairos.config (environment first, then backend/.env). Kept in this package so the
voice feature adds no field to the shared Settings object.

CARTESIA_API_KEY unset = voice runs on the phone (AVSpeechSynthesizer / SFSpeechRecognizer): GET /v1/voice/status
says `cartesia: false` and the Cartesia routes answer 503 `voice_unavailable`. The key never leaves the backend.
"""
from __future__ import annotations

from functools import cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from repairos.config import ENV_FILE

# Cartesia API facts, from docs.cartesia.ai (2026-10-24):
#   base URL + the required `Cartesia-Version` header (one allowed value): api-reference/tts/bytes.md
#   `sonic-3.6` = the stable latest TTS model id: build-with-cartesia/tts-models/latest.md
#   batch STT `/stt` accepts only the `ink-whisper` family (ink-2 is streaming-only):
#   use-the-api/stt/compare-endpoints.md, api-reference/stt/transcribe.md
#   generation_config {speed 0.6-1.5, emotion}: api-reference/tts/bytes.md
#   the voice: "Courtney" (Composed Professional: warm, measured), picked from GET /voices?language=en against
#   capability-guides/choosing-a-voice.md; samples of three candidates were made for the owner to choose
CARTESIA_BASE_URL = "https://api.cartesia.ai"
CARTESIA_VERSION = "2026-08-14"
TTS_MODEL = "sonic-3.6"
STT_MODEL = "ink-whisper"
VOICE_ID = "16a4052e-1f11-47ac-95f5-9330bee062f9"
# Calm, slightly unhurried delivery for cues and answers.
SPEED = 0.95
EMOTION = "calm"
# The phone plays 16-bit mono PCM at this rate (one of the documented sample rates).
SAMPLE_RATE = 24000
# The model that answers "Ask Lea" questions (grounded in the scan's report only). Measured 2026-10-24 on the
# example report: claude-haiku-5-5 (effort low) 0.86-0.90 s, claude-opus-5-5 (effort low) 1.9-2.8 s. The answer has
# to start playing within 3 s of the button's release (STT and TTS included), so Haiku is the default here;
# LEA_VOICE_ANSWER_MODEL=claude-opus-5-5 trades that for the larger model.
ANSWER_MODEL = "claude-haiku-5-5"
# The model for a looking turn (one with camera photos). Chosen by eval/live_vision (RESULTS.md, 39 real stills):
# honesty first, then latency. Haiku made no overclaim (Sonnet 1) at $0.0004 a turn (Sonnet $0.0059); Sonnet read
# a little more right (31 vs 29 correct) and starts sooner at the median (1.37 vs 2.35 s to the first sentence,
# p95 4.07 vs 3.49 s). LEA_VOICE_LOOK_MODEL=claude-sonnet-5-5 switches.
LOOK_MODEL = "claude-haiku-5-5"


class VoiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore",
                                      env_ignore_empty=True, case_sensitive=False)

    cartesia_api_key: SecretStr | None = None
    cartesia_base_url: str = CARTESIA_BASE_URL
    cartesia_version: str = CARTESIA_VERSION
    cartesia_tts_model: str = TTS_MODEL
    cartesia_stt_model: str = STT_MODEL
    cartesia_voice_id: str = VOICE_ID
    cartesia_speed: float = SPEED
    cartesia_emotion: str = EMOTION
    lea_voice_answer_model: str = ANSWER_MODEL
    # "low" keeps the spoken answer quick; the answer is two short sentences grounded in one report.
    lea_voice_answer_effort: str = "low"
    # Conversation guards: turns kept per scan, and turns allowed per minute per scan.
    lea_voice_turns_per_scan: int = 30
    lea_voice_turns_per_minute: int = 8
    # Looking turns (camera photos with the question): the model, and how many per scan carry photos (later turns
    # go on words alone). At most MAX_LOOK_IMAGES photos per turn (conversation.py).
    lea_voice_look_model: str = LOOK_MODEL
    lea_voice_look_turns_per_scan: int = 40

    @property
    def cartesia_key(self) -> str | None:
        k = self.cartesia_api_key
        return k.get_secret_value() if k is not None and k.get_secret_value() else None


@cache
def get_voice_settings() -> VoiceSettings:
    return VoiceSettings()
