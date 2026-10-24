"""SEG slot interface (BUILD.md §3.1). Card M.SEG.0 fills in the Protocol bodies and the conformance suite
in backend/tests/conformance/seg/. Implementations live in repairos.slots.seg_<impl>."""
from __future__ import annotations

from typing import Protocol


class Segmenter(Protocol):
    """segment(crop, boxes, points_pos, points_neg) -> masks with scores."""
