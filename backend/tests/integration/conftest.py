"""Live tests (paid Claude and Cartesia calls) are opt-in: `pytest -m live backend/tests/integration`. Without `-m live`
they skip, so `make test` stays fast and free. A missing key is HUMAN ACTION NEEDED."""
from __future__ import annotations

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live: calls Claude and Cartesia for real; -m live")


@pytest.fixture(autouse=True)
def _live_opt_in(request):
    if request.node.get_closest_marker("live") and "live" not in (request.config.getoption("-m") or ""):
        pytest.skip("live test (Claude, Cartesia): run with `pytest -m live backend/tests/integration`")
