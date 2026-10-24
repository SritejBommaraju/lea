"""One fusion step for a scan: findings + readings -> ranked Suspects + one NextAction (task B7).

A FUSION engine failure raises FusionUnavailable to the caller (owner: Jev down is a blocker, never a degrade).
`insufficient_evidence` is a real outcome with a reason (I4). Subjects nobody inspected are decided in code and
never asked about (I3); the rest are split into requests that fit the engine's context limits (fusion/batches.py),
sent in parallel, and every one must be answered.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from repairos.fusion.batches import TARGET_REQUEST_TOKENS, Batch, ask_batches, plan_batches
from repairos.fusion.next_action import decide
from repairos.fusion.questions import question_groups
from repairos.fusion.state import FusionState, build_state
from repairos.fusion.suspects import Judgment, rank_suspects, read_judgments
from repairos.slots.fusion import Answers, FusionEngine, Question, check_answers


@dataclass
class FusionResult:
    outcome: str                         # "suspects" | "insufficient_evidence"
    reason: str | None                   # why, when the outcome is insufficient_evidence
    suspects: list[dict[str, Any]]       # Suspect contract dicts, rank 1 first
    next_action: dict[str, Any]          # NextAction contract dict
    fusion_state: FusionState
    questions: list[Question]
    answers: Answers | None
    judgments: dict[tuple[str, str], Judgment]
    seed: int
    batches: list[Batch]                 # the requests sent (empty when nothing needed judging)


def fuse(scan_id: str, findings: list[Any], readings: list[Any] = (), symptom: str | None = None, *,
         engine: FusionEngine | None = None, actions: dict[str, dict[str, Any]] | None = None,
         expectations: dict[tuple[str, str], dict[str, Any]] | None = None, photos_requested: int = 0,
         seed: int | None = None, now: datetime | None = None,
         request_tokens: int | None = TARGET_REQUEST_TOKENS, still_words: dict[str, str] | None = None
         ) -> FusionResult:
    """`still_words` ({still_id: "photo 3"}) names subjects in plain words in the next step (state.subject_words)."""
    fs = build_state(scan_id, findings, readings, symptom, actions, still_words)
    groups, base = question_groups(fs, seed)
    questions = [q for _, qs in groups for q in qs]
    answers: Answers | None = None
    judgments: dict[tuple[str, str], Judgment] = {}
    batches: list[Batch] = []
    if questions:   # nothing judged -> code decides alone (not inspected, or no subject: insufficient evidence)
        if engine is None:
            from repairos.registry import slot
            engine = slot("FUSION")
        batches = plan_batches(fs, groups, getattr(engine, "context_limits", None), request_tokens)
        answers = ask_batches(engine, batches)
        check_answers(questions, answers)
        judgments = read_judgments(fs, answers, base)
    model = answers.model if answers else "none"
    suspects = rank_suspects(fs, judgments, model, now)
    action = decide(fs, suspects, judgments, photos_requested, expectations, now)
    insufficient = action["kind"] == "report_insufficient_evidence"
    return FusionResult("insufficient_evidence" if insufficient else "suspects",
                        action.get("insufficient_evidence_reason") if insufficient else None,
                        suspects, action, fs, questions, answers, judgments, base, batches)
