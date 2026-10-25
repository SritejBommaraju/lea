"""The report verdict: a truth table in code (task B11; PLAN I3, I4, S5/A2 "Jev judges, code decides").

Three facts about the scan's regions, each computed in code from the report's buckets:

  judged      a model (VLM, classifier or meter) judged at least one region's state (`findings` or `cosmetic`)
  issue       some region in `findings` shows an issue that no meter reading ruled out, or a meter reading confirmed
              one (a cosmetic issue never counts)
  unsettled   some region was not inspected (I3) or could not be judged (I4)

| row | judged | issue | unsettled | verdict               | reason code          |
|-----|--------|-------|-----------|-----------------------|----------------------|
| V0  | no     | no    | no        | insufficient_evidence | no_regions           |
| V1  | no     | no    | yes       | insufficient_evidence | nothing_inspected    |
| V2  | no     | yes   | no        | potential_issues      | issues_found         |
| V3  | no     | yes   | yes       | potential_issues      | issues_found         |
| V4  | yes    | no    | no        | looks_good            | all_inspected_ok     |
| V5  | yes    | yes   | no        | potential_issues      | issues_found         |
| V6  | yes    | yes   | yes       | potential_issues      | issues_found         |
| V7  | yes    | no    | yes       | insufficient_evidence | some_not_inspected   |
| V8  | yes    | no    | yes       | no_problems_seen      | nothing_seen_wrong   |  (V7 when the capture covered
|     |        |       |           |                       |                      |   the object well: `covered`)

V7 is the honest one when coverage is poor: nothing judged shows an issue, but "looks good" would cover regions
nobody looked at (I3). V8 (2026-10-24, owner: "if there is no issue it defaults to not enough evidence but nothing
looks wrong"): the same facts, but the capture saw the object well (config/verdict.yaml: enough regions judged,
enough of the view dome and enough directions around it; report/coverage.py), so the verdict is "No problems seen",
never "looks good": the regions not checked are listed as the caveat (verdict_reason.caveat), not hidden.
V2/V3 do not arise from `facts_of` (an issue is always on a judged region); they keep the table total, and an issue
always wins. The reason texts are words only: they go to the summary writer (S5).
"""
from __future__ import annotations

from dataclasses import dataclass

LOOKS_GOOD, POTENTIAL_ISSUES, INSUFFICIENT = "looks_good", "potential_issues", "insufficient_evidence"
NO_PROBLEMS_SEEN = "no_problems_seen"

TABLE: dict[tuple[bool, bool, bool], tuple[str, str, str]] = {
    (False, False, False): ("V0", INSUFFICIENT, "no_regions"),
    (False, False, True): ("V1", INSUFFICIENT, "nothing_inspected"),
    (False, True, False): ("V2", POTENTIAL_ISSUES, "issues_found"),
    (False, True, True): ("V3", POTENTIAL_ISSUES, "issues_found"),
    (True, False, False): ("V4", LOOKS_GOOD, "all_inspected_ok"),
    (True, True, False): ("V5", POTENTIAL_ISSUES, "issues_found"),
    (True, True, True): ("V6", POTENTIAL_ISSUES, "issues_found"),
    (True, False, True): ("V7", INSUFFICIENT, "some_not_inspected"),
}

REASON_TEXT = {
    "no_regions": "Nothing was captured or detected that could be inspected.",
    "nothing_inspected": "Regions were found, but none of them could be inspected or judged.",
    "issues_found": "At least one inspected region shows a potential issue.",
    "all_inspected_ok": "Every detected region was inspected and none shows a functional or safety issue.",
    "some_not_inspected": ("Nothing that was inspected shows an issue, but some regions were not inspected or could "
                           "not be judged, so the product cannot be called good yet."),
    "nothing_seen_wrong": ("Nothing looked wrong in the areas that could be seen clearly, and the scan covered the "
                           "object well; some areas still need a closer photo, so this is not a clean bill of health."),
}


