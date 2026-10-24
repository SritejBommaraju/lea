"""Every word Jev is asked, every option and every threshold, in one place (TypeSafe skill: "keep all question
wording in one place"; BUILD F4.1). Jev makes the judgments; code decides (next_action.py truth table).

Per subject, atomic questions, each naming the subject directly and pointing at its evidence by path:
  problem   noul    do the findings show a problem with it?
  clear     noul    is the evidence clear enough to judge it?
  explains  noul    would a problem with it explain the symptom?
  kind      choice  which kind of problem (read only when `problem` is yes; the noul is the null case)
  limit     choice  what limits the evidence (read only when `clear` is not yes)
Choice options are shuffled with a recorded seed (jaggedness: choice option order).

Docs (read 2026-10-24): https://docs.typesafe.ai/primitives/noul.md, https://docs.typesafe.ai/primitives/choice.md,
https://docs.typesafe.ai/primitives/advanced.md, https://docs.typesafe.ai/model-jaggedness/jev-1.13.md,
https://raw.githubusercontent.com/typesafe-ai/skills/main/skills/typesafe-ai/SKILL.md
"""
from __future__ import annotations

import random
import zlib

from repairos.fusion.state import FusionState, Subject
from repairos.slots.fusion import Question

# Noul thresholds (skill: 0.7 / 0.3 starting points; between them the question is unsettled and gets its own row).
YES = 0.7
NO = 0.3

# User-effort budget (Q12): extra photos the fusion step may ask for before it reports insufficient evidence.
MAX_EXTRA_PHOTOS = 2

FAULT_KINDS: dict[str, dict[str, str]] = {
    "wire": {
        "unplugged": "An end of the wire is unplugged, or its connector is not fully seated.",
        "wrong_pin_or_colour_order": "The wire lands on the wrong pin, or wires are in the wrong colour order.",
        "broken_or_missing": "The wire is cut, broken or missing.",
        "damaged_insulation": "The wire's insulation is damaged, melted or pinched.",
    },
    "part": {
        "unseated_or_loose": "The part or its connector is not fully seated, unplugged or loose.",
        "broken_or_damaged": "The part is cracked, broken, burnt or otherwise damaged.",
        "missing": "The part is not there.",
        "out_of_position": "The part is shifted or out of its expected position.",
        "wrong_or_extra_part": "A different part from the expected one is fitted, or there is an extra part.",
    },
    "net": {
        "open_circuit": "The connection is broken: there is no continuity where there should be.",
        "short_circuit": "The connection touches another connection or ground where it should not.",
        "wrong_voltage": "The connection has power, but at a voltage outside the expected range.",
    },
}

# What limits the evidence -> NextAction.insufficient_evidence_reason
LIMITS: dict[str, tuple[str, str]] = {
    "blurred": ("The photos of it are blurred or out of focus.", "blur"),
    "too_dark": ("The photos of it are too dark to see detail.", "dark"),
    "glare": ("Glare or reflections hide the detail.", "glare"),
    "hidden_or_out_of_frame": ("It is hidden behind something or outside the frame.", "occluded"),
    "not_photographed": ("No photo or scan has looked at it yet.", "other"),
}


def ids(i: int) -> dict[str, str]:
    """Question ids for subject number i (ids are not sent to the model)."""
    return {k: f"s{i}_{k}" for k in ("problem", "clear", "explains", "kind", "limit")}


def _path(s: Subject) -> str:
    return f'`subjects["{s.label}"]`'


def question_seed(base_seed: int, qid: str) -> int:
    return (base_seed + zlib.crc32(qid.encode())) & 0x7FFFFFFF


def default_seed(scan_id: str) -> int:
    return zlib.crc32(scan_id.encode()) & 0x7FFFFFFF


def _shuffled(options: dict[str, str], seed: int) -> dict[str, str]:
    keys = list(options)
    random.Random(seed).shuffle(keys)
    return {k: options[k] for k in keys}


def subject_questions(i: int, s: Subject, base_seed: int) -> list[Question]:
    q = ids(i)
    name, path = s.name, _path(s)
    kind_seed, limit_seed = question_seed(base_seed, q["kind"]), question_seed(base_seed, q["limit"])
    return [
        Question(q["problem"], "noul", f"Do the findings in {path} show a problem with the {name}?", {
            "true": (f"The findings describe a visible or measured problem with the {name}: for example damaged, "
                     "broken, missing, loose, out of position, a connector not fully seated, a wire on the wrong "
                     "pin, or a meter reading that disagrees with the expected value."),
            "false": (f"The findings describe the {name} as present, in place and intact, or a meter reading "
                      "agrees with the expected value."),
        }),
        Question(q["clear"], "noul", f"Is the evidence in {path} clear enough to judge the condition of the {name}?", {
            "true": (f"The {name} was looked at in sharp views or measured with a meter, and the findings state "
                     "its condition."),
            "false": (f"The {name} was not inspected, was hidden or only partly seen, the photos were blurred or "
                      "dark, or the inspection could not tell."),
        }),
        Question(q["explains"], "noul", f"Would a problem with the {name} explain `symptom`?", {
            "true": (f"The {name} takes part in the function `symptom` describes, for example it carries the "
                     "power or the signal that function depends on."),
            "false": (f"The {name} has no role in the function `symptom` describes, for example a cosmetic cover "
                      "when the product does not power on."),
        }),
        Question(q["kind"], "choice", f"Which kind of problem do the findings in {path} describe for the {name}?",
                 _shuffled(FAULT_KINDS[s.kind], kind_seed), seed=kind_seed),
        Question(q["limit"], "choice", f"What most limits the evidence in {path} about the {name}?",
                 _shuffled({k: v for k, (v, _) in LIMITS.items()}, limit_seed), seed=limit_seed),
    ]


def judged_subjects(fs: FusionState) -> list[Subject]:
    """Subjects with findings a person could judge. A subject known only from a meter reading (a net nobody
    photographed) gets no questions: its verdict is the reading's, in code (skill: leave out what no one could
    judge)."""
    return [s for s in fs.subjects.values() if s.finding_ids]


def build_questions(fs: FusionState, seed: int | None = None) -> tuple[list[Question], int]:
    """All questions for one fusion step and the base seed used for the option shuffles."""
    base = default_seed(fs.scan_id) if seed is None else seed
    out: list[Question] = []
    for i, s in enumerate(judged_subjects(fs)):
        out += subject_questions(i, s, base)
    return out, base


def band(p: float) -> str:
    """yes | unsettled | no for a noul."""
    return "yes" if p >= YES else "no" if p <= NO else "unsettled"
