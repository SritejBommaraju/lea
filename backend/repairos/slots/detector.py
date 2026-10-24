"""DET slot interface (BUILD.md §3.1): open-vocabulary detection from text prompts on a full-resolution still.

Implementations live in repairos.slots.det_<impl>/ (e.g. det_gdino) and register with
`@repairos.registry.register("DET", "<impl>")`. Keep heavy imports (torch, transformers, weights) inside the
implementation's constructor or first call, never at module import time, so the backend starts fast without them.
Prompts come from generic classes + BOM text (never product names in core code, GEN1). The conformance suite lives
in backend/tests/conformance/det/.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Detection:
    box: tuple[float, float, float, float]  # x0, y0, x1, y1 in pixels of the image passed to detect()
    label: str                              # the prompt that matched, verbatim
    score: float                            # 0..1, the detector's own score (not a calibrated probability, I6)


class Detector(Protocol):
    model_id: str  # which model and weights produced the boxes (recorded with every finding)

    def detect(self, image: Any, prompts: list[str], min_score: float | None = None) -> list[Detection]:
        """Boxes for `prompts` in `image`: an H x W x 3 uint8 RGB array (or PIL image) at full resolution.
        The implementation may downscale internally; returned boxes are always in the input image's pixels.
        `min_score` None = the implementation's configured default threshold."""
        ...
