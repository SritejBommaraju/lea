"""Row rules every STATE write passes (SPEC.md §7). Pure: `lookup(kind, id) -> dict | None` reads the store inside
the writing transaction. The same rules apply to every STATE implementation (M.STATE.0)."""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from repairos.slots.state import InvariantViolation

Lookup = Callable[[str, str], Mapping[str, Any] | None]
ELECTRICAL = {"wire", "net"}
METER = "repairos.meter"
_VERIFIED = re.compile(r"\bverified\b", re.IGNORECASE)
_POWER = {"run_continuity_test": "off", "run_resistance_test": "off", "run_voltage_test": "on"}


def _need(lookup: Lookup, kind: str, id: str | None, what: str) -> Mapping[str, Any]:
    row = lookup(kind, id) if id else None
    if row is None:
        raise InvariantViolation("REF", f"{what} {id!r} does not exist")
    return row


def _scan(lookup: Lookup, data: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    scan = _need(lookup, "scan-package", data.get("scan_id"), f"{kind}.scan_id")
    if "session_id" in data and data["session_id"] != scan["session_id"]:
        raise InvariantViolation("REF", f"{kind}.session_id {data['session_id']!r} is not the scan's session "
                                        f"{scan['session_id']!r}")
    return scan


def check(kind: str, data: Mapping[str, Any], prev: Mapping[str, Any] | None, lookup: Lookup) -> None:
    """Raise InvariantViolation when writing `data` (over `prev`, None for a new row) would break a rule."""
    fn = _CHECKS.get(kind)
    if fn:
        fn(data, prev, lookup)


def _scan_package(d, prev, lookup):
    _need(lookup, "session", d["session_id"], "scan-package.session_id")


def _still(d, prev, lookup):
    _scan(lookup, d, "still")


def _finding(d, prev, lookup):
    _scan(lookup, d, "finding")
    is_meter = d["source"] == METER
    if d["is_proof"] != is_meter:
        raise InvariantViolation("I1", f"finding {d['finding_id']}: is_proof is true only for source {METER} "
                                       f"(source {d['source']}, is_proof {d['is_proof']})")
    if is_meter:
        _need(lookup, "meter-reading", (d.get("evidence_refs") or {}).get("reading_id"),
              "meter finding evidence_refs.reading_id")
    if prev is not None and prev.get("state") == "not_inspected" and d["state"] == "ok":
        raise InvariantViolation("I3", f"finding {d['finding_id']}: not_inspected never becomes ok")
    if d["state"] == "insufficient_evidence" and not any(s.strip() for s in d.get("evidence", [])):
        raise InvariantViolation("I4", f"finding {d['finding_id']}: insufficient_evidence needs a reason in evidence")


def _suspect(d, prev, lookup):
    _scan(lookup, d, "suspect")
    proof = d.get("proof_reading_id")
    if proof:
        reading = _need(lookup, "meter-reading", proof, "suspect.proof_reading_id")
        if reading["scan_id"] != d["scan_id"]:
            raise InvariantViolation("REF", f"suspect {d['suspect_id']}: reading {proof} is from another scan")
    proven = d["verdict"] in ("confirmed", "cleared")
    if proven and d["subject"]["kind"] in ELECTRICAL and not proof:
        raise InvariantViolation("I2", f"suspect {d['suspect_id']}: an electrical subject is {d['verdict']} only "
                                       "with a proof_reading_id")
    if prev is None:
        return
    if prev.get("verdict") == "not_inspected" and d["verdict"] == "cleared" and not proof:
        raise InvariantViolation("I3", f"suspect {d['suspect_id']}: not_inspected never becomes cleared without "
                                       "a meter reading")
    prev_proof = prev.get("proof_reading_id")
    if prev_proof and (not proof or (d["verdict"] != prev.get("verdict") and proof == prev_proof)):
        raise InvariantViolation("I2", f"suspect {d['suspect_id']}: proven by reading {prev_proof}; only a new "
                                       "meter reading changes it (a reading overrides any engine answer)")


def _next_action(d, prev, lookup):
    _scan(lookup, d, "next-action")
    want = _POWER.get(d["kind"])
    if want and d.get("power_state") != want:
        raise InvariantViolation("S4", f"next-action {d['action_id']}: {d['kind']} needs power_state {want!r} "
                                       f"(got {d.get('power_state')!r})")
    if d.get("power_state") == "on" and not d.get("safety_notes"):
        raise InvariantViolation("S4", f"next-action {d['action_id']}: a power-on step needs safety_notes")
    if d["kind"] == "report_insufficient_evidence" and not d.get("insufficient_evidence_reason"):
        raise InvariantViolation("I4", f"next-action {d['action_id']}: insufficient evidence needs a reason")


def _reading(d, prev, lookup):
    _scan(lookup, d, "meter-reading")


def _plan(d, prev, lookup):
    _scan(lookup, d, "repair-plan")
    if _VERIFIED.search(json.dumps(d)):
        raise InvariantViolation("I5", f"repair-plan {d['plan_id']}: the word 'verified' is banned (a Grain "
                                       "rebuild is a simulation check)")
    for rc in d.get("root_causes", []):
        proof = rc.get("proof_reading_id")
        if proof:
            _need(lookup, "meter-reading", proof, f"root cause {rc['suspect_id']} proof_reading_id")
        elif rc["verdict"] == "confirmed" and rc["subject"]["kind"] in ELECTRICAL:
            raise InvariantViolation("I2", f"repair-plan {d['plan_id']}: electrical root cause {rc['suspect_id']} "
                                           "is confirmed without a proof reading (mark it suspect: unproven)")


_CHECKS = {"scan-package": _scan_package, "still": _still, "finding": _finding, "suspect": _suspect,
           "next-action": _next_action, "meter-reading": _reading, "repair-plan": _plan}
