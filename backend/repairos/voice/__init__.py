"""Lea's voice: spoken report summaries, scan cues and "Ask Lea" (Cartesia TTS/STT + a grounded Claude answer).
The gateway mounts the routes with register_voice_routes (routes.py). No key ever reaches the phone."""
from repairos.voice.routes import VoiceRuntime, register_voice_routes

__all__ = ["VoiceRuntime", "register_voice_routes"]
