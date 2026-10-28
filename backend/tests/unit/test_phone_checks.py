"""The product's self-test and the phone's sensors through the gateway (repairos.grain.service, report.from_grain).

No network and no invented service: the answers below are written by hand from the documented shape of the design
engine's electrical plan and POST /call, and the design engine's side of the self-test is being built separately, so
what these tests prove is this side only: what the gateway forwards to /call, what it copies from the plan into the
report, and that every report and reading still validates against the contracts.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from repairos import contracts
from repairos.grain import service
from repairos.report import from_grain

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api_gateway import ex, make_scan, serve  # noqa: E402
from test_grain_client import CALL, CHAT, Recorder, gateway, paired, report_rev  # noqa: E402
from test_report_from_grain import PLAN, SCAN, build, reading  # noqa: E402

SELF_TEST = {
    "transports": ["ble", "led"],
    "checks": [
        {"index": 0, "kind": "rail", "net": "VIN", "expected": {"volts_min": 4.75, "volts_max": 5.25}},
        {"index": 1, "kind": "rail", "net": "3V3", "expected": {"volts_min": 3.1, "volts_max": 3.5}},
        {"index": 2, "kind": "output", "net": "LED_DRV", "expected": {"state": "on"}},
    ],
    "ble": {"service": "7A1E0001-5E1F-4E57-B5A1-0000C0FFEE01", "result": "7A1E0002-5E1F-4E57-B5A1-0000C0FFEE01",
            "control": "7A1E0003-5E1F-4E57-B5A1-0000C0FFEE01", "name_prefix": "GRAIN-"},
    "blink": {"timings": {"flash_on_ms": 100, "flash_off_ms": 100, "long_on_ms": 600, "long_off_ms": 300,
                          "gap_ms": 1000, "preamble_flashes": 3, "end_flashes": 3},
              "long_blinks": [1, 2, 3]},
}


def phone(rid, kind, part, value, **kw):
    return {"reading_id": rid, "scan_id": "scan_x", "session_id": "sess_x", "kind": kind, "part": part, "value": value,
            "power_state": "on", "source": "phone", "recorded_at": "2026-10-28T12:00:00Z", **kw}


def selftest(rid, net, value, **kw):
    return {"reading_id": rid, "scan_id": "scan_x", "session_id": "sess_x", "kind": "selftest", "net": net,
            "value": value, "power_state": "on", "source": "selftest", "recorded_at": "2026-10-28T12:00:00Z", **kw}


# ------------------------------------------------------------------------------------------ what goes to /call
def test_every_new_reading_goes_to_the_design_engine_flat_and_unchanged():
    sent = service.readings_payload([
        selftest("a", "VIN", {"index": 0, "value_mv": 4980, "ok": "ok", "transport": "ble"}, action_id="chk_vin"),
        selftest("b", "LED_DRV", {"index": 2, "state": "on", "ok": "fail", "transport": "led"}),
        phone("c", "magnetometer", "M1", {"delta_uT": 42.5, "state": "current"}),
        phone("d", "audio", "M1", {"hz": 187.5}),
        phone("e", "flicker", "LED1", {"hz": 120, "depth": 0.35}),
        phone("f", "light", "LED1", {"state": "dim"}),
        phone("g", "led", "LED_STATUS", {"state": "blinking", "color": "green"}),
        phone("h", "led", "LED_STATUS", {"state": "off"}),
    ])
    assert sent == [
        {"id": "a", "power_state": "on", "kind": "selftest", "net": "VIN", "index": 0, "value_mv": 4980, "ok": "ok",
         "transport": "ble", "check": "chk_vin"},
        {"id": "b", "power_state": "on", "kind": "selftest", "net": "LED_DRV", "index": 2, "state": "on", "ok": "fail",
         "transport": "led"},
        {"id": "c", "power_state": "on", "kind": "magnetometer", "part": "M1", "delta_uT": 42.5, "state": "current"},
        {"id": "d", "power_state": "on", "kind": "audio", "part": "M1", "hz": 187.5},
        {"id": "e", "power_state": "on", "kind": "flicker", "part": "LED1", "hz": 120, "depth": 0.35},
        {"id": "f", "power_state": "on", "kind": "light", "part": "LED1", "state": "dim"},
        {"id": "g", "power_state": "on", "kind": "led", "part": "LED_STATUS", "state": "blinking", "color": "green"},
        {"id": "h", "power_state": "on", "kind": "led", "part": "LED_STATUS", "state": "off"},
    ]


def test_a_multimeter_reading_still_goes_in_its_old_shape():
    sent = service.readings_payload([reading("a", "VIN", "voltage_dc", {"volts": 4.98}, action="chk_vin")])
    assert sent == [{"net": "VIN", "id": "a", "power_state": "on", "volts": 4.98, "check": "chk_vin"}]


@pytest.mark.parametrize("bad", [
    phone("x", "magnetometer", "", {"delta_uT": 3, "state": "none"}),            # no part
    phone("x", "magnetometer", "M1", {"delta_uT": 3}),                           # no state
    phone("x", "flicker", "L1", {"hz": 50}),                                     # no depth
    selftest("x", "", {"index": 0, "ok": "ok", "transport": "ble"}),             # no net
    selftest("x", "VIN", {"index": 0, "transport": "ble"}),                      # the product's verdict is missing
    {**phone("x", "audio", "M1", {"hz": 1}), "kind": "spectrum"},                 # a shape nobody knows
])
def test_a_reading_that_cannot_be_forwarded_whole_is_left_out_not_guessed(bad):
    assert service.readings_payload([bad]) == []


def test_the_words_for_the_customer_chat_cover_every_kind():
    assert service._reading_words(selftest("a", "VIN", {"index": 0, "value_mv": 4980, "ok": "ok", "transport": "ble"})) \
        == "VIN self-test via ble: 4.98 V, the product says ok"
    assert service._reading_words(phone("c", "magnetometer", "M1", {"delta_uT": 42.5, "state": "current"})) \
        == "M1 magnetometer (measured by the phone): delta_uT 42.5, state current"


def test_the_fix_message_lists_phone_readings_with_the_design_engines_verdict():
    msg = service.fix_message(project="demo_product", session={}, scan={"scan_id": "scan_x"}, call={},
                              readings=[phone("c", "audio", "M1", {"hz": 187.5})], words="it hums")
    assert "- M1 audio (measured by the phone): hz 187.5: not judged yet" in msg


def test_a_reading_about_a_part_is_never_matched_to_a_net_verdict():
    call = {**CALL, "electrical": {"readings": [{"net": "", "verdict": "ok"}], "findings": []}}
    pairs = from_grain.pair_readings(call, [phone("c", "audio", "M1", {"hz": 187.5})])
    assert pairs[0][1] is None


# ------------------------------------------------------------------------------------ what goes into the report
def test_the_plans_self_test_is_copied_into_the_report_and_the_report_validates():
    report = build(plan={**PLAN, "self_test": copy.deepcopy(SELF_TEST)})
    assert report["self_test"] == SELF_TEST
    contracts.validate("report", report)


def test_a_plan_without_a_self_test_gives_a_report_without_one():
    assert "self_test" not in build()
    assert "self_test" not in build(plan={**PLAN, "self_test": None})


def test_a_plan_steps_instrument_reaches_the_report_when_it_is_one_the_contract_names():
    plan = copy.deepcopy(PLAN)
    plan["checks"][0]["instrument"] = "selftest_ble"
    assert build(plan=plan)["next_step"]["target"]["instrument"] == "selftest_ble"
    plan["checks"][0]["instrument"] = "ouija_board"
    assert "instrument" not in build(plan=plan)["next_step"]["target"]


def test_only_what_the_contract_names_is_kept_so_the_report_still_validates():
    st = copy.deepcopy(SELF_TEST)
    st["firmware_hint"] = "x"
    st["ble"]["rssi_floor"] = -80
    st["checks"][0]["calibration"] = 1.01
    st["blink"]["timings"]["jitter"] = 5
    report = build(plan={**PLAN, "self_test": st})
    assert report["self_test"] == SELF_TEST


@pytest.mark.parametrize("change", [
    lambda st: st["checks"].append({"index": 0, "kind": "rail", "net": "DUP"}),            # a repeated index
    lambda st: st["checks"].append({"index": 5, "kind": "analog", "net": "X"}),            # a kind nobody knows
    lambda st: st["checks"].append({"index": -1, "kind": "rail", "net": "X"}),
    lambda st: st["checks"].append({"index": 5, "kind": "rail"}),                          # no net
    lambda st: st["checks"].append({"index": "5", "kind": "rail", "net": "X"}),            # not an integer
    lambda st: st.update(checks=[]),
])
def test_a_check_the_phone_could_not_map_drops_the_whole_self_test(change):
    st = copy.deepcopy(SELF_TEST)
    change(st)
    assert from_grain.self_test_block(st) is None


def test_a_way_to_read_the_self_test_is_kept_only_when_its_block_is_whole():
    st = copy.deepcopy(SELF_TEST)
    del st["ble"]["control"]
    only_led = from_grain.self_test_block(st)
    assert only_led["transports"] == ["led"] and "ble" not in only_led and "blink" in only_led
    st2 = copy.deepcopy(SELF_TEST)
    del st2["blink"]["timings"]["gap_ms"]
    only_ble = from_grain.self_test_block(st2)
    assert only_ble["transports"] == ["ble"] and "blink" not in only_ble
    st3 = copy.deepcopy(SELF_TEST)
    del st3["ble"]
    del st3["blink"]
    assert from_grain.self_test_block(st3) is None   # nothing left to read it with


def test_the_plans_long_blink_counts_are_kept_and_the_protocols_are_filled_in_when_it_states_none():
    st = copy.deepcopy(SELF_TEST)
    st["blink"]["long_blinks"] = [2, 3, 4]
    assert from_grain.self_test_block(st)["blink"]["long_blinks"] == [2, 3, 4]
    del st["blink"]["long_blinks"]
    assert from_grain.self_test_block(st)["blink"]["long_blinks"] == [1, 2, 3]


@pytest.mark.parametrize("counts", [[1, 1, 3], [1, 2], [0, 2, 3], [1, 2, "3"], "123"])
def test_results_that_cannot_be_told_apart_drop_the_light_but_keep_bluetooth(counts):
    st = copy.deepcopy(SELF_TEST)
    st["blink"]["long_blinks"] = counts
    got = from_grain.self_test_block(st)
    assert got["transports"] == ["ble"] and "blink" not in got
    contracts.validate("report", build(plan={**PLAN, "self_test": st}))


# ----------------------------------------------------------------------------- through the gateway, end to end
def answers_with_self_test():
    scan_record = {"id": "scan_g1", "points": 4, "bbox_mm": [10, 10, 10], "photos": [], "readings": [],
                   "lea": {"session_id": "sess_x", "scan_id": "scan_x", "frame": "arkit_world", "recommended_align": "icp",
                           "transform_arkit_m_to_design_mm": [1000.0] + [0.0] * 14 + [1.0]}}
    return {("POST", "/projects/demo_product/scans"): scan_record, ("POST", "/call"): CALL,
            ("GET", "/projects/demo_product/electrical/plan"): {**PLAN, "self_test": SELF_TEST},
            ("POST", "/projects/demo_product/chat"): CHAT}


def test_the_phone_gets_the_plan_and_its_readings_reach_the_design_engine(tmp_path):
    t = Recorder(answers_with_self_test())
    with serve(gateway(tmp_path, t)) as c:
        paired(c)
        _, session, _ = c.req("POST", "/v1/sessions", {"symptom": "no output", "product": "demo_product"})
        pkg = make_scan(c, session["session_id"])
        assert c.req("POST", "/v1/scans", pkg)[0] == 201
        assert c.req("POST", f"/v1/scans/{pkg['scan_id']}/complete")[0] == 202
        report, rev = report_rev(c, pkg["scan_id"], 1)
        assert report["self_test"] == SELF_TEST

        base = {"session_id": session["session_id"], "scan_id": pkg["scan_id"]}
        st = {**ex("meter-reading.selftest"), **base, "reading_id": "rd_st", "net": "VIN"}
        mag = {**ex("meter-reading.magnetometer"), **base, "reading_id": "rd_mag"}
        for r in (st, mag):
            contracts.validate("meter-reading", r)
            assert c.req("POST", "/v1/readings", r)[0] == 201
        report_rev(c, pkg["scan_id"], rev + 1)
        sent = json.loads(t.of("POST", "/call")[-1].content)["readings"]
        by_id = {r["id"]: r for r in sent}
        assert by_id["rd_st"] == {"id": "rd_st", "power_state": "on", "kind": "selftest", "net": "VIN", "index": 0,
                                  "value_mv": 4980, "ok": "ok", "transport": "ble", "check": "chk_power_in"}
        assert by_id["rd_mag"] == {"id": "rd_mag", "power_state": "on", "kind": "magnetometer", "part": "M1",
                                   "delta_uT": 42.5, "state": "current"}
        # the stored reading is exactly what the phone sent: no part on the self-test, no net on the sensor
        status, got, _ = c.req("GET", "/v1/readings/rd_mag")
        assert status == 200 and got["reading"]["part"] == "M1" and "net" not in got["reading"]


def test_the_contract_refuses_readings_the_phone_must_not_send(tmp_path):
    t = Recorder(answers_with_self_test())
    with serve(gateway(tmp_path, t)) as c:
        paired(c)
        _, session, _ = c.req("POST", "/v1/sessions", {"product": "demo_product"})
        pkg = make_scan(c, session["session_id"])
        base = {"session_id": session["session_id"], "scan_id": pkg["scan_id"]}
        no_part = {**ex("meter-reading.magnetometer"), **base, "reading_id": "rd_a"}
        no_part.pop("part")
        lux = {**ex("meter-reading.light"), **base, "reading_id": "rd_b", "value": {"state": "dim", "lux": 40}}
        typed_sensor = {**ex("meter-reading.led"), **base, "reading_id": "rd_c", "source": "typed"}
        for bad in (no_part, lux, typed_sensor):
            assert c.req("POST", "/v1/readings", bad)[0] >= 400
