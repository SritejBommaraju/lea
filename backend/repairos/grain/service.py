"""What the gateway does with the design engine for one scan (PLAN.md section 2: "Grain judges; the phone captures and
asks"): upload the session, ask for the judgement, build the report, forward a meter reading, relay a design-change
request. Blocking calls; the gateway runs them in a thread. Every answer is saved under the session's `grain/` folder.

The pure parts (what to send, how to word a request, how to read a chat answer) are plain functions so they can be
tested without a network.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from repairos.grain.bundle import zip_session
from repairos.grain.client import GRAIN_DIR, GrainClient
from repairos.report import from_grain

MAX_FIELD = 400   # characters of any one free-text field put in a message to the design engine


def grain_dir(root: Path, session_id: str) -> Path:
    return Path(root) / session_id / GRAIN_DIR


def latest(directory: Path, kind: str, scan_id: str) -> dict | None:
    """The newest recorded answer of `kind` for the scan (file names end in a sortable timestamp), or None."""
    if not directory.is_dir():
        return None
    files = sorted(directory.glob(f"{kind}-{scan_id}-*.json"))
    for path in reversed(files):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def project_for(session: Mapping[str, Any], client: GrainClient) -> str | None:
    """The design a session is judged against: the product it names, else the configured default, else None."""
    return (str(session.get("product") or "").strip() or client.project) or None


def readings_payload(readings: list[Mapping[str, Any]]) -> list[dict]:
    """LEA meter readings in the shape POST /call takes: {net, volts} | {net, pins, continuity} | {net, pins, ohms},
    plus the reading's id, power state, meter and the plan check it answers."""
    out = []
    for r in readings:
        v, kind = r.get("value") or {}, r.get("kind")
        d: dict[str, Any] = {"net": r["net"], "id": r["reading_id"], "power_state": r["power_state"]}
        if r.get("pins"):
            d["pins"] = list(r["pins"])
        if kind == "voltage_dc" and "volts" in v:
            d["volts"] = v["volts"]
        elif kind == "continuity" and "continuity" in v:
            d["continuity"] = v["continuity"]
        elif kind == "resistance" and "ohms" in v:
            d["ohms"] = v["ohms"]
        else:
            continue
        if r.get("meter"):
            d["meter"] = r["meter"]
        if r.get("action_id"):
            d["check"] = r["action_id"]
        out.append(d)
    return out


def _flagged(call: Mapping[str, Any]) -> list[str]:
    parts = ((call.get("diagnosis") or {}).get("parts") or []) if isinstance(call.get("diagnosis"), Mapping) else []
    names = [str(p["name"]) for p in parts if isinstance(p, Mapping) and p.get("name")
             and str(p.get("verdict")) not in ("ok", "not_visible")]
    names += [f["part"] for f in from_grain.fix_rows(call) if f["part"] not in names]
    return names


def build_report(client: GrainClient, *, project: str, scan: Mapping[str, Any], call: dict, readings: list[dict],
                 stills: int, record_to: Path, now: datetime | None = None) -> dict:
    """Plan, BOM rows for the parts to buy and part-file URLs for the parts to remake, then the report."""
    scan_id = scan["scan_id"]
    plan = client.plan(project, _flagged(call) or None, scan_id=scan_id, record_to=record_to)
    fixes = from_grain.fix_rows(call)
    bom_rows: dict[str, Any] = {}
    for f in fixes:
        if str(f.get("action")).lower() == "buy" and not f.get("bom") and f["part"] not in bom_rows:
            row = client.bom_row(project, f["part"], scan_id=scan_id, record_to=record_to)
            if row:
                bom_rows[f["part"]] = row
    part_urls = {f["part"]: client.part_file_url(project, f["part"]) for f in fixes
                 if str(f.get("action")).lower() == "remake"}
    return from_grain.build(scan=scan, project=project, call=call, plan=plan, readings=readings, bom_rows=bom_rows,
                            part_urls=part_urls, stills=stills, now=now)


def judge_scan(client: GrainClient, *, root: Path, session: Mapping[str, Any], scan: Mapping[str, Any],
               readings: list[dict], stills: int, now: datetime | None = None) -> dict:
    """Upload the scan's session folder, ask for the judgement, build the report. With no design to judge against the
    report says so and nothing is sent."""
    scan_id, session_id = scan["scan_id"], scan["session_id"]
    project = project_for(session, client)
    if project is None:
        return from_grain.unknown_design(scan=scan, stills=stills, now=now)
    gdir = grain_dir(root, session_id)
    data = zip_session(Path(root) / session_id, scan_id)
    record = client.post_scan(project, f"{scan_id}.zip", data, units="m", scan_id=scan_id, record_to=gdir)
    call = client.call(project, _scan_record_id(record), question=session.get("symptom") or scan.get("symptom"),
                       scan_id=scan_id, record_to=gdir)
    return build_report(client, project=project, scan=scan, call=call, readings=readings, stills=stills,
                        record_to=gdir, now=now)


def _scan_record_id(record: Mapping[str, Any]) -> str:
    rid = record.get("id")
    if not isinstance(rid, str) or not rid:
        raise ValueError("the design engine's scan record has no id")
    return rid


