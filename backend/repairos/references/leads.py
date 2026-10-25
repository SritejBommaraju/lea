"""The person's own guess as a lead, never a fact (2026-10-24).

What the person says is wrong ("I think the port broke when I dropped it") becomes a hypothesis (the VLM slot's
story_hypothesis), research leads from public search results (Bright Data SERP, the same zone and spend guard as the
reference hints; 24 h cache of the finished leads, checked before any spend), live `lead` events that steer the scan
(pipeline/story.py) and, in the report, `user_lead`: what the photos say about that guess.

A lead only changes WHERE the pipeline looks first (the order regions are cropped in, vision/inspect.prioritise).
It is never given to the vision model and never creates, removes or upgrades a finding: the photos and the honesty
gate decide. Matching a lead's area to a region uses the region's generic words (part class, part words): the
area's head noun must be one of them (a shared modifier such as the object's own name is not enough). This orders crops and picks the report rows to cite; it never decides a state.
Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

CACHE_TTL_S = 24 * 3600
MAX_LEADS = 3
MAX_SAID = 300
_STOP = {"the", "a", "an", "of", "on", "in", "at", "near", "and", "or", "to", "its", "it", "my", "this", "that",
         "part", "area", "side", "thing", "one", "some", "piece", "inside", "outside", "around", "by", "with"}


# ------------------------------------------------------------------------------------------------- words
def tokens(text: Any) -> set[str]:
    """Lower-case content words of a generic phrase, plural s dropped ("plug prongs" -> {plug, prong})."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", str(text or "").lower().replace("_", " ")):
        if len(w) < 3 or w in _STOP:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out


_PREP = {"near", "on", "in", "at", "by", "of", "with", "under", "behind", "next", "beside", "inside", "above",
         "below", "around", "from", "to"}


def head(area: Any) -> str | None:
    """The head noun of an area phrase: the last content word before any preposition ("USB port" -> port, "the cord
    near the back" -> cord, "power switch area" -> switch)."""
    words = re.findall(r"[a-z0-9]+", str(area or "").lower().replace("_", " "))
    for i, w in enumerate(words):
        if w in _PREP and i:
            words = words[:i]
            break
    content = [w for w in words if len(w) >= 3 and w not in _STOP]
    if not content:
        return None
    w = content[-1]
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def matches(area: Any, *texts: Any) -> bool:
    """True when a text names the area's head noun ("USB port" matches "USB port opening", never "USB charger
    plug": a shared modifier is not the same part)."""
    h = head(area)
    return bool(h) and any(h in tokens(t) for t in texts)


def focus_areas(story: Mapping[str, Any] | None) -> list[str]:
    """The areas to look at first: the person's suspect, then the research leads' areas."""
    story = story or {}
    out = []
    suspect = (story.get("hypothesis") or {}).get("suspect")
    if suspect:
        out.append(str(suspect))
    out += [str(x["area"]) for x in story.get("leads") or [] if x.get("area")]
    seen: set[str] = set()
    return [a for a in out if not (a.lower() in seen or seen.add(a.lower()))]


# ------------------------------------------------------------------------------------------------- research
def object_words(identity: Mapping[str, Any] | None, hypothesis: Mapping[str, Any]) -> str:
    """The object for the search: brand and model from the photos when known, else the category, else the person's
    own words for it."""
    ident = identity or {}
    name = " ".join(str(ident[k]) for k in ("brand", "model") if ident.get(k))
    cat = str(ident.get("category") or "")
    if name and ident.get("confidence") != "low":
        return name if ident.get("model") else f"{name} {cat}".strip()
    if cat and cat != "unknown":
        return cat
    return str(hypothesis.get("object_words") or "").strip()


def queries(hypothesis: Mapping[str, Any], identity: Mapping[str, Any] | None) -> list[str]:
    """At most two searches: "<object> <symptom> <event> common cause" and "<object> <suspect> failure"."""
    obj = object_words(identity, hypothesis)
    symptom, suspect = str(hypothesis.get("symptom") or ""), str(hypothesis.get("suspect") or "")
    event = hypothesis.get("event") if hypothesis.get("event") not in (None, "", "unknown") else ""
    out = []
    if obj and (symptom or event):
        out.append(" ".join(x for x in (obj, symptom, event, "common cause") if x))
    if obj and suspect:
        out.append(f"{obj} {suspect} failure")
    return out


def cache_dir() -> Path:
    from repairos.config import ROOT

    return Path(os.environ.get("REPAIROS_DATA_DIR") or ROOT / "data") / "cache" / "leads"


def cache_key(qs: Sequence[str]) -> str:
    return hashlib.sha256("|".join(q.strip().lower() for q in qs).encode()).hexdigest()[:32]


def cached(qs: Sequence[str]) -> dict | None:
    """{leads, sources} made for these searches within a day (leads and source titles + links only)."""
    try:
        rec = json.loads((cache_dir() / f"{cache_key(qs)}.json").read_text())
    except (OSError, ValueError):
        return None
    return rec if time.time() - float(rec.get("at") or 0) < CACHE_TTL_S else None


