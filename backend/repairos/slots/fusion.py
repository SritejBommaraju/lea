"""FUSION slot interface (BUILD.md §3.1, card M.FUSION.0). Implementations live in repairos.slots.fusion_<impl>.

    engine = repairos.registry.slot("FUSION")
    answers = engine.ask(state_words, questions)

The engine fuses evidence; it never perceives (A2). It gets words only: `state` and every question must pass
`assert_words_only` (S5) before anything leaves the process. Code owns everything around the call: the word state
(repairos.fusion.state), the option shuffle and its seed, the meter-reading override (I2) and the expansion of the
chosen action (repairos.fusion.next_action). There is no fallback engine behind an implementation; an engine that
cannot answer raises `FusionUnavailable` (owner, 2026-10-24).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

QuestionType = Literal["noul", "choice"]

# A numeric token that is not part of an identifier: "0.72", "2", "5%", "~2 mm". Identifiers that carry digits
# ("J1", "J1.1", "s7", "kf_000", "3V3", "WIRE_12") do not match: a digit glued to a letter, "_", "." or ":" on its
# left, or to a letter or digit on its right, is part of a name.
NUMERIC_TOKEN = re.compile(r"(?<![A-Za-z_\-.:\d])\d+(?:[.,]\d+)?(?![A-Za-z_\d]|[.,]\d)")
# A number glued to a unit or rating: "5V", "1.5V", "2mm", "10k", "22AWG", "500mAh". "3V3" stays a name.
UNIT_TOKEN = re.compile(r"(?<![A-Za-z_\-.:\d])\d+(?:[.,]\d+)?\s?(?:[kKmMuµnp]?(?:V|A|W|Hz|F|Ω|ohms?|Ohms?)|mm|cm|um|m"
                        r"|mAh|Wh|AWG|k|M|s|ms|°C|C)(?![A-Za-z\d_])")


class FusionError(RuntimeError):
    """Base for fusion failures. Surfaced to the caller; never hidden behind another engine."""


class FusionUnavailable(FusionError):
    """The engine could not answer (network, auth, rate limit, timeout, server error). A blocker, not a degrade."""


class FusionBadAnswer(FusionError):
    """The engine answered, but the answer breaks the contract (missing id, probabilities out of range, ...)."""


class S5Violation(ValueError):
    """A number would leave code (S5: numbers are compared in code; Jev and the LLM receive words)."""


@dataclass(frozen=True)
class Question:
    """One atomic question. `options` (choice only) keeps the order it is sent in: code shuffles it with `seed`
    before building the Question, and the engine must send it in exactly this order (first-option bias)."""

    id: str
    type: QuestionType
    instructions: str
    options: dict[str, str] = field(default_factory=dict)   # choice: option -> when it applies; noul: {} or true/false
    seed: int | None = None

    def __post_init__(self):
        if self.type not in ("noul", "choice"):
            raise ValueError(f"question {self.id}: unknown type {self.type!r}")
        if self.type == "choice" and not self.options:
            raise ValueError(f"question {self.id}: a choice needs options")
        if self.type == "noul" and self.options and set(self.options) != {"true", "false"}:
            raise ValueError(f"question {self.id}: noul criteria are exactly 'true' and 'false'")


@dataclass(frozen=True)
class Answers:
    """`by_id[qid]` is P(yes) for a noul and {option: p} for a choice. `confidence[qid]` when the engine gives one."""

    by_id: dict[str, float | dict[str, float]]
    model: str
    latency_ms: int
    confidence: dict[str, float] = field(default_factory=dict)

    def noul(self, qid: str) -> float:
        v = self.by_id[qid]
        if not isinstance(v, float):
            raise FusionBadAnswer(f"{qid} is not a noul answer")
        return v

    def choice(self, qid: str) -> dict[str, float]:
        v = self.by_id[qid]
        if not isinstance(v, dict):
            raise FusionBadAnswer(f"{qid} is not a choice answer")
        return v


@runtime_checkable
class FusionEngine(Protocol):
    """ask(state_words, questions[noul/choice]) -> Answers (probabilities, model id, latency)."""

    model: str

    def ask(self, state: dict[str, Any], questions: list[Question]) -> Answers: ...


def numeric_tokens(text: str, allowed: tuple[str, ...] = ()) -> list[str]:
    """Numeric tokens in `text` after removing the `allowed` identifiers (refs and pins)."""
    for name in sorted(allowed, key=len, reverse=True):
        text = text.replace(name, " ")
    return NUMERIC_TOKEN.findall(text) + UNIT_TOKEN.findall(text)


def identifiers(state: Any) -> tuple[str, ...]:
    """Refs and pins named in the state (values of keys "ref" and "pins"): the names S5 lets through."""
    out: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "ref" and isinstance(v, str):
                    out.add(v)
                elif k == "pins" and isinstance(v, list):
                    out.update(x for x in v if isinstance(x, str))
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(state)
    return tuple(sorted(out))


def assert_words_only(state: Any, questions: list[Question] | tuple = ()) -> None:
    """S5 guard: raise S5Violation if a JSON number or a numeric token appears in the state or in any question's
    instructions or options. Booleans and identifiers (refs and pins declared in the state; names with digits glued
    to letters, such as still ids) are allowed."""
    allowed = identifiers(state)

    def walk(node: Any, path: str) -> None:
        if isinstance(node, bool) or node is None:
            return
        if isinstance(node, (int, float)):
            raise S5Violation(f"number at {path}: numbers stay in code (S5)")
        if isinstance(node, str):
            hits = numeric_tokens(node, allowed)
            if hits:
                raise S5Violation(f"numeric token {hits[0]!r} at {path}: numbers stay in code (S5)")
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(k, f"{path}.<key>")
                walk(v, f"{path}.{k}")
            return
        if isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
            return
        raise S5Violation(f"unsupported value at {path}: {type(node).__name__}")

    walk(state, "state")
    for q in questions:
        walk(q.instructions, f"questions.{q.id}.instructions")
        walk(q.options, f"questions.{q.id}.options")
    json.dumps(state)  # the state must be plain JSON


def check_answers(questions: list[Question], answers: Answers, tol: float = 0.02) -> None:
    """Contract on an engine's answers: every question answered, probabilities in [0, 1], a choice covers exactly
    its options and sums to 1 (within `tol`). Raises FusionBadAnswer."""
    for q in questions:
        if q.id not in answers.by_id:
            raise FusionBadAnswer(f"no answer for {q.id}")
        v = answers.by_id[q.id]
        if q.type == "noul":
            if not isinstance(v, float) or not 0.0 <= v <= 1.0:
                raise FusionBadAnswer(f"{q.id}: noul {v!r} is not a probability")
        else:
            if not isinstance(v, dict) or set(v) != set(q.options):
                raise FusionBadAnswer(f"{q.id}: choice options {sorted(v) if isinstance(v, dict) else v!r} != "
                                      f"{sorted(q.options)}")
            if any(not 0.0 <= p <= 1.0 for p in v.values()) or abs(sum(v.values()) - 1.0) > tol:
                raise FusionBadAnswer(f"{q.id}: choice probabilities do not form a distribution")
