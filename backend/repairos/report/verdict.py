"""The report verdict: a truth table in code (task B11; PLAN I3, I4, S5/A2 "Jev judges, code decides").

Three facts about the scan's regions, each computed in code from the report's buckets:

  judged      a model (VLM, classifier or meter) judged at least one region's state
  issue       some region shows an issue that no meter reading ruled out, or a meter reading confirmed one
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

V7 is the honest one: nothing judged shows an issue, but "looks good" would cover regions nobody looked at (I3).
V2/V3 do not arise from `facts_of` (an issue is always on a judged region); they keep the table total, and an issue
always wins. The reason texts are words only: they go to the summary writer (S5).
"""
from __future__ import annotations

from dataclasses import dataclass

LOOKS_GOOD, POTENTIAL_ISSUES, INSUFFICIENT = "looks_good", "potential_issues", "insufficient_evidence"

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
    "all_inspected_ok": "Every detected region was inspected and none shows an issue.",
    "some_not_inspected": ("Nothing that was inspected shows an issue, but some regions were not inspected or could "
                           "not be judged, so the product cannot be called good yet."),
}


@dataclass(frozen=True)
class Facts:
    judged: bool
    issue: bool
    unsettled: bool


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
    return Verdict(verdict, rule, code, REASON_TEXT[code])


def facts_of(report: dict) -> Facts:
    """The three facts from a report's buckets (findings / not_inspected / insufficient_evidence)."""
    findings = report.get("findings", [])
    judged = bool(findings)
    issue = any((e["issue"] != "none" and e["proof"]["status"] != "ruled_out_by_meter")
                or e["proof"]["status"] == "confirmed_by_meter" for e in findings)
    unsettled = bool(report.get("not_inspected")) or bool(report.get("insufficient_evidence"))
    return Facts(judged=judged, issue=issue, unsettled=unsettled)
