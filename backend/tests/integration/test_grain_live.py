"""The Lea -> Grain handoff against a real Grain server: recovery and one-worker-at-a-time, where only Grain can say
what it already holds.

    GRAIN_URL=http://127.0.0.1:8010 pytest -m live -s backend/tests/integration/test_grain_live.py

Lea's side is its real SQLite store and blob store; the scan's still, report and run rows are read from fixtures (one
real photo, deploy/smoke-still.jpg). The runs stop where a design turn would start: a Grain with a model key would
spend money there, so such a Grain is skipped unless LEA_GRAIN_LIVE_TURN=1. A missing GRAIN_URL skips with HUMAN
ACTION NEEDED. Nothing about Grain is faked.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from repairos.handoff import service
from repairos.slots.blob_local import LocalBlobStore
from repairos.slots.grain_http import GrainHTTP
from repairos.slots.state_sqlite import SqliteState

pytestmark = pytest.mark.live
PHOTO = Path(__file__).resolve().parents[3] / "deploy" / "smoke-still.jpg"


@pytest.fixture
def grain():
    url = os.environ.get("GRAIN_URL")
    if not url:
        pytest.skip("HUMAN ACTION NEEDED: GRAIN_URL (a running Grain server)")
    with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=5) as r:
        if json.load(r).get("agent_ready") and os.environ.get("LEA_GRAIN_LIVE_TURN") != "1":
            pytest.skip("this Grain can design (a paid turn would start); set LEA_GRAIN_LIVE_TURN=1 to allow it")
    return GrainHTTP(base_url=url, key=os.environ.get("GRAIN_KEY", ""))


class _State:
    """Lea's real store for the handoff row; the scan's other rows from fixtures (they are only read here)."""

    def __init__(self, real, rows):
        self.real, self.rows = real, rows

    async def get(self, kind, id):
        if kind == service.KIND:
            return await self.real.get(kind, id)
        data = self.rows.get((kind, id))
        return SimpleNamespace(data=data, rev=1) if data else None

    async def list(self, kind, **by):
        if kind == service.KIND:
            return await self.real.list(kind, **by)
        return [SimpleNamespace(data=d, rev=1) for (k, _), d in self.rows.items() if k == kind]

    async def put(self, kind, data, expect_rev=None):
        return await self.real.put(kind, data, expect_rev=expect_rev)


def _ctx(tmp_path, grain, scan_id):
    blobs = LocalBlobStore(tmp_path / "blobs")
    ref = blobs.put(PHOTO.read_bytes(), "image/jpeg")
    rows = {("scan-package", scan_id): {"scan_id": scan_id},
            ("still", "st1"): {"still_id": "st1", "scan_id": scan_id, "kind": "guided", "image_file": ref},
            ("report", "r1"): {"findings": []}}
    return SimpleNamespace(state=_State(SqliteState(tmp_path / "s.sqlite3"), rows), blobs=blobs, slot=lambda _: grain)


def test_a_photo_grain_took_while_its_answer_was_lost_is_not_sent_again(tmp_path, grain):
    scan_id = f"live_{os.urandom(3).hex()}"
    ctx = _ctx(tmp_path, grain, scan_id)
    row = asyncio.run(service.create(ctx.state, {"scan_id": scan_id}, "a mounting plate", None, []))
    hid = row["handoff_id"]
    pid = grain.create_project(f"Lea {hid}")["id"]                    # a first attempt got this far ...
    took = grain.upload_photo(pid, None, f"lea-{hid}-01.jpg", PHOTO.read_bytes())   # ... and lost this answer
    asyncio.run(service.run_handoff(ctx, hid))
    r = asyncio.run(ctx.state.get(service.KIND, hid)).data
    assert r["grain"]["project_id"] == pid and r["grain"]["scan_id"] == took["id"]
    held = [s for s in grain.scans(pid) if s.get("photos")]
    assert len(held) == 1 and len(held[0]["photos"]) == 1               # no second record, no second photo
    assert sum(p["name"] == f"Lea {hid}" for p in grain.projects()) == 1


def test_two_runs_at_once_reach_grain_once(tmp_path, grain):
    scan_id = f"live_{os.urandom(3).hex()}"
    ctx = _ctx(tmp_path, grain, scan_id)
    hid = asyncio.run(service.create(ctx.state, {"scan_id": scan_id}, "a mounting plate", None, []))["handoff_id"]

    async def both():
        return await asyncio.gather(service.run_handoff(ctx, hid), service.run_handoff(ctx, hid))
    a, b = asyncio.run(both())
    assert a["stage"] == b["stage"]                 # the second waited on the first's lease, then had nothing to do
    assert sum(p["name"] == f"Lea {hid}" for p in grain.projects()) == 1
    pid = asyncio.run(ctx.state.get(service.KIND, hid)).data["grain"]["project_id"]
    assert sum(len(s.get("photos") or []) for s in grain.scans(pid)) == 1
