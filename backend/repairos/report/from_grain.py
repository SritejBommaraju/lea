"""The LEA report from the design engine's answer (PLAN.md section 2: "Grain judges; the phone captures and asks").

Pure: no I/O, no model calls. The inputs are the scan package LEA stored, POST /call's answer (the judgement of the scan
against the product's own design), the meter plan, the readings LEA stored, and BOM rows the caller fetched for parts
to buy. The output is a report that validates against spec/contracts/report.schema.json, with `inspected_by: ["grain"]`
on rows the design engine judged (`["meter", "grain"]` on a row a meter reading settled) and `design: {engine, project}`.

Honesty rules kept from the rest of the report:
  - a part the scan could not see from outside (`not_visible`) is `not_inspected`, never ok (I3);
  - a part judged `ok` is a judged row with issue `none`; nothing else is shown as fine;
  - a scan or photo finding is a suspect; only a meter reading is `confirmed_by_meter` / `ruled_out_by_meter` (I1, I2);
  - confidence is a word (I6): the design engine's own word for a fix, the coverage band for a part it found fine;
  - the verdict is picked here in code from the rows (rules G0-G3), the design engine's summary is shown as its own text.

Mapping of the design engine's verdict for a part to the report's `issue`:
  not_seen -> missing, damaged -> damaged, shifted -> shifted, partly -> partly (evidence says how much was seen);
  a part only a photo or a fix names: the photo judge's state when it gave one the contract knows, else by the fix's
  action (reseat -> loose, remake -> damaged, buy -> broken, rewire -> open); `check`, `rescan` and `fix_design` are
  not faults: they become `insufficient_evidence` rows (check, rescan) or a line under `limits` (fix_design).
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from repairos.report import report_id_for

SOURCE = "grain"
ENGINE_NAME = "design engine (Grain)"
NOT_RUN = "not run (the design engine judged this scan)"

# Word bands for coverage and confidence (I6: words, never numbers).
COVERAGE_BANDS = ((0.3, "mostly not covered"), (0.8, "partly covered"), (None, "well covered"))
CONFIDENCE_BANDS = ((0.35, "low"), (0.7, "medium"), (None, "high"))
MAINS_ABOVE_V = 50.0   # IEC 61140: above 50 V AC / 120 V DC a shock is a hazard, so the step says so

PART_ISSUE = {"not_seen": "missing", "damaged": "damaged", "shifted": "shifted", "partly": "partly"}
ACTION_ISSUE = {"reseat": "loose", "remake": "damaged", "buy": "broken", "rewire": "open"}
# states a photo observation may carry (report issue enum, minus the scan-only ones)
PHOTO_STATES = {"broken", "missing", "shifted", "damaged", "worn", "dirty", "loose"}
READING_ISSUE = {"off": "out_of_range", "open": "open", "wrong_pin": "wrong_pin"}
READING_UNJUDGED = {
    "unknown_net": "The design has no net of that name, so the reading could not be compared.",
    "no_expected": "The design states no expected value for this net, so the reading could not be compared.",
    "invalid": "The reading could not be compared with the design.",
}
CHECK_KIND = {"voltage_dc": "run_voltage_test", "continuity": "run_continuity_test",
              "resistance": "run_resistance_test"}
CONF_ORDER = {"high": 0, "medium": 1, "low": 2}

LIMITS = [
    "A phone scan sees the outside of the product only. Parts inside are listed as not looked at, never as fine.",
    "A scan or a photo shows a suspect; only a meter reading proves an electrical fault.",
]


# ----------------------------------------------------------------------------------------------- small helpers
def _band(bands, value: float) -> str:
    for bound, word in bands:
        if bound is None or value < bound:
            return word
    raise AssertionError("unreachable")


def coverage_words(c: float | None) -> str | None:
    return _band(COVERAGE_BANDS, float(c)) if isinstance(c, (int, float)) and not isinstance(c, bool) else None


def confidence_word(value: Any, default: str = "medium") -> str:
    """The design engine's own word (high | medium | low), or a number banded, else `default`."""
    if isinstance(value, str) and value.lower() in CONF_ORDER:
        return value.lower()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _band(CONFIDENCE_BANDS, float(value))
    return default


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "x"


