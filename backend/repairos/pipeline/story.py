"""The person's story ("I think the port broke when I dropped it") as a lead for the scan, never a fact (2026-10-24).

    POST /v1/sessions/{id}/story     -> record_story (gateway: stores the words, queues a `story` job); works on a
                                        session with no scan yet: the conversation before scanning
    POST /v1/sessions/{id}/identify  -> record_photo (one photo of the product or its label, before scanning)
    story job (worker)            -> run_story: hypothesis (one quick Claude call through the VLM slot), the person's
                                     own suspect as the first lead at once, then research leads (references.leads)
    story job, before the scan      -> maybe_brief: identify (the photo's label, else the person's words), research
                                        the exact product (Bright Data), live event `product_brief`; its known
                                        failure points become leads too
    each new still                -> apply_story: the session's leads copied onto the scan's run row, one live
                                     event `lead` {scan_id, area, why, source_title, from} per new lead

The story row is an internal kind (`story`, one per session, id = session_id). Leads change only the order regions
are inspected in (vision/inspect.prioritise); they are never sent to the vision model and never create or change a
finding. The report's `user_lead` (references.leads.user_lead) says what the photos showed about the guess.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from repairos.slots.state import RevConflict

log = logging.getLogger(__name__)
STORY_KIND = "story"
MAX_SAID = 20          # utterances kept per session
MAX_TEXT = 1000        # characters per utterance
ENDED = ("done", "failed", "closed")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _patch(state: Any, session_id: str, fn) -> Any:
    """Read-modify-write the story row (optimistic: retried on a concurrent write). `announce` (a named live event,
    product_brief) lives only on the write that sets it, as on the run row."""
    for _ in range(5):
        row = await state.get(STORY_KIND, session_id)
        data = dict(row.data) if row else {"session_id": session_id, "said": []}
        data.pop("announce", None)
        new = fn(data)
        try:
            rev = await state.put(STORY_KIND, new, expect_rev=row.rev if row else 0)
        except RevConflict:
            continue
        return rev, new
    raise RevConflict(STORY_KIND, session_id, None, None)


async def record_story(state: Any, session_id: str, text: str, source: str = "typed",
                       scan_id: str | None = None) -> tuple[int, dict]:
    """Add what the person said (typed or spoken) to the session's story. Returns (story rev, row)."""
    text = " ".join(text.split())[:MAX_TEXT]

    def add(d: dict) -> dict:
        said = list(d.get("said") or [])
        if said and said[-1].get("text") == text:   # the same words again (a retried post)
            return d
        said.append({"text": text, "source": source, "at": _now(), **({"scan_id": scan_id} if scan_id else {})})
        return {**d, "said": said[-MAX_SAID:], "said_seq": said_seq(d) + 1, "updated_at": _now()}
    return await _patch(state, session_id, add)


def said_seq(row: dict) -> int:
    """Utterances ever added (the list keeps only the last MAX_SAID, so its length stops changing). Rows from before
    the counter count their list."""
    return int(row.get("said_seq", len(row.get("said") or [])))


def photo_seq(row: dict) -> int:
    """Identification photos ever added (only the last three are kept)."""
    return int(row.get("photo_seq", len(row.get("photos") or [])))


def already_read(row: dict) -> bool:
    """The hypothesis was made from everything said so far (by count of utterances ever added, not the kept list)."""
    read = row.get("hypothesis") or {}
    return read.get("said_seq", read.get("said_count")) == said_seq(row)


def job_key(session_id: str, row: dict) -> str:
    """A story job per change in what was said or shown: the same words or photo again maps to the same job."""
    return f"{session_id}:{said_seq(row)}:{photo_seq(row)}"


async def record_photo(state: Any, session_id: str, image_ref: str) -> tuple[int, dict]:
    """A photo of the product (or its label) shown before scanning, for identifying it."""
    def add(d: dict) -> dict:
        photos = list(d.get("photos") or [])
        if image_ref in [p["image"] for p in photos]:   # the same photo again (a retried post)
            return d
        photos.append({"image": image_ref, "at": _now()})
        return {**d, "photos": photos[-3:], "photo_seq": photo_seq(d) + 1, "updated_at": _now()}
    return await _patch(state, session_id, add)


async def _identity(ctx: Any, session_id: str) -> dict | None:
    runs = [r.data for r in await ctx.state.list("pipeline-run", session_id=session_id)]
    for r in sorted(runs, key=lambda r: str((r.get("identity") or {}).get("at") or ""), reverse=True):
        if (r.get("identity") or {}).get("status") == "done":
            return r["identity"]
    return None


