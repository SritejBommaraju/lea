"""The bridge's reducer calls match the SpacetimeDB module's wire format (spacetime/module/src/index.ts).

SpacetimeDB 2.x sends a reducer's arguments as a JSON array in parameter order, and a product type (FindingIn) as an
object keyed by the canonical field names: the module's camelCase fields in snake_case (`findingId` ->
`finding_id`). Checked against a local server (spacetime 2.13): `{"findingId": ...}` is refused with HTTP 400
"unknown field `findingId`, expected `finding_id`", the snake_case object is accepted. These tests keep the
bridge's payloads in step with the module source, so a renamed field or a new parameter fails here, not on the big
screen.
"""
from __future__ import annotations

import re

from repairos.config import ROOT
from repairos.live import spacetime_bridge as sb

MODULE = (ROOT / "spacetime" / "module" / "src" / "index.ts").read_text()


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _braced(text: str, start: int) -> str:
    """The text of the {...} block that opens at or after `start`."""
    i = text.index("{", start)
    depth = 0
    for j in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[i + 1:j]
    raise ValueError("unbalanced braces")


def _keys(block: str) -> list[str]:
    """Top-level `name:` keys of an object literal (nested calls hold no braces in this module)."""
    keys, depth = [], 0
    for m in re.finditer(r"[{}()\[\]]|(\b[A-Za-z_]\w*)\s*:", block):
        tok = m.group(0)
        if tok in "{([":
            depth += 1
        elif tok in "})]":
            depth -= 1
        elif depth == 0:
            keys.append(m.group(1))
    return keys


def module_reducers() -> dict[str, list[str]]:
    out = {}
    for m in re.finditer(r"export const (\w+) = spacetimedb\.reducer\(", MODULE):
        out[m.group(1)] = _keys(_braced(MODULE, m.end()))
    return out


def finding_in_fields() -> list[str]:
    m = re.search(r"const FindingIn = t\.object\('FindingIn',", MODULE)
    assert m, "FindingIn not found in the module"
    return [_snake(k) for k in _keys(_braced(MODULE, m.end()))]


def _mirror_calls() -> list[tuple[str, list]]:
    out = sb.Outbox()
    m = sb.Mirror(out, None, 1)

    def ev(kind, row, **k):
        return {"kind": kind, "row": {"scan_id": "scan_a", **row}, "at": "t", **k}
    m.handle("sess", ev("still", {"still_id": "s1", "segment": 1, "kind": "photo", "captured_at": "c"}))
    m.handle("sess", ev("pipeline-run", {"status": "inspecting", "identity": {"status": "done", "category": "board"},
                                         "progress": {"stills_done": 1},
                                         "mesh": {"status": "done", "rev": 2, "cloud_ref": "x"}}))
    m.handle("sess", ev("finding", {"finding_id": "f1", "state": "damaged", "evidence_refs": {}}))
    m.handle("sess", ev("report", {"report_id": "r", "verdict": "potential_issues", "findings": [
        {"finding_id": "f1", "issue": "damaged", "location": {"status": "computed", "point_m": [1, 2, 3]}}]}, rev=1))
    m.handle("sess2", {"kind": "still", "row": {"scan_id": "scan_b", "still_id": "s9"}, "at": "t"})  # evicts scan_a
    return [(reducer, args) for reducer, args in out._items.values()]


def test_the_module_source_parses():
    reducers = module_reducers()
    assert reducers["upsert_finding"] == ["scanId", "sessionId", "finding", "sourceAt"]
    assert finding_in_fields()[:2] == ["finding_id", "rank"]


def test_every_mirror_call_has_the_module_reducers_arity():
    reducers = module_reducers()
    calls = _mirror_calls()
    names = {r for r, _ in calls}
    assert {"upsert_scan", "add_still", "follow_scan", "set_identity", "set_progress", "set_mesh", "upsert_finding",
            "set_report", "forget_scan"} <= names
    for reducer, args in calls:
        assert reducer in reducers, f"{reducer} is not a reducer of the module"
        assert len(args) == len(reducers[reducer]), f"{reducer}: {len(args)} args, module takes {reducers[reducer]}"
    assert len(reducers["heartbeat"]) == 2   # the bridge's heartbeat call: [ok, note]


def test_findings_use_the_module_wire_names():
    """Live findings (upsert_finding) and report rows (set_report) carry exactly FindingIn's canonical fields."""
    want = set(finding_in_fields())
    calls = _mirror_calls()
    live = [args[2] for r, args in calls if r == "upsert_finding"]
    rows = [row for r, args in calls if r == "set_report" for row in args[12]]
    assert live and rows
    for f in live + rows:
        assert set(f) == want, f"payload keys {sorted(f)} != FindingIn {sorted(want)}"
