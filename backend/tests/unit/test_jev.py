"""M.FUSION.1: the Jev client. Offline tests cover the request shape, recorded real exchanges and typed errors;
live tests call TypeSafe and skip with HUMAN ACTION NEEDED when TYPESAFE_KEY is missing. Jev is never mocked."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from repairos.config import MissingSettingError, Settings, get_settings
from repairos.contracts import validate
from repairos.fusion import fuse
from repairos.slots.fusion import FusionUnavailable, Question, S5Violation
from repairos.slots.fusion_jev import JevEngine, parse_answers, request_body

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "spec" / "contracts" / "examples"
SMOKE = ROOT / "samples" / "fusion" / "jev_smoke.json"
RECORDED = ROOT / "data" / "fusion" / "jev"


def have_key() -> bool:
    try:
        get_settings().require("TYPESAFE_KEY")
        return True
    except MissingSettingError:
        return False


live = pytest.mark.skipif(not have_key(), reason="HUMAN ACTION NEEDED: jev_key: TYPESAFE_KEY in backend/.env "
                                                 "(TypeSafe console; then run tools/jev_smoke.py)")


def test_request_body_keeps_option_order():
    q = Question("k", "choice", "Which?", {"zeta": "z", "alpha": "a", "mid": "m"}, seed=3)
    body = request_body("jev-latest", {"symptom": "x"}, [q, Question("n", "noul", "Is it?")])
    assert list(body["questions"]["k"]["criteria"]) == ["zeta", "alpha", "mid"]
    assert body["questions"]["n"] == {"type": "noul", "instructions": "Is it?"}
    assert body["model"] == "jev-latest"


def test_missing_key_names_the_key(monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_KEY", raising=False)
    monkeypatch.setattr("repairos.slots.fusion_jev.get_settings", lambda: Settings(_env_file=tmp_path / "none"))
    with pytest.raises(MissingSettingError, match="TYPESAFE_KEY"):
        JevEngine()


def test_s5_guard_runs_before_any_request():
    eng = JevEngine(key="unused", url="http://127.0.0.1:9/v1/systemone", record_dir=None)
    with pytest.raises(S5Violation):
        eng.ask({"gap": "a gap of 2.1 mm"}, [Question("q", "noul", "Is there a gap?")])
    assert eng.last_exchange is None


def test_unreachable_engine_is_a_typed_error_not_an_answer():
    eng = JevEngine(key="unused", url="http://127.0.0.1:9/v1/systemone", record_dir=None, timeout_s=1.0)
    with pytest.raises(FusionUnavailable):
        eng.ask({"symptom": "does not start"}, [Question("q", "noul", "Is `symptom` about power?")])
    assert eng.last_exchange["error"] and eng.last_exchange["response"] is None
    assert eng.last_exchange["request"]["questions"]["q"]["type"] == "noul"


def recorded_exchanges():
    files = ([SMOKE] if SMOKE.exists() else []) + (sorted(RECORDED.glob("*.json"))[-20:] if RECORDED.exists() else [])
    return [f for f in files if json.loads(f.read_text()).get("response")]


@pytest.mark.parametrize("path", recorded_exchanges() or [None], ids=lambda p: p.name if p else "none")
def test_recorded_real_exchanges_parse(path):
    if path is None:
        pytest.skip("HUMAN ACTION NEEDED: no recorded Jev exchange yet; run tools/jev_smoke.py with TYPESAFE_KEY")
    rec = json.loads(path.read_text())
    qs = [Question(qid, q["type"], q.get("instructions") or "",
                   {k: v for k, v in (q.get("criteria") or {}).items()}) for qid, q in rec["request"]["questions"].items()]
    answers = parse_answers(qs, rec["response"], rec["latency_ms"])
    assert answers.model.startswith("jev-")


@live
def test_live_smoke_call():
    eng = JevEngine(record_dir=None)
    qs = [Question("power", "noul", "Is `symptom` about the product not getting power?"),
          Question("where", "choice", "Which part of the product does `symptom` mention?",
                   {"switch": "The power switch or button.", "screen": "The display.", "speaker": "The speaker."})]
    a = eng.ask({"symptom": "Nothing happens when I press the power button."}, qs)
    assert a.model.startswith("jev-") and a.latency_ms < 3000
    assert a.noul("power") > 0.5 and max(a.choice("where"), key=a.choice("where").get) == "switch"


@live
def test_live_fuse_on_contract_examples():
    fds = [json.loads((EXAMPLES / n).read_text()) for n in ("finding.json", "finding.partly.json")]
    res = fuse("scan_7f3a", fds, symptom="The product does not power on.", engine=JevEngine())
    assert res.answers.latency_ms < 3000
    for s in res.suspects:
        validate("suspect", s)
    validate("next-action", res.next_action)
    top = res.suspects[0]
    assert top["subject"]["ref"] == "J1" and top["verdict"] == "suspect"   # a vision finding is never proof (I1)
