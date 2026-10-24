"""Printable exports of a report (task B11): Markdown, and a self-contained HTML page for sharing or printing.

    to_markdown(report)
    to_html(report, crop_image=fn)   # fn(crop_ref) -> JPEG bytes or None; crops are embedded as data URIs

Shows words only: the verdict, confidence words, reasons and evidence. No probability number appears (I6, A5).
"""
from __future__ import annotations

import base64
import html
from collections.abc import Callable, Mapping
from typing import Any

from repairos.fusion import words

TITLE = {"looks_good": "Looks good", "potential_issues": "Potential issues",
         "insufficient_evidence": "Not enough evidence"}
PROOF = {"not_measured": "suspicion from photos, not proof", "confirmed_by_meter": "confirmed by a meter reading",
         "ruled_out_by_meter": "ruled out by a meter reading"}
NOT_PROOF = "vision suspect, not proof"


def _issue(e: Mapping[str, Any]) -> str:
    return "no visible issue" if e["issue"] == "none" else words.FINDING_STATE[e["issue"]]


def _where(loc: Mapping[str, Any] | None) -> str:
    if not loc:
        return "location not computed"
    if loc["status"] == "computed":
        x, y, z = loc["point_m"]
        return f"on the scan at ({x:.3f}, {y:.3f}, {z:.3f}) m, {loc['frame']}"
    return f"location not computed: {loc['reason']}"


def _photo(e: Mapping[str, Any]) -> str:
    p = e.get("crop") or e.get("photo")
    if not p:
        return "no photo"
    box = p.get("box_px")
    return f"still {p['still_id']}" + (f", region {box}" if box else "")


def _evidence(e: Mapping[str, Any]) -> list[str]:
    return [s for s in e.get("evidence", []) if s.strip() and s.strip() != NOT_PROOF]


def to_markdown(report: Mapping[str, Any]) -> str:
    out = [f"# Inspection report: {TITLE[report['verdict']]}", "",
           f"Scan `{report['scan_id']}`, report `{report['report_id']}`, {report['created_at']}", "",
           report["summary"]["text"], "", f"**Why this verdict:** {report['verdict_reason']['text']}", ""]
    issues = [e for e in report["findings"] if e["issue"] != "none"]
    fine = [e for e in report["findings"] if e["issue"] == "none"]
    if report.get("next_step"):
        out += [f"**Next step:** {report['next_step']['instruction']}", ""]
    out += ["## Potential issues", ""]
    out += [f"- **{e['part_class']}** ({e['subject']['ref']}): {_issue(e)}; confidence {e['confidence_word']}; "
            f"{PROOF[e['proof']['status']]}. {_photo(e)}; {_where(e.get('location'))}. "
            f"Evidence: {' '.join(_evidence(e))}" for e in issues] or ["- none"]
    out += ["", "## Looked at, no visible issue", ""]
    out += [f"- {e['part_class']} ({e['subject']['ref']}): confidence {e['confidence_word']}. {_photo(e)}; "
            f"{_where(e.get('location'))}." for e in fine] or ["- none"]
    out += ["", "## Not inspected", ""]
    out += [f"- {e['part_class']} ({e['subject']['ref']}): {e['reason']}. {_photo(e)}."
            for e in report["not_inspected"]] or ["- none"]
    out += ["", "## Could not be judged", ""]
    out += [f"- {e['part_class']} ({e['subject']['ref']}): {e['reason']}. {_photo(e)}."
            for e in report["insufficient_evidence"]] or ["- none"]
    cap, m = report["capture"], report["models"]
    out += ["", "## Capture", "",
            f"- Depth: {'LiDAR' if cap['depth_source'] == 'lidar' else 'none (sizes cannot be claimed)'}",
            f"- Close-up stills: {cap['stills']} received, {cap['stills_inspected']} with a judged region",
            f"- Scan keyframes: {cap['keyframes']}; coverage: {cap['coverage_words']}",
            "", "## Models", ""]
    out += [f"- {k.replace('_', ' ')}: {v}" for k, v in m.items()]
    out += [f"- summary written by {report['summary']['written_by']} from words only", "- Grain: not called", "",
            "Photo findings are suspicions; only a meter reading proves a fault.", ""]
    return "\n".join(out)


