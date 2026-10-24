"""Split one fusion step into requests that fit the engine's context limits (task B7; TypeSafe models.md).

Jev reads the `state` once per request and answers every question against it; a request must fit two limits, the
state plus all questions (per request) and the state plus any one question (per question). A scan with many
judged subjects does not fit one request, so code splits it:

  - a subject's questions always travel together, with that subject's own entry in the state (they are atomic
    questions about one subject and point at it by path);
  - each request's state holds the shared fields (symptom, design trust, evidence rules) and only the subjects it
    asks about, so no request carries detail its questions do not need (jaggedness: large state full of
    irrelevant detail);
  - subjects are packed in order until the next one would pass the budget: SAFETY of each limit, and at most
    TARGET_REQUEST_TOKENS per request so requests stay small, fast and run in parallel;
  - a subject that cannot fit alone raises FusionRequestTooLarge before anything is sent.

The requests run in parallel (at most MAX_PARALLEL at a time). Any failed request fails the whole step: a missing
answer is never filled in (owner rule: Jev down is a blocker).
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from repairos.fusion.state import FusionState, Subject
from repairos.slots.fusion import (REQUEST_OVERHEAD_TOKENS, Answers, ContextLimits, FusionBadAnswer, FusionEngine,
                                   FusionRequestTooLarge, Question, estimate_tokens, question_tokens)

SAFETY = 0.75                  # of each limit: the token count is an estimate (BYTES_PER_TOKEN)
TARGET_REQUEST_TOKENS = 16_000  # per request; a quarter of Jev's limit keeps each call fast and lets calls overlap
MAX_PARALLEL = 4               # requests in flight at once for one fusion step


@dataclass
class Batch:
    state: dict[str, Any]
    questions: list[Question]
    subjects: list[str]          # state["subjects"] keys in this request
    tokens: int                  # estimated per-request tokens
    tokens_one_question: int     # estimated state plus its largest question


def budgets(limits: ContextLimits, target: int | None = TARGET_REQUEST_TOKENS) -> tuple[int, int]:
    """(per-request budget, per-question budget) in estimated tokens."""
    per_request = int(limits.per_request * SAFETY)
    if target:
        per_request = min(per_request, target)
    return per_request, int(limits.per_question * SAFETY)


def plan_batches(fs: FusionState, groups: list[tuple[Subject, list[Question]]], limits: ContextLimits | None,
                 target: int | None = TARGET_REQUEST_TOKENS) -> list[Batch]:
    """Pack (subject, its questions) groups into requests. `limits` None (an engine without declared limits) puts
    everything in one request."""
    shared = {k: v for k, v in fs.state.items() if k != "subjects"}
    if not groups:
        return []
    if limits is None:
        subs = [s.label for s, _ in groups]
        return [_batch(shared, fs, subs, [q for _, qs in groups for q in qs])]
    per_request, per_question = budgets(limits, target)
    base = estimate_tokens({**shared, "subjects": {}}) + REQUEST_OVERHEAD_TOKENS
    out: list[Batch] = []
    cur_subs: list[str] = []
    cur_qs: list[Question] = []
    cur_state = cur_questions = cur_max_q = 0
    for s, qs in groups:
        entry = estimate_tokens({s.label: fs.state["subjects"][s.label]})
        q_tok = [question_tokens(q) for q in qs]
        alone_req, alone_q = base + entry + sum(q_tok), base + entry + max(q_tok, default=0)
        if alone_req > per_request or alone_q > per_question:
            raise FusionRequestTooLarge(
                f"subject {s.label!r} alone needs about {alone_req} tokens (state plus its largest question about "
                f"{alone_q}), over the budget of {per_request} per request / {per_question} per question")
        fits = (cur_subs and base + cur_state + entry + cur_questions + sum(q_tok) <= per_request
                and base + cur_state + entry + max([cur_max_q, *q_tok]) <= per_question)
        if cur_subs and not fits:
            out.append(_batch(shared, fs, cur_subs, cur_qs))
            cur_subs, cur_qs, cur_state, cur_questions, cur_max_q = [], [], 0, 0, 0
        cur_subs.append(s.label)
        cur_qs += qs
        cur_state += entry
        cur_questions += sum(q_tok)
        cur_max_q = max([cur_max_q, *q_tok])
    out.append(_batch(shared, fs, cur_subs, cur_qs))
    return out


def _batch(shared: dict[str, Any], fs: FusionState, subs: list[str], qs: list[Question]) -> Batch:
    state = {**shared, "subjects": {k: fs.state["subjects"][k] for k in subs}}
    st = estimate_tokens(state) + REQUEST_OVERHEAD_TOKENS
    q_tok = [question_tokens(q) for q in qs]
    return Batch(state, list(qs), list(subs), st + sum(q_tok), st + max(q_tok, default=0))


def merge_answers(parts: list[Answers], latency_ms: int) -> Answers:
    """One Answers from the answers of every request of a step. Every request must have been answered by the same
    model version (a step judged by two versions would not be one judgment)."""
    models = {a.model for a in parts}
    if len(models) != 1:
        raise FusionBadAnswer(f"the requests of one fusion step were answered by different models: {sorted(models)}")
    by_id: dict[str, Any] = {}
    conf: dict[str, float] = {}
    for a in parts:
        dup = set(by_id) & set(a.by_id)
        if dup:
            raise FusionBadAnswer(f"question ids answered twice across requests: {sorted(dup)[:3]}")
        by_id.update(a.by_id)
        conf.update(a.confidence)
    return Answers(by_id=by_id, model=models.pop(), latency_ms=latency_ms, confidence=conf)


def ask_batches(engine: FusionEngine, batches: list[Batch], max_parallel: int = MAX_PARALLEL) -> Answers:
    """Send every request (in parallel, capped) and merge the answers. The first failure is raised."""
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, min(max_parallel, len(batches)))) as pool:
        parts = list(pool.map(lambda b: engine.ask(b.state, b.questions), batches))
    return merge_answers(parts, int((time.perf_counter() - t0) * 1000))
