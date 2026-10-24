"""Live tests (paid Claude and Jev calls, detector weights) are opt-in: `pytest -m live backend/tests/integration`.
Without `-m live` they skip, so `make test` stays fast and free. A missing key or sample is HUMAN ACTION NEEDED."""
from __future__ import annotations

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live: calls Claude and Jev for real (and loads detector weights); -m live")


@pytest.fixture(autouse=True)
def _live_opt_in(request):
    if request.node.get_closest_marker("live") and "live" not in (request.config.getoption("-m") or ""):
        pytest.skip("live test (Claude, Jev, detector weights): run with `pytest -m live backend/tests/integration`")
