"""What the gateway does with the design engine for one scan (PLAN.md section 2: "Grain judges; the phone captures and
asks"): upload the session, ask for the judgement, build the report, forward a meter reading, relay a design-change
request. Blocking calls; the gateway runs them in a thread. Every answer is saved under the session's `grain/` folder.

The pure parts (what to send, how to word a request, how to read a chat answer) are plain functions so they can be
tested without a network.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repairos.grain.bundle import zip_session
from repairos.grain.client import GRAIN_DIR, GrainClient, GrainError
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
    """LEA readings in the shape POST /call takes. A multimeter reading is {net, volts} | {net, pins, continuity} |
    {net, pins, ohms}. The product's self-test and the phone's sensors go through unchanged and flat:
    {kind: selftest, index, net, value_mv?, state?, ok, transport}, {kind: magnetometer, part, delta_uT, state},
    {kind: audio, part, hz}, {kind: flicker, part, hz, depth}, {kind: light, part, state}, {kind: led, part, state, color?}.
    Every reading also carries its id, power state and the plan check it answers. A reading of a shape this does not
    know is left out, never guessed."""
    out = []
    for r in readings:
        v, kind = r.get("value") or {}, r.get("kind")
        d: dict[str, Any] = {"id": r["reading_id"], "power_state": r["power_state"]}
        if kind in PHONE_KINDS:
            part = _part(r)
            if part is None or not _phone_value(kind, v, d):
                continue
            d["kind"], d["part"] = kind, part
        elif kind == "selftest":
            if not r.get("net") or not _selftest_value(v, d):
                continue
            d["kind"], d["net"] = "selftest", r["net"]
        else:
            if not r.get("net"):
                continue
            d["net"] = r["net"]
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


# phone-sensor reading kind -> (the fields of its `value` that go to the design engine and must be there, the optional ones)
PHONE_KINDS = {
    "magnetometer": (("delta_uT", "state"), ()),
    "audio": (("hz",), ()),
    "flicker": (("hz", "depth"), ()),
    "light": (("state",), ()),
    "led": (("state",), ("color",)),
}


def _part(r: Mapping[str, Any]) -> str | None:
    part = r.get("part")
    return part.strip() if isinstance(part, str) and part.strip() else None


def _phone_value(kind: str, v: Mapping[str, Any], d: dict[str, Any]) -> bool:
    required, optional = PHONE_KINDS[kind]
    if any(k not in v for k in required):
        return False
    for k in (*required, *optional):
        if k in v:
            d[k] = v[k]
    return True


def _selftest_value(v: Mapping[str, Any], d: dict[str, Any]) -> bool:
    if any(k not in v for k in ("index", "ok", "transport")):
        return False
    for k in ("index", "value_mv", "state", "ok", "transport"):
        if k in v:
            d[k] = v[k]
    return True


def _flagged(call: Mapping[str, Any]) -> list[str]:
    parts = ((call.get("diagnosis") or {}).get("parts") or []) if isinstance(call.get("diagnosis"), Mapping) else []
    names = [str(p["name"]) for p in parts if isinstance(p, Mapping) and p.get("name")
             and str(p.get("verdict")) not in ("ok", "not_visible")]
    names += [f["part"] for f in from_grain.fix_rows(call) if f["part"] not in names]
    return names


def build_report(client: GrainClient, *, project: str, scan: Mapping[str, Any], call: dict, readings: list[dict],
                 stills: int, record_to: Path, now: datetime | None = None) -> dict:
    """Plan and BOM rows for the parts to buy, then the report. A part to remake carries no address: the phone gets
    its file from the gateway (GET /v1/scans/{id}/fixes/{row}/file)."""
    scan_id = scan["scan_id"]
    plan = client.plan(project, _flagged(call) or None, scan_id=scan_id, record_to=record_to)
    fixes = from_grain.fix_rows(call)
    bom_rows: dict[str, Any] = {}
    for f in fixes:
        if str(f.get("action")).lower() == "buy" and not f.get("bom") and f["part"] not in bom_rows:
            row = client.bom_row(project, f["part"], scan_id=scan_id, record_to=record_to)
            if row:
                bom_rows[f["part"]] = row
    return from_grain.build(scan=scan, project=project, call=call, plan=plan, readings=readings, bom_rows=bom_rows,
                            stills=stills, now=now)


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
    for r in readings:   # the call judged them all: each one's verdict (or that it was not judged) is saved
        entry, notes = from_grain.reading_verdict(call, readings, r["reading_id"])
        client.record(gdir, "reading", r["reading_id"],
                      {"reading_id": r["reading_id"], "grain_verdict": (entry or {}).get("verdict"),
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
        lines.append("Readings the customer took (meter, the product's self-test, phone sensors), with the design engine's verdict on each:")
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
    v, kind = r.get("value") or {}, r.get("kind")
    if kind == "selftest":
        what = f"{v['value_mv'] / 1000:g} V" if "value_mv" in v else (f"output {v['state']}" if "state" in v else "no value")
        return f"{r.get('net')} self-test via {v.get('transport')}: {what}, the product says {v.get('ok')}"
    if kind in PHONE_KINDS:
        what = ", ".join(f"{k} {v[k]}" for k in (*PHONE_KINDS[kind][0], *PHONE_KINDS[kind][1]) if k in v)
        return f"{r.get('part')} {kind} (measured by the phone): {what}"
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


# ------------------------------------------------------------------------------------------ what the person answered
OUTCOMES = ("fixed", "not_fixed", "not_tried")
ROW_BUCKETS = ("findings", "not_inspected", "insufficient_evidence")
# The state the person confirms about a part, in the words the design engine takes, from the report row's issue. Only
# issues that say the same thing are mapped; dirt, a half-seated part or an electrical fault is not "worn" or "damaged".
OUTCOME_STATE = {"missing": "missing", "shifted": "shifted", "damaged": "damaged", "broken": "damaged",
                 "worn": "worn", "loose": "loose"}
VIEW_TTL_S = 6 * 3600


def row_key(row: Mapping[str, Any]) -> str:
    """What a row is about, stable when the report is made again (the finding id is a hash of what it says)."""
    s = row.get("subject") or {}
    key = f"{s.get('kind')}:{s.get('ref')}"
    if s.get("pins"):
        key += "@" + "|".join(str(p) for p in s["pins"])   # two checks on one net are two rows
    reading = (row.get("proof") or {}).get("reading_id")
    return key + (f"#{reading}" if reading else "")


def find_row(report: Mapping[str, Any], row_id: str) -> dict | None:
    for bucket in ROW_BUCKETS:
        for row in report.get(bucket) or []:
            if row.get("finding_id") == row_id:
                return row
    return None


def outcomes_path(root: Path, session_id: str) -> Path:
    return Path(root) / session_id / "outcomes.json"


def _read_outcomes(root: Path, session_id: str) -> dict:
    try:
        data = json.loads(outcomes_path(root, session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


_OUTCOMES_LOCK = threading.Lock()   # one writer of any session's outcomes.json at a time (read, change, replace)


def _write_outcomes(root: Path, session_id: str, data: dict) -> None:
    path = outcomes_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_outcomes(root: Path, session_id: str, scan_id: str) -> dict[str, dict]:
    """{row key: saved answer} for one scan. Missing or unreadable file: none."""
    got = _read_outcomes(root, session_id).get(scan_id)
    return dict(got) if isinstance(got, dict) else {}


def save_outcome(root: Path, session_id: str, scan_id: str, row: Mapping[str, Any], outcome: dict) -> None:
    """Keep the person's answer for the row (a newer answer replaces the older one), atomically."""
    with _OUTCOMES_LOCK:
        data = _read_outcomes(root, session_id)
        data.setdefault(scan_id, {})[row_key(row)] = outcome
        _write_outcomes(root, session_id, data)


