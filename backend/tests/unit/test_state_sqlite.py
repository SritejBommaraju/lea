"""B4: SQLite specifics (the shared assertions are in tests/conformance/state, run with --impl sqlite)."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from repairos.registry import Registry
from repairos.slots.state import Job
from repairos.slots.state_sqlite import SqliteState

EXAMPLES = Path(__file__).resolve().parents[3] / "spec" / "contracts" / "examples"


def ex(name):
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def test_wal_lifted_columns_and_persistence(tmp_path):
    path = tmp_path / "state.sqlite3"
    s = SqliteState(path)
    pkg = ex("scan-package")

    async def go():
        await s.transition("session", pkg["session_id"], "session.create", {"session_id": pkg["session_id"]})
        await s.transition("scan-package", pkg["scan_id"], "scan.create", pkg)
        f = ex("finding")
        await s.transition("finding", f["finding_id"], "finding.create", f)
    asyncio.run(go())
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        row = c.execute("SELECT session_id, scan_id FROM rows WHERE kind='finding'").fetchone()
    assert row == (pkg["session_id"], pkg["scan_id"])  # session_id lifted through the scan
    again = SqliteState(path)
    assert asyncio.run(again.get("scan-package", pkg["scan_id"])).data == pkg


def test_device_rows_never_reach_a_live_stream(tmp_path):
    s = SqliteState(tmp_path / "s.db")

    async def go():
        await s.transition("device", "ab" * 32, "device.pair", {"token_sha256": "ab" * 32, "device_id": "dev_1"})
        with sqlite3.connect(s.path) as c:
            return c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert asyncio.run(go()) == 0


def test_stale_running_job_is_reclaimed(tmp_path, monkeypatch):
    import repairos.slots.state_sqlite as mod
    s = SqliteState(tmp_path / "s.db")

    async def go():
        jid = await s.enqueue(Job(kind="detect"))
        assert (await s.claim("w1", ["detect"])).job_id == jid
        assert await s.claim("w2", ["detect"]) is None
        monkeypatch.setattr(mod, "STALE_HEARTBEAT_S", -1)  # w1 stopped heartbeating
        j = await s.claim("w2", ["detect"])
        assert j.job_id == jid and j.worker == "w2" and j.attempts == 2
    asyncio.run(go())


def test_registry_selects_sqlite_for_the_phase1_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("REPAIROS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("REPAIROS_PROFILE", raising=False)
    monkeypatch.delenv("REPAIROS_SLOT_STATE", raising=False)
    store = Registry(release=False).slot("STATE")
    assert isinstance(store, SqliteState) and store.path == tmp_path / "state.sqlite3"
