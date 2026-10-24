"""Evidence -> words: the fusion state builder (card F4.1; SPEC S5, A9, I1, I3, I7).

    fs = build_state(scan_id, findings, readings, symptom="...")
    fs.state      # JSON-ready words for the FUSION slot; no number survives (S5 guard)
    fs.subjects   # (kind, ref) -> Subject, the code-side index used by suspects.py / next_action.py

Product-agnostic: every ref, pin and name comes from the findings and readings themselves (GEN1). Numbers stay in
the Finding's `measurements` and are turned into words with the bands in words.py; numbers inside free text
(evidence sentences, OCR text, the user's symptom) are replaced with "[measured]". User and OCR text go into the
state only, never into question instructions (A9).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from repairos.contracts import validate
from repairos.fusion import words
from repairos.fusion.override import Proof, SubjectKey, proofs, reading_subjects, reading_verdict
from repairos.slots.fusion import NUMERIC_TOKEN, UNIT_TOKEN, assert_words_only

_SCRUB = re.compile(r"~?(?:" + UNIT_TOKEN.pattern + r")|~?(?:" + NUMERIC_TOKEN.pattern + r")\s?%?")
_REPEAT = re.compile(r"(?:\[measured\][\s,x×/-]*){2,}")


@dataclass
class Subject:
    kind: str
    ref: str
    pins: list[str] = field(default_factory=list)
    finding_ids: list[str] = field(default_factory=list)
    still_ids: list[str] = field(default_factory=list)
    on_path: bool | None = None
    not_inspected_only: bool = True     # every finding says not_inspected (I3); a subject with none is not inspected
    meter: str | None = None            # agrees | disagrees | unknown | None (not tested)
    proof: Proof | None = None

    @property
    def key(self) -> SubjectKey:
        return (self.kind, self.ref)

    @property
    def name(self) -> str:
        return f"{self.kind} {self.ref}"

    label = name  # the key of this subject in state["subjects"]


@dataclass
class FusionState:
    scan_id: str
    state: dict[str, Any]
    subjects: dict[SubjectKey, Subject]
    design_trust: str
    findings: list[dict[str, Any]]
    readings: list[dict[str, Any]]


def as_dict(obj: Any, contract: str) -> dict[str, Any]:
    """A contract object (pydantic model or dict) as a validated JSON dict."""
    d = obj.model_dump(mode="json", exclude_none=True) if isinstance(obj, BaseModel) else dict(obj)
    validate(contract, d)
    return d


def scrub(text: str, allowed: tuple[str, ...] = ()) -> str:
    """Replace every number in free text with "[measured]", leaving the `allowed` identifiers intact."""
    protected: dict[str, str] = {}
    for i, name in enumerate(sorted(allowed, key=len, reverse=True)):
        token = "\x00" + _letters(i) + "\x00"
        if name and name in text:
            text = text.replace(name, token)
            protected[token] = name
    text = _SCRUB.sub(words.MEASURED, text)
    text = _REPEAT.sub(words.MEASURED + " ", text).strip()
    for token, name in protected.items():
        text = text.replace(token, name)
    return re.sub(r"\s{2,}", " ", text)


def _letters(i: int) -> str:
    out = ""
    while True:
        out = chr(65 + i % 26) + out
        i = i // 26 - 1
        if i < 0:
            return out


def _finding_words(f: dict[str, Any], allowed: tuple[str, ...]) -> dict[str, Any]:
    w: dict[str, Any] = {
        "from": words.FINDING_SOURCE[f["source"]],
        "says": words.FINDING_STATE[f["state"]],
        "source_confidence": words.band("confidence", f["confidence"]),
    }
    if f.get("vlm_answer"):
        w["photo_check"] = words.VLM_ANSWER[f["vlm_answer"]]
    m = f.get("measurements") or {}
    if "coverage" in m:
        w["coverage"] = words.band("coverage", m["coverage"])
    if "offset_mm" in m:
        w["surface"] = words.band("offset_mm", abs(m["offset_mm"]))
    if "shift_mm" in m:
        w["position"] = words.band("shift_mm", sum(x * x for x in m["shift_mm"]) ** 0.5)
    if "gap_mm" in m:
        w["connector_gap"] = words.band("gap_mm", m["gap_mm"])
    if "colour_match_p" in m:
        w["colour"] = words.band("colour_match_p", m["colour_match_p"])
    if "silhouette_iou" in m:
        w["outline"] = words.band("silhouette_iou", m["silhouette_iou"])
    if "sharpness" in m:
        w["focus"] = words.band("sharpness", m["sharpness"])
    if "views" in m:
        w["views"] = words.band("views", m["views"])
    if m.get("ocr_text"):
        w["label_text"] = scrub(m["ocr_text"], allowed)
    notes = [scrub(s, allowed) for s in f.get("evidence") or [] if s.strip()]
    if notes:
        w["notes"] = notes
    return w


def build_state(scan_id: str, findings: list[Any], readings: list[Any] = (), symptom: str | None = None,
                actions: dict[str, dict[str, Any]] | None = None) -> FusionState:
    """The word state for one scan. `findings` are Finding objects or dicts (validated against the contract),
    `readings` MeterReading objects or dicts, `actions` the NextActions already issued (by action_id), used to tie
    a reading to the harness row it answered."""
    fs = [as_dict(f, "finding") for f in findings]
    rs = [as_dict(r, "meter-reading") for r in readings]
    for f in fs:
        if f["is_proof"] and f["source"] != "repairos.meter":
            raise ValueError(f"finding {f['finding_id']}: is_proof is only for repairos.meter (I1), "
                             f"not {f['source']}")

    subjects: dict[SubjectKey, Subject] = {}
    for f in sorted(fs, key=lambda f: f["finding_id"]):
        key = (f["subject"]["kind"], f["subject"]["ref"])
        s = subjects.setdefault(key, Subject(*key))
        for p in f["subject"].get("pins") or []:
            if p not in s.pins:
                s.pins.append(p)
        s.finding_ids.append(f["finding_id"])
        s.still_ids += [x for x in (f.get("evidence_refs") or {}).get("still_ids", []) if x not in s.still_ids]
        if f["state"] != "not_inspected":
            s.not_inspected_only = False
        if "on_symptom_power_path" in f:
            s.on_path = bool(s.on_path) or f["on_symptom_power_path"]
    for r in rs:  # a reading on a net nobody reported makes that net a subject
        if ("net", r["net"]) not in subjects:
            subjects[("net", r["net"])] = Subject("net", r["net"], not_inspected_only=False)

    plain = {k: {"pins": s.pins} for k, s in subjects.items()}
    for r in rs:
        v = reading_verdict(r)
        for key in reading_subjects(r, plain, actions):
            if key in subjects:
                s = subjects[key]
                s.not_inspected_only = False
                s.meter = v if s.meter in (None, "unknown") else s.meter
    for key, p in proofs(rs, fs, plain, actions).items():
        if key in subjects:
            subjects[key].proof = p
            subjects[key].meter = p.verdict

    trusts = {f.get("design_trust") for f in fs if f.get("design_trust")}
    design_trust = "high" if trusts == {"high"} else "low"   # I7: anything not known-good runs low
    allowed = tuple(sorted({s.ref for s in subjects.values()} | {p for s in subjects.values() for p in s.pins}))

    by_subject: dict[SubjectKey, list[dict[str, Any]]] = {}
    for f in sorted(fs, key=lambda f: f["finding_id"]):
        by_subject.setdefault((f["subject"]["kind"], f["subject"]["ref"]), []).append(_finding_words(f, allowed))

    state_subjects: dict[str, Any] = {}
    for key in sorted(subjects):
        s = subjects[key]
        entry: dict[str, Any] = {"ref": s.ref}   # the key already names the kind
        if s.pins:
            entry["pins"] = list(s.pins)
        if s.on_path is not None:
            entry["on_symptom_power_path"] = "yes" if s.on_path else "no"
        entry["meter"] = words.METER[s.meter]
        entry["findings"] = by_subject.get(key, [])
        state_subjects[s.label] = entry

    state = {
        "symptom": scrub(symptom, allowed) if symptom and symptom.strip() else "not described",
        "design_trust": words.DESIGN_TRUST[design_trust],
        "evidence_rules": ("Findings from photos and scans are suspicions; only a multimeter reading is proof. "
                           f"{words.MEASURED} marks a number kept in code; the words beside it carry its meaning."),
        "subjects": state_subjects,
    }
    assert_words_only(state)
    return FusionState(scan_id, state, dict(sorted(subjects.items())), design_trust, fs, rs)
