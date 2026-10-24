"""Row rules every STATE write passes (PLAN.md §5 invariants). Pure: `lookup(kind, id) -> dict | None` reads the store inside
the writing transaction. The same rules apply to every STATE implementation (B4)."""
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


# Report buckets (task B9) against the stored Finding.state they summarise.
NOT_LOOKED_AT = {"not_inspected", "not_seen_in_photo"}
UNDECIDED = {"insufficient_evidence", "unsure"}
RUN_STATUSES = {"inspecting", "running", "done", "failed"}


def _stored_finding(lookup: Lookup, report: Mapping[str, Any], entry: Mapping[str, Any], where: str):
    f = _need(lookup, "finding", entry["finding_id"], f"report {where}.finding_id")
    if f["scan_id"] != report["scan_id"]:
        raise InvariantViolation("REF", f"report {report['report_id']}: finding {entry['finding_id']} is from "
                                        f"scan {f['scan_id']}")
    return f


def _report(d, prev, lookup):
    _scan(lookup, d, "report")
    rid = d["report_id"]
    for e in d.get("findings", []):
        f = _stored_finding(lookup, d, e, "findings[]")
        if f["state"] in NOT_LOOKED_AT:
            raise InvariantViolation("I3", f"report {rid}: finding {f['finding_id']} was not inspected; it cannot be "
                                           "listed as judged")
        if f["state"] in UNDECIDED:
            raise InvariantViolation("I4", f"report {rid}: finding {f['finding_id']} could not be judged; it belongs "
                                           "in insufficient_evidence with its reason")
        want = "none" if f["state"] == "ok" else f["state"]
        if e["issue"] != want:
            raise InvariantViolation("I3" if e["issue"] == "none" else "REF",
                                     f"report {rid}: finding {f['finding_id']} is {f['state']!r}, the report says "
                                     f"issue {e['issue']!r}")
        proof = e.get("proof") or {}
        if proof.get("status", "not_measured") != "not_measured":
            _need(lookup, "meter-reading", proof.get("reading_id"), f"report finding {f['finding_id']} proof")
    for e in d.get("not_inspected", []):
        f = _stored_finding(lookup, d, e, "not_inspected[]")
        if f["state"] not in NOT_LOOKED_AT:
            raise InvariantViolation("I3", f"report {rid}: finding {f['finding_id']} is {f['state']!r}, not "
                                           "not_inspected")
    for e in d.get("insufficient_evidence", []):
        f = _stored_finding(lookup, d, e, "insufficient_evidence[]")
        if f["state"] not in UNDECIDED:
            raise InvariantViolation("I4", f"report {rid}: finding {f['finding_id']} is {f['state']!r}, not "
                                           "insufficient_evidence")
        if not e["reason"].strip():
            raise InvariantViolation("I4", f"report {rid}: finding {f['finding_id']} needs its reason")
    if d["verdict"] == "insufficient_evidence" and not d["verdict_reason"]["text"].strip():
        raise InvariantViolation("I4", f"report {rid}: insufficient_evidence needs a reason")
    if d["verdict"] == "looks_good" and (d.get("not_inspected") or d.get("insufficient_evidence")):
        raise InvariantViolation("I3", f"report {rid}: looks_good while regions were not inspected or not judged")


def _pipeline_run(d, prev, lookup):
    _scan(lookup, d, "pipeline-run")
    rid, status = d["run_id"], d.get("status")
    if status not in RUN_STATUSES:
        raise InvariantViolation("SCHEMA", f"pipeline-run {rid}: status {status!r} not in {sorted(RUN_STATUSES)}")
    err = d.get("error") or {}
    if status == "failed" and not (str(err.get("message") or "").strip() and err.get("stage")):
        raise InvariantViolation("I4", f"pipeline-run {rid}: a failed run names its stage and the error")
    for sid, st in (d.get("stills") or {}).items():
        if st.get("status") == "done":
            ids = st.get("finding_ids") or []
            if not ids:   # I3: a still that was handled ends in findings (not_inspected at least), never in silence
                raise InvariantViolation("I3", f"pipeline-run {rid}: still {sid} is done with no findings")
            for fid in ids:
                _need(lookup, "finding", fid, f"pipeline-run {rid} still {sid} finding")
        if st.get("status") == "failed" and not str(st.get("error") or "").strip():
            raise InvariantViolation("I4", f"pipeline-run {rid}: still {sid} failed without the error")
    if status == "done":
        rep = _need(lookup, "report", d.get("report_id"), f"pipeline-run {rid} report_id")
        if rep["scan_id"] != d["scan_id"]:
            raise InvariantViolation("REF", f"pipeline-run {rid}: report {d['report_id']} is for another scan")
        open_ = [sid for sid, st in (d.get("stills") or {}).items() if st.get("status") != "done"]
        if open_:
            raise InvariantViolation("I3", f"pipeline-run {rid}: done while stills {open_} are not")


_CHECKS = {"scan-package": _scan_package, "still": _still, "finding": _finding, "suspect": _suspect,
           "next-action": _next_action, "meter-reading": _reading, "repair-plan": _plan, "report": _report,
           "pipeline-run": _pipeline_run}
