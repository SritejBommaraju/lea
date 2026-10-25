"""Unit tests never reach a paid web service: Bright Data is off for every test, whatever backend/.env holds (a test
that needs it passes its own credentials and replaces the HTTP request)."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_bright_data(monkeypatch):
    from repairos.references import brightdata

    monkeypatch.setattr(brightdata, "credentials", lambda: None)