def remember(qs: Sequence[str], leads: list[dict], sources: list[dict]) -> None:
    d = cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{cache_key(qs)}.json"
    tmp = f.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"at": time.time(), "leads": leads, "sources": sources}))
    os.replace(tmp, f)


def research(hypothesis: Mapping[str, Any], identity: Mapping[str, Any] | None, vlm: Any,
             creds: tuple[str, str] | None) -> tuple[list[dict], dict]:
    """(web leads [{area, why, from: web, source_title, source_link}], a record: status, seconds, cached). The cache
    comes first (no spend); each search passes the spend guard (references.brightdata.search). Never raises."""
    from repairos.references import brightdata as bd

    qs = queries(hypothesis, identity)
    if creds is None or not qs:
        return [], {"status": "off" if creds is None else "nothing_to_search"}
    t0 = time.monotonic()
    hit = cached(qs)
    if hit is not None:
        return list(hit["leads"]), {"status": "done", "cached": True, "seconds": round(time.monotonic() - t0, 3)}
    try:
        results = _search_all(qs, creds)   # the spend guard first for every search, then both at once
        raw = vlm.story_leads(hypothesis, identity, results) if results else []
    except Exception as e:  # noqa: BLE001 - optional; the record says why (never a key)
        status = "limited" if bd.is_spend_limit(e) else "failed"
        return [], {"status": status, "error": type(e).__name__, "seconds": round(time.monotonic() - t0, 3)}
    leads = [{"area": x["area"], "why": x["why"], "from": "web", "source_title": results[x["source"]]["title"][:200],
              "source_link": results[x["source"]]["link"]} for x in raw[:MAX_LEADS]]
    remember(qs, leads, [{"title": r["title"], "link": r["link"]} for r in results[:5]])
    return leads, {"status": "done", "cached": False, "seconds": round(time.monotonic() - t0, 3)}


def _search_all(qs: Sequence[str], creds: tuple[str, str]) -> list[dict]:
    """Every query through the spend guard first (at the cap none is sent), then all at once; one failed search
    leaves the others; all failed raises the first error."""
    from concurrent.futures import ThreadPoolExecutor

    from repairos.references import brightdata as bd

    for q in qs:
        bd.charge(q)

    def one(q: str) -> list[dict] | BaseException:
        try:
            return bd.search(q, *creds, charged=True)
        except (OSError, ValueError) as e:   # unreachable, or an empty body (seen 2026-10-24)
            return e
    with ThreadPoolExecutor(max_workers=max(1, len(qs)), thread_name_prefix="lead-search") as pool:
        answers = list(pool.map(one, qs))
    if answers and all(isinstance(a, BaseException) for a in answers):
        raise answers[0]
    results: list[dict] = []
    for rs in answers:
        if not isinstance(rs, BaseException):
            results += [r for r in rs if r["link"] not in {x["link"] for x in results}]
    return results


# ------------------------------------------------------------------------------------------------- product brief
def product_name(identity: Mapping[str, Any] | None, hypothesis: Mapping[str, Any] | None) -> str:
    """The product to research before the scan: brand and model from the photo's label, else what the person
    called it, else the photo's category."""
    ident, hyp = identity or {}, hypothesis or {}
    named = " ".join(str(ident[k]) for k in ("brand", "model") if ident.get(k))
    said = str(hyp.get("object_words") or "").strip()
    cat = str(ident.get("category") or "")
    if named:
        return f"{named} {cat}".strip() if not ident.get("model") and cat and cat != "unknown" else named
    if said:
        return said
    return cat if cat and cat != "unknown" else ""


def brief_queries(name: str) -> list[str]:
    return [f"{name} common problems", f"{name} parts teardown"] if name else []


def _backed(word: str, *texts: str) -> str:
    """A brand or model word only when the label text or the person's words contain it (never invented)."""
    w = " ".join(str(word or "").split())
    return w if w and any(w.lower() in str(t or "").lower() for t in texts) else ""


