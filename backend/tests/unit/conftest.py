"""Unit tests never reach a paid web service: Bright Data is off for every test, whatever backend/.env holds (a test
that needs it passes its own credentials and replaces the HTTP request)."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_bright_data(monkeypatch):
    from repairos.references import brightdata

    monkeypatch.setattr(brightdata, "credentials", lambda: None)


@pytest.fixture(autouse=True)
def _no_design_engine(monkeypatch):
    """No test reaches the design engine because backend/.env names one: the gateway builds its client from the
    settings only when a test does not pass one (`create_app(grain=...)`), and then it is off."""
    from repairos.grain.client import GrainClient

    monkeypatch.setattr(GrainClient, "from_settings", classmethod(lambda cls, settings=None, transport=None: cls(None)))
