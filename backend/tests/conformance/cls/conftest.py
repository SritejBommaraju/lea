"""CLS conformance (task M7) on REAL images only. Heavy (model weights) and live (paid API) tests are opt-in:
`pytest -m vision backend/tests/conformance/cls [--impl dinov2]`. Without `-m vision` they skip."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
VISA = ROOT / "data" / "datasets" / "visa" / "raw"
NEED_VISA = ("HUMAN ACTION NEEDED: download VisA (CC BY 4.0, training/datasets/MANIFEST.json) and extract it to "
             "data/datasets/visa/raw/")


def pytest_configure(config):
    config.addinivalue_line("markers", "vision: heavy model or live API perception test; run with -m vision")


@pytest.fixture(autouse=True)
def _vision_opt_in(request):
    if request.node.get_closest_marker("vision") and "vision" not in (request.config.getoption("-m") or ""):
        pytest.skip("heavy/live perception test: run with `pytest -m vision`")


@pytest.fixture(scope="session")
def cls_impl(request):
    """--impl, else the configured CLS, else dinov2 when the profile says CLS=none."""
    from repairos.registry import Registry

    if "vision" not in (request.config.getoption("-m") or ""):  # session fixtures run before the autouse skip
        pytest.skip("heavy/live perception test: run with `pytest -m vision`")
    reg = Registry()
    name = request.config.getoption("--impl", default=None) or reg.selected("CLS")
    return reg.load("CLS", "dinov2" if name == "none" else name)


@pytest.fixture(scope="session")
def heldout_rows() -> list[dict]:
    """VisA pcb4 rows: a board design the classifier never trained on (held out in the MANIFEST)."""
    anno = VISA / "pcb4" / "image_anno.csv"
    if not anno.exists():
        pytest.skip(NEED_VISA)
    return list(csv.DictReader(anno.open()))