def product_brief(identity: Mapping[str, Any] | None, hypothesis: Mapping[str, Any] | None, said: str, vlm: Any,
                  creds: tuple[str, str] | None) -> tuple[dict | None, dict]:
    """(product_brief or None, a record). The brief: {name, category, brand, model, known_issues[{area, why,
    source_title, source_link}] (<= MAX_LEADS), sources[{title, link}]}; cached a day per product (checked before
    any spend); every search passes the spend guard. Brand and model are kept only as read on the label or said."""
    from repairos.references import brightdata as bd

    name = product_name(identity, hypothesis)
    qs = brief_queries(name)
    if creds is None or not qs:
        return None, {"status": "off" if creds is None else "nothing_to_search"}
    t0 = time.monotonic()
    hit = cached(["brief", *qs])
    if hit is not None:
        return hit["brief"], {"status": "done", "cached": True, "seconds": round(time.monotonic() - t0, 3)}
    try:
        results = _search_all(qs, creds)
        raw = vlm.product_brief(identity, said, results) if results else None
    except Exception as e:  # noqa: BLE001 - optional; the record says why (never a key)
        status = "limited" if bd.is_spend_limit(e) else "failed"
        return None, {"status": status, "error": type(e).__name__, "seconds": round(time.monotonic() - t0, 3)}
    if not raw:
        return None, {"status": "none", "seconds": round(time.monotonic() - t0, 3)}
    ident = identity or {}
    label = str(ident.get("label_text") or "")
    brief = {"name": raw.get("name") or name,
             "category": raw.get("category") or str(ident.get("category") or "") or "unknown",
             "brand": ident.get("brand") or _backed(raw.get("brand"), label, said),
             "model": ident.get("model") or _backed(raw.get("model"), label, said),
             "known_issues": [{"area": x["area"], "why": x["why"], "source_title": results[x["source"]]["title"][:200],
                               "source_link": results[x["source"]]["link"]} for x in raw.get("known_issues", [])][:MAX_LEADS],
             "sources": [{"title": r["title"], "link": r["link"]} for r in results[:5]]}
    brief = {k: v for k, v in brief.items() if v not in ("", None)}
    path = cache_dir() / f"{cache_key(['brief', *qs])}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": time.time(), "brief": brief}))
    return brief, {"status": "done", "cached": False, "seconds": round(time.monotonic() - t0, 3)}


def own_lead(hypothesis: Mapping[str, Any]) -> dict | None:
    """The person's suspect as the first lead (no search needed): a cue to photograph it closely."""
    suspect = str(hypothesis.get("suspect") or "").strip()
    if not suspect:
        return None
    symptom = str(hypothesis.get("symptom") or "").strip()
    return {"area": suspect, "why": f"you mentioned it{f' ({symptom})' if symptom else ''}", "from": "you",
            "source_title": ""}


# ------------------------------------------------------------------------------------------------- the report
def _words(row: Mapping[str, Any]) -> list[str]:
    return [str(row.get("part_class") or ""), str(row.get("part_words") or "")]


def user_lead(story: Mapping[str, Any] | None, report: Mapping[str, Any]) -> dict | None:
    """The report's `user_lead`: what the person suspected and what the photos say about it.
    confirmed = a judged region of that area has an issue (the finding stands on its own photo); not_seen = regions of
    that area were judged with no visible issue; could_not_check = no photo of that area was judged (or no area was
    named). The lead never changes a finding; this only cites them."""
    story = story or {}
    hyp = story.get("hypothesis") or {}
    said = " ".join(str(u.get("text") or "") for u in story.get("said") or []).strip()
    if not said or hyp.get("status") != "done":
        return None
    suspect = str(hyp.get("suspect") or "").strip()
    judged = list(report.get("findings") or []) + list(report.get("setup") or []) + list(report.get("cosmetic") or [])
    unjudged = list(report.get("insufficient_evidence") or []) + list(report.get("not_inspected") or [])
    top_ids = list(report.get("most_likely") or [])
    top = next((f for f in report.get("findings") or [] if top_ids and f["finding_id"] == top_ids[0]), None)
    issue = [f for f in judged if suspect and f.get("issue") not in (None, "none") and matches(suspect, *_words(f))]
    clear = [f for f in judged if suspect and f.get("issue") == "none" and matches(suspect, *_words(f))]
    unclear = [f for f in unjudged if suspect and matches(suspect, *_words(f))]
    what = f"the {suspect}" if suspect else "something specific"
    lead_in = f"You thought {what} was the problem." if suspect else f"You said: {said[:120]}."
    if issue:
        checked, ids = "confirmed", [f["finding_id"] for f in issue]
        f = issue[0]
        note = f"{lead_in} A photo shows it {str(f['issue']).replace('_', ' ')}."
    elif clear:
        checked, ids = "not_seen", [f["finding_id"] for f in clear]
        note = f"{lead_in} The close photo shows no visible issue there"
    else:
        checked, ids = "could_not_check", [f["finding_id"] for f in unclear]
        note = (f"{lead_in} No photo showed it clearly enough to check" if suspect else
                f"{lead_in} No specific part was named to check")
    if checked != "confirmed":
        if top is not None and top["finding_id"] not in ids:
            note += (f"; the more likely problem is the {str(top.get('part_class') or 'part').replace('_', ' ')} "
                     f"({str(top['issue']).replace('_', ' ')}).")
        else:
            note += "."
    out = {"said": said[:MAX_SAID], "checked": checked, "evidence_finding_ids": ids, "note": note}
    if suspect:
        out["suspect"] = suspect
    sources = [{"title": x.get("source_title") or "", "link": x["source_link"]}
               for x in story.get("leads") or [] if x.get("from") == "web" and x.get("source_link")][:5]
    if sources:
        out["sources"] = sources
    return out