def judge_reading(client: GrainClient, *, root: Path, session: Mapping[str, Any], scan: Mapping[str, Any],
                  reading: Mapping[str, Any], readings: list[dict], stills: int,
                  now: datetime | None = None) -> dict | None:
    """Forward the readings of the scan through POST /call (the new one is among them), keep the design engine's verdict
    and row for this reading next to the answers, and build the report again. None when the scan was never uploaded
    (the reading then goes with the scan at the next /complete)."""
    scan_id, session_id = scan["scan_id"], scan["session_id"]
    gdir = grain_dir(root, session_id)
    record = latest(gdir, "scan", scan_id)
    project = project_for(session, client)
    if record is None or project is None:
        return None
    call = client.call(project, _scan_record_id(record), question=session.get("symptom") or scan.get("symptom"),
                       readings=readings_payload(readings), scan_id=scan_id, record_to=gdir)
    entry, notes = from_grain.reading_verdict(call, readings, reading["reading_id"])
    client.record(gdir, "reading", reading["reading_id"],
                  {"reading_id": reading["reading_id"], "grain_verdict": (entry or {}).get("verdict"),
                   "grain_row": {"row": entry, "findings": notes}})
    return build_report(client, project=project, scan=scan, call=call, readings=readings, stills=stills,
                        record_to=gdir, now=now)


# ------------------------------------------------------------------------------------------------ fix from the phone
def _cut(v: Any, n: int = MAX_FIELD) -> str:
    t = " ".join(str(v).split()) if v is not None else ""
    return t if len(t) <= n else t[: n - 1] + "…"


def fix_message(*, project: str, session: Mapping[str, Any], scan: Mapping[str, Any], call: Mapping[str, Any] | None,
                readings: list[Mapping[str, Any]], words: str) -> str:
    """One message for the design engine's agent, with the context the customer cannot type: the symptom, the design,
    what the scan flagged (verdict and how much was seen), every reading with the verdict it got, the fixes suggested
    so far, then the customer's own words last."""
    call = call or {}
    lines = ["A customer has a built unit of this design and is using the LEA phone app to find a problem.",
             f"Design (project): {project}"]
    symptom = _cut(session.get("symptom") or scan.get("symptom"))
    lines.append(f"Symptom the customer reported: {symptom}" if symptom else "The customer reported no symptom.")
    diag = call.get("diagnosis") if isinstance(call.get("diagnosis"), Mapping) else {}
    flagged = [p for p in diag.get("parts") or [] if isinstance(p, Mapping) and p.get("verdict") not in ("ok", "not_visible")]
    if flagged:
        lines.append("Parts the phone scan flagged against the design:")
        for p in flagged:
            cov = p.get("coverage")
            seen = f", coverage {cov:.2f}" if isinstance(cov, (int, float)) and not isinstance(cov, bool) else ""
            lines.append(f"- {_cut(p.get('name'), 80)}: {p.get('verdict')}{seen}" + (f" ({_cut(p.get('why'))})" if p.get("why") else ""))
    elif diag.get("parts"):
        lines.append("The phone scan flagged no part against the design.")
    if readings:
        lines.append("Meter readings the customer took, with the design engine's verdict on each:")
        for r, entry in from_grain.pair_readings(call, readings):
            lines.append(f"- {_cut(_reading_words(r), 160)}: {(entry or {}).get('verdict', 'not judged yet')}")
    fixes = from_grain.fix_rows(call)
    if fixes:
        lines.append("Fixes suggested so far:")
        for f in fixes:
            lines.append(f"- {f.get('action')} {_cut(f.get('part'), 80)}: {_cut(f.get('problem') or f.get('detail'))}")
    lines += ["", "The customer says:", _cut(words, 4000)]
    return "\n".join(lines)


def _reading_words(r: Mapping[str, Any]) -> str:
    v = r.get("value") or {}
    pins = f" between {r['pins'][0]} and {r['pins'][1]}" if len(r.get("pins") or []) == 2 else ""
    if "volts" in v:
        what = f"{v['volts']:g} V"
    elif "ohms" in v:
        what = f"{v['ohms']:g} ohms"
    else:
        what = "continuity" if v.get("continuity") else "no continuity"
    return f"{r.get('net')}{pins} read {what} with the product {r.get('power_state')}"


def chat_result(answer: Mapping[str, Any]) -> dict:
    """What the phone is shown of the design engine's reply: its words, whether it finished or is waiting for the
    customer, its errors, and whether it ran a build that passed (`ok`), failed, or none."""
    reply = _cut(answer.get("text"), 20000)
    question = answer.get("question") if isinstance(answer.get("question"), Mapping) else None
    if question and not reply:
        reply = _cut(question.get("why") or question.get("questions"), 2000)
    build = answer.get("last_build")
    return {"reply": reply, "finished": bool(answer.get("finished")), "waiting": bool(answer.get("waiting")),
            "errors": [str(e) for e in answer.get("errors") or []],
            "build": "none" if not isinstance(build, Mapping) else "ok" if build.get("ok") else "failed"}