def _text(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _now(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first(*vals: Any) -> str:
    return next((t for t in (_text(v) for v in vals) if t), "")


def offer_from_row(row: Mapping[str, Any] | None, part: str) -> dict | None:
    """One `fix.options` entry from a BOM row, or None when the row has no usable link (the contract needs one)."""
    if not isinstance(row, Mapping):
        return None
    link = _first(row.get("url"), row.get("link"), row.get("product_url"), row.get("supplier_url"))
    if urlparse(link).scheme not in ("http", "https") or not urlparse(link).netloc:
        return None
    title = _first(row.get("description"), row.get("name"), row.get("mpn"), row.get("value"), part)
    seller = _first(row.get("supplier"), row.get("vendor"), row.get("seller")) or urlparse(link).netloc
    out: dict[str, Any] = {"title": title, "seller": seller, "link": link}
    for key in ("unit_price_usd", "price_usd", "unit_price", "price", "unit_cost", "cost"):
        price = _num(row.get(key))
        if price is not None and price > 0:
            out.update(price=price, currency="USD")   # the design engine's BOM is priced in US dollars
            break
    return out


def fix_rows(call: Mapping[str, Any]) -> list[dict]:
    return [f for f in call.get("fixes") or [] if isinstance(f, Mapping) and _text(f.get("part"))]


def _bom_of(fix: Mapping[str, Any], fetched: Mapping[str, Any]) -> dict | None:
    row = fix.get("bom")
    if isinstance(row, list):
        row = next((r for r in row if isinstance(r, Mapping)), None)
    if isinstance(row, Mapping):
        return dict(row)
    got = fetched.get(fix["part"])
    return dict(got) if isinstance(got, Mapping) else None


def photo_states(call: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """part -> (state, note) from the photo judge's observations, only states the report can show."""
    photos = call.get("photos") or {}
    judgement = photos.get("judgement") if isinstance(photos, Mapping) else None
    obs = (judgement or {}).get("observations") if isinstance(judgement, Mapping) else None
    out: dict[str, tuple[str, str]] = {}
    for o in obs or []:
        if not isinstance(o, Mapping):
            continue
        part, state = _text(o.get("part")), _text(o.get("state")).lower()
        if part and state in PHOTO_STATES and part not in out:
            out[part] = (state, _first(o.get("note"), o.get("why"), o.get("detail")))
    return out


# ------------------------------------------------------------------------------------------------- the report
def build(*, scan: Mapping[str, Any], project: str, call: Mapping[str, Any], plan: Mapping[str, Any] | None = None,
          readings: Iterable[Mapping[str, Any]] = (), bom_rows: Mapping[str, Any] | None = None,
          part_urls: Mapping[str, str] | None = None, stills: int = 0, now: datetime | None = None) -> dict[str, Any]:
    """The report for `scan` (a LEA scan package) judged against `project`. See the module doc for the mapping."""
    bom_rows, part_urls = bom_rows or {}, part_urls or {}
    readings = [r for r in readings if r.get("scan_id") == scan["scan_id"]]
    diagnosis = call.get("diagnosis") if isinstance(call.get("diagnosis"), Mapping) else {}
    parts = [p for p in diagnosis.get("parts") or [] if isinstance(p, Mapping) and _text(p.get("name"))]
    fixes = fix_rows(call)
    fix_by_part: dict[str, Mapping[str, Any]] = {}
    for f in fixes:
        fix_by_part.setdefault(f["part"], f)
    photo = photo_states(call)
    used_ids: set[str] = set()

    def new_id(prefix: str, name: str) -> str:
        base = f"g_{prefix}{_slug(name)}"
        fid, n = base, 2
        while fid in used_ids:
            fid, n = f"{base}_{n}", n + 1
        used_ids.add(fid)
        return fid

    findings: list[dict] = []
    not_inspected: list[dict] = []
    insufficient: list[dict] = []
    limits = list(LIMITS)
    seen_parts: set[str] = set()
    rank_of: dict[str, int] = {}
    for i, f in enumerate(fixes):
        rank_of.setdefault(f["part"], i)

    def location(why: str = "The design engine names the part; it gives no point in this scan's frame.") -> dict:
        return {"status": "not_computed", "reason": why}

    # 1. every part the design engine looked for in the scan
    for p in parts:
        name, verdict = _text(p["name"]), _text(p.get("verdict")).lower()
        seen_parts.add(name)
        cov = coverage_words(_num(p.get("coverage")))
        why = _text(p.get("why"))
        subject = {"kind": "part", "ref": name}
        if verdict == "not_visible":
            not_inspected.append({"finding_id": new_id("", name), "subject": subject, "part_class": name,
                                  "reason": why or "The scan could not see this part from outside, so it was not looked at."})
            continue
        fix = fix_by_part.get(name)
        if verdict == "ok":
            row = {"finding_id": new_id("", name), "subject": subject, "part_class": name, "issue": "none",
                   "confidence_word": {"mostly not covered": "low", "partly covered": "medium",
                                       "well covered": "high"}.get(cov or "", "low"),
                   "evidence": [x for x in (f"The scan matches the design for this part ({cov})." if cov else
                                            "The scan matches the design for this part.", why) if x],
                   "inspected_by": [SOURCE], "location": location(), "proof": {"status": "not_measured"}}
            findings.append(row)
            continue
        issue = PART_ISSUE.get(verdict)
        if issue is None:   # a verdict this module does not know is not shown as a fault or as fine
            insufficient.append({"finding_id": new_id("", name), "subject": subject, "part_class": name,
                                 "reason": f"The design engine's verdict for this part, {verdict or 'none'}, is not one this report can show.",
                                 "inspected_by": [SOURCE]})
            continue
        evidence = [f"Design engine: {verdict.replace('_', ' ')}" + (f" ({cov})" if cov else "") + "."]
        if why:
            evidence.append(why)
        row = {"finding_id": new_id("", name), "subject": subject, "part_class": name, "issue": issue,
               "confidence_word": confidence_word((fix or {}).get("confidence")), "evidence": evidence,
               "inspected_by": [SOURCE], "location": location(), "proof": {"status": "not_measured"}}
        if fix:
            _apply_fix(row, fix, bom_rows, part_urls)
        findings.append(row)

    # 2. parts only a photo or a fix names
    for f in fixes:
        name = f["part"]
        if name in seen_parts:
            continue
        seen_parts.add(name)
        action = _text(f.get("action")).lower()
        detail = _first(f.get("detail"), f.get("problem"))
        subject = {"kind": "part", "ref": name}
        if action == "fix_design":
            limits.append(f"The design itself may need a change for {name}: {detail}" if detail else
                          f"The design itself may need a change for {name}.")
            continue
        if action in ("check", "rescan") or action not in ACTION_ISSUE:
            insufficient.append({"finding_id": new_id("", name), "subject": subject, "part_class": name,
                                 "reason": detail or ("The scan did not show enough of this part." if action == "rescan"
                                                      else "The design engine could not decide this part."),
                                 "inspected_by": [SOURCE]})
            continue
        state, note = photo.get(name, (ACTION_ISSUE[action], ""))
        evidence = [x for x in (f"Design engine, from the photos: {state}." if name in photo else
                                f"Design engine: {_text(f.get('problem')) or state}.", note, detail) if x]
        row = {"finding_id": new_id("", name), "subject": subject, "part_class": name, "issue": state,
               "confidence_word": confidence_word(f.get("confidence")), "evidence": list(dict.fromkeys(evidence)),
               "inspected_by": [SOURCE], "location": location("Found in the photos; no point in the scan."),
               "proof": {"status": "not_measured"}}
        _apply_fix(row, f, bom_rows, part_urls)
        findings.append(row)

    # 3. meter readings, judged by the design engine against the design's expected values
    electrical = call.get("electrical") if isinstance(call.get("electrical"), Mapping) else {}
    e_findings = [x for x in electrical.get("findings") or [] if isinstance(x, Mapping)]
    unmatched = list(readings)
    for entry in electrical.get("readings") or []:
        if not isinstance(entry, Mapping):
            continue
        net, verdict = _text(entry.get("net")), _text(entry.get("verdict")).lower()
        reading = _take_reading(unmatched, entry)
        notes = [e_findings[i] for i in entry.get("findings") or [] if isinstance(i, int) and 0 <= i < len(e_findings)]
        pins = (reading or {}).get("pins") or []
        subject: dict[str, Any] = {"kind": "net", "ref": net or "unknown"}
        for n in notes:
            sub = n.get("subject") if isinstance(n.get("subject"), Mapping) else {}
            if _text(sub.get("wire")):
                subject = {"kind": "wire", "ref": _text(sub["wire"])}
            pins = pins or sub.get("pins") or []
        if len(pins) == 2 and all(isinstance(x, str) and x for x in pins):
            subject["pins"] = list(pins)
        lines = [x for n in notes for x in (_text(n.get("text")), _text(n.get("where"))) if x]
        rid = (reading or {}).get("reading_id")
        proof_base = {"reading_id": rid} if rid else {}
        if verdict in READING_ISSUE and rid:
            fixtext = _first(*(n.get("fix") for n in notes))
            row = {"finding_id": new_id("net_", net), "subject": subject, "part_class": "net" if subject["kind"] == "net" else "wire",
                   "issue": READING_ISSUE[verdict], "confidence_word": "high",
                   "evidence": lines or [f"The reading on {net} does not match the design ({verdict.replace('_', ' ')})."],
                   "inspected_by": ["meter", SOURCE], "location": location("A meter reading has no place on the scan."),
                   "proof": {"status": "confirmed_by_meter", **proof_base}, "severity": "functional"}
            if fixtext:
                row["fix"] = fixtext
            findings.append(row)
        elif verdict == "ok" and rid:
            findings.append({"finding_id": new_id("net_", net), "subject": subject,
                             "part_class": "net" if subject["kind"] == "net" else "wire", "issue": "none",
                             "confidence_word": "high", "evidence": lines or [f"The reading on {net} agrees with the design."],
                             "inspected_by": ["meter", SOURCE], "location": location("A meter reading has no place on the scan."),
                             "proof": {"status": "ruled_out_by_meter", **proof_base}})
        else:
            reason = READING_UNJUDGED.get(verdict) or f"The design engine's verdict on this reading, {verdict or 'none'}, is not one this report can show."
            insufficient.append({"finding_id": new_id("net_", net), "subject": subject, "part_class": "net", "reason":
                                 " ".join([*lines[:1], reason]) if lines else reason, "inspected_by": ["meter", SOURCE]})

    # 4. verdict, ranking, next step
    issues = [r for r in findings if r["issue"] != "none" and r["proof"]["status"] != "ruled_out_by_meter"]

    def rank(r: Mapping[str, Any]) -> tuple:
        return (r["proof"]["status"] != "confirmed_by_meter", rank_of.get(r["part_class"], 1 << 20),
                CONF_ORDER[r["confidence_word"]], r["finding_id"])

    findings.sort(key=lambda r: (r["issue"] == "none", *rank(r)))
    issues.sort(key=rank)
    seen = [p for p in parts if _text(p.get("verdict")).lower() != "not_visible"]
    if issues:
        verdict, reason = "potential_issues", {"code": "issues_found", "rule": "G1", "text":
                          "The design engine compared the scan with the product's design and found parts that do not match it."}
    elif findings:
        verdict, reason = "no_problems_seen", {"code": "nothing_seen_wrong", "rule": "G2", "text":
                          "The design engine compared the scan with the product's design and found nothing that does not match."}
        if not_inspected or insufficient:
            reason["caveat"] = (f"{len(not_inspected)} part(s) could not be seen from outside and were not looked at."
                                if not_inspected else "Some parts could not be decided.")
    elif parts:
        verdict, reason = "insufficient_evidence", {"code": "nothing_inspected", "rule": "G3", "text":
                          "The scan could not see any part of the design from outside, so nothing could be judged."}
    else:
        verdict, reason = "insufficient_evidence", {"code": "no_regions", "rule": "G3", "text":
                          "The design engine found no part of the design in this scan."}

    summary = _text(call.get("summary")) or reason["text"]
    report: dict[str, Any] = {
        "report_id": report_id_for(scan["scan_id"]), "scan_id": scan["scan_id"], "session_id": scan["session_id"],
        "build_id": uuid.uuid4().hex, "created_at": _now(now),
        "verdict": verdict, "verdict_reason": reason,
        "summary": {"text": summary, "written_by": ENGINE_NAME, "from_words_only": False},
        "design": {"engine": SOURCE, "project": project},
        "findings": findings, "most_likely": [r["finding_id"] for r in issues][:3],
        "not_inspected": not_inspected, "insufficient_evidence": insufficient,
        "capture": _capture(scan, parts, seen, call, stills),
        "models": _models(call),
        "limits": limits,
    }
    offers = _offers(fixes, bom_rows)
    if offers:
        report["fix"] = {"part": offers[0], "options": offers[1], "searched_at": report["created_at"]}
    step = next_step(plan, readings, fixes)
    if step:
        report["next_step"] = step
    safety = _safety(plan, step)
    if safety:
        report["safety"] = safety
    return finalize(report)


def finalize(report: dict[str, Any]) -> dict[str, Any]:
    """Give every row the id of the finding it stands for. The state store keeps a finding once and never changes it,
    so the id is a hash of what the finding says: the same judgement keeps its id across rebuilds, a changed one is a new
    finding. `most_likely` follows the new ids."""
    renamed: dict[str, str] = {}
    used: set[str] = set()
    for bucket in ("findings", "not_inspected", "insufficient_evidence"):
        for row in report[bucket]:
            body = [report["scan_id"], bucket, row["subject"], row.get("issue"), row.get("confidence_word"),
                    row.get("evidence") or row.get("reason"), row.get("inspected_by"),
                    (row.get("proof") or {}).get("reading_id")]
            digest = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
            fid = f"g_{_slug(str(row['subject']['ref']))}_{digest[:8]}"
            n = 2
            while fid in used:
                fid, n = f"g_{_slug(str(row['subject']['ref']))}_{digest[:8]}_{n}", n + 1
            used.add(fid)
            renamed[row["finding_id"]] = row["finding_id"] = fid
    if "most_likely" in report:
        report["most_likely"] = [renamed[i] for i in report["most_likely"]]
    return report


CONFIDENCE_VALUE = {"low": 0.35, "medium": 0.6, "high": 0.8}   # the values the vision stage stores for its three words


def findings_of(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The Finding rows (spec/contracts/finding.schema.json) the report's rows stand for: the state store publishes a
    report only when each row names a stored finding. A row a meter reading settled is a `repairos.meter` finding
    (proof, I1); everything else the design engine judged is source `grain`."""
    out = []
    for bucket in ("findings", "not_inspected", "insufficient_evidence"):
        for row in report.get(bucket) or []:
            meter = bool((row.get("proof") or {}).get("reading_id"))   # a reading settled this row: it is the proof
            state = "not_inspected" if bucket == "not_inspected" else "insufficient_evidence" \
                if bucket == "insufficient_evidence" else ("ok" if row["issue"] == "none" else row["issue"])
            f: dict[str, Any] = {
                "finding_id": row["finding_id"], "scan_id": report["scan_id"], "subject": dict(row["subject"]),
                "state": state, "source": "repairos.meter" if meter else SOURCE,
                "confidence": CONFIDENCE_VALUE[row["confidence_word"]] if "confidence_word" in row else 0.0,
                "is_proof": meter, "evidence": list(row.get("evidence") or [row["reason"]]),
                "created_at": report["created_at"]}
            if meter:
                f["evidence_refs"] = {"reading_id": row["proof"]["reading_id"]}
            out.append(f)
    return out


def _take_reading(unmatched: list[Mapping[str, Any]], entry: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The stored reading an `electrical.readings` entry answers: by id when the engine echoes it, else by net."""
    for key in ("id", "reading_id"):
        wanted = _text(entry.get(key))
        if wanted:
            for i, r in enumerate(unmatched):
                if r.get("reading_id") == wanted:
                    return unmatched.pop(i)
    net = _text(entry.get("net")).lower()
    for i, r in enumerate(unmatched):
        if _text(r.get("net")).lower() == net:
            return unmatched.pop(i)
    return None


def _entries(call: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    electrical = call.get("electrical") if isinstance(call.get("electrical"), Mapping) else {}
    return ([e for e in electrical.get("readings") or [] if isinstance(e, Mapping)],
            [x for x in electrical.get("findings") or [] if isinstance(x, Mapping)])


def pair_readings(call: Mapping[str, Any], readings: Iterable[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], Mapping[str, Any] | None]]:
    """Each stored reading with the design engine's entry for it (None: not judged yet), in the readings' order."""
    readings = list(readings)
    unmatched = list(readings)
    got: dict[int, Mapping[str, Any]] = {}
    for entry in _entries(call)[0]:
        r = _take_reading(unmatched, entry)
        if r is not None:
            got[id(r)] = entry
    return [(r, got.get(id(r))) for r in readings]


def reading_verdict(call: Mapping[str, Any], readings: Iterable[Mapping[str, Any]], reading_id: str
                    ) -> tuple[Mapping[str, Any] | None, list[Mapping[str, Any]]]:
    """The design engine's entry for one stored reading and the findings that entry points at."""
    findings = _entries(call)[1]
    for r, entry in pair_readings(call, readings):
        if r.get("reading_id") == reading_id:
            idx = [i for i in (entry or {}).get("findings") or [] if isinstance(i, int) and 0 <= i < len(findings)]
            return entry, [findings[i] for i in idx]
    return None, []


def _apply_fix(row: dict, fix: Mapping[str, Any], bom_rows: Mapping[str, Any], part_urls: Mapping[str, str]) -> None:
    """The plain fix on a row: what to buy (BOM row and price), the part file to remake it from, or where to reseat."""
    action, part = _text(fix.get("action")).lower(), fix["part"]
    how = _first(fix.get("how"), fix.get("detail"))
    if action == "buy":
        bom = _bom_of(fix, bom_rows)
        offer = offer_from_row(bom, part)
        what = _first((bom or {}).get("mpn"), (bom or {}).get("description"), (bom or {}).get("name"))
        parts_ = [f"Buy a replacement {part}" + (f": {what}" if what else "")]
        if offer:
            parts_.append(f"from {offer['seller']}" + (f" ({offer['currency']} {offer['price']:.2f})" if "price" in offer else ""))
        row["fix"] = " ".join(parts_) + "." + (f" {how}" if how else "")
    elif action == "remake":
        url = part_urls.get(part)
        row["fix"] = f"Remake {part} from the design's part file" + (f" ({url})" if url else "") + "." + (f" {how}" if how else "")
    elif how:
        row["fix"] = how
    if action == "reseat" and "fix" not in row:
        row["fix"] = f"Reseat {part}."


def _offers(fixes: list[Mapping[str, Any]], bom_rows: Mapping[str, Any]) -> tuple[str, list[dict]] | None:
    """(part, up to three options) for the first buy fix that has BOM rows with a link; the app's Fix it card."""
    for f in fixes:
        if _text(f.get("action")).lower() != "buy":
            continue
        rows = f.get("bom")
        rows = rows if isinstance(rows, list) else [rows] if isinstance(rows, Mapping) else []
        if not rows and isinstance(bom_rows.get(f["part"]), Mapping):
            rows = [bom_rows[f["part"]]]
        options = [o for o in (offer_from_row(r, f["part"]) for r in rows) if o][:3]
        if options:
            return f["part"], options
    return None


def next_step(plan: Mapping[str, Any] | None, readings: Iterable[Mapping[str, Any]],
              fixes: list[Mapping[str, Any]]) -> dict | None:
    """The first meter check of the plan whose net has no reading yet, else the design engine's rescan request."""
    done_nets = {_text(r.get("net")).lower() for r in readings}
    done_actions = {r.get("action_id") for r in readings if r.get("action_id")}
    for c in (plan or {}).get("checks") or []:
        if not isinstance(c, Mapping) or CHECK_KIND.get(_text(c.get("kind"))) is None:
            continue
        net = _text(c.get("net"))
        if net.lower() in done_nets or c.get("id") in done_actions:
            continue
        return _check_step(c)
    for f in fixes:
        if _text(f.get("action")).lower() == "rescan":
            text = _first(f.get("how"), f.get("detail")) or f"Scan it again, slowly, so {f['part']} can be seen."
            return {"action_id": f"grain_rescan_{_slug(f['part'])}", "kind": "rescan", "instruction": text,
                    "reason": f"The scan did not show enough of {f['part']}."}
    return None


def _check_step(c: Mapping[str, Any]) -> dict:
    kind, net = _text(c["kind"]), _text(c.get("net"))
    pins = [str(p) for p in c.get("pins") or []]
    on = _text(c.get("power_state")).lower() == "on"
    exp_in = c.get("expected") if isinstance(c.get("expected"), Mapping) else {}
    expected: dict[str, Any] = {"source": "design_engine"}
    for k in ("volts_min", "volts_max", "ohms_max"):
        if _num(exp_in.get(k)) is not None:
            expected[k] = _num(exp_in[k])
    if isinstance(exp_in.get("continuity"), bool):
        expected["continuity"] = exp_in["continuity"]
    probe = f"touch the probes to {pins[0]} and {pins[1]}" if len(pins) == 2 else f"probe the {net} net"
    power = ("With the product switched on, " if on else "With the product switched off and unplugged, ")
    mode = {"voltage_dc": "set the meter to DC volts and ", "continuity": "set the meter to continuity and ",
            "resistance": "set the meter to ohms and "}[kind]
    good = ""
    if "volts_min" in expected and "volts_max" in expected:
        good = f" A good reading is between {expected['volts_min']:g} and {expected['volts_max']:g} volts."
    elif "ohms_max" in expected:
        good = f" A good reading is {expected['ohms_max']:g} ohms or less."
    elif expected.get("continuity"):
        good = " A good reading beeps."
    step: dict[str, Any] = {"action_id": _text(c.get("id")) or f"grain_check_{_slug(net)}", "kind": CHECK_KIND[kind],
                            "instruction": power + mode + probe + "." + good,
                            "power_state": "on" if on else "off", "expected": expected}
    target: dict[str, Any] = {}
    if net:
        target["net"] = net
    if len(pins) == 2:
        target["pins"] = pins
    parts = [str(p) for p in c.get("parts") or [] if isinstance(p, str)]
    if parts:
        target["part_refs"] = parts
    if target:
        step["target"] = target
    if _text(c.get("why")):
        step["reason"] = _text(c["why"])
    safety = c.get("safety")
    notes = [safety] if isinstance(safety, str) else [s for s in safety or [] if isinstance(s, str)]
    notes = [s.strip() for s in notes if s.strip()]
    if notes:
        step["safety_notes"] = notes
    return step


def _safety(plan: Mapping[str, Any] | None, step: Mapping[str, Any] | None) -> list[dict]:
    out = []
    top = _num((plan or {}).get("max_expected_volts"))
    if step and step.get("power_state") == "off":
        out.append({"code": "unplug_first", "text": "Unplug the product and let it sit before you probe anything."})
    if top is not None and top > MAINS_ABOVE_V:
        out.append({"code": "mains_power", "text": "This design has parts above the extra-low voltage limit. Do not probe it while it is plugged in unless you know how."})
    return out


def _capture(scan: Mapping[str, Any], parts: list[Mapping[str, Any]], seen: list[Mapping[str, Any]],
             call: Mapping[str, Any], stills: int) -> dict:
    judged = bool(((call.get("photos") or {}) if isinstance(call.get("photos"), Mapping) else {}).get("judgement"))
    out: dict[str, Any] = {
        "depth_source": "lidar" if (scan.get("depth_source") or "lidar") == "lidar" else "none",
        "stills": stills, "stills_inspected": stills if judged else 0,
        "keyframes": len(scan.get("keyframes") or []),
        "coverage_words": coverage_words(len(seen) / len(parts)) if parts else "not measured",
    }
    if scan.get("point_count") is not None:
        out["scan_points"] = int(scan["point_count"])
    return out


def _models(call: Mapping[str, Any]) -> dict:
    judged = bool(((call.get("photos") or {}) if isinstance(call.get("photos"), Mapping) else {}).get("judgement"))
    return {"detector": NOT_RUN, "classifier": NOT_RUN,
            "vlm": "design engine photo judge" if judged else NOT_RUN,
            "summary_writer": ENGINE_NAME, "jev": NOT_RUN, "locate": "not computed (the design names the parts)"}


def unknown_design(*, scan: Mapping[str, Any], stills: int = 0, now: datetime | None = None) -> dict[str, Any]:
    """The report when the session names no product and none is configured: nothing is called (rule G0)."""
    text = ("Lea does not know which product this is, so there is no design to compare the scan with. "
            "Enter the product's design id (printed on it) and scan again.")
    return {
        "report_id": report_id_for(scan["scan_id"]), "scan_id": scan["scan_id"], "session_id": scan["session_id"],
        "build_id": uuid.uuid4().hex, "created_at": _now(now), "verdict": "insufficient_evidence",
        "verdict_reason": {"code": "no_regions", "rule": "G0", "text": text},
        "summary": {"text": text, "written_by": "repairos", "from_words_only": False},
        "findings": [], "not_inspected": [], "insufficient_evidence": [], "limits": list(LIMITS),
        "capture": {"depth_source": "lidar" if (scan.get("depth_source") or "lidar") == "lidar" else "none",
                    "stills": stills, "stills_inspected": 0, "keyframes": len(scan.get("keyframes") or []),
                    "coverage_words": "not measured", **({"scan_points": int(scan["point_count"])}
                                                         if scan.get("point_count") is not None else {})},
        "models": {"detector": NOT_RUN, "classifier": NOT_RUN, "vlm": NOT_RUN, "summary_writer": "none",
                   "jev": NOT_RUN, "locate": "not computed"},
    }
