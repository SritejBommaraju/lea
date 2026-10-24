"""The next action: a truth table in code over Jev's judgments (card F4.4; TypeSafe skill "judgments from Jev,
decisions in code"; SPEC I2, I4, S1, S4).

Order of rows:
  1. a confirmed suspect (meter proof)                -> report_finding
  2. no subject at all                                -> report_insufficient_evidence (other)
  3. walk the unresolved suspects in rank order; the first whose row is not NEXT decides:
       not inspected                                  -> LOOK_CLOSE
       explains = no                                  -> NEXT (a problem there would not explain the symptom)
       TABLE[(clear, problem)]                        (every combination of yes / unsettled / no has a row)
  4. every unresolved suspect is NEXT                 -> continuity on the top meter-testable power-path subject
                                                         (photos cannot show a break inside a wire), else
                                                         report_insufficient_evidence (other)
Steps resolve to a kind:
  LOOK_CLOSE / LOOK_ANGLE  request_closeup / request_other_angle while the photo budget lasts; then a meter test
                           if the subject is meter-testable; else report_insufficient_evidence (Jev's `limit`)
  PROVE                    meter test if meter-testable, else report_finding marked unproven (I2)
  PROVE_OR_LOOK            meter test if meter-testable, else LOOK_ANGLE
Meter-testable: a voltage expectation and two pins -> run_voltage_test (power on); a wire or net with two pins ->
run_continuity_test (power off, expect continuity: the ~0 ohm rule for wires); an ohms expectation and two pins ->
run_resistance_test (power off). Phase 1 has no Grain, so voltage and resistance need `expectations` from the caller.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from repairos.contracts import validate
from repairos.fusion import questions as Q
from repairos.fusion.state import FusionState, Subject
from repairos.fusion.suspects import Judgment

LOOK_CLOSE, LOOK_ANGLE, PROVE, PROVE_OR_LOOK, NEXT = "look_close", "look_angle", "prove", "prove_or_look", "next"

# (clear, problem) -> step
TABLE: dict[tuple[str, str], str] = {
    ("no", "yes"): LOOK_CLOSE,
    ("no", "unsettled"): LOOK_CLOSE,
    ("no", "no"): LOOK_CLOSE,
    ("unsettled", "yes"): LOOK_CLOSE,
    ("unsettled", "unsettled"): LOOK_ANGLE,
    ("unsettled", "no"): LOOK_ANGLE,
    ("yes", "yes"): PROVE,
    ("yes", "unsettled"): PROVE_OR_LOOK,
    ("yes", "no"): NEXT,
}

S1_NOTE = ("Do not judge polarity or connection points from wire colour or a rating; probe only the points "
           "labelled in AR.")
POWER_OFF_NOTE = "Switch the product off and disconnect its power source and battery before probing."
POWER_ON_NOTE = "The product is powered: keep fingers behind the probe guards and keep the probes from touching."
LIMIT_WORDS = {"blur": "the photos are blurred", "dark": "the photos are too dark", "glare": "glare hides the detail",
               "occluded": "it is hidden or out of frame", "other": "nothing seen so far settles it"}


def step_for(s: Subject, j: Judgment | None) -> str:
    if s.not_inspected_only:
        return LOOK_CLOSE
    if j is None:          # known only from a meter reading: nothing for photos to settle
        return NEXT
    if Q.band(j.explains) == "no":
        return NEXT
    return TABLE[(Q.band(j.clear), Q.band(j.problem))]


def meter_kind(s: Subject, expectations: dict[tuple[str, str], dict[str, Any]]) -> str | None:
    exp = expectations.get(s.key) or {}
    two_pins = len(s.pins) == 2
    if not two_pins or s.proof is not None:
        return None
    if exp.get("volts_min") is not None and exp.get("volts_max") is not None and (s.kind == "net" or exp.get("net")):
        return "run_voltage_test"
    if s.kind in ("wire", "net"):
        return "run_continuity_test"
    if exp.get("ohms_max") is not None:
        return "run_resistance_test"
    return None


def ar_label(pin: str) -> str:
    """"J1.1" -> "J1 pin 1": a label that names the part, never the bare pin id."""
    ref, _, num = pin.rpartition(".")
    return f"{ref} pin {num}" if ref and num else f"probe point at {pin}"


def decide(fs: FusionState, suspects: list[dict[str, Any]], judgments: dict[tuple[str, str], Judgment],
           photos_requested: int = 0, expectations: dict[tuple[str, str], dict[str, Any]] | None = None,
           now: datetime | None = None) -> dict[str, Any]:
    """The NextAction for this fusion step, schema-valid."""
    expectations = expectations or {}
    by_key = {(r["subject"]["kind"], r["subject"]["ref"]): r for r in suspects}
    budget_left = photos_requested < Q.MAX_EXTRA_PHOTOS

    confirmed = [r for r in suspects if r["verdict"] == "confirmed"]
    if confirmed:
        top = confirmed[0]
        s = fs.subjects[(top["subject"]["kind"], top["subject"]["ref"])]
        return _build(fs, "report_finding", s, judgments.get(s.key), top, expectations, now, proven=True)
    if not fs.subjects:
        return _build(fs, "report_insufficient_evidence", None, None, None, expectations, now, limit="other",
                      why="There are no findings to judge yet.")

    unresolved = [fs.subjects[(r["subject"]["kind"], r["subject"]["ref"])] for r in suspects
                  if r["verdict"] in ("suspect", "insufficient_evidence", "not_inspected")]
    for s in unresolved:
        j = judgments.get(s.key)
        step = step_for(s, j)
        if step == NEXT:
            continue
        mk = meter_kind(s, expectations)
        if step == PROVE_OR_LOOK:
            step = PROVE if mk else LOOK_ANGLE
        if step == PROVE:
            return _build(fs, mk or "report_finding", s, j, by_key[s.key], expectations, now, proven=False)
        if budget_left:
            return _build(fs, "request_closeup" if step == LOOK_CLOSE else "request_other_angle", s, j,
                          by_key[s.key], expectations, now)
        if mk:
            return _build(fs, mk, s, j, by_key[s.key], expectations, now,
                          why="The photo budget is used up; a meter check settles it instead.")
        limit = "other" if s.not_inspected_only else Q.LIMITS[j.top_limit][1]
        return _build(fs, "report_insufficient_evidence", s, j, by_key[s.key], expectations, now, limit=limit)

    for s in unresolved:
        mk = meter_kind(s, expectations)
        if mk and s.on_path:
            return _build(fs, mk, s, judgments.get(s.key), by_key[s.key], expectations, now,
                          why=f"Nothing in the photos explains the symptom; photos cannot show a break inside the "
                              f"{s.name}, a meter check can.")
    return _build(fs, "report_insufficient_evidence", None, None, None, expectations, now, limit="other",
                  why="Nothing inspected so far explains the symptom.")


def _build(fs: FusionState, kind: str, s: Subject | None, j: Judgment | None, suspect: dict[str, Any] | None,
           expectations: dict[tuple[str, str], dict[str, Any]], now: datetime | None, proven: bool = False,
           limit: str | None = None, why: str | None = None) -> dict[str, Any]:
    stamp = (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    act: dict[str, Any] = {"action_id": f"act_{uuid.uuid4().hex[:12]}", "scan_id": fs.scan_id, "kind": kind,
                           "chosen_by": "code_on_jev", "created_at": stamp}
    target: dict[str, Any] = {}
    if s is not None:
        parts = sorted({p.rpartition(".")[0] for p in s.pins if "." in p})
        if s.kind == "part":
            target["part_refs"] = [s.ref]
        elif parts:
            target["part_refs"] = parts
        if s.kind == "wire":
            target["harness_row"] = s.ref
        if s.kind == "net":
            target["net"] = s.ref
        elif (expectations.get(s.key) or {}).get("net"):
            target["net"] = expectations[s.key]["net"]
        if len(s.pins) == 2:
            target["pins"] = list(s.pins)
            target["ar_labels"] = [ar_label(p) for p in s.pins]
        elif s.kind == "part":
            target["ar_labels"] = [s.ref]
        if s.still_ids:
            target["still_ids"] = list(s.still_ids)
    if target:
        act["target"] = target
    if j is not None:
        act["probabilities"] = {"problem_shown": j.problem, "evidence_clear": j.clear, "explains_symptom": j.explains}
    name = s.name if s else "product"
    labels = target.get("ar_labels") or [name]
    exp = (expectations.get(s.key) if s else None) or {}
    kind_words = ((suspect or {}).get("fault_kind") or {}).get("choice", "").replace("_", " ")

    if kind == "request_closeup":
        act["power_state"] = "n/a"
        act["instruction"] = (f"Take a close, sharp photo of the {name}. Fill the frame with it and hold the phone "
                              "steady.")
        act["reason"] = why or (f"The {name} has not been looked at yet." if s.not_inspected_only else
                                f"The evidence about the {name} is not clear enough to judge it.")
    elif kind == "request_other_angle":
        act["power_state"] = "n/a"
        act["instruction"] = f"Take a photo of the {name} from a different angle, so nothing hides it."
        act["reason"] = why or f"The photos so far do not settle whether the {name} has a problem."
    elif kind == "run_continuity_test":
        act["power_state"] = "off"
        act["instruction"] = (f"Power OFF: switch the product off and disconnect its power. Set the meter to "
                              f"continuity (beep). Probe {labels[0]} and {labels[1]} (labelled in AR). Expect a beep.")
        act["expected"] = {"continuity": True, "source": "repairos_derived"}
        act["safety_notes"] = [POWER_OFF_NOTE, S1_NOTE]
        act["reason"] = why or f"One continuity check proves or clears the {name}."
    elif kind == "run_resistance_test":
        act["power_state"] = "off"
        act["instruction"] = (f"Power OFF: switch the product off and disconnect its power. Set the meter to "
                              f"resistance (ohms). Probe {labels[0]} and {labels[1]} (labelled in AR). Read the value.")
        act["expected"] = {k: v for k, v in exp.items() if k in ("ohms_max", "continuity", "source")}
        act["safety_notes"] = [POWER_OFF_NOTE, S1_NOTE]
        act["reason"] = why or f"A resistance reading proves or clears the {name}."
    elif kind == "run_voltage_test":
        act["power_state"] = "on"
        act["instruction"] = (f"Power ON: connect the power and switch the product on. Set the meter to DC volts. "
                              f"Red probe on {labels[0]}, black probe on {labels[1]} (labelled in AR). Read the "
                              "voltage.")
        act["expected"] = {k: v for k, v in exp.items() if k in ("volts_min", "volts_max", "source")}
        act["safety_notes"] = [POWER_ON_NOTE, S1_NOTE]
        act["reason"] = why or f"A voltage reading proves or clears the {name}."
    elif kind == "report_finding":
        act["power_state"] = "n/a"
        if proven:
            act["instruction"] = (f"Found it: the {name}" + (f" ({kind_words})" if kind_words else "") +
                                  ". A meter reading proves it.")
            act["reason"] = "A meter reading disagrees with the expected value for it."
        else:
            act["instruction"] = (f"Likely problem: the {name} ({kind_words}). Unproven: no meter reading has "
                                  "confirmed it.")
            act["reason"] = why or (f"The photos clearly show a problem with the {name}, and it cannot be "
                                    "checked with the meter.")
    elif kind == "report_insufficient_evidence":
        act["power_state"] = "n/a"
        act["insufficient_evidence_reason"] = limit or "other"
        detail = LIMIT_WORDS[limit or "other"]
        act["instruction"] = (f"Not enough evidence to name the cause yet: {detail}." +
                              (f" Check the {name} by hand or take a new scan." if s else
                               " Take photos of the product around the problem."))
        act["reason"] = why or f"The evidence about the {name} cannot be judged and the photo budget is used up."
    else:
        raise ValueError(f"no expansion for action kind {kind!r}")
    validate("next-action", act)
    return act
