"""GRAIN slot interface (BUILD.md §3.1). Card M.GRAIN.0 fills in the Protocol bodies and the conformance suite
in backend/tests/conformance/grain/. Implementations live in repairos.slots.grain_<impl>."""
from __future__ import annotations

from typing import Protocol


class GrainClient(Protocol):
    """reference_files, bom, electrical, upload_scan, diagnose, call, electrical_check,
    chat_detached + poll, copy_project, interface_version. Shapes: the grain-* schemas in spec/contracts/."""
