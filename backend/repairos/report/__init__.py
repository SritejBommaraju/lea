"""Report generation (PLAN.md task B11): code picks the verdict (verdict.py, a tested truth table), Claude writes
the plain words from words only (summary.py, S5), the report validates against spec/contracts/report.schema.json
(B9), and export.py prints it as Markdown or self-contained HTML.

    draft = build_report(scan=..., stills=..., findings=..., suspects=..., next_action=..., regions=...,
                         locations=..., models=...)
    draft["summary"] = write_summary(draft, llm, symptom=...)
    validate("report", draft)
"""
from __future__ import annotations

from repairos.report.build import ReportError, build_report, honesty_violations, report_id_for
from repairos.report.export import to_html, to_markdown
from repairos.report.summary import SummaryError, words_for, write_summary
from repairos.report.verdict import Facts, Verdict, decide, facts_of

__all__ = ["Facts", "ReportError", "SummaryError", "Verdict", "build_report", "decide", "facts_of",
           "honesty_violations", "report_id_for", "to_html", "to_markdown", "words_for", "write_summary"]