def load_config(path=None) -> dict:
    """config/verdict.yaml (the coverage gate of V8). A missing or malformed file is an error: the thresholds live
    in config, never in code."""
    import yaml

    from repairos.config import ROOT

    p = path or ROOT / "config" / "verdict.yaml"
    d = yaml.safe_load(open(p, encoding="utf-8")) or {}
    out = {"min_judged_regions": int(d["min_judged_regions"]), "min_view_share": float(d["min_view_share"]),
           "min_sectors": int(d["min_sectors"])}
    if out["min_judged_regions"] < 1 or not 0 < out["min_view_share"] <= 1 or out["min_sectors"] < 1:
        raise ValueError(f"{p}: thresholds out of range")
    return out


@dataclass(frozen=True)
class Facts:
    judged: bool
    issue: bool
    unsettled: bool
    covered: bool = False   # the capture saw the object well (load_config thresholds); only changes V7 -> V8


@dataclass(frozen=True)
class Verdict:
    verdict: str
    rule: str
    code: str
    text: str

    def as_reason(self) -> dict[str, str]:
        return {"code": self.code, "rule": self.rule, "text": self.text}


def decide(facts: Facts) -> Verdict:
    rule, verdict, code = TABLE[(facts.judged, facts.issue, facts.unsettled)]
    if rule == "V7" and facts.covered:
        rule, verdict, code = "V8", NO_PROBLEMS_SEEN, "nothing_seen_wrong"
    return Verdict(verdict, rule, code, REASON_TEXT[code])


def judged_regions(report: dict) -> int:
    return len(report.get("findings", [])) + len(report.get("cosmetic") or [])


def is_covered(report: dict, cfg: dict | None = None) -> bool:
    """The coverage gate: enough regions judged, and the capture's view dome (report capture.views) seen widely
    enough. No measured views (a loose photo): never covered."""
    cfg = cfg or load_config()
    views = (report.get("capture") or {}).get("views")
    if not views:
        return False
    return (judged_regions(report) >= cfg["min_judged_regions"] and views["share"] >= cfg["min_view_share"]
            and views["sectors"] >= cfg["min_sectors"])


def caveat(report: dict) -> str | None:
    """What was not checked, in plain words with counts (shown with the verdict; not sent to the summary writer):
    "Nothing looked wrong in the 2 areas Lea could see clearly. 11 areas need a closer photo." Photos left out because
    another photo already covers the same view are not areas."""
    seen = judged_regions(report)
    closer = len(report.get("insufficient_evidence") or []) + sum(   # a grouped row stands for `regions` areas
        int(e.get("regions", 1)) for e in report.get("not_inspected") or [] if e.get("part_class") != "whole photo")
    if not closer:
        return None
    first = (f"Nothing looked wrong in the {seen} area{'s' if seen != 1 else ''} Lea could see clearly."
             if seen and not any(e["issue"] != "none" for e in report.get("findings", [])) else "")
    tail = f"{closer} area{'s' if closer != 1 else ''} need{'s' if closer == 1 else ''} a closer photo."
    return f"{first} {tail}".strip()


def facts_of(report: dict) -> Facts:
    """The three facts from a report's buckets (findings / cosmetic / not_inspected / insufficient_evidence). A
    cosmetic issue counts as judged, never as an issue: it would not stop the device working."""
    findings = report.get("findings", [])
    judged = bool(findings) or bool(report.get("cosmetic"))
    issue = any((e["issue"] != "none" and e["proof"]["status"] != "ruled_out_by_meter")
                or e["proof"]["status"] == "confirmed_by_meter" for e in findings)
    unsettled = bool(report.get("not_inspected")) or bool(report.get("insufficient_evidence"))
    covered = bool(report.get("capture", {}).get("views")) and is_covered(report)
    return Facts(judged=judged, issue=issue, unsettled=unsettled, covered=covered)
