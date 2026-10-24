"""Slot policy and lazy imports for DET/VLM/LLM implementations."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from repairos.slots.vlm import CANNOT_TELL, escalate, with_cannot_tell

BACKEND = Path(__file__).resolve().parents[3]


def test_escalation_stops_at_first_answer():
    asked = []
    res, idx = escalate(["small", "large"], lambda m: asked.append(m) or ("ok" if m == "small" else "x"), lambda r: r)
    assert (res, idx, asked) == ("ok", 0, ["small"])


def test_escalation_on_cannot_tell():
    asked = []
    res, idx = escalate(["small", "large"], lambda m: asked.append(m) or (CANNOT_TELL if m == "small" else "loose"),
                        lambda r: r)
    assert (res, idx, asked) == ("loose", 1, ["small", "large"])
    res, idx = escalate(["small", "large"], lambda m: CANNOT_TELL, lambda r: r)
    assert (res, idx) == (CANNOT_TELL, 1)
    with pytest.raises(ValueError):
        escalate([], lambda m: m, lambda r: r)


def test_cannot_tell_always_an_option():
    assert with_cannot_tell(["seated", "unplugged", "seated"]) == ["seated", "unplugged", CANNOT_TELL]


@pytest.mark.parametrize("module", ["repairos.slots.det_gdino", "repairos.slots.det_owlv2",
                                    "repairos.slots.vlm_anthropic", "repairos.slots.llm_anthropic",
                                    "repairos.vision.inspect"])
def test_import_is_light(module):
    """Selecting/importing an implementation must not load torch, transformers or the SDK (BUILD §3.2)."""
    code = (f"import sys, {module}; heavy = [m for m in ('torch', 'transformers', 'anthropic', 'PIL') "
            "if m in sys.modules]; print(heavy); sys.exit(1 if heavy else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_implementations_register():
    from repairos import registry

    import repairos.slots.det_gdino  # noqa: F401
    import repairos.slots.det_owlv2  # noqa: F401
    import repairos.slots.llm_anthropic  # noqa: F401
    import repairos.slots.vlm_anthropic  # noqa: F401
    for key in [("DET", "gdino"), ("DET", "owlv2"), ("VLM", "anthropic"), ("LLM", "anthropic")]:
        assert key in registry._FACTORIES
