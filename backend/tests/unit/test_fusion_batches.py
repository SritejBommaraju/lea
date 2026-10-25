"""B7: what goes to Jev and in how many requests. No network: the only engine used is the real Jev client pointed
at a closed port (an outage) or refused before sending. The live split is in test_jev.py (-k live)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from repairos.fusion import fuse
from repairos.fusion.batches import (SAFETY, TARGET_REQUEST_TOKENS, ask_batches, budgets, merge_answers,
                                     plan_batches)
from repairos.fusion.questions import build_questions, judged_subjects, question_groups
from repairos.fusion.state import build_state
from repairos.slots.fusion import (Answers, ContextLimits, FusionBadAnswer, FusionRequestTooLarge, FusionUnavailable,
                                   Question, estimate_tokens, request_tokens)
from repairos.slots.fusion_jev import CONTEXT, JevEngine

ROOT = Path(__file__).resolve().parents[3]
RECORDED = [ROOT / "samples" / "fusion" / "jev_smoke.json", *sorted((ROOT / "data" / "fusion" / "jev").glob("*.json"))]
NOTE = ("vision model: the part sits flat in its socket, the latch is closed and the solder joints look even; the "
        "view is sharp and well lit, and nothing hides the edges")


def finding(fid, ref, state, notes=(NOTE,), still="s1"):
    return {"finding_id": fid, "scan_id": "scan_b", "subject": {"kind": "part", "ref": ref}, "state": state,
            "source": "repairos.vision", "confidence": 0.0 if state == "not_inspected" else 0.6, "is_proof": False,
            "evidence": list(notes), "evidence_refs": {"still_ids": [still]}, "created_at": "2026-10-24T10:00:00Z"}


def real_photo_scan(judged: int = 199, not_inspected: int = 900):
    """The shape of the first real-photo session that broke the fuse stage: 12 stills, 1,099 findings, 900 of
    them not_inspected (counts from data/replay/sess_blind1, 2026-10-24)."""
    out = []
    for i in range(judged):
        out.append(finding(f"f_j{i:04d}", f"s{i % 12 + 1}/det{i}", ("ok", "insufficient_evidence", "unseated")[i % 3]))
    for i in range(not_inspected):
        out.append(finding(f"f_n{i:04d}", f"s{i % 12 + 1}/det{1000 + i}", "not_inspected",
                           notes=(f"region labelled 'screw' in still s{i % 12 + 1} was detected but not inspected: "
                                  "over this still's crop budget",)))
    return out


def subject_paths(q: Question) -> set[str]:
    return set(re.findall(r'`subjects\["([^"]+)"\]`', q.instructions))


# ------------------------------------------------------------------------------- not inspected never goes to Jev
def test_not_inspected_only_subjects_get_no_questions():
    fs = build_state("scan_b", [finding("f1", "s1/det0", "not_inspected"), finding("f2", "s1/det1", "ok"),
                                finding("f3", "s1/det2", "not_inspected"), finding("f4", "s1/det2", "damaged")])
    assert [s.ref for s in judged_subjects(fs)] == ["s1/det1", "s1/det2"]   # det2 has a judged finding too
    qs, _ = build_questions(fs)
    assert {p for q in qs for p in subject_paths(q)} == {"part s1/det1", "part s1/det2"}


def test_a_scan_with_nothing_inspected_is_decided_in_code_without_any_request():
    fds = [finding(f"f{i}", f"s1/det{i}", "not_inspected") for i in range(30)]
    res = fuse("scan_b", fds, symptom="it stopped working", engine=None)   # no engine is even loaded
    assert res.batches == [] and res.answers is None and res.questions == []
    assert {s["verdict"] for s in res.suspects} == {"not_inspected"}
    assert res.next_action["kind"] == "request_closeup"


# ------------------------------------------------------------------------------------------------ batching
def test_the_real_photo_session_fits_in_several_requests_under_the_budget():
    fs = build_state("scan_b", real_photo_scan(), symptom="it was working and then stopped working")
    groups, _ = question_groups(fs)
    assert len(groups) == 199
    one_request, _ = request_tokens(fs.state, [q for _, qs in groups for q in qs])
    assert one_request > CONTEXT.per_request            # the failure: everything in one request does not fit
    batches = plan_batches(fs, groups, CONTEXT)
    per_request, per_question = budgets(CONTEXT)
    assert per_request == min(TARGET_REQUEST_TOKENS, int(CONTEXT.per_request * SAFETY))
    assert len(batches) > 1
    asked = [k for b in batches for k in b.subjects]
    assert sorted(asked) == sorted(s.label for s, _ in groups)                     # every judged subject, once
    assert asked == [s.label for s, _ in groups]                                    # in order
    for b in batches:
        total, one = request_tokens(b.state, b.questions)
        assert (total, one) == (b.tokens, b.tokens_one_question)
        assert total <= per_request and one <= per_question
        assert set(b.state["subjects"]) == set(b.subjects)                          # only its own subjects
        assert {k: v for k, v in b.state.items() if k != "subjects"} == \
            {k: v for k, v in fs.state.items() if k != "subjects"}                  # shared fields in every one
        for q in b.questions:
            assert subject_paths(q) <= set(b.subjects)                              # every path resolves
    by_subject = {s.label: {q.id for q in qs} for s, qs in groups}
    for b in batches:                                                               # a subject's questions together
        ids = {q.id for q in b.questions}
        assert all(by_subject[k] <= ids for k in b.subjects)


def test_a_tighter_target_means_more_smaller_requests():
    fs = build_state("scan_b", real_photo_scan(judged=40, not_inspected=0))
    groups, _ = question_groups(fs)
    wide, tight = plan_batches(fs, groups, CONTEXT), plan_batches(fs, groups, CONTEXT, target=4000)
    assert len(tight) > len(wide) and all(b.tokens <= 4000 for b in tight)
    assert len(plan_batches(fs, groups, None)) == 1     # an engine without declared limits: one request


def test_a_subject_that_cannot_fit_alone_is_refused_before_sending():
    fs = build_state("scan_b", [finding("f1", "s1/det0", "ok", notes=[NOTE] * 400)])
    groups, _ = question_groups(fs)
    with pytest.raises(FusionRequestTooLarge, match="alone"):
        plan_batches(fs, groups, CONTEXT)


def test_the_jev_client_refuses_an_oversized_request_before_sending():
    eng = JevEngine(key="unused", url="http://127.0.0.1:9/v1/systemone", record_dir=None)
    fs = build_state("scan_b", real_photo_scan())
    qs, _ = build_questions(fs)
    with pytest.raises(FusionRequestTooLarge, match="exceeds the limits"):
        eng.ask(fs.state, qs)
    assert eng.last_exchange is None


def test_the_token_estimate_is_on_the_safe_side_of_recorded_real_exchanges():
    seen = 0
    for p in RECORDED:
        if not p.exists():
            continue
        rec = json.loads(p.read_text())
        usage = (rec.get("response") or {}).get("usage")
        if not usage:
            continue
        qs = [Question(qid, q["type"], q.get("instructions") or "", dict(q.get("criteria") or {}))
              for qid, q in rec["request"]["questions"].items()]
        total, _ = request_tokens(rec["request"]["state"], qs)
        assert total >= usage["input_tokens"], (p.name, total, usage["input_tokens"])
        seen += 1
    if not seen:
        pytest.skip("HUMAN ACTION NEEDED: no recorded Jev exchange with usage; run tools/jev_smoke.py")


# ------------------------------------------------------------------------------------- answers of several requests
def _answers(model, ids):
    return Answers(by_id={i: 0.5 for i in ids}, model=model, latency_ms=10)


def test_answers_of_several_requests_merge_into_one():
    a = merge_answers([_answers("jev-1.14.0", ["a", "b"]), _answers("jev-1.14.0", ["c"])], latency_ms=42)
    assert set(a.by_id) == {"a", "b", "c"} and a.model == "jev-1.14.0" and a.latency_ms == 42


def test_two_model_versions_or_a_question_answered_twice_is_a_bad_answer():
    with pytest.raises(FusionBadAnswer, match="different models"):
        merge_answers([_answers("jev-1.14.0", ["a"]), _answers("jev-1.15.0", ["b"])], 1)
    with pytest.raises(FusionBadAnswer, match="twice"):
        merge_answers([_answers("jev-1.14.0", ["a"]), _answers("jev-1.14.0", ["a"])], 1)


def test_a_failed_request_fails_the_step_and_is_never_an_answer():
    fs = build_state("scan_b", real_photo_scan(judged=40, not_inspected=0))
    groups, _ = question_groups(fs)
    batches = plan_batches(fs, groups, CONTEXT, target=6000)
    assert len(batches) > 1
    down = JevEngine(key="unused", url="http://127.0.0.1:9/v1/systemone", record_dir=None, timeout_s=1.0,
                     retries=0)
    with pytest.raises(FusionUnavailable):
        ask_batches(down, batches)
    with pytest.raises(FusionUnavailable):
        fuse("scan_b", real_photo_scan(judged=40, not_inspected=5), engine=down, request_tokens=6000)


def test_estimate_counts_utf8_bytes():
    assert estimate_tokens("é" * 100) > estimate_tokens("e" * 100)
    assert ContextLimits(64_000, 32_000) == CONTEXT


def test_a_request_without_the_longer_ref_still_passes_the_words_only_guard():
    """Regression (sess_blind1, 2026-10-24): a note naming s8/det11 sat in a request that declared s8/det1 but not
    s8/det11; removing "s8/det1" from "s8/det11" left a stray "1" and the S5 guard refused the request."""
    from repairos.slots.fusion import assert_words_only, numeric_tokens

    assert numeric_tokens("judged in the crop of s8/det11", ("s8/det1",)) == []
    assert numeric_tokens("a gap of 2 mm next to s8/det1", ("s8/det1",)) == ["2", "2 mm"]
    fds = [finding("f1", "s8/det1", "ok"),
           finding("f2", "s8/det109", "insufficient_evidence", notes=["judged in the crop of s8/det11, which covers "
                                                                      "this region"]),
           finding("f3", "s8/det11", "insufficient_evidence")]
    fs = build_state("scan_b", fds)
    groups, _ = question_groups(fs)
    alone = max(plan_batches(fs, [g], CONTEXT)[0].tokens for g in groups) + 16
    for b in plan_batches(fs, groups, CONTEXT, target=alone):   # one subject per request
        assert_words_only(b.state, b.questions)
