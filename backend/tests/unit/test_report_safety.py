"""Safety notes, limits and honest verdict wording (PLAN §6a)."""
from repairos.report.export import TITLE
from repairos.report.safety import LIMITS, SYMPTOM_NOT_EXPLAINED, limits, safety_notes


def f(part, issue="damaged", evidence=(), proof="not_measured"):
    return {"part_class": part, "issue": issue, "evidence": list(evidence), "proof": {"status": proof}}


def codes(findings):
    return [n["code"] for n in safety_notes(findings)]


def test_no_issues_no_safety_notes():
    assert codes([f("capacitor", "none"), f("connector", "none")]) == []


def test_battery_issue_comes_first_and_unplug_is_added():
    assert codes([f("battery")]) == ["battery_hazard", "unplug_first"]


def test_capacitor_and_heat_words():
    assert codes([f("electrolytic capacitor", evidence=["top looks scorched"])]) == [
        "heat_damage", "unplug_first", "capacitor_charge"]


def test_issue_ruled_out_by_meter_gives_no_note():
    assert codes([f("battery", proof="ruled_out_by_meter")]) == []


def test_limits_always_present_and_meter_suggested_for_unexplained_symptom():
    assert limits("looks_good", None) == LIMITS
    assert limits("looks_good", "won't turn on")[-1] == SYMPTOM_NOT_EXPLAINED
    assert limits("insufficient_evidence", "no sound")[-1] == SYMPTOM_NOT_EXPLAINED
    assert limits("potential_issues", "won't turn on") == LIMITS


def test_verdict_never_says_looks_good():
    assert TITLE["looks_good"] == "No visible problems found"
