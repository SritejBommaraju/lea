"""The LEA report built from the design engine's answer (repairos.report.from_grain).

The answers below are written by hand from the documented API shape of POST /call, GET .../electrical/plan and
GET .../bom/{ref}; they are not recorded responses. They only exercise how an answer becomes a report: the mapping, the
verdict, the honesty rules, and that every report validates against spec/contracts/report.schema.json.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from repairos import contracts
from repairos.report import from_grain

EXAMPLES = Path(__file__).resolve().parents[3] / "spec" / "contracts" / "examples"
SCAN = json.loads((EXAMPLES / "scan-package.json").read_text(encoding="utf-8"))

CALL = {
    "summary": "The rear wheel is missing and the cover sits proud of the case. The power input should be measured.",
    "fixes": [
        {"part": "rear_wheel", "problem": "not in the scan where the design puts it", "action": "buy",
         "confidence": "high", "detail": "The scan has no points where the wheel belongs.", "how": "Clip the new wheel on.",
         "bom": {"ref": "W1", "mpn": "WHL-65", "description": "65 mm wheel", "supplier": "Example Parts",
                 "url": "https://parts.example/wheel-65", "unit_price": 3.5}},
        {"part": "cover", "problem": "sits 4 mm off its seat", "action": "reseat", "confidence": "medium",
         "detail": "Press the cover down until it clicks."},
        {"part": "hinge", "problem": "worn through in the photos", "action": "remake", "confidence": "low",
         "detail": "Print a new hinge."},
        {"part": "latch", "problem": "the scan did not reach it", "action": "rescan", "confidence": "low",
         "detail": "Scan the back of the case slowly."},
        {"part": "case", "problem": "the design has no room for a screw head", "action": "fix_design",
         "confidence": "medium", "detail": "Counterbore the screw seat."},
    ],
    "buy": [], "remake": [],
    "diagnosis": {"parts": [
        {"name": "rear_wheel", "verdict": "not_seen", "coverage": 0.0, "why": "no scan points on the wheel's surface"},
        {"name": "cover", "verdict": "shifted", "coverage": 0.9, "why": "sits about 4 mm off its seat"},
        {"name": "front_wheel", "verdict": "ok", "coverage": 0.95},
        {"name": "lid", "verdict": "partly", "coverage": 0.55, "why": "half of its surface was scanned"},
        {"name": "board", "verdict": "not_visible", "coverage": 0.0, "why": "inside the case"},
        {"name": "dented_panel", "verdict": "damaged", "coverage": 0.8},
    ]},
    "electrical": None,
    "photos": None,
    "bom_total_usd": 12.5,
    "seconds": 31.0,
}

PLAN = {"status": "ok", "notes": [], "max_expected_volts": 5.0, "checks": [
    {"id": "chk_vin", "kind": "voltage_dc", "net": "VIN", "pins": ["J1.1", "J1.2"], "power_state": "on",
     "expected": {"volts_min": 4.75, "volts_max": 5.25}, "why": "The input rail feeds everything else.",
     "safety": "Keep the probes apart; the product is powered.", "parts": ["J1"]},
    {"id": "chk_gnd", "kind": "continuity", "net": "GND", "pins": ["J1.2", "U1.4"], "power_state": "off",
     "expected": {"continuity": True, "ohms_max": 1.0}, "why": "A broken ground starves the chip.", "safety": ""},
]}


def reading(rid, net, kind, value, *, pins=None, power="on", action=None):
    r = {"reading_id": rid, "scan_id": SCAN["scan_id"], "session_id": SCAN["session_id"], "kind": kind, "net": net,
         "value": value, "power_state": power, "source": "typed", "recorded_at": "2026-10-28T12:00:00Z"}
    if pins:
        r["pins"] = pins
    if action:
        r["action_id"] = action
    return r


def build(call=CALL, **kw):
    report = from_grain.build(scan=SCAN, project="demo_product", call=call, plan=kw.pop("plan", PLAN), **kw)
    contracts.validate("report", report)
    return report


def by_part(report, part):
    return next(r for r in report["findings"] if r["subject"]["ref"] == part)


def test_flagged_parts_become_findings_with_the_mapped_state():
    r = build()
    assert r["verdict"] == "potential_issues" and r["verdict_reason"]["code"] == "issues_found"
    assert r["design"] == {"engine": "grain", "project": "demo_product"}
    assert r["summary"]["text"] == CALL["summary"] and r["summary"]["from_words_only"] is False
    issue = {f["subject"]["ref"]: f["issue"] for f in r["findings"]}
    assert issue["rear_wheel"] == "missing" and issue["cover"] == "shifted" and issue["lid"] == "partly"
    assert issue["dented_panel"] == "damaged" and issue["front_wheel"] == "none"
    assert issue["hinge"] == "damaged"   # named only by a fix (remake): the photo judge gave no state
    for f in r["findings"]:
        assert f["subject"]["kind"] == "part" and f["inspected_by"] == ["grain"] and "crop" not in f
        assert f["proof"] == {"status": "not_measured"} and f["location"]["status"] == "not_computed"
    assert by_part(r, "rear_wheel")["confidence_word"] == "high" and by_part(r, "cover")["confidence_word"] == "medium"
    assert "partly covered" in " ".join(by_part(r, "lid")["evidence"])
    assert len({f["finding_id"] for f in r["findings"]}) == len(r["findings"])


def test_nothing_is_shown_ok_that_the_scan_could_not_see():
    r = build()
    assert [x["subject"]["ref"] for x in r["not_inspected"]] == ["board"]
    assert r["not_inspected"][0]["reason"] == "inside the case"
    assert "board" not in {f["subject"]["ref"] for f in r["findings"]}


def test_the_top_finding_is_the_first_fix_and_most_likely_has_at_most_three():
    r = build()
    assert r["most_likely"][0] == by_part(r, "rear_wheel")["finding_id"] and len(r["most_likely"]) == 3
    assert [f["issue"] == "none" for f in r["findings"]] == sorted(f["issue"] == "none" for f in r["findings"])


def test_fixes_buy_remake_reseat_rescan_and_design_change():
    r = build(part_urls={"hinge": "https://design.example/projects/demo/bom/part/hinge/file?format=stl"})
    assert "WHL-65" in by_part(r, "rear_wheel")["fix"] and "Example Parts" in by_part(r, "rear_wheel")["fix"]
    assert "USD 3.50" in by_part(r, "rear_wheel")["fix"]
    assert r["fix"]["part"] == "rear_wheel"
    assert r["fix"]["options"] == [{"title": "65 mm wheel", "seller": "Example Parts",
                                    "link": "https://parts.example/wheel-65", "price": 3.5, "currency": "USD"}]
    assert "format=stl" in by_part(r, "hinge")["fix"]
    assert by_part(r, "cover")["fix"].startswith("Press the cover down")
    assert [x["subject"]["ref"] for x in r["insufficient_evidence"]] == ["latch"]
    assert any("Counterbore the screw seat" in line for line in r["limits"])
    assert "case" not in {f["subject"]["ref"] for f in r["findings"]}


def test_a_bom_row_fetched_for_a_buy_fix_with_no_row_is_used():
    call = copy.deepcopy(CALL)
    del call["fixes"][0]["bom"]
    row = {"mpn": "WHL-65", "description": "65 mm wheel", "supplier": "Example Parts",
           "url": "https://parts.example/wheel-65", "unit_price": 3.5}
    r = build(call, bom_rows={"rear_wheel": row})
    assert r["fix"]["options"][0]["link"] == "https://parts.example/wheel-65"
    no_link = build(call, bom_rows={"rear_wheel": {"mpn": "WHL-65", "unit_price": 3.5}})
    assert "fix" not in no_link   # the contract needs a link; without one there is no Fix it card
    assert "WHL-65" in by_part(no_link, "rear_wheel")["fix"]


def test_a_photo_judges_state_is_used_for_a_part_only_photos_name():
    call = copy.deepcopy(CALL)
    call["photos"] = {"judgement": {"observations": [{"part": "hinge", "state": "worn", "note": "the pin is shiny"}]}}
    r = build(call, stills=2)
    assert by_part(r, "hinge")["issue"] == "worn" and "the pin is shiny" in by_part(r, "hinge")["evidence"]
    assert r["capture"]["stills"] == 2 and r["capture"]["stills_inspected"] == 2
    assert r["models"]["vlm"] == "design engine photo judge"


def test_next_step_is_the_first_unanswered_check_and_prefills_the_meter_sheet():
    r = build()
    step = r["next_step"]
    assert step["action_id"] == "chk_vin" and step["kind"] == "run_voltage_test" and step["power_state"] == "on"
    assert step["target"] == {"net": "VIN", "pins": ["J1.1", "J1.2"], "part_refs": ["J1"]}
    assert step["expected"] == {"volts_min": 4.75, "volts_max": 5.25, "source": "design_engine"}
    assert step["instruction"].startswith("With the product switched on, set the meter to DC volts")
    assert "J1.1 and J1.2" in step["instruction"] and step["safety_notes"] == ["Keep the probes apart; the product is powered."]
    assert step["reason"] == "The input rail feeds everything else."
    # a reading on VIN moves it on to the ground check, which is read with the product off
    r2 = build(readings=[reading("rd_1", "VIN", "voltage_dc", {"volts": 5.0}, action="chk_vin")])
    s2 = r2["next_step"]
    assert s2["action_id"] == "chk_gnd" and s2["kind"] == "run_continuity_test" and s2["power_state"] == "off"
    assert s2["instruction"].startswith("With the product switched off and unplugged")
    assert s2["expected"] == {"continuity": True, "ohms_max": 1.0, "source": "design_engine"}
    assert {s["code"] for s in r2["safety"]} == {"unplug_first"}
    # every check answered: the design engine's rescan request, else nothing
    r3 = build(readings=[reading("rd_1", "VIN", "voltage_dc", {"volts": 5.0}),
                         reading("rd_2", "gnd", "continuity", {"continuity": True}, pins=["J1.2", "U1.4"], power="off")])
    assert r3["next_step"]["kind"] == "rescan" and r3["next_step"]["instruction"] == "Scan the back of the case slowly."


def test_no_plan_endpoint_leaves_the_plan_empty():
    r = build(plan={})
    assert r["next_step"]["kind"] == "rescan"
    call = copy.deepcopy(CALL)
    call["fixes"] = [f for f in call["fixes"] if f["action"] != "rescan"]
    assert "next_step" not in build(call, plan={})


def test_a_high_voltage_plan_says_so():
    plan = {**PLAN, "max_expected_volts": 230.0}
    assert "mains_power" in {s["code"] for s in build(plan=plan)["safety"]}


def test_a_meter_reading_the_design_engine_calls_open_is_proof_on_the_net():
    call = copy.deepcopy(CALL)
    call["electrical"] = {
        "readings": [{"net": "GND", "verdict": "open", "findings": [0]}, {"net": "VIN", "verdict": "ok", "findings": []}],
        "findings": [{"severity": "error", "text": "GND is open between J1.2 and U1.4.", "where": "wire W3",
                      "fix": "Resolder the ground wire.", "subject": {"net": "GND", "wire": "W3", "pins": ["J1.2", "U1.4"]}}]}
    readings = [reading("rd_vin", "VIN", "voltage_dc", {"volts": 5.0}),
                reading("rd_gnd", "GND", "continuity", {"continuity": False}, pins=["J1.2", "U1.4"], power="off")]
    r = build(call, readings=readings)
    gnd = next(f for f in r["findings"] if f["subject"].get("kind") == "wire")
    assert gnd["subject"] == {"kind": "wire", "ref": "W3", "pins": ["J1.2", "U1.4"]}
    assert gnd["issue"] == "open" and gnd["proof"] == {"status": "confirmed_by_meter", "reading_id": "rd_gnd"}
    assert gnd["inspected_by"] == ["meter", "grain"] and gnd["fix"] == "Resolder the ground wire."
    vin = next(f for f in r["findings"] if f["subject"] == {"kind": "net", "ref": "VIN"})
    assert vin["issue"] == "none" and vin["proof"] == {"status": "ruled_out_by_meter", "reading_id": "rd_vin"}
    assert r["most_likely"][0] == gnd["finding_id"]   # proof first


def test_a_reading_that_cannot_be_compared_is_insufficient_evidence_with_a_reason():
    call = copy.deepcopy(CALL)
    call["electrical"] = {"readings": [{"net": "MYSTERY", "verdict": "no_expected", "findings": []}], "findings": []}
    r = build(call, readings=[reading("rd_1", "MYSTERY", "voltage_dc", {"volts": 3.3})])
    row = next(x for x in r["insufficient_evidence"] if x["subject"]["ref"] == "MYSTERY")
    assert "no expected value" in row["reason"] and "proof" not in row


def test_nothing_flagged_is_no_problems_seen_with_what_was_not_looked_at():
    call = {"summary": "", "fixes": [], "diagnosis": {"parts": [
        {"name": "front_wheel", "verdict": "ok", "coverage": 0.95}, {"name": "board", "verdict": "not_visible"}]}}
    r = build(call)
    assert r["verdict"] == "no_problems_seen" and r["verdict_reason"]["code"] == "nothing_seen_wrong"
    assert "could not be seen from outside" in r["verdict_reason"]["caveat"]
    assert r["summary"]["text"] == r["verdict_reason"]["text"] and r["most_likely"] == []
    assert r["findings"][0]["issue"] == "none" and r["findings"][0]["confidence_word"] == "high"
    assert [x["subject"]["ref"] for x in r["not_inspected"]] == ["board"]


def test_nothing_seen_at_all_is_insufficient_evidence_not_ok():
    call = {"fixes": [], "diagnosis": {"parts": [{"name": "board", "verdict": "not_visible"}]}}
    r = build(call)
    assert r["verdict"] == "insufficient_evidence" and r["verdict_reason"]["code"] == "nothing_inspected"
    empty = build({"fixes": [], "diagnosis": {"parts": []}})
    assert empty["verdict"] == "insufficient_evidence" and empty["verdict_reason"]["code"] == "no_regions"


def test_an_unknown_verdict_is_not_shown_as_a_fault_or_as_fine():
    call = {"fixes": [], "diagnosis": {"parts": [{"name": "knob", "verdict": "sparkly"}]}}
    r = build(call)
    assert r["findings"] == [] and r["insufficient_evidence"][0]["subject"]["ref"] == "knob"


def test_capture_facts_come_from_the_scan_package():
    r = build()
    assert r["capture"]["keyframes"] == len(SCAN["keyframes"]) and r["capture"]["scan_points"] == SCAN["point_count"]
    assert r["capture"]["depth_source"] == "lidar" and r["capture"]["coverage_words"] == "well covered"


def test_no_design_means_the_report_says_so():
    r = from_grain.unknown_design(scan=SCAN)
    contracts.validate("report", r)
    assert r["verdict"] == "insufficient_evidence" and "design" not in r and r["findings"] == []
    assert "which product" in r["summary"]["text"]


@pytest.mark.parametrize("value,word", [(0.1, "low"), (0.5, "medium"), (0.9, "high"), ("HIGH", "high"), (None, "medium")])
def test_confidence_is_always_a_word(value, word):
    assert from_grain.confidence_word(value) == word


def test_every_row_names_a_finding_the_state_store_can_keep():
    call = copy.deepcopy(CALL)
    call["electrical"] = {"readings": [{"net": "VIN", "verdict": "ok", "findings": []}], "findings": []}
    r = build(call, readings=[reading("rd_vin", "VIN", "voltage_dc", {"volts": 5.0})])
    found = from_grain.findings_of(r)
    for f in found:
        contracts.validate("finding", f)
    rows = [x for b in ("findings", "not_inspected", "insufficient_evidence") for x in r[b]]
    assert [f["finding_id"] for f in found] == [x["finding_id"] for x in rows]
    state = {f["subject"]["ref"]: (f["state"], f["source"], f["is_proof"]) for f in found}
    assert state["rear_wheel"] == ("missing", "grain", False) and state["front_wheel"] == ("ok", "grain", False)
    assert state["board"] == ("not_inspected", "grain", False) and state["latch"][0] == "insufficient_evidence"
    assert state["VIN"] == ("ok", "repairos.meter", True)   # a reading settled it: proof, and it names the reading
    assert next(f for f in found if f["subject"]["ref"] == "VIN")["evidence_refs"] == {"reading_id": "rd_vin"}


def test_the_same_judgement_keeps_its_finding_id_and_a_changed_one_gets_a_new_one():
    now = __import__("datetime").datetime(2026, 10, 28, 12, 0, 0)
    a, b = build(now=now), build(now=now)
    assert [f["finding_id"] for f in a["findings"]] == [f["finding_id"] for f in b["findings"]]
    changed = copy.deepcopy(CALL)
    changed["diagnosis"]["parts"][0]["verdict"] = "damaged"
    c = build(changed, now=now)
    assert by_part(c, "rear_wheel")["finding_id"] != by_part(a, "rear_wheel")["finding_id"]
    assert by_part(c, "cover")["finding_id"] == by_part(a, "cover")["finding_id"]
    assert a["most_likely"][0] == by_part(a, "rear_wheel")["finding_id"]


# --- review fixes (Greptile on PR #48): photos merge with the scan, a verdict never lands on a guessed reading
def with_photos(*observations, parts=None):
    call = copy.deepcopy(CALL)
    call["photos"] = {"judgement": {"observations": list(observations)}}
    if parts is not None:
        call["diagnosis"] = {"parts": parts}
    return call


def test_a_fault_the_photos_show_in_a_part_the_scan_found_fine_stands():
    call = with_photos({"part": "front_wheel", "state": "damaged", "note": "the tyre is split", "confidence": "high"})
    r = build(call)
    row = by_part(r, "front_wheel")
    assert row["issue"] == "damaged" and row["confidence_word"] == "high" and row["proof"] == {"status": "not_measured"}
    assert "the tyre is split" in row["evidence"] and any("matches the design" in x for x in row["evidence"])
    assert row["finding_id"] in r["most_likely"] or r["verdict"] == "potential_issues"


def test_photos_add_to_a_part_the_scan_already_flagged_and_can_judge_one_it_could_not_see():
    call = with_photos({"part": "cover", "state": "dirty", "note": "grease on the seat"},
                       {"part": "board", "state": "worn", "note": "burnt pad"})
    r = build(call)
    cover = by_part(r, "cover")
    assert cover["issue"] == "shifted" and any("also show it dirty" in x for x in cover["evidence"])
    assert by_part(r, "board")["issue"] == "worn" and r["not_inspected"] == []   # the photos did look at it


def test_a_photo_only_fault_with_no_fix_and_no_scan_verdict_is_kept():
    r = build(with_photos({"part": "strap", "state": "loose", "note": "hangs by one screw"}))
    row = by_part(r, "strap")
    assert row["issue"] == "loose" and row["inspected_by"] == ["grain"] and row["confidence_word"] == "medium"


def test_a_verdict_is_never_given_to_a_reading_it_may_not_be_about():
    call = copy.deepcopy(CALL)
    call["electrical"] = {"readings": [{"net": "GND", "verdict": "open", "findings": []}], "findings": []}
    two = [reading("rd_a", "GND", "continuity", {"continuity": False}, pins=["J1.2", "U1.4"], power="off"),
           reading("rd_b", "GND", "continuity", {"continuity": True}, pins=["J2.2", "U2.4"], power="off")]
    r = build(call, readings=two)
    assert not [f for f in r["findings"] if f["proof"]["status"] != "not_measured"]   # two readings, no id: no proof
    assert any("could not tell which of your readings" in x["reason"] for x in r["insufficient_evidence"])
    call["electrical"]["readings"][0]["id"] = "rd_zzz"   # an id the engine echoes must match one of ours
    r2 = build(call, readings=two[:1])
    assert not [f for f in r2["findings"] if f["proof"]["status"] != "not_measured"]
    call["electrical"]["readings"][0]["id"] = "rd_b"
    r3 = build(call, readings=two)
    proven = [f for f in r3["findings"] if f["proof"]["status"] == "confirmed_by_meter"]
    assert [f["proof"]["reading_id"] for f in proven] == ["rd_b"]


def test_the_unknown_design_report_is_plain_words_and_validates():
    r = from_grain.unknown_design(scan=SCAN)
    contracts.validate("report", r)
    assert r["summary"]["from_words_only"] is True and "design" not in r
