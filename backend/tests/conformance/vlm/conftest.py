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


import os  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[4]
# REPAIROS_SAMPLES_DIR points the suite at another folder of REAL recorded sessions (same layout as samples/).
SAMPLES = Path(os.environ.get("REPAIROS_SAMPLES_DIR") or ROOT / "samples") / "sessions"
HUMAN_CAPTURE = ("HUMAN ACTION NEEDED: record a real capture (samples/): samples/sessions/<session_id>/stills/"
                 "<still_id>.jpg from the phone (C1.6/C1.8). No synthetic images are used in their place.")


@pytest.fixture(scope="session")
def stills() -> list[Path]:
    found = sorted(p for p in SAMPLES.glob("*/stills/*.jpg") if "heldout" not in p.parts) if SAMPLES.is_dir() else []
    if not found:
        pytest.skip(HUMAN_CAPTURE)
    return found[:3]
