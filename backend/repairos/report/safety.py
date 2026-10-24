"""Safety notes and limits on every report (PLAN §6a, critical review 2026-10-24).

Rules are generic (part families and damage words), never product-specific. Code decides; no model writes them.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

HEAT_WORDS = ("burn", "burnt", "scorch", "char", "melt", "smoke", "soot")

SAFETY_TEXT = {
    "unplug_first": "Unplug the device and remove its battery if you can before opening it or touching parts inside.",
    "battery_hazard": "Stop: a battery may be damaged. Do not charge, power on, puncture or bend it; move it to a "
                      "non-flammable surface.",
    "capacitor_charge": "Capacitors can hold a charge after unplugging. Wait before touching them and do not short "
                        "their leads.",
    "heat_damage": "Something looks heat-damaged. Do not power the device on again until that area is checked.",
}

LIMITS = [
    "Photos cannot see failed chips, internal shorts or firmware faults.",
    "Photo findings are suspicions; only a meter reading proves a fault.",
]
SYMPTOM_NOT_EXPLAINED = ("Nothing visible explains the reported problem. A multimeter check is the next step: "
                         "photos alone cannot rule out an electrical fault.")


def _is_issue(e: Mapping[str, Any]) -> bool:
    return e.get("issue", "none") != "none" and (e.get("proof") or {}).get("status") != "ruled_out_by_meter"


def safety_notes(findings: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Ordered, de-duplicated safety notes for the findings that show an issue."""
    codes: list[str] = []
    issues = [e for e in findings if _is_issue(e)]
    if issues:
        codes.append("unplug_first")
    for e in issues:
        part = str(e.get("part_class", "")).lower()
        text = " ".join(e.get("evidence", [])).lower()
        if "battery" in part:
            codes.append("battery_hazard")
        if "capacitor" in part:
            codes.append("capacitor_charge")
        if any(w in text for w in HEAT_WORDS):
            codes.append("heat_damage")
    order = ["battery_hazard", "heat_damage", "unplug_first", "capacitor_charge"]
    seen = sorted(set(codes), key=order.index)
    return [{"code": c, "text": SAFETY_TEXT[c]} for c in seen]


def limits(verdict: str, symptom: str | None) -> list[str]:
    out = list(LIMITS)
    if symptom and symptom.strip() and verdict != "potential_issues":
        out.append(SYMPTOM_NOT_EXPLAINED)
    return out
