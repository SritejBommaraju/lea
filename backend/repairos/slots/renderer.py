"""RENDER slot interface (BUILD.md §3.1). Card M.RENDER.0 fills in the Protocol bodies and the conformance suite
in backend/tests/conformance/render/. Implementations live in repairos.slots.render_<impl>."""
from __future__ import annotations

from typing import Protocol


class ExpectedViewRenderer(Protocol):
    """render(reference, pose, intrinsics, size) -> part-id mask, depth,
    projected pins, rows in view."""