async def run_story(ctx: Any, session_id: str) -> dict:
    """The story job: hypothesis, the person's own lead at once, the product brief before a scan, then research
    leads; each announced on the session's scans that are still going on. One story job per session at a time (a
    photo and words posted together would otherwise research the product twice)."""
    async with ctx.scan_lock(f"story:{session_id}"):
        return await _run_story(ctx, session_id)


async def _run_story(ctx: Any, session_id: str) -> dict:
    from repairos.references import leads as L
    from repairos.references import parts

    row = await ctx.state.get(STORY_KIND, session_id)
    if row is None or not (row.data.get("said") or row.data.get("photos")):
        return {"stage": "story", "skipped": "nothing said"}
    said = row.data.get("said") or []
    seq = said_seq(row.data)
    if already_read(row.data) or not said:
        brief = await maybe_brief(ctx, session_id)
        return {"stage": "story", "skipped": "already read", "brief": brief} if brief else \
            {"stage": "story", "skipped": "already read"}
    t0 = time.monotonic()
    identity = await _identity(ctx, session_id)
    vlm = ctx.slot("VLM")
    text = "\n".join(u["text"] for u in said)
    try:
        hyp = await asyncio.to_thread(vlm.story_hypothesis, text, identity)
    except Exception as e:  # noqa: BLE001 - a guess is optional; the row says why (spend cap included)
        status = "limited" if type(e).__name__ == "SpendLimit" else "failed"
        await _patch(ctx.state, session_id, lambda d: {**d, "hypothesis": {
            "status": status, "error": type(e).__name__, "said_count": len(said), "said_seq": seq}})
        return {"stage": "story", "hypothesis": status}
    hyp = {**hyp, "status": "done", "said_count": len(said), "said_seq": seq, "seconds": round(time.monotonic() - t0, 3),
           "at": _now()}
    own = L.own_lead(hyp)
    first = [own] if own else []
    await _patch(ctx.state, session_id, lambda d: {**d, "hypothesis": hyp, "leads": first})
    announced = await announce(ctx, session_id)
    brief = await maybe_brief(ctx, session_id)   # before the scan: what this exact product is (seeds leads too)
    if identity is None:
        identity = await _brief_identity(ctx, session_id)
    web, rec = await asyncio.to_thread(L.research, hyp, identity, vlm, parts.credentials())
    def merge(d: dict) -> dict:
        have = list(d.get("leads") or first)
        areas = {x["area"].lower() for x in have}
        return {**d, "leads": have + [w for w in web if w["area"].lower() not in areas][:L.MAX_LEADS],
                "research": rec}
    _rev, saved = await _patch(ctx.state, session_id, merge)
    all_leads = saved["leads"]
    announced += await announce(ctx, session_id)
    log.info("story for session %s: suspect %r, %d lead(s), research %s", session_id, hyp.get("suspect"),
             len(all_leads), rec.get("status"))
    out = {"stage": "story", "leads": len(all_leads), "announced": announced, "research": rec.get("status")}
    if brief:
        out["brief"] = brief
    return out


async def _brief_identity(ctx: Any, session_id: str) -> dict | None:
    row = await ctx.state.get(STORY_KIND, session_id)
    d = row.data if row else {}
    b = d.get("brief") or {}
    if b.get("status") == "done":
        return {k: b[k] for k in ("brand", "model", "category") if b.get(k)} | {"confidence": "medium"}
    return d.get("identity")