_CSS = """
:root { --fg: #1b1d21; --muted: #5d636e; --line: #d9dce1; --bad: #a3361f; --ok: #2f6b3a; --warn: #7a5a00;
        --bg: #ffffff; }
@media (prefers-color-scheme: dark) { :root { --fg: #e8eaed; --muted: #a2a8b3; --line: #3a3f47; --bad: #f08a70;
        --ok: #8fd19b; --warn: #e6c25a; --bg: #15171a; } }
body { font: 15px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; color: var(--fg); background: var(--bg);
       max-width: 860px; margin: 0 auto; padding: 24px 16px; }
h1 { font-size: 24px; margin: 0 0 4px; } h2 { font-size: 17px; margin: 28px 0 8px; border-bottom: 1px solid var(--line);
     padding-bottom: 4px; }
.meta, .muted { color: var(--muted); font-size: 13px; }
.verdict-potential_issues { color: var(--bad); } .verdict-looks_good { color: var(--ok); }
.verdict-insufficient_evidence { color: var(--warn); }
.item { display: flex; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--line); break-inside: avoid; }
.item img { width: 140px; height: 140px; object-fit: cover; border-radius: 6px; border: 1px solid var(--line); }
.item p { margin: 2px 0; } code { font-size: 12px; }
@media print { body { max-width: none; } .item img { width: 110px; height: 110px; } }
"""


def to_html(report: Mapping[str, Any], crop_image: Callable[[str], bytes | None] | None = None) -> str:
    esc = html.escape

    def img(e: Mapping[str, Any]) -> str:
        p = e.get("crop") or e.get("photo") or {}
        ref = p.get("crop_ref")
        data = crop_image(ref) if (crop_image and ref) else None
        if not data:
            return ""
        return f'<img alt="crop of {esc(e["part_class"])}" src="data:image/jpeg;base64,{base64.b64encode(data).decode()}">'

    def item(e: Mapping[str, Any], lines: list[str]) -> str:
        body = "".join(f"<p>{line}</p>" for line in lines)
        return f'<div class="item">{img(e)}<div>{body}</div></div>'

    def section(title: str, entries: list[str]) -> str:
        return f"<h2>{esc(title)}</h2>" + ("".join(entries) or '<p class="muted">None.</p>')

    issues = [e for e in report["findings"] if e["issue"] != "none"]
    fine = [e for e in report["findings"] if e["issue"] == "none"]
    v = report["verdict"]
    parts = [
        f'<h1 class="verdict-{v}">{esc(TITLE[v])}</h1>',
        f'<p class="meta">Scan <code>{esc(report["scan_id"])}</code> &middot; {esc(report["created_at"])}</p>',
        f"<p>{esc(report['summary']['text'])}</p>",
        f"<p><strong>Why this verdict:</strong> {esc(report['verdict_reason']['text'])}</p>",
    ]
    if report.get("next_step"):
        parts.append(f"<p><strong>Next step:</strong> {esc(report['next_step']['instruction'])}</p>")
    parts.append(section("Potential issues", [item(e, [
        f"<strong>{esc(e['part_class'])}</strong>: {esc(_issue(e))}",
        f"Confidence {esc(e['confidence_word'])}; {esc(PROOF[e['proof']['status']])}",
        esc(" ".join(_evidence(e))),
        f'<span class="muted">{esc(_photo(e))}; {esc(_where(e.get("location")))}</span>']) for e in issues]))
    parts.append(section("Looked at, no visible issue", [item(e, [
        f"<strong>{esc(e['part_class'])}</strong>: no visible issue; confidence {esc(e['confidence_word'])}",
        f'<span class="muted">{esc(_photo(e))}; {esc(_where(e.get("location")))}</span>']) for e in fine]))
    parts.append(section("Not inspected", [item(e, [
        f"<strong>{esc(e['part_class'])}</strong>: not inspected", esc(e["reason"]),
        f'<span class="muted">{esc(_photo(e))}</span>']) for e in report["not_inspected"]]))
    parts.append(section("Could not be judged", [item(e, [
        f"<strong>{esc(e['part_class'])}</strong>: not enough evidence", esc(e["reason"]),
        f'<span class="muted">{esc(_photo(e))}</span>']) for e in report["insufficient_evidence"]]))
    cap = report["capture"]
    parts.append("<h2>Capture and models</h2><p class=\"muted\">"
                 f"Depth: {'LiDAR' if cap['depth_source'] == 'lidar' else 'none (sizes cannot be claimed)'}. "
                 f"Close-up stills: {cap['stills']} received, {cap['stills_inspected']} with a judged region. "
                 f"Scan coverage: {esc(cap['coverage_words'])}.<br>"
                 + "<br>".join(f"{esc(k.replace('_', ' '))}: {esc(v)}" for k, v in report["models"].items())
                 + f"<br>Summary written by {esc(report['summary']['written_by'])} from words only. Grain: not called."
                 "</p><p class=\"muted\">Photo findings are suspicions; only a meter reading proves a fault.</p>")
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>Inspection report</title><style>{_CSS}</style></head><body>{''.join(parts)}</body></html>\n")


def crop_thumbnail(data: bytes, max_side: int = 360) -> bytes:
    """A small JPEG of a crop for the export (Pillow, from the vision extra)."""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
