"""Safety notes and limits on every report (PLAN §6a, critical review 2026-10-24).

Rules are generic (part families and damage words), never product-specific. Code decides; no model writes them.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from repairos.vision.generic_classes import MAINS_PAIR, MAINS_WORDS

# Heat damage, as whole words with their inflections ("char" must not match "charger"). A clause that negates it
# ("no scorch, bubbles or cracks", "without burn marks") does not count.
HEAT_WORDS = re.compile(r"\b(?:burn(?:s|t|ed|ing)?|scorch(?:es|ed|ing)?|char(?:s|red|ring)?|melt(?:s|ed|ing)?|"
                        r"molten|smok(?:e|ed|es|y|ing)|soot(?:y)?|heat[- ]damaged?)\b")
NEGATION = re.compile(r"\b(?:no|not|without|never|none|nor|free of|isn't|aren't|doesn't|don't|wasn't|n't)\b")
# A clause ends at sentence punctuation or a contrast word, so "no cracks, but the top is scorched" still counts.
CLAUSE_END = re.compile(r"[.;:!?]|\b(?:but|however|although|though|yet|except)\b")

SAFETY_TEXT = {
    "unplug_first": "Unplug the device and remove its battery if you can before opening it or touching parts inside.",
    "battery_hazard": "Stop: a battery may be damaged. Do not charge, power on, puncture or bend it; move it to a "
                      "non-flammable surface.",
    "capacitor_charge": "Capacitors can hold a charge after unplugging. Wait before touching them and do not short "
                        "their leads.",
    "heat_damage": "Something looks heat-damaged. Do not power the device on again until that area is checked.",
    "mains_power": "This device looks like it runs on wall power: keep it unplugged while it is open, and remember "
                   "that large capacitors inside can stay charged after unplugging.",
}

LIMITS = [
    "Photos cannot see failed chips, internal shorts or firmware faults.",
    "Photo findings are suspicions; only a meter reading proves a fault.",
]
NOT_MERGED_ACROSS_PHOTOS = ("The same part seen in more than one photo may be listed more than once: merging it "
                            "across photos needs a scan from the app.")
SYMPTOM_NOT_EXPLAINED = ("Nothing visible explains the reported problem. A multimeter check is the next step: "
                         "photos alone cannot rule out an electrical fault.")


def _is_issue(e: Mapping[str, Any]) -> bool:
    return e.get("issue", "none") != "none" and (e.get("proof") or {}).get("status") != "ruled_out_by_meter"


def _text(e: Mapping[str, Any]) -> str:
    parts = [e.get("part_class", ""), e.get("part_words", ""), e.get("reason", ""), *e.get("evidence", [])]
    return " ".join(str(p) for p in parts).lower()


# Detector labels quoted in evidence ("detector label 'melted plastic'") are zero-shot prompts that fire on blur and
# intact parts (eval/defects/QUICKWINS.md §3): never evidence of damage.
DETECTOR_LABEL = re.compile(r"label(?:led)? '[^']*'")


def mentions_heat_damage(text: str) -> bool:
    """A heat-damage word (HEAT_WORDS) in a clause with no negation before it, ignoring quoted detector labels."""
    for clause in CLAUSE_END.split(DETECTOR_LABEL.sub("", text.lower())):
        for m in HEAT_WORDS.finditer(clause):
            if not NEGATION.search(clause[:m.start()]):
                return True
    return False


def _has(text: str, phrases: Iterable[str]) -> bool:
    return any(re.search(rf"(?<![a-z]){re.escape(p)}(?![a-z])", text) for p in phrases)


def runs_on_mains(rows: Iterable[Mapping[str, Any]]) -> bool:
    """A row a model looked at names wall power (MAINS_WORDS), or the rows together name a spade terminal and a
    motor, heater, thermostat or timer (MAINS_PAIR)."""
    text = " | ".join(_text(e) for e in rows)
    return _has(text, MAINS_WORDS) or (_has(text, MAINS_PAIR[0]) and _has(text, MAINS_PAIR[1]))


def safety_notes(findings: list[Mapping[str, Any]], cosmetic: list[Mapping[str, Any]] = (),
                 looked_at: list[Mapping[str, Any]] = ()) -> list[dict[str, str]]:
    """Ordered, de-duplicated safety notes. Issues in `findings` decide unplug_first, battery_hazard and
    capacitor_charge; heat words count in cosmetic rows too; mains_power reads every row a model looked at
    (`findings` including ok rows, `cosmetic`, and `looked_at`: the insufficient-evidence rows)."""
    codes: list[str] = []
    issues = [e for e in findings if _is_issue(e)]
    if issues:
        codes.append("unplug_first")
    for e in issues:
        part = str(e.get("part_class", "")).lower()
        if "battery" in part:
            codes.append("battery_hazard")
        if "capacitor" in part:
            codes.append("capacitor_charge")
    for e in [*issues, *cosmetic]:
        if any(mentions_heat_damage(t) for t in e.get("evidence", [])):
            codes.append("heat_damage")
    if runs_on_mains([*findings, *cosmetic, *looked_at]):
        codes.append("mains_power")
    order = ["battery_hazard", "heat_damage", "mains_power", "unplug_first", "capacitor_charge"]
    seen = sorted(set(codes), key=order.index)
    return [{"code": c, "text": SAFETY_TEXT[c]} for c in seen]


def explains_symptom(findings: Iterable[Mapping[str, Any]]) -> bool:
    """Some unresolved issue that Jev judged would explain the symptom, or a meter-confirmed fault."""
    return any(_is_issue(e) and ((e.get("judgment") or {}).get("explains_symptom") == "yes"
                                 or (e.get("proof") or {}).get("status") == "confirmed_by_meter") for e in findings)


def limits(symptom: str | None, findings: Iterable[Mapping[str, Any]] = (), unmerged_photos: bool = False
           ) -> list[str]:
    """LIMITS, plus NOT_MERGED_ACROSS_PHOTOS when judged rows come from several photos and none is located on a scan
    (rows are then merged within a photo only), plus SYMPTOM_NOT_EXPLAINED when a symptom was reported and no finding
    explains it (explains_symptom): an issue Jev does not tie to the symptom still leaves the meter as the next check."""
    out = list(LIMITS)
    if unmerged_photos:
        out.append(NOT_MERGED_ACROSS_PHOTOS)
    if symptom and symptom.strip() and not explains_symptom(findings):
        out.append(SYMPTOM_NOT_EXPLAINED)
    return out
