"""No unit test reaches the design engine because backend/.env names one: the gateway builds its client from the
settings only when a test does not pass one (`create_app(grain=...)`), and then it is off."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_design_engine(monkeypatch):
    from repairos.grain.client import GrainClient

    monkeypatch.setattr(GrainClient, "from_settings", classmethod(lambda cls, settings=None, transport=None: cls(None)))