def mark_sent(root: Path, session_id: str, scan_id: str, key: str) -> None:
    with _OUTCOMES_LOCK:
        data = _read_outcomes(root, session_id)
        try:
            data[scan_id][key]["sent"] = True
        except (KeyError, TypeError):
            return
        _write_outcomes(root, session_id, data)


def outcome_view(entry: Mapping[str, Any]) -> dict:
    """The contract's `finding.outcome` of a saved answer (the sending state stays in the file)."""
    out = {k: entry[k] for k in ("state", "recorded_at", "confirmed_by") if k in entry}
    if entry.get("note"):
        out["note"] = entry["note"]
    return out


def with_outcomes(report: dict, outcomes: Mapping[str, Mapping[str, Any]]) -> dict:
    """The report with each row's saved answer as `outcome`. The stored report is never changed."""
    if not outcomes:
        return report
    out = dict(report)
    for bucket in ROW_BUCKETS:
        rows = []
        for row in report.get(bucket) or []:
            got = outcomes.get(row_key(row))
            rows.append({**row, "outcome": outcome_view(got)} if got else row)
        out[bucket] = rows
    return out


def outcome_entry(row: Mapping[str, Any], outcome: str, note: str | None, now: datetime | None = None) -> dict:
    """What is saved for one answer: the contract fields, plus what was flagged and whether it reached the design engine."""
    entry: dict[str, Any] = {"state": outcome,
                             "recorded_at": (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "confirmed_by": "person", "row_id": row["finding_id"], "part": str(row["subject"]["ref"]),
                             "kind": row["subject"].get("kind"), "flagged": row.get("issue"), "sent": False}
    if note and note.strip():
        entry["note"] = note.strip()[:500]
    return entry


def outcome_note(entry: Mapping[str, Any]) -> str:
    """The note sent with the outcome: the answer in words first, then what the person wrote."""
    said = {"fixed": "The person tried the suggested fix and it fixed the problem.",
            "not_fixed": "The person tried the suggested fix and it did not fix the problem."}[entry["state"]]
    return said + (f" {entry['note']}" if entry.get("note") else "")


def forward_outcome(client: GrainClient, *, root: Path, session: Mapping[str, Any], scan: Mapping[str, Any],
                    entry: Mapping[str, Any]) -> str:
    """Send one saved answer to the design engine. Returns "sent", "not_applicable" (not_tried says nothing about the
    part; a row that is not a part with a fault the design engine judged, such as a net a meter cleared or a region nobody
    could look at, names nothing to label), or "waiting" (the scan was never uploaded, or the design is unknown: it goes
    when it can). A design engine error propagates; the caller keeps the answer saved and tries again later."""
    if entry["state"] == "not_tried" or entry.get("kind") != "part" or entry.get("flagged") not in OUTCOME_STATE \
            or entry.get("flagged") == "none":
        return "not_applicable"
    scan_id, session_id = scan["scan_id"], scan["session_id"]
    gdir = grain_dir(root, session_id)
    record, project = latest(gdir, "scan", scan_id), project_for(session, client)
    if record is None or project is None:
        return "waiting"
    state = OUTCOME_STATE[str(entry["flagged"])]
    client.outcome(project, _scan_record_id(record), part=entry["part"], state=state, note=outcome_note(entry),
                   scan_id=scan_id, record_to=gdir)
    return "sent"


def unsent_outcomes(root: Path, session_id: str, scan_id: str) -> dict[str, dict]:
    return {k: e for k, e in load_outcomes(root, session_id, scan_id).items()
            if isinstance(e, dict) and not e.get("sent") and e.get("state") in ("fixed", "not_fixed")}


# ------------------------------------------------------------------------------------------ the design's own picture
def view_cache_path(root: Path, project: str, view: str) -> Path:
    return Path(root) / "_design_views" / hashlib.sha256(project.encode()).hexdigest()[:16] / f"{view}.png"


def design_view(client: GrainClient, *, root: Path, project: str, view: str = "iso",
                now: float | None = None) -> tuple[bytes, str]:
    """A rendered view of the design, cached per design for VIEW_TTL_S. When the design engine cannot be reached the
    cached picture (however old) is served; with none, the error is raised."""
    path = view_cache_path(root, project, view)
    clock = time.time() if now is None else now
    try:
        if clock - path.stat().st_mtime < VIEW_TTL_S:
            return path.read_bytes(), "image/png"
    except OSError:
        pass
    try:
        data, ctype = client.design_view(project, view)
    except GrainError:
        if path.is_file():
            return path.read_bytes(), "image/png"
        raise
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")   # its own file: two requests never share one
    tmp.write_bytes(data)
    try:
        os.replace(tmp, path)
    except OSError:   # another request is storing the same picture (Windows refuses a replace under a reader)
        tmp.unlink(missing_ok=True)
    return data, ctype if ctype.startswith("image/") else "image/png"


def clear_design_views(root: Path, project: str) -> None:
    """Forget the cached pictures of a design (it was just changed: the next request fetches the new ones)."""
    shutil.rmtree(view_cache_path(root, project, "iso").parent, ignore_errors=True)


def reading_view(root: Path, session_id: str, reading_id: str) -> dict | None:
    """The design engine's verdict on one reading as it was saved when the judgement came back, or None while it has
    not (the app asks again). `verdict` is None when the judgement came back without an answer for this reading.
    `grain_row` holds the design engine's own entry and the findings it points at."""
    got = latest(grain_dir(root, session_id), "reading", reading_id)
    if got is None:
        return None
    row = got.get("grain_row") or {}
    entry = row.get("row") if isinstance(row.get("row"), Mapping) else {}
    return {"verdict": got.get("grain_verdict"), "net": entry.get("net"),
            "notes": [str(f.get("text")) for f in row.get("findings") or [] if isinstance(f, Mapping) and f.get("text")]}
