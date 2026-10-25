"""GRAIN slot interface (Phase 2, PLAN.md §9): the Grain API calls Lea makes, as field shapes only (A10).
Implementations live in repairos.slots.grain_<impl>; `http` is the REST client (repairos.slots.grain_http).

Shapes (Grain's REST surface; units and frames are Lea's):
  project   {"id", "name", "busy": bool, "waiting": bool, "turns": int,
             "last_build": {"ok", "errors": int, "artifacts": [name]} | None}
  scan      {"id", "name", "units", "points", "photos": [...] , "kind"?}         POST body: the file, units declared
  message   {"role", "type": "text"|"tool_use"|"tool_result"|"question"|..., ...}; a question carries
            {"id", "why", "questions": [{"id", "text", "options", "free_text"}]}
  report    the build report: {"ok", "errors": [str], "warnings": [str], "domains": {"fit": {...}, ...}} | None
A failed call raises GrainError with the HTTP status (409: the project is busy with a turn).
"""
from __future__ import annotations

from typing import Any, Protocol


class GrainError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        self.status = status
        super().__init__(f"Grain {status}: {message}" if status else f"Grain unreachable: {message}")

    @property
    def busy(self) -> bool:
        return self.status == 409

    @property
    def transient(self) -> bool:
        """Worth retrying later: unreachable, busy or a server error, never our request."""
        return self.status is None or self.status == 409 or self.status >= 500


class GrainClient(Protocol):
    def projects(self) -> list[dict[str, Any]]: ...
    def create_project(self, name: str) -> dict[str, Any]: ...
    def project(self, project_id: str) -> dict[str, Any]: ...
    def upload_scan(self, project_id: str, name: str, data: bytes, units: str) -> dict[str, Any]: ...
    def upload_photo(self, project_id: str, scan_id: str | None, name: str, data: bytes) -> dict[str, Any]:
        """Attach a JPEG to a scan record, or with scan_id None start a photo-only record."""
    def scans(self, project_id: str) -> list[dict[str, Any]]: ...
    def write_file(self, project_id: str, path: str, content: str) -> None:
        """A text file under src/ (Lea writes only src/LEA_CAPTURE.json)."""
    def read_file(self, project_id: str, path: str) -> bytes:
        """A file under src/ or out/ (out/model.step, out/model.glb, ...)."""
    def files(self, project_id: str) -> list[dict[str, Any]]: ...
    def start_turn(self, project_id: str, message: str) -> dict[str, Any]:
        """A detached design turn: returns at once; poll project() and messages()."""
    def messages(self, project_id: str) -> list[dict[str, Any]]: ...
    def report(self, project_id: str) -> dict[str, Any] | None: ...
