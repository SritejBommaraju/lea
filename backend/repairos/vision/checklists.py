"""Per-part-family failure checklists for the VLM crop prompt, and the family priors for the crop budget.

ONE table: part family -> what failure looks like, what "ok" looks like, and how failure-prone the family is.
Generic hardware knowledge only (no product, brand or model names, GEN1); every detector label it maps comes from
the one vocabulary in `generic_classes.py`, and a test checks that each label there has a family here.

Sources (research, not tuned on any benchmark): capacitor repair guides and MLCC flex-crack notes, IPC-A-610 section
headings for solder joints, battery swelling and leakage safety guides, connector and harness failure write-ups
(~/RepairOS-research/general-defect-detection.md §2 and §5). Signs that are rarely visible from outside are marked
as such, so the model does not overclaim.

How it is used:
- `vision.inspect` picks a family per crop from the detector label (`family_for`), or `unknown_part` when that label
  is a weak match or the defect classifier abstains (`crop_family`); the VLM prompt then names that family's checks.
- The VLM system prompt carries the whole table (`table_text`), because detector labels are often wrong: the model
  is told to judge the part it actually sees, using that part's row.
- `prior` (words, never numbers in findings) ranks failure-prone families first when the crop budget binds.
The detector label itself always reaches the model as quoted data, never as an instruction (A9).
"""
from __future__ import annotations

from dataclasses import dataclass

UNKNOWN_PART = "unknown_part"
PRIORS: tuple[str, ...] = ("high", "medium", "low")  # how often a family fails in the field, in words


@dataclass(frozen=True)
class Checklist:
    family: str                  # stable code
    words: str                   # how the prompt and the report name the family
    look_for: tuple[str, ...]    # visible failure signs, where to look
    ok_looks: str                # what an intact part of this family looks like
    prior: str                   # one of PRIORS
    labels: tuple[str, ...] = ()  # generic_classes labels (part or damage words) that suggest this family


# Checks that apply to every part, whatever it is (the generic defect axis). The unknown-part checklist is exactly
# these; every other family gets them as well.
GENERAL_CHECKS: tuple[str, ...] = (
    "burn, scorch or soot marks, melted or discoloured plastic",
    "cracks, chips or broken pieces",
    "corrosion: green, white or rust-coloured deposits",
    "bulging, swelling or bent and deformed shapes",
    "leaked liquid, crust or residue",
    "loose, unplugged, displaced or missing parts and fasteners (an empty place where something belongs)",
)

