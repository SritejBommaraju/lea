"""Meter readings are the only proof (I1, I2). Verdicts are computed here, in code, against the expected values
(S5); the result overrides whatever the fusion engine said about the same subject.

A reading proves a subject when it names it: the reading's net is a net subject, the reading's two pins are the
subject's pins, or the NextAction the reading answers targeted that harness row. A reading across two pins never
proves either part on its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SubjectKey = tuple[str, str]  # (kind, ref)

CONDUCTS_OHMS = 5.0          # under 5 ohm conducts (meter-reading.schema.json verdict)
VOLT_TOL_ABS = 0.25          # tol = 0.25 V or 5 %, whichever is larger
VOLT_TOL_REL = 0.05
FAULT_STATES = {"open", "short", "out_of_range"}   # a meter Finding in one of these states disagrees


@dataclass(frozen=True)
class Proof:
    verdict: str        # agrees | disagrees
    reading_id: str
    recorded_at: str


def reading_verdict(reading: dict[str, Any]) -> str:
    """agrees | disagrees | unknown. A verdict already judged in code is kept; otherwise it is judged here
    against `expected`. No expected value -> unknown (never a guess, I4)."""
    given = reading.get("verdict")
    if given in ("agrees", "disagrees"):
        return given
    exp = reading.get("expected") or {}
    val = reading.get("value") or {}
    kind = reading.get("kind")
    if kind == "continuity" and "continuity" in val:
        if "continuity" in exp:
            return "agrees" if bool(val["continuity"]) == bool(exp["continuity"]) else "disagrees"
    elif kind == "resistance" and "ohms" in val:
        ohms = float(val["ohms"])
        if "ohms_max" in exp:
            return "agrees" if ohms <= float(exp["ohms_max"]) else "disagrees"
        if exp.get("continuity") is not None:
            return "agrees" if (ohms < CONDUCTS_OHMS) == bool(exp["continuity"]) else "disagrees"
    elif kind == "voltage_dc" and "volts" in val:
        lo, hi = exp.get("volts_min"), exp.get("volts_max")
        if lo is not None and hi is not None:
            v = float(val["volts"])
            tol_lo = max(VOLT_TOL_ABS, VOLT_TOL_REL * abs(float(lo)))
            tol_hi = max(VOLT_TOL_ABS, VOLT_TOL_REL * abs(float(hi)))
            return "agrees" if float(lo) - tol_lo <= v <= float(hi) + tol_hi else "disagrees"
    return "unknown"


def reading_subjects(reading: dict[str, Any], subjects: dict[SubjectKey, dict[str, Any]],
                     actions: dict[str, dict[str, Any]] | None = None) -> set[SubjectKey]:
    """The subjects a reading proves or clears (see module docstring)."""
    out: set[SubjectKey] = set()
    net = reading.get("net")
    if net:
        out.add(("net", net))
    pins = set(reading.get("pins") or [])
    if len(pins) == 2:
        out.update(k for k, s in subjects.items() if set(s.get("pins") or []) == pins)
    act = (actions or {}).get(reading.get("action_id") or "")
    row = ((act or {}).get("target") or {}).get("harness_row")
    if row:
        out.add(("wire", row))
    return out


def proofs(readings: list[dict[str, Any]], meter_findings: list[dict[str, Any]],
           subjects: dict[SubjectKey, dict[str, Any]],
           actions: dict[str, dict[str, Any]] | None = None) -> dict[SubjectKey, Proof]:
    """Subject -> the latest judged proof (agrees or disagrees). Readings with verdict unknown prove nothing."""
    found: dict[SubjectKey, Proof] = {}

    def put(key: SubjectKey, p: Proof) -> None:
        if key not in found or p.recorded_at >= found[key].recorded_at:
            found[key] = p

    for r in readings:
        v = reading_verdict(r)
        if v == "unknown":
            continue
        for key in reading_subjects(r, subjects, actions):
            put(key, Proof(v, r["reading_id"], str(r.get("recorded_at", ""))))
    for f in meter_findings:
        if f.get("source") != "repairos.meter":
            continue
        state = f.get("state")
        v = "agrees" if state == "ok" else "disagrees" if state in FAULT_STATES else None
        if v is None:
            continue
        rid = (f.get("evidence_refs") or {}).get("reading_id") or f["finding_id"]
        put((f["subject"]["kind"], f["subject"]["ref"]), Proof(v, rid, str(f.get("created_at", ""))))
    return found
