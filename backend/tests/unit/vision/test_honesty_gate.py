"""The honesty gate on VLM crop answers (vision.inspect.honest_crop_state) and the crop prompt it relies on.

Regression facts from the opened-appliance scan of 2026-10-24 (scan_799d9ab8d44e0e2b, stills from about half a metre):
its three "most likely" findings were all guesses on soft photos of an appliance the owner had opened on purpose:
  - wire ends "loose" above a motor ("the photo is slightly blurred"), severity safety;
  - a wire end "frayed" (damaged) ("the photo is blurry"), severity safety;
  - a control panel "loose", "swung open and hangs off the main body", severity safety.
Their texts are copied below; no photo is used. Code decides from the VLM's typed answers (visibility,
open_for_access), never from words in its text."""
from __future__ import annotations

import pytest

from repairos.contracts import validate
from repairos.slots.detector import Detection
from repairos.slots.vlm import CropState
from repairos.slots.vlm_anthropic import CHECKLIST_SYSTEM, CROP_SCHEMA, SYSTEM
from repairos.vision import inspect as insp
from repairos.vision.crops import Crop

DET = Detection(box=(1530, 333, 3059, 1862), label="loose wire", score=0.4)
CROP = Crop(image=None, region_px=(1530, 333, 3059, 1862), scale=1.0, box_px=(1530.0, 333.0, 3059.0, 1862.0))


def cs(state, *, visibility="", open_for_access=False, severity="safety", text="", part="wire", confidence="medium"):
    return CropState(part_class=part, state=state, evidence_text=text, confidence_word=confidence,
                     model="claude-sonnet-5-5", latency_ms=1, severity=severity, visibility=visibility,
                     open_for_access=open_for_access)


def finding(state_obj):
    return validate("finding", insp.finding_from_crop(scan_id="scan_t", still_id="s22", index=2, det=DET, crop=CROP,
                                                      crop_state=state_obj)).model_dump(mode="json", exclude_none=True)


LOOSE_ENDS = ("Red and blue wires with bare twisted copper ends hang in the air above the small fan motor, with no "
              "terminal visible holding them. The photo is slightly blurred, but the free ends look unconnected.")
FRAYED = ("The white wire at the bottom right has a frayed, splayed end with loose strands that does not seem to sit "
          "in a terminal. Several other wires are tangled and looped, and the photo is blurry.")
PANEL = ("The control panel is swung open and hangs off the main body, with its wires still attached and exposed. The "
         "metal casing and fan motor are open with no cover on them.")


@pytest.mark.parametrize("state,text", [("loose", LOOSE_ENDS), ("damaged", FRAYED)])
def test_a_fault_on_a_soft_view_is_could_not_judge_get_closer(state, text):
    f = finding(cs(state, visibility="partial", text=text))
    assert f["state"] == "insufficient_evidence" and "severity" not in f
    assert f["evidence"][0].startswith("could not judge the wire")
    assert any("not a finding" in e and "closer, sharper photo" in e for e in f["evidence"])
    assert finding(cs(state, visibility="poor", text=text))["state"] == "insufficient_evidence"


def test_an_opened_device_is_not_a_fault():
    f = finding(cs("loose", visibility="clear", open_for_access=True, text=PANEL, part="appliance housing"))
    assert f["state"] == "ok" and "severity" not in f
    assert any("open for inspection" in e and "not a fault" in e for e in f["evidence"])
    blurred = finding(cs("missing", visibility="poor", open_for_access=True, text=PANEL))
    assert blurred["state"] == "insufficient_evidence"   # open and also unclear: no claim either way


def test_a_clear_fault_stays_a_fault_and_safety_needs_a_clear_view():
    f = finding(cs("burnt", visibility="clear", text="black scorch and melted insulation at the terminal"))
    assert f["state"] == "damaged" and f["severity"] == "safety"
    state, why = insp.honest_crop_state(cs("ok", visibility="partial", severity="safety"))
    assert state.severity == "functional" and "clear view" in why
    assert finding(cs("ok", visibility="poor", severity="none"))["state"] == "insufficient_evidence"
    assert finding(cs("ok", visibility="partial", severity="none"))["state"] == "ok"


def test_an_answer_without_visibility_passes_unchanged():
    state, why = insp.honest_crop_state(cs("loose", text=LOOSE_ENDS))   # older recordings had no visibility field
    assert state.state == "loose" and why is None


def test_the_crop_prompt_treats_an_open_device_as_normal_and_asks_for_visibility():
    for text in (SYSTEM, CHECKLIST_SYSTEM):
        assert "The device is normally open" in text and "never a fault by themselves" in text
        assert "visibility:" in text and "open_for_access:" in text
        assert "hanging, not fastened" not in text            # the old loose rule that called an open panel loose
    assert {"visibility", "open_for_access"} <= set(CROP_SCHEMA["required"])
    assert CROP_SCHEMA["properties"]["visibility"]["enum"] == ["clear", "partial", "poor"]
