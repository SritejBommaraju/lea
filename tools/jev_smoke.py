#!/usr/bin/env python3
"""B0.11: one real Jev call on a small hand-written RepairOS word state (no numbers, S5).

    backend/.venv/bin/python tools/jev_smoke.py

Sends one noul and one choice through the FUSION=jev client (repairos.slots.fusion_jev), prints the probabilities,
the model id that answered and the latency, and saves the request/response pair to samples/fusion/jev_smoke.json
(a recorded real Jev exchange, labelled as such). Needs TYPESAFE_KEY in backend/.env. Exit 1 on any failure:
there is no rules fallback on the demo path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from repairos.config import MissingSettingError  # noqa: E402
from repairos.slots.fusion import FusionError, Question  # noqa: E402

STATE = {
    "symptom": "The product does not power on when the switch is pressed.",
    "design_trust": "low: the reference design may be wrong; what the photos show takes precedence",
    "subjects": {
        "part J1": {
            "kind": "part", "ref": "J1", "on_symptom_power_path": "yes", "meter": "not tested",
            "findings": [{"from": "photo inspection", "says": "connector not fully seated",
                          "connector_gap": "a clear gap", "scan": "partly covered", "focus": "sharp",
                          "views": "seen in two views"}],
        },
    },
}
QUESTIONS = [
    Question("j1_problem", "noul", 'Do the findings in `subjects["part J1"]` show a problem with the part J1?'),
    Question("j1_kind", "choice", 'Which kind of problem do the findings in `subjects["part J1"]` describe?', {
        "broken_or_damaged": "The part is cracked, broken, burnt or otherwise damaged.",
        "unseated_or_loose": "The part or its connector is not fully seated, unplugged or loose.",
        "missing": "The part is not there.",
    }),
]


def main() -> int:
    try:
        from repairos.slots.fusion_jev import JevEngine
        engine = JevEngine()
    except MissingSettingError as e:
        print(f"HUMAN ACTION NEEDED: {e}. Create a key in the TypeSafe console and set TYPESAFE_KEY in backend/.env.")
        return 1
    try:
        answers = engine.ask(STATE, QUESTIONS)
    except FusionError as e:
        print(f"FAIL: {e}")
        return 1
    print(f"model: {answers.model}  latency: {answers.latency_ms} ms  (target < 3000 ms)")
    for qid, v in answers.by_id.items():
        print(f"  {qid}: {json.dumps(v)}")
    out = ROOT / "samples" / "fusion" / "jev_smoke.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(engine.last_exchange, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"saved {out.relative_to(ROOT)}")
    return 0 if answers.latency_ms < 3000 else 1


if __name__ == "__main__":
    sys.exit(main())
