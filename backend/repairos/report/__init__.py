"""Reports. The report is built from the design engine's answer (from_grain.py: Grain judges the scan against the
product's own design; PLAN.md section 2), validated against spec/contracts/report.schema.json, and printed by export.py
as Markdown or self-contained HTML."""
from __future__ import annotations

from repairos.report.export import to_html, to_markdown


def report_id_for(scan_id: str) -> str:
    return f"rep_{scan_id}"


__all__ = ["report_id_for", "to_html", "to_markdown"]
