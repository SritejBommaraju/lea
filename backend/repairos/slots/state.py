"""STATE slot interface (task B4). Implementations live in repairos.slots.state_<impl>.

Rows are contract objects (spec/contracts/<kind>.schema.json, validated with repairos.contracts.validate) plus a
few internal kinds (INTERNAL_KINDS). Every row has a `rev` (optimistic concurrency) and a `session_id` when it
belongs to one. Writes go through named transitions (TRANSITIONS); each enforces the PLAN.md invariants I1-I4 (and I5/S4 where
they touch a row) and raises a typed InvariantViolation naming the rule.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

# contract kind -> id field
ID_FIELDS: dict[str, str] = {
    "scan-package": "scan_id",
    "still": "still_id",
    "finding": "finding_id",
    "suspect": "suspect_id",
    "next-action": "action_id",
    "meter-reading": "reading_id",
    "part-on-hand": "item_id",
    "repair-plan": "plan_id",
    "reference-design": "product_id",
    "shopping-quote": "quote_id",
    "report": "report_id",
}
# internal kinds (no contract schema): id field
# pipeline-run: one per scan, the Phase 1 pipeline's stage results and error state (task B10, repairos.pipeline)
INTERNAL_KINDS: dict[str, str] = {"session": "session_id", "device": "token_sha256", "pipeline-run": "run_id"}

TRANSITIONS = (
    "session.create", "device.pair",
    "scan.create", "scan.set_alignment",
    "still.create", "still.mark_not_inspected",
    "finding.create",
    "suspect.update_from_fusion", "suspect.confirm", "suspect.clear",
    "next_action.issue",
    "reading.record",
    "part.add",
    "plan.create", "plan.confirm", "plan.reopen",
    "pipeline.update", "pipeline.claim_still",
    "report.publish",
)

JOB_KINDS = ("align", "render", "detect", "segment", "ocr", "wire_check", "compare", "fuse", "crosscheck", "plan",
             "sim_check", "shopping", "pipeline")


class StateError(Exception):
    pass


class NotFound(StateError, KeyError):
    pass


class RevConflict(StateError):
    def __init__(self, kind: str, id: str, expected: int | None, actual: int | None):
        self.kind, self.id, self.expected, self.actual = kind, id, expected, actual
        super().__init__(f"{kind} {id}: expected rev {expected}, store has {actual}")


class AlreadyClaimed(StateError):
    """pipeline.claim_still on a still another worker is inspecting (or has inspected). `status` is its stage."""

    def __init__(self, still_id: str, status: str, worker: str | None):
        self.still_id, self.status, self.worker = still_id, status, worker
        super().__init__(f"still {still_id} is {status} (worker {worker})")


class InvariantViolation(StateError, ValueError):
    """A write that would break a PLAN.md invariant. `rule` is the invariant id (I1, I2, I3, I4, I5, S4) or REF/SCHEMA."""

    def __init__(self, rule: str, message: str):
        self.rule = rule
        super().__init__(f"{rule}: {message}")


@dataclass(frozen=True)
class Row:
    kind: str
    id: str
    rev: int
    session_id: str | None
    data: dict[str, Any]
    updated_at: str
    status: str | None = None  # lifted internal column (repair-plan: open|closed)


@dataclass(frozen=True)
class RowEvent:
    event_id: int
    session_id: str | None
    kind: str
    id: str
    op: Literal["insert", "update", "delete"]
    rev: int
    row: dict[str, Any]
    at: str


@dataclass
class Job:
    kind: str
    session_id: str | None = None
    scan_id: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    job_id: str = ""
    status: Literal["queued", "running", "done", "failed"] = "queued"
    attempts: int = 0
    worker: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = ""
    heartbeat_at: str | None = None


@runtime_checkable
class StateStore(Protocol):
    async def put(self, kind: str, data: Mapping[str, Any], *, expect_rev: int | None = None) -> int:
        """Validate and write one row (rules checked); returns the new rev; RevConflict when expect_rev is stale
        (expect_rev=0 means 'must not exist yet')."""
    async def get(self, kind: str, id: str) -> Row | None: ...
    async def list(self, kind: str, *, session_id: str | None = None, scan_id: str | None = None) -> list[Row]: ...
    async def transition(self, kind: str, id: str, action: str, payload: Mapping[str, Any]) -> Row:
        """Named reducer (TRANSITIONS). Raises InvariantViolation, NotFound or StateError."""


@runtime_checkable
class JobQueue(Protocol):
    async def enqueue(self, job: Job) -> str:
        """Queue a job; returns its id. Idempotent per job_id: a job_id already queued (or run) is left alone."""
    async def claim(self, worker: str, kinds: list[str]) -> Job | None: ...
    async def heartbeat(self, job_id: str) -> None: ...
    async def complete(self, job_id: str, result: dict[str, Any]) -> None: ...
    async def fail(self, job_id: str, error: str, retry: bool) -> None: ...
    async def get_job(self, job_id: str) -> Job | None: ...


@runtime_checkable
class LiveEvents(Protocol):
    async def events_since(self, session_id: str, after: int = 0, limit: int = 500) -> list[RowEvent]: ...
    def subscribe(self, session_id: str, after: int = 0) -> AsyncIterator[RowEvent]:
        """Row changes for one session, oldest first, from event id `after` (exclusive), then live."""
