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


def j(explains, issue="loose", proof="not_measured"):
    return {**f("connector", issue, proof=proof), "judgment": {"rank": 1, "likelihood_word": "likely",
                                                               "verdict": "suspect", "explains_symptom": explains}}


def test_limits_always_present_and_meter_suggested_for_unexplained_symptom():
    assert limits(None) == LIMITS
    assert limits("won't turn on")[-1] == SYMPTOM_NOT_EXPLAINED
    assert limits("won't turn on", [j("yes")]) == LIMITS


def test_meter_suggested_when_issues_exist_but_none_explains_the_symptom():
    # Greptile #6.4: an issue Jev does not tie to the symptom (a scuffed cover, a loose screw) is no reason to drop
    # the meter suggestion
    assert limits("won't turn on", [j("no"), j("unsure")])[-1] == SYMPTOM_NOT_EXPLAINED
    assert limits("won't turn on", [j("yes", issue="none")])[-1] == SYMPTOM_NOT_EXPLAINED   # no issue there
    assert limits("won't turn on", [j("yes", proof="ruled_out_by_meter")])[-1] == SYMPTOM_NOT_EXPLAINED
    assert limits("won't turn on", [j("no", proof="confirmed_by_meter")]) == LIMITS


def test_heat_words_are_whole_words_and_negations_do_not_count():
    # Greptile #6.3: "char" matched "charger"; "no scorch marks" matched "scorch"
    assert codes([f("usb charger", evidence=["the charger housing has a crack"])]) == ["unplug_first"]
    assert codes([f("circuit board", evidence=["even colour with no scorch, bubbles or cracks"])]) == ["unplug_first"]
    assert codes([f("terminal", evidence=["without burn marks, soot or melting"])]) == ["unplug_first"]
    assert codes([f("terminal", evidence=["judged in the crop of s8/det5 (detector label 'melted plastic')",
                                          "detector region labelled 'burn mark' (weak match)"])]) == ["unplug_first"]
    assert "heat_damage" in codes([f("plug prong", evidence=["the prong tip looks charred"])])
    assert "heat_damage" in codes([f("board", evidence=["no cracks, but the top is scorched"])])
    assert "heat_damage" in codes([f("board", evidence=["no cracks. Melted plastic near the jack"])])


def test_verdict_never_says_looks_good():
    assert TITLE["looks_good"] == "No visible problems found"
