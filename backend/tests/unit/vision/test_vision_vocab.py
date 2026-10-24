"""M.DET.0: one generic vocabulary, runtime prompt building (GEN1)."""
from __future__ import annotations

import re

from repairos.vision import generic_classes as gc
from repairos.vision.prompts import build_prompts, chunk, normalise_phrase


def test_classes_are_generic_lowercase_unique():
    for name in gc.GENERIC_CLASSES:
        assert name == name.lower().strip() and name, name
        assert "." not in name and not re.search(r"\d", name), name  # '.' separates prompts; digits = part numbers
    assert len(set(gc.GENERIC_CLASSES)) == len(gc.GENERIC_CLASSES)
    assert set(gc.WIRE_CLASSES) <= set(gc.GENERIC_CLASSES)


def test_owner_minimum_classes_present():
    want = {"connector", "wire", "cable", "capacitor", "electrolytic capacitor", "resistor", "fuse", "switch", "button",
            "battery", "motor", "screw", "led", "solder joint", "burn mark", "crack", "corrosion"}
    assert want <= set(gc.GENERIC_CLASSES)


def test_crop_states_always_allow_cannot_tell():
    assert gc.CROP_STATES[-1] == "cannot_tell" and "ok" in gc.CROP_STATES


def test_build_prompts_adds_normalised_runtime_phrases_once():
    ps = build_prompts(["10uF Electrolytic Capacitor", "XH2.54-2P Connector Housing", "", "relay"])
    assert ps[: len(gc.GENERIC_CLASSES)] == list(gc.GENERIC_CLASSES)
    extra = ps[len(gc.GENERIC_CLASSES):]
    assert extra == ["connector housing"]  # numbers and part-number tokens stripped; duplicates of classes dropped
    assert normalise_phrase("  M3x8 Screw!! ") == "screw"


def test_build_prompts_caps_runtime_phrases():
    extra = build_prompts([f"thing {chr(97 + i % 26)}{chr(97 + i // 26)}" for i in range(100)])
    assert len(extra) - len(gc.GENERIC_CLASSES) == 40


def test_chunk():
    assert chunk(list("abcde"), 2) == [["a", "b"], ["c", "d"], ["e"]]