async def maybe_brief(ctx: Any, session_id: str) -> str | None:
    """Before the scan: identify the product (the photo's label via the VLM slot's identify, else the person's own
    words), research it (references.leads.product_brief) and publish the live event `product_brief`. Its known
    failure points join the leads (from: product). Once per product name. Returns the brief status, or None when
    nothing was done."""
    from repairos.pipeline.run import load_preview
    from repairos.references import leads as L
    from repairos.references import parts

    row = await ctx.state.get(STORY_KIND, session_id)
    d = row.data if row else {}
    photos = d.get("photos") or []
    identity = d.get("identity")
    if photos and (identity or {}).get("from_photo") != photos[-1]["image"]:
        try:
            path = ctx.local_file(f"pre_{session_id}", photos[-1]["image"])
            image = await asyncio.to_thread(load_preview, path)
            ans = await asyncio.to_thread(ctx.slot("VLM").identify, [image])
            identity = {**ans, "status": "done", "from_photo": photos[-1]["image"], "at": _now()}
        except Exception as e:  # noqa: BLE001 - the photo is optional; the person's words still count
            identity = {"status": "failed", "error": type(e).__name__, "from_photo": photos[-1]["image"]}
        await _patch(ctx.state, session_id, lambda x: {**x, "identity": identity})
    ident = identity if (identity or {}).get("status") == "done" else await _identity(ctx, session_id)
    hyp = d.get("hypothesis") or {}
    name = L.product_name(ident, hyp)
    if not name or ((d.get("brief") or {}).get("key") == name.lower()):
        return None
    said = " ".join(u.get("text", "") for u in d.get("said") or [])
    t0 = time.monotonic()
    brief, rec = await asyncio.to_thread(L.product_brief, ident, hyp, said, ctx.slot("VLM"), parts.credentials())
    if brief is None:
        await _patch(ctx.state, session_id, lambda x: {**x, "brief": {**rec, "key": name.lower()}})
        return rec["status"]
    seeded = [{"area": k["area"], "why": k["why"], "from": "product", "source_title": k["source_title"],
               "source_link": k["source_link"]} for k in brief.get("known_issues") or []]

    def put(x: dict) -> dict:
        have = list(x.get("leads") or [])
        areas = {i["area"].lower() for i in have}
        return {**x, "brief": {**brief, **rec, "key": name.lower(), "seconds": round(time.monotonic() - t0, 3)},
                "leads": have + [i for i in seeded if i["area"].lower() not in areas], "announce": "product_brief"}
    await _patch(ctx.state, session_id, put)
    await announce(ctx, session_id)
    log.info("product brief for session %s: %r, %d known issue(s)", session_id, brief.get("name"),
             len(brief.get("known_issues") or []))
    return "done"


def brief_payload(session_id: str, row: dict) -> dict | None:
    """The live event `product_brief` for the story write that announced it."""
    b = row.get("brief") or {}
    if b.get("status") != "done":
        return None
    return {"session_id": session_id, **{k: b[k] for k in ("name", "category", "brand", "model") if b.get(k)},
            "known_issues": [{"area": k["area"], "why": k["why"], "source_title": k.get("source_title", "")}
                             for k in b.get("known_issues") or []],
            "sources": list(b.get("sources") or [])}


async def announce(ctx: Any, session_id: str, scan: dict | None = None) -> int:
    """Copy the session's leads onto its scans that are still going on (or onto `scan`) and send one live `lead`
    event per lead not yet announced there. Returns how many events went out."""
    if scan is not None:
        scans = [scan]
    else:
        runs = [r.data for r in await ctx.state.list("pipeline-run", session_id=session_id)]
        scans = [{"scan_id": r["scan_id"], "session_id": session_id} for r in runs
                 if r.get("scan_id") and r.get("status") not in ENDED]
    if not scans:
        return 0
    row = await ctx.state.get(STORY_KIND, session_id)
    items = list((row.data if row else {}).get("leads") or [])
    if not items:
        return 0
    from repairos.pipeline.run import _run_row, _update

    sent = 0
    for sc in scans:
        async with ctx.scan_lock(sc["scan_id"]):
            run = await _run_row(ctx, sc["scan_id"]) or {}
            have = (run.get("leads") or {})
            done = list(have.get("announced") or [])
            for i, it in enumerate(items):
                key = it["area"].lower()
                if key in done:
                    continue
                done.append(key)
                await _update(ctx, sc, {"leads": {"items": items, "announced": done}, "lead_announce": i,
                                        "announce": "lead"})
                sent += 1
            if not sent and have.get("items") != items:
                await _update(ctx, sc, {"leads": {"items": items, "announced": done}})
    return sent


async def apply_story(ctx: Any, scan: dict) -> int:
    """On a new still: the session's leads (if any) onto this scan, announced once each."""
    if not await ctx.state.get(STORY_KIND, scan["session_id"]):
        return 0
    return await announce(ctx, scan["session_id"], {"scan_id": scan["scan_id"], "session_id": scan["session_id"]})


def lead_payload(scan_id: str, run: dict) -> dict | None:
    """The live event `lead` for the run row write that announced it."""
    items = (run.get("leads") or {}).get("items") or []
    i = run.get("lead_announce")
    if not isinstance(i, int) or not 0 <= i < len(items):
        return None
    it = items[i]
    return {"scan_id": scan_id, "area": it["area"], "why": it.get("why", ""), "source_title": it.get("source_title", ""),
            "from": it.get("from", "web")}
