"""Every test counts paid calls in its own spend file (repairos.spend), never in the live data dir's counters."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _own_spend_file(tmp_path, monkeypatch):
    monkeypatch.setenv("REPAIROS_SPEND_DB", str(tmp_path / "spend.sqlite3"))
