"""FUSION slot conformance (task B7): the same assertions for every implementation.

    pytest backend/tests/conformance/fusion [--impl jev]

Runs against the real engine on a word state built from the contract examples; skips with HUMAN ACTION NEEDED when
the implementation's key is missing (conftest.py). Recorded real exchanges land in data/fusion/<impl>/ and the
Jev smoke fixture in samples/fusion/.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from repairos.fusion.questions import build_questions
from repairos.fusion.state import build_state
from repairos.slots.fusion import Answers, FusionEngine, S5Violation, check_answers

ROOT = Path(__file__).resolve().parents[4]
EXAMPLES = ROOT / "spec" / "contracts" / "examples"


@pytest.fixture
def word_state():
    fds = [json.loads((EXAMPLES / n).read_text()) for n in ("finding.json", "finding.partly.json")]
    fs = build_state("scan_7f3a", fds, [json.loads((EXAMPLES / "meter-reading.json").read_text())],
                     symptom="The product does not power on.")
    qs, _seed = build_questions(fs, seed=4127)
    return fs.state, qs


def test_protocol(impl):
    assert isinstance(impl, FusionEngine) and impl.model


def test_probabilities_valid_and_choices_sum_to_one(impl, word_state):
    state, qs = word_state
    a = impl.ask(state, qs)
    assert isinstance(a, Answers) and a.model and a.latency_ms >= 0
    check_answers(qs, a)


def test_shuffled_order_honoured(impl, word_state):
    state, qs = word_state
    impl.ask(state, qs)
    sent = (getattr(impl, "last_exchange", None) or {}).get("request")
    if sent is None:
        pytest.skip(f"{type(impl).__name__} does not expose its last request")
    for q in qs:
        if q.type == "choice":
            assert list(sent["questions"][q.id]["criteria"]) == list(q.options)


def test_s5_guard(impl, word_state):
    state, qs = word_state
    with pytest.raises(S5Violation):
        impl.ask({**state, "leak": "gap of 2.1 mm"}, qs)
    with pytest.raises(S5Violation):
        impl.ask({**state, "leak": 2.1}, qs)


def test_p95_latency_under_three_seconds(impl, word_state):
    state, qs = word_state
    lat = []
    for _ in range(5):
        t0 = time.perf_counter()
        impl.ask(state, qs)
        lat.append(time.perf_counter() - t0)
    lat.sort()
    assert lat[int(0.95 * (len(lat) - 1))] < 3.0, lat
