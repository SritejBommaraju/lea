"""OCRB slot interface (task M8, planned). M8 fills in the Protocol bodies and the conformance suite in
backend/tests/conformance/ocrb/. Implementations live in repairos.slots.ocrb_<impl>."""
from __future__ import annotations

from typing import Protocol


class TextReader(Protocol):
    """read(crop) -> regions with text, confidence, polygon."""
