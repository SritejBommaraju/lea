"""Conformance runner (BUILD.md §3.3, B0.9).

    pytest backend/tests/conformance/<slot> [--impl <impl>]

A suite lives in backend/tests/conformance/<slot>/ (slot id in lower case: grain, state, det, ...). Its tests take
the `impl` fixture: the implementation named by --impl, else the one config/providers.yaml selects. When the
implementation's human prerequisites (build/tasks.yaml `human`, checked by build/doctor.py's checks) are not met,
the test is skipped with a loud HUMAN ACTION NEEDED reason. Nothing is faked in their place.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from repairos.registry import Registry

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent


def pytest_addoption(parser):
    parser.addoption("--impl", default=None, help="implementation id to test (default: the configured one)")


def _lib():
    sys.path.insert(0, str(ROOT / "build"))
    try:
        import _lib
    finally:
        sys.path.pop(0)
    return _lib


def missing_prerequisites(slot_id: str, impl: str, env: dict | None = None) -> list[str]:
    """Human steps not met for the card that implements `slot_id`/`impl`, as 'id: what (how)' lines."""
    lib = _lib()
    data = lib.prepare_tasks(lib.load_tasks())
    steps = {s["id"]: s for s in data.get("human_steps", [])}
    human = sorted({h for c in data["cards"] if c.get("slot") == slot_id and c.get("impl") == impl
                    for h in c.get("human", [])})
    checks = lib.run_checks({steps[h]["check"] for h in human if h in steps}, offline=True, env=env)
    out = []
    for h in human:
        ok = lib.human_step_satisfied(steps[h], checks)[0] if h in steps else False
        if ok is False:  # None = a manual step the tools cannot check; it does not block
            out.append(f"{h}: {steps.get(h, {}).get('what', '?')} (how: {steps.get(h, {}).get('how', '?')})")
    return out


def slot_of(path: Path) -> str:
    rel = path.resolve().relative_to(HERE)
    if len(rel.parts) < 2:
        raise pytest.UsageError(f"conformance tests live in backend/tests/conformance/<slot>/, not {path}")
    return rel.parts[0].upper()


@pytest.fixture
def impl(request):
    slot_id = slot_of(Path(request.node.fspath))
    registry = Registry()
    name = request.config.getoption("--impl", default=None) or registry.selected(slot_id)
    missing = missing_prerequisites(slot_id, name)
    if missing:
        pytest.skip(f"HUMAN ACTION NEEDED for {slot_id}={name}: " + "; ".join(missing))
    return registry.load(slot_id, name)
