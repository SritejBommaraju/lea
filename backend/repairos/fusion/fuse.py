"""One fusion step for a scan: findings + readings -> ranked Suspects + one NextAction (task B7).

A FUSION engine failure raises FusionUnavailable to the caller (owner: Jev down is a blocker, never a degrade).
`insufficient_evidence` is a real outcome with a reason (I4).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from repairos.fusion.next_action import decide
from repairos.fusion.questions import build_questions
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


def fuse(scan_id: str, findings: list[Any], readings: list[Any] = (), symptom: str | None = None, *,
         engine: FusionEngine | None = None, actions: dict[str, dict[str, Any]] | None = None,
         expectations: dict[tuple[str, str], dict[str, Any]] | None = None, photos_requested: int = 0,
         seed: int | None = None, now: datetime | None = None) -> FusionResult:
    fs = build_state(scan_id, findings, readings, symptom, actions)
    questions, base = build_questions(fs, seed)
    answers: Answers | None = None
    judgments: dict[tuple[str, str], Judgment] = {}
    if questions:   # no subject -> nothing to judge; the decision below reports insufficient evidence
        if engine is None:
            from repairos.registry import slot
            engine = slot("FUSION")
        answers = engine.ask(fs.state, questions)
        check_answers(questions, answers)
        judgments = read_judgments(fs, answers, base)
    model = answers.model if answers else "none"
    suspects = rank_suspects(fs, judgments, model, now)
    action = decide(fs, suspects, judgments, photos_requested, expectations, now)
    insufficient = action["kind"] == "report_insufficient_evidence"
    return FusionResult("insufficient_evidence" if insufficient else "suspects",
                        action.get("insufficient_evidence_reason") if insufficient else None,
                        suspects, action, fs, questions, answers, judgments, base)
