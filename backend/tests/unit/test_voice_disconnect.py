"""A phone that cancels a turn while its body is still uploading (a barge-in) must not crash the gateway: the voice
routes that read a body answer 499 quietly instead of raising ClientDisconnect ("Exception in ASGI application")."""
from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

from repairos.api.app import create_app
from repairos.slots.blob_local import LocalBlobStore
from repairos.slots.state_sqlite import SqliteState
from repairos.voice.audio import TTSCache
from repairos.voice.conversation import ConversationStore
from repairos.voice.routes import VoiceRuntime
from repairos.voice.settings import VoiceSettings

CODE = "pairing-code-for-disconnect-tests"


def _call(app, method: str, path: str, query: str, headers: dict[str, str]) -> list[dict]:
    """Drive the ASGI app with a client that disconnects instead of sending its body."""
    sent: list[dict] = []
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": query.encode(), "root_path": "",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
             "client": ("127.0.0.1", 1), "server": ("test", 80)}

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


@pytest.fixture
def app_and_token(tmp_path):
    app = create_app(SqliteState(tmp_path / "state.sqlite3"), LocalBlobStore(tmp_path / "blobs"), pairing_code=CODE,
                     recordings=tmp_path / "sessions")
    # voice on (a test key: nothing reaches Cartesia, the body never arrives), so audio turns read their body
    app.state.voice = VoiceRuntime(settings=VoiceSettings(_env_file=None, cartesia_api_key="sk_car_test_only"),
                                   cache=TTSCache(tmp_path / "vc"), conversations=ConversationStore(tmp_path / "c"))
    tok = TestClient(app).post("/v1/devices/pair", json={"code": CODE}).json()["token"]
    return app, tok


@pytest.mark.parametrize("path,query,ctype", [
    ("/v1/voice/converse", "audio=none", "application/json"),     # a typed / recognised turn, cancelled
    ("/v1/voice/converse", "audio=none", "audio/m4a"),            # a held-to-talk recording, cancelled
    ("/v1/voice/ask", "audio=none", "application/json"),
    ("/v1/voice/stt", "", "audio/m4a"),
])
def test_a_cancelled_upload_ends_quietly(app_and_token, caplog, path, query, ctype):
    app, tok = app_and_token
    with caplog.at_level(logging.INFO):
        sent = _call(app, "POST", path, query, {"authorization": f"Bearer {tok}", "content-type": ctype,
                                                "content-length": "100"})
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 499
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("went away" in r.getMessage() for r in caplog.records)
