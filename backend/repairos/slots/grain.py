"""GRAIN slot interface (Phase 2, PLAN.md §9). Not in the Phase 1 profile; Phase 2 fills in the Protocol bodies and
a conformance suite in backend/tests/conformance/grain/. Implementations will live in repairos.slots.grain_<impl>."""
from __future__ import annotations

from typing import Protocol


class GrainClient(Protocol):
    """reference_files, bom, electrical, upload_scan, diagnose, call, electrical_check,
    chat_detached + poll, copy_project, interface_version. Shapes: the grain-* schemas in spec/contracts/."""
