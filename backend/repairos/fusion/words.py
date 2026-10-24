"""Number -> word bands and enum -> phrase maps for the fusion state (F4.1, S5).

Numbers are compared here, in code; the engine only ever sees the words. Generic and product-agnostic. The bands
are unvalidated starting points: change them only with held-out eval numbers in the PR (GEN2).

A band list is [(upper_bound_exclusive, word), ...] ascending; the last entry's bound is None (= +inf).
"""
from __future__ import annotations

import math

Band = list[tuple[float | None, str]]

BANDS: dict[str, Band] = {
    # share of the expected visible surface the scan or photos covered, 0..1
    "coverage": [(0.3, "mostly not covered"), (0.8, "partly covered"), (None, "well covered")],
    # |mean signed distance to the design surface|, mm
    "offset_mm": [(1.0, "flush with the design surface"), (3.0, "slightly off the design surface"),
                  (None, "clearly off the design surface")],
    # length of the translation vector against the design, mm
    "shift_mm": [(1.0, "in its design position"), (4.0, "slightly out of position"), (None, "clearly out of position")],
    # connector housing gap estimate, mm
    "gap_mm": [(0.5, "no gap seen"), (1.5, "a small gap"), (None, "a clear gap")],
    # P(observed colour matches the expected colour)
    "colour_match_p": [(0.35, "colour does not match"), (0.65, "colour match unclear"), (None, "colour matches")],
    # silhouette IoU against the reference render
    "silhouette_iou": [(0.5, "outline does not match the reference"), (0.8, "outline partly matches the reference"),
                       (None, "outline matches the reference")],
    # focus measure of the crop (variance of the Laplacian)
    "sharpness": [(100.0, "blurred"), (300.0, "slightly soft"), (None, "sharp")],
    # number of stills or keyframes that saw the subject
    "views": [(1, "not seen in any view"), (2, "seen in one view"), (3, "seen in two views"),
              (None, "seen in several views")],
    # the source's own (uncalibrated) confidence
    "confidence": [(0.35, "low"), (0.7, "medium"), (None, "high")],
    # Suspect.p_fault -> Suspect.p_fault_word (I6: words until calibration is validated)
    "p_fault": [(0.15, "very_unlikely"), (0.35, "unlikely"), (0.65, "unsure"), (0.85, "likely"), (None, "very_likely")],
}


def band(name: str, value: float) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        raise ValueError(f"{name}: no value to band")
    for bound, word in BANDS[name]:
        if bound is None or value < bound:
            return word
    raise AssertionError("unreachable: the last band is open")


# Finding.state -> what it says, in words (every value of finding.schema.json state.enum)
FINDING_STATE = {
    "ok": "looks as expected",
    "partly": "only partly seen or partly present",
    "missing": "missing",
    "shifted": "out of its expected position",
    "damaged": "damaged",
    "broken": "broken",
    "worn": "worn",
    "dirty": "dirty or contaminated",
    "loose": "loose",
    "unseated": "connector not fully seated",
    "wrong_pin": "wire on the wrong pin",
    "colour_mismatch": "wire colour differs from the expected colour",
    "open": "open circuit",
    "short": "short circuit",
    "out_of_range": "reading outside the expected range",
    "unexpected_part": "a part that is not in the expected design",
    "unexpected_wire": "a wire that is not in the expected design",
    "not_seen_in_photo": "not visible in the photos",
    "not_inspected": "not inspected",
    "insufficient_evidence": "not enough evidence to judge",
    "unsure": "the inspection could not tell",
}

# Finding.source -> where the evidence came from (every value of finding.schema.json source.enum)
FINDING_SOURCE = {
    "repairos.scan": "3D scan",
    "repairos.vision": "photo inspection",
    "repairos.wire_check": "wire check on photos",
    "repairos.ocr": "label text read from a photo",
    "repairos.meter": "multimeter reading",
    "repairos.user": "the user",
    "grain.diagnose": "design comparison",
    "grain.look": "second-opinion photo check",
    "grain.electrical": "design electrical check",
}

# Finding.vlm_answer -> words (every value of finding.schema.json vlm_answer.enum)
VLM_ANSWER = {
    "seated": "connector looks fully seated",
    "gap_visible": "a gap is visible at the connector",
    "unplugged": "connector looks unplugged",
    "present": "present",
    "absent": "absent",
    "cannot_tell": "the photo check could not tell",
}

DESIGN_TRUST = {
    "high": "high: the reference design is trusted",
    "low": "low: the reference design may be wrong; what the photos show takes precedence",
}

# MeterReading verdict (computed in code, override.py) -> words for the state
METER = {
    None: "not tested",
    "agrees": "measured; the reading agrees with the expected value",
    "disagrees": "measured; the reading disagrees with the expected value",
    "unknown": "measured; no expected value to judge the reading against",
}

MEASURED = "[measured]"  # stands in for a number scrubbed from free text; its meaning is in the words beside it
