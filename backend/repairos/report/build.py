"""Build the report (task B11) from what the pipeline stored: findings, suspects, the next action, capture facts,
locations and provenance. Pure: no I/O, no model calls (the summary is written by repairos.report.summary).

Every stored finding of the scan lands in exactly one bucket (MS1):
  findings               Finding.state ok (issue `none`) or an issue state: a model judged it
  not_inspected          not_inspected / not_seen_in_photo: nobody looked (I3), with the reason
  insufficient_evidence  insufficient_evidence / unsure: looked at, could not judge (I4), with the reason
Numbers never become user-facing confidence: Finding.confidence becomes a word (I6).

A still whose not-inspected rows cover GROUP_AT or more detector regions gets one grouped row ("N regions in this
photo were not inspected: ..."), with `finding_ids` (each finding still in exactly one row) and the exact region
count in `regions` (a roll-up finding from vision.inspect stands for many regions: `regions[fid]["regions"]`).
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from repairos.fusion import words
from repairos.report.verdict import decide, facts_of
from repairos.slots.blob import REF_PREFIX
from repairos.report.safety import limits, safety_notes

NOT_LOOKED_AT = {"not_inspected", "not_seen_in_photo"}
GROUP_AT = 3   # not-inspected regions in one still from which they become one row
UNDECIDED = {"insufficient_evidence", "unsure"}
NOT_PROOF = "vision suspect, not proof"
JUDGES = {"vlm", "classifier", "meter"}


class ReportError(RuntimeError):
    """The report cannot be built honestly from what is stored (a pipeline bug, never papered over)."""


def confidence_word(confidence: float) -> str:
    """Finding.confidence (the source's own, uncalibrated) as a word. Bands sit between the values
    vision/inspect.py stores for the VLM's low / medium / high (0.35 / 0.6 / 0.8)."""
    return "low" if confidence < 0.5 else "medium" if confidence < 0.7 else "high"


def report_id_for(scan_id: str) -> str:
    return f"rep_{scan_id}"


def _photo(f: Mapping[str, Any], need_box: bool) -> dict[str, Any] | None:
    refs = f.get("evidence_refs") or {}
    still_ids = refs.get("still_ids") or []
    if not still_ids:
        if need_box:
            raise ReportError(f"finding {f['finding_id']}: judged, but it names no still")
        return None
    out: dict[str, Any] = {"still_id": still_ids[0]}
    if refs.get("crop_box_px"):
        out["box_px"] = [int(v) for v in refs["crop_box_px"]]
    elif need_box:
        raise ReportError(f"finding {f['finding_id']}: judged, but it has no crop box")
    if str(refs.get("crop_file", "")).startswith(REF_PREFIX):
        out["crop_ref"] = refs["crop_file"]
    return out


def _reason(f: Mapping[str, Any]) -> str:
    text = "; ".join(s.strip() for s in f.get("evidence") or [] if s.strip() and s.strip() != NOT_PROOF)
    if not text:
        raise ReportError(f"finding {f['finding_id']} is {f['state']} without a reason in its evidence (I4)")
    return text


def _proof(suspect: Mapping[str, Any] | None) -> dict[str, Any]:
    if suspect and suspect.get("proof_reading_id"):
        if suspect["verdict"] == "confirmed":
            return {"status": "confirmed_by_meter", "reading_id": suspect["proof_reading_id"]}
        if suspect["verdict"] == "cleared":
            return {"status": "ruled_out_by_meter", "reading_id": suspect["proof_reading_id"]}
    return {"status": "not_measured"}


def capture_summary(scan: Mapping[str, Any] | None, stills: list[Mapping[str, Any]],
                    judged_stills: set[str]) -> dict[str, Any]:
    kfs = (scan or {}).get("keyframes") or []
    hint = (scan or {}).get("coverage_hint")
    out: dict[str, Any] = {
        # the scan package's depth_source (absent = lidar, the contract); a loose photo has no scan, so no depth
        "depth_source": ((scan.get("depth_source") or "lidar") if scan and scan.get("keyframes") is not None
                         else "none"),
        "stills": len(stills),
        "stills_inspected": len(judged_stills & {s["still_id"] for s in stills}),
        "keyframes": len(kfs),
        "coverage_words": words.band("coverage", hint) if hint is not None else "not measured",
    }
    if scan and scan.get("point_count") is not None:
        out["scan_points"] = int(scan["point_count"])
    return out


def build_report(*, scan: Mapping[str, Any], stills: list[Mapping[str, Any]], findings: list[Mapping[str, Any]],
                 suspects: list[Mapping[str, Any]], next_action: Mapping[str, Any] | None,
                 regions: Mapping[str, Mapping[str, Any]], locations: Mapping[str, Mapping[str, Any]],
                 models: Mapping[str, str], now: datetime | None = None) -> dict[str, Any]:
    """The report without `summary` (repairos.report.summary adds it). `regions[finding_id]` gives the region's
    `part_class` and `inspected_by` (the stages that looked); `locations[finding_id]` its report location."""
    by_finding: dict[str, Mapping[str, Any]] = {}
    for s in suspects:
        for fid in s.get("finding_ids", []):
            by_finding[fid] = s

    judged, not_inspected, undecided = [], [], []
    judged_stills: set[str] = set()
    for f in sorted(findings, key=lambda f: f["finding_id"]):
        fid = f["finding_id"]
        info = regions.get(fid)
        if info is None:
            raise ReportError(f"finding {fid}: the pipeline recorded no region for it (part class, stages)")
        base = {"finding_id": fid, "subject": dict(f["subject"]), "part_class": info["part_class"]}
        loc = locations.get(fid)
        if f["state"] in NOT_LOOKED_AT:
            e = {**base, "reason": _reason(f)}
            if (p := _photo(f, need_box=False)):
                e["photo"] = p
            if loc:
                e["location"] = dict(loc)
            not_inspected.append(e)
        elif f["state"] in UNDECIDED:
            e = {**base, "reason": _reason(f), "inspected_by": list(info["inspected_by"])}
            if (p := _photo(f, need_box=False)):
                e["photo"] = p
            if loc:
                e["location"] = dict(loc)
            undecided.append(e)
        else:
            if not JUDGES & set(info["inspected_by"]):
                raise ReportError(f"finding {fid} is {f['state']!r} but no model judged it (I3)")
            sus = by_finding.get(fid)
            evidence = [s for s in f.get("evidence") or [] if s.strip()]
            if not evidence or loc is None:
                raise ReportError(f"finding {fid}: judged, but it has no evidence text or no location entry")
            e = {**base, "issue": "none" if f["state"] == "ok" else f["state"],
                 "confidence_word": confidence_word(f["confidence"]),
                 "evidence": evidence,
                 "inspected_by": list(info["inspected_by"]), "crop": _photo(f, need_box=True),
                 "location": dict(loc),
                 "proof": _proof(sus)}
            if sus:
                e["judgment"] = {"rank": sus["rank"], "likelihood_word": sus["p_fault_word"],
                                 "verdict": sus["verdict"]}
            judged.append(e)
            judged_stills.add(e["crop"]["still_id"])

    not_inspected = group_not_inspected(not_inspected, {fid: int(r.get("regions", 1)) for fid, r in regions.items()},
                                        {f["finding_id"]: f for f in findings})
    judged.sort(key=lambda e: (e["issue"] == "none", (e.get("judgment") or {}).get("rank", 1 << 30),
                               e["finding_id"]))
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report: dict[str, Any] = {
        "report_id": report_id_for(scan["scan_id"]),
        "scan_id": scan["scan_id"],
        "session_id": scan["session_id"],
        "created_at": stamp,
        "findings": judged,
        "not_inspected": not_inspected,
        "insufficient_evidence": undecided,
        "capture": capture_summary(scan, stills, judged_stills),
        "models": dict(models),
        "grain": "not called",
    }
    v = decide(facts_of(report))
    report["verdict"] = v.verdict
    report["verdict_reason"] = v.as_reason()
    report["safety"] = safety_notes(judged)
    report["limits"] = limits(v.verdict, scan.get("symptom"))
    if next_action:
        step = {"action_id": next_action["action_id"], "kind": next_action["kind"],
                "instruction": next_action["instruction"]}
        if next_action.get("reason"):
            step["reason"] = next_action["reason"]
        report["next_step"] = step
    return report


def _why(f: Mapping[str, Any]) -> str:
    """The reason a finding was not inspected, without its subject: the text after "not inspected: "."""
    lead = next((s.strip() for s in f.get("evidence") or [] if s.strip() and s.strip() != NOT_PROOF), "")
    _, sep, tail = lead.partition("not inspected: ")
    return tail.strip() if sep and tail.strip() else _reason(f)


def group_not_inspected(rows: list[dict[str, Any]], counts: Mapping[str, int],
                        findings: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per still for stills whose not-inspected rows cover GROUP_AT or more regions; other rows unchanged
    (a single roll-up row gets its exact `regions`). Rows with no still keep their own row."""
    by_still: dict[str, list[dict[str, Any]]] = {}
    out: list[dict[str, Any]] = []
    for e in rows:
        sid = (e.get("photo") or {}).get("still_id")
        (by_still.setdefault(sid, []) if sid else out).append(e)
    for sid in sorted(by_still):
        group = by_still[sid]
        total = sum(counts.get(e["finding_id"], 1) for e in group)
        if len(group) == 1 or total < GROUP_AT:
            for e in group:
                if counts.get(e["finding_id"], 1) > 1:
                    e = {**e, "regions": counts[e["finding_id"]]}
                out.append(e)
            continue
        reasons: dict[str, int] = {}
        for e in group:
            why = _why(findings[e["finding_id"]])
            reasons[why] = reasons.get(why, 0) + counts.get(e["finding_id"], 1)
        parts = "; ".join(f"{n} {'region' if n == 1 else 'regions'}: {why}"
                          for why, n in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])))
        ids = [e["finding_id"] for e in group]
        out.append({"finding_id": ids[0], "subject": {"kind": "part", "ref": f"{sid}/not_inspected"},
                    "part_class": "regions in this photo", "photo": {"still_id": sid},
                    "reason": f"{total} regions in this photo were not inspected: {parts}",
                    "regions": total, "finding_ids": ids})
    return out


def honesty_violations(report: Mapping[str, Any], findings: list[Mapping[str, Any]]) -> list[str]:
    """MS1: every stored finding in exactly one bucket; nothing shown judged that no model looked at."""
    seen: dict[str, list[str]] = {}
    for bucket in ("findings", "not_inspected", "insufficient_evidence"):
        for e in report.get(bucket, []):
            for fid in e.get("finding_ids") or [e["finding_id"]]:
                seen.setdefault(fid, []).append(bucket)
    out = []
    for f in findings:
        where = seen.pop(f["finding_id"], [])
        if len(where) != 1:
            out.append(f"finding {f['finding_id']} ({f['state']}) is in {where or 'no bucket'}")
    out += [f"report lists {fid}, which is not a stored finding of the scan" for fid in seen]
    for e in report.get("not_inspected", []):
        ids = e.get("finding_ids")
        if ids and (ids[0] != e["finding_id"] or e.get("regions", 1) < len(ids)):
            out.append(f"grouped row {e['finding_id']}: finding_id is not its first finding, or it counts fewer "
                       "regions than it groups")
    for e in report.get("findings", []):
        if not JUDGES & set(e["inspected_by"]):
            out.append(f"finding {e['finding_id']} shown as judged but no model looked at it")
    return out
