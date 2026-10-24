"""The ONE generic vocabulary for vision (task M1, invariant GEN1).

Every class name and crop state used by perception lives here and nowhere else. These are generic electronics and
mechanical categories that apply to any hardware product: no product names, no refs, no brand words. Product-specific
text (BOM row descriptions) is added at runtime by `prompts.py` in Phase 2; it is never stored.
"""
from __future__ import annotations

# Parts a detector is asked to find. Short noun phrases: open-vocabulary detectors match these as text.
PART_CLASSES: tuple[str, ...] = (
    # wiring and connection
    "wire", "cable", "ribbon cable", "connector", "plug", "socket", "pin header", "screw terminal", "barrel jack",
    "usb port", "crimp", "heat shrink", "solder joint",
    # boards and components
    "circuit board", "integrated circuit", "chip", "microcontroller board", "capacitor", "electrolytic capacitor",
    "resistor", "inductor", "transformer", "diode", "led", "fuse", "fuse holder", "relay", "switch", "button",
    "potentiometer", "crystal", "voltage regulator", "transistor",
    # power
    "battery", "battery holder", "cell", "power supply",
    # electromechanical and mechanical
    "motor", "servo", "fan", "gear", "belt", "pulley", "bearing", "spring", "screw", "nut", "bolt", "standoff",
    "bracket", "housing", "cover", "heatsink",
    # interface and sensing
    "display", "speaker", "buzzer", "sensor", "antenna", "lamp", "label",
)

# Visible damage a detector is asked to find directly (a box on the damage itself, not on a part).
DAMAGE_CLASSES: tuple[str, ...] = (
    "burn mark", "scorch mark", "melted plastic", "crack", "corrosion", "rust", "bulging capacitor", "broken wire",
    "frayed wire", "loose wire", "disconnected connector", "missing screw",
)

GENERIC_CLASSES: tuple[str, ...] = PART_CLASSES + DAMAGE_CLASSES

# Wire-like classes: a finding about one of these is subject kind "wire", every other class is a "part".
WIRE_CLASSES: frozenset[str] = frozenset({"wire", "cable", "ribbon cable", "broken wire", "frayed wire", "loose wire"})

# What the VLM may say about one crop (M3). `cannot_tell` is always allowed (I4).
CROP_STATES: tuple[str, ...] = (
    "ok", "damaged", "burnt", "cracked", "corroded", "loose", "unplugged", "missing", "cannot_tell",
)

CONFIDENCE_WORDS: tuple[str, ...] = ("low", "medium", "high")

# Words that mean a device runs on wall (mains) power, for the report's safety note (report/safety.py). Matched as
# whole words in the part names, part words, evidence and reasons of rows a model looked at.
MAINS_WORDS: tuple[str, ...] = (
    "mains", "wall plug", "wall charger", "wall adapter", "ac plug", "ac adapter", "plug prong", "plug prongs",
    "plug blade", "plug blades", "plug pin", "plug pins", "power cord", "mains cable", "line cable", "line cord",
    "heating element", "thermostat", "power supply", "transformer",
)
# Neither alone says wall power, together they do: a spade terminal on a motor, heater, thermostat or timer.
MAINS_PAIR: tuple[tuple[str, ...], tuple[str, ...]] = (
    ("spade terminal", "spade connector", "quick-connect terminal"),
    ("motor", "heating element", "heater", "thermostat", "timer"),
)

# Defect kinds the defect classifier (CLS slot, task M7) can name for one crop, given that it sees a defect. Only
# kinds with licence-clean training data are listed (training/datasets/MANIFEST.json); a kind without data is not
# claimed. Each maps to the crop state a finding uses.
DEFECT_CLASSES: tuple[str, ...] = (
    "burnt or melted", "bent or deformed", "missing part", "scratch or surface damage", "solder fault",
    "misplaced or extra part",
)
DEFECT_TO_CROP_STATE: dict[str, str] = {
    "burnt or melted": "burnt", "bent or deformed": "damaged", "missing part": "missing",
    "scratch or surface damage": "damaged", "solder fault": "damaged", "misplaced or extra part": "damaged",
}
