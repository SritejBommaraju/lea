"""Heavy (model weights) and live (paid API) tests here are opt-in: `pytest -m vision backend/tests`.
Without `-m vision` they skip, so `make test` stays fast and free."""
from __future__ import annotations

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "vision: heavy model or live API perception test; run with -m vision")


@pytest.fixture(autouse=True)
def _vision_opt_in(request):
    if request.node.get_closest_marker("vision") and "vision" not in (request.config.getoption("-m") or ""):
        pytest.skip("heavy/live perception test: run with `pytest -m vision`")
