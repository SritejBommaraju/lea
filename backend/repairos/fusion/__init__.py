"""Evidence fusion (BUILD.md F4.1, F4.4; SPEC A2, I1-I7, S5).

    from repairos.fusion import fuse
    result = fuse(scan_id, findings, readings, symptom="...")

Findings (vision on photos, typed meter readings) become a word state (state.py; numbers never leave code, S5).
The FUSION slot (Jev) answers atomic judgments about each subject (questions.py holds every word of them and every
threshold). Code ranks the suspects (suspects.py), applies the meter-reading override (override.py, I2) and decides
the next action from an explicit truth table (next_action.py). Product-agnostic: refs and names come from the
findings, never from this package (GEN1).
"""
from __future__ import annotations

from repairos.fusion.fuse import FusionResult, fuse

__all__ = ["FusionResult", "fuse"]
