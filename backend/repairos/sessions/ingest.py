"""Load a recorded session into the configured STATE and BLOB slots through the same transitions the gateway uses,
and score the findings the pipeline wrote against truth.json."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from repairos.sessions.layout import TYPE_BY_EXT, map_files
from repairos.sessions.reader import Session

SESSION_ROW_FIELDS = ("product_id", "make_model", "symptom", "created_at")


def session_row(session_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {"session_id": session_id, **{k: meta[k] for k in SESSION_ROW_FIELDS if meta.get(k) is not None}}


async def ingest(session: Session, state, blobs) -> dict[str, int]:
    sid = session.session_id
    await state.transition("session", sid, "session.create", session_row(sid, session.meta))

    def to_blob(value: str, _base: str) -> str:
        p = session.file(value)
        return blobs.put(p.read_bytes(), TYPE_BY_EXT.get(Path(value).suffix.lower(), "application/octet-stream"))

    counts = {"scans": 0, "stills": 0, "readings": 0, "parts": 0}
    for pkg in session.scans():
        await state.transition("scan-package", pkg["scan_id"], "scan.create", map_files("scan-package", pkg, to_blob))
        counts["scans"] += 1
    for st in session.stills():
        await state.transition("still", st["still_id"], "still.create", map_files("still", st, to_blob))
        counts["stills"] += 1
    for rd in session.readings():
        await state.transition("meter-reading", rd["reading_id"], "reading.record", rd)
        counts["readings"] += 1
    for part in session.parts():
        await state.transition("part-on-hand", part["item_id"], "part.add", part)
        counts["parts"] += 1
    return counts


def _key(subject: dict) -> tuple[str, str]:
    return subject["kind"], subject["ref"]


def score(truth: dict[str, Any] | None, findings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """caught / missed faults, expected not_inspected reported or not, and false alarms (non-ok findings on
    subjects with no induced fault). None without truth.json."""
    if truth is None:
        return None
    not_a_fault = {"ok", "not_inspected", "insufficient_evidence", "unsure", "not_seen_in_photo"}
    flagged: dict[tuple[str, str], set[str]] = {}
    not_inspected = set()
    for f in findings:
        k = _key(f["subject"])
        if f["state"] == "not_inspected":
            not_inspected.add(k)
        elif f["state"] not in not_a_fault:
            flagged.setdefault(k, set()).add(f["state"])
    faults = {_key(t["subject"]): t["fault"] for t in truth["faults"]}
    expected_ni = {_key(s) for s in truth["not_inspected_expected"]}
    fmt = lambda k: f"{k[0]}:{k[1]}"  # noqa: E731
    return {
        "caught": sorted(fmt(k) for k in faults if k in flagged),
        "missed": sorted(fmt(k) for k in faults if k not in flagged),
        "fault_kind_match": sorted(fmt(k) for k, v in faults.items() if v in flagged.get(k, ())),
        "not_inspected_reported": sorted(fmt(k) for k in expected_ni if k in not_inspected),
        "not_inspected_missing": sorted(fmt(k) for k in expected_ni if k not in not_inspected),
        "false_alarms": sorted(fmt(k) for k in flagged if k not in faults),
    }