FAMILIES: tuple[Checklist, ...] = (
    Checklist(
        "capacitor", "capacitor",
        ("electrolytic (can type): the top vent should be flat; a domed, bulging or split top along the scored "
         "lines is a failure; look at the base for brown or orange crust, an oily ring or a raised rubber bung; a "
         "sleeve that is discoloured or shrunk from heat; a dark ring on the board under it; a can tilted or lifted "
         "off its base",
         "tantalum (small block, often yellow, orange or black): a crater, black or brown scorch on the body or "
         "the board, a cracked body",
         "ceramic chip (small, tan or grey, two metal ends): a crack across the body, a chipped corner, a missing "
         "end; most ceramic cracks are hidden inside the body, so ok means only that no crack is visible"),
        "flat top, dry and clean base, even sleeve colour, body intact with no scorch", "high",
        ("capacitor", "electrolytic capacitor", "bulging capacitor")),
    Checklist(
        "semiconductor", "chip or other semiconductor",
        ("a crater, hole or bulge in the package, scorch on the package or the board beside it, a cracked or "
         "lifted corner, missing or bent leads, a burnt or browned LED lens",),
        "flat package, legs even and straight, marking legible, no scorch around it", "medium",
        ("integrated circuit", "chip", "diode", "led", "crystal", "voltage regulator", "transistor")),
    Checklist(
        "connector", "connector",
        ("a plug not fully inserted (a gap, or contacts showing past the housing), an open or broken latch, "
         "bent, pushed-back or missing pins, a melted or browned housing, scorched or corroded contacts, a crimp "
         "that does not cover the wire strands",),
        "fully seated and square, latch closed, pins even, housing colour even", "high",
        ("connector", "plug", "socket", "pin header", "screw terminal", "barrel jack", "usb port", "crimp",
         "disconnected connector")),
    Checklist(
        "wire", "wire or cable",
        ("exposed copper, a cut or broken conductor, frayed or splayed strands, chafed, cracked or melted "
         "insulation, a sharp kink, green or black copper, a wire end not in its terminal, a damaged ribbon "
         "cable edge",),
        "jacket continuous and evenly coloured, ends seated in their terminals", "high",
        ("wire", "cable", "ribbon cable", "heat shrink", "broken wire", "frayed wire", "loose wire")),
    Checklist(
        "battery", "battery or cell",
        ("a swollen or pillowed cell (puffy, rounded faces, lifted lid or neighbouring parts), a torn or split "
         "wrapper, a dent, white, blue or green crust at the terminals or contacts, leaked residue, rust on the "
         "holder springs, discoloured tabs",),
        "flat cell with a tight wrapper that fits its bay, clean terminals and contacts", "high",
        ("battery", "battery holder", "cell")),
    Checklist(
        "solder_joint", "solder joint",
        ("a bridge of solder to a neighbouring pad, a dull, grainy or cracked fillet (cold or cracked joint), a "
         "missing or too-small fillet, a solder ball, a lifted pad, a part standing on one end, a ring crack "
         "around a lead",),
        "shiny, smooth, concave fillet wetted to both the lead and the pad", "high",
        ("solder joint",)),
    Checklist(
        "pcb", "circuit board",
        ("brown or black heat marks, bubbles or lifting layers, a cut, lifted or burnt trace, green or white "
         "corrosion, pools of residue or water lines, a crack in the board, an empty footprint where a part is "
         "missing, a part out of place, dirt or a foreign object",),
        "even board colour, traces continuous, every footprint filled, clean surface", "medium",
        ("circuit board", "microcontroller board", "power supply")),
    Checklist(
        "fuse", "fuse",
        ("a broken or melted wire inside a glass fuse, blackened or cloudy glass, a melted window on a blade "
         "fuse, scorched or corroded fuse clips; a fuse that looks intact can still be open, so ok means only "
         "no visible fault",),
        "filament continuous, glass clear, clips clean and tight", "high",
        ("fuse", "fuse holder")),
    Checklist(
        "switch_relay", "switch, relay or control",
        ("a melted or cracked case, scorch at the pins, a broken or missing actuator, button or knob, a cracked "
         "potentiometer body, corrosion on the terminals",),
        "case intact, actuator present and straight, clean terminals", "medium",
        ("relay", "switch", "button", "potentiometer")),
    Checklist(
        "motor", "motor or fan",
        ("brown or darkened windings, melted insulation, a broken or missing fan blade, debris or hair in the hub, "
         "a cracked mount, scorched or loose brush leads and terminals",),
        "blades intact, windings evenly coloured, mount whole", "medium",
        ("motor", "servo", "fan")),
    Checklist(
        "inductor_transformer", "coil, inductor or transformer",
        ("a melted or cracked case, tar or varnish leaking out, darkened windings, scorch at the pins, a cracked "
         "or chipped core",),
        "case and core intact, windings evenly coloured, no leak", "medium",
        ("inductor", "transformer")),
    Checklist(
        "resistor", "resistor",
        ("a charred, browned or blistered body, a cracked body or chip, colour bands burnt dark in the middle, "
         "scorch on the board under it",),
        "body evenly coloured with crisp bands or marking, no scorch beneath", "medium",
        ("resistor",)),
    Checklist(
        "enclosure", "housing, cover or mechanical part",
        ("a crack, a warped or open seam, a melted spot, a lifted corner (which can mean something swells "
         "underneath), a missing or stripped screw, a broken clip, tab, bracket or spring, a stripped gear or a broken belt, "
         "rust",),
        "seams flush, every screw and clip present, parts straight and whole", "low",
        ("housing", "cover", "bracket", "standoff", "screw", "nut", "bolt", "heatsink", "gear", "belt", "pulley",
         "bearing", "spring", "missing screw")),
    Checklist(
        UNKNOWN_PART, "unidentified part",
        GENERAL_CHECKS,
        "no visible damage of any of these kinds", "medium",
        # labels with no family of their own, and damage words that name no part
        ("display", "speaker", "buzzer", "sensor", "antenna", "lamp", "label", "burn mark", "scorch mark",
         "melted plastic", "crack", "corrosion", "rust")),
)

BY_FAMILY: dict[str, Checklist] = {c.family: c for c in FAMILIES}
_BY_LABEL: dict[str, str] = {label: c.family for c in FAMILIES for label in c.labels}

WEAK_LABEL_BELOW = 0.35  # detector score under this is a weak match (the same band vision.inspect calls "weak")


def family_for(label: str | None) -> str:
    """The family a detector label suggests; `unknown_part` for a label with no family or no label at all."""
    return _BY_LABEL.get((label or "").strip().lower(), UNKNOWN_PART)


def crop_family(label: str | None, det_score: float | None = None, classifier_abstained: bool = False) -> str:
    """The family whose checklist a crop gets: `unknown_part` when the detector label is a weak match or the defect
    classifier abstained on the crop (unfamiliar part); else the label's family."""
    if classifier_abstained or (det_score is not None and det_score < WEAK_LABEL_BELOW):
        return UNKNOWN_PART
    return family_for(label)


def checklist(family: str) -> Checklist:
    return BY_FAMILY.get(family, BY_FAMILY[UNKNOWN_PART])


def checks_text(family: str) -> str:
    """The checks for one family as prompt text (the general checks are always added)."""
    c = checklist(family)
    own = "" if c.family == UNKNOWN_PART else "".join(f"\n- {s}" for s in c.look_for)
    general = "".join(f"\n- {s}" for s in GENERAL_CHECKS)
    return (f"Checks for a {c.words}:{own}" if own else "") + \
        f"\nGeneral checks for any part:{general}\nLooks ok when: {c.ok_looks}."


def table_text() -> str:
    """Every family's checks, for the VLM system prompt (identical across calls, so it caches)."""
    rows = []
    for c in FAMILIES:
        if c.family == UNKNOWN_PART:
            continue
        rows.append(f"* {c.words}: look for " + "; ".join(c.look_for) + f". Looks ok when: {c.ok_looks}.")
    rows.append("* any part, including one you cannot identify: look for " + "; ".join(GENERAL_CHECKS) + ".")
    return "\n".join(rows)


def prior_of(family: str) -> str:
    return checklist(family).prior
