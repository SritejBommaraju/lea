"""Suspect ranking (task B7; PLAN I1-I4, I6).

Jev's atomic judgments per subject are combined in code: p_fault = P(problem shown) x P(a problem there explains
the symptom). The verdict is decided in code, in this order:
  1. a judged meter reading (override.py) -> confirmed (disagrees) or cleared (agrees); p_fault 1 or 0 (I2)
  2. nothing but not_inspected findings -> not_inspected (I3)
  3. evidence judged not clear -> insufficient_evidence (I4)
  4. otherwise -> suspect
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from repairos.contracts import validate
from repairos.fusion import questions as Q
from repairos.fusion.state import FusionState, Subject
from repairos.fusion.words import band
from repairos.slots.fusion import Answers

VERDICT_ORDER = {"confirmed": 0, "suspect": 1, "insufficient_evidence": 2, "not_inspected": 3, "cleared": 4}


@dataclass(frozen=True)
class Judgment:
    """What Jev judged about one subject."""
    problem: float
    clear: float
    explains: float
    kind: dict[str, float]
    kind_seed: int
    limit: dict[str, float]

    @property
    def top_kind(self) -> str:
        return max(self.kind, key=self.kind.get)

    @property
    def top_limit(self) -> str:
        return max(self.limit, key=self.limit.get)


def read_judgments(fs: FusionState, answers: Answers, base_seed: int) -> dict[tuple[str, str], Judgment]:
    out = {}
    for i, subj in enumerate(Q.judged_subjects(fs)):
        q = Q.ids(i)
        out[subj.key] = Judgment(answers.noul(q["problem"]), answers.noul(q["clear"]), answers.noul(q["explains"]),
                            answers.choice(q["kind"]), Q.question_seed(base_seed, q["kind"]), answers.choice(q["limit"]))
    return out


def verdict_of(s: Subject, j: Judgment | None) -> str:
    if s.proof is not None:
        return "confirmed" if s.proof.verdict == "disagrees" else "cleared"
    if s.not_inspected_only:
        return "not_inspected"
    if j is None:          # measured, but no expected value to judge it against and nothing photographed
        return "insufficient_evidence"
    if Q.band(j.clear) == "no":
        return "insufficient_evidence"
    return "suspect"


def rank_suspects(fs: FusionState, judgments: dict[tuple[str, str], Judgment], model: str,
                  now: datetime | None = None) -> list[dict[str, Any]]:
    """Schema-valid Suspect dicts, rank 1 first."""
    stamp = (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    rows = []
    for key, s in fs.subjects.items():
        j = judgments.get(key)
        verdict = verdict_of(s, j)
        p = (1.0 if verdict == "confirmed" else 0.0 if verdict == "cleared" else
             j.problem * j.explains if j else 0.5)
        if j is None:
            choice = None
        elif verdict == "cleared":
            choice = "ok"
        elif verdict in ("insufficient_evidence", "not_inspected"):
            choice = "not_enough_evidence"
        elif verdict == "suspect" and Q.band(j.problem) == "no":
            choice = "ok"
        else:
            choice = j.top_kind
        row: dict[str, Any] = {
            "suspect_id": f"sus_{zlib.crc32(f'{fs.scan_id}|{s.label}'.encode()):08x}",
            "scan_id": fs.scan_id,
            "subject": {"kind": s.kind, "ref": s.ref, **({"pins": list(s.pins)} if s.pins else {})},
            "p_fault": round(p, 4),
            "p_fault_word": band("p_fault", p),
            "rank": 0,
            "verdict": verdict,
            "proof_reading_id": s.proof.reading_id if s.proof else None,
            "finding_ids": list(s.finding_ids),
            "fusion_source": "jev",
            "jev_model": model,
            "design_trust": fs.design_trust,
            "calibration_validated": False,   # I6: words only until a reliability diagram on our own data
            "updated_at": stamp,
        }
        if j is not None:
            row["fault_kind"] = {"choice": choice, "probabilities": dict(j.kind), "option_order_seed": j.kind_seed}
        if s.on_path is not None:
            row["on_symptom_power_path"] = s.on_path
        rows.append(row)
    # ties: subjects with photo evidence before reading-only ones, then by kind and ref (deterministic)
    rows.sort(key=lambda r: (VERDICT_ORDER[r["verdict"]], -r["p_fault"], not r["finding_ids"],
                             r["subject"]["kind"], r["subject"]["ref"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        validate("suspect", r)
    return rows
