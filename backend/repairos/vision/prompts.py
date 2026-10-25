"""Detector prompt sets built at runtime (task M1).

Phase 1: the generic classes only. Phase 2 adds noun phrases from the reference BOM at runtime (`extra_phrases`);
nothing product-specific is ever stored in code (GEN1).
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from repairos.vision.generic_classes import GENERIC_CLASSES

MAX_EXTRA_PHRASES = 40
CHUNK_SIZE = 16  # prompts per detector call: open-vocabulary detectors lose accuracy with very long prompt lists

_MPN_OR_NUMBER = re.compile(r"\b(?=[a-z0-9-]*\d)[a-z0-9-]+\b")  # tokens with a digit: part numbers, values, sizes
_NON_WORD = re.compile(r"[^a-z ]+")


def normalise_phrase(text: str) -> str:
    """Lower case, digits/part-number tokens and punctuation removed, spaces collapsed."""
    t = _MPN_OR_NUMBER.sub(" ", text.lower())
    return " ".join(_NON_WORD.sub(" ", t).split())


def build_prompts(extra_phrases: Iterable[str] = ()) -> list[str]:
    """Generic classes, then up to MAX_EXTRA_PHRASES normalised runtime phrases (BOM text in Phase 2), deduped."""
    out = list(dict.fromkeys(GENERIC_CLASSES))
    seen = set(out)
    added = 0
    for raw in extra_phrases:
        p = normalise_phrase(raw)
        if p and p not in seen and added < MAX_EXTRA_PHRASES:
            out.append(p)
            seen.add(p)
            added += 1
    return out


def chunk(prompts: list[str], size: int = CHUNK_SIZE) -> list[list[str]]:
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    return [prompts[i:i + size] for i in range(0, len(prompts), size)]
