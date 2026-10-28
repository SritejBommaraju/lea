"""The printable exports of a report (repairos.report.export): Markdown and self-contained HTML. They must work on a
report the design engine judged (rows with no photo, a summary that is not words only) and on the contract's pipeline-
shaped examples (rows with a crop)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from repairos.report import from_grain, to_html, to_markdown
from test_report_from_grain import CALL, PLAN, SCAN, reading

EXAMPLES = Path(__file__).resolve().parents[3] / "spec" / "contracts" / "examples"


def grain_report(**kw):
    return from_grain.build(scan=SCAN, project="demo_product", call=CALL, plan=PLAN, **kw)


def test_markdown_of_a_design_engine_report_has_no_photo_and_says_who_wrote_the_summary():
    md = to_markdown(grain_report())
    assert md.startswith("# Inspection report: Potential issues")
    assert "rear_wheel" in md and "missing" in md and "no photo" in md
    assert "Summary written by design engine (Grain)." in md and "from words only" not in md
    assert "**Next step:**" in md and "J1.1 and J1.2" in md
    assert "A phone scan sees the outside of the product only" in md   # the limits it carries
    assert "## Not inspected" in md and "board" in md


def test_html_of_a_design_engine_report_escapes_and_has_no_image():
    r = grain_report()
    r["findings"][0]["evidence"] = ["<b>bold</b> & more"]
    page = to_html(r)
    assert page.startswith("<!doctype html>") and "&lt;b&gt;bold&lt;/b&gt; &amp; more" in page
    assert "<img" not in page and "Summary written by design engine (Grain)." in page


def test_a_report_with_a_meter_reading_names_its_proof():
    call = json.loads(json.dumps(CALL))
    call["electrical"] = {"readings": [{"net": "GND", "verdict": "open", "findings": [0]}],
                          "findings": [{"text": "GND is open.", "where": "wire W3", "fix": "Resolder it.",
                                        "subject": {"net": "GND", "wire": "W3"}}]}
    r = from_grain.build(scan=SCAN, project="p", call=call, plan=PLAN,
                         readings=[reading("rd_gnd", "GND", "continuity", {"continuity": False}, pins=["J1.2", "U1.4"],
                                           power="off")])
    assert "confirmed by a meter reading" in to_markdown(r)


@pytest.mark.parametrize("name", ["report.json", "report.looks_good.json", "report.insufficient.json"])
def test_the_pipeline_shaped_examples_still_export(name):
    r = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
    md, page = to_markdown(r), to_html(r)
    assert "Inspection report" in md and "from words only" in md and page.startswith("<!doctype html>")
