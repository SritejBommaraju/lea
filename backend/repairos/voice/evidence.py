"""A finished scan's evidence for Lea's report turns, read from the gateway's store and blobs (repairos.voice.views
does the maths): the scan's surface, its posed stills, the images for a turn, the real photo of any spot, and the
part index.

The part index: once per scan, in the background after the report exists, one vision call over the scan's best
photos lists what is visible (buttons, ports, pins and headers with their printed labels, connectors, screws, wire
ends with their colour) with a pixel in a photo; each pixel becomes a point on the model. "Where's A2" or "where's
the X button" is then a lookup (<show part="ix_3"/>), and the phone circles it. A model's pixel is not precise
enough to pick one of two neighbouring pins, so an index point is a region (INDEX_RADIUS_M) and Lea says the
position in words. Kept as one small JSON file per scan beside the conversations; one call per scan, through the
spend guard.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from repairos.voice import views as V
from repairos.voice.spend_guard import charge, key_of, record_claude

log = logging.getLogger(__name__)

# Ring sizes follow how sure the spot is (measured on the owner's controller: a model's pixel on a whole photo was
# 59-78 mm off the named button; zoomed crops bring it to the part). Never a tight ring on a guess.
COARSE_RADIUS_M = 0.03     # a model's pixel on a whole photo, not refined: a region
REFINED_RADIUS_M = 0.014   # refined on full-resolution crops, seen in one photo
TIGHT_MIN_M = 0.012        # refined and agreeing across two photos: the part, plus the photos' pose offsets
AGREE_M = 0.015            # two photos' refined points closer than this agree
INDEX_RADIUS_M = COARSE_RADIUS_M
POINT_RADIUS_M = COARSE_RADIUS_M   # a spot Lea pointed at this turn, until its refine lands
FINDING_RADIUS_M = 0.012
REFINE_SIDES = (0.5, 0.18)  # crop windows, as fractions of the photo's shorter side: pass 1, pass 2 (and checks)
LIVE_REFINE_SIDE = 0.3
INDEX_PHOTOS = 4
INDEX_PX = 1280            # the index reads printed labels: larger photos than a turn's
MAX_INDEX = 40
KINDS = ["button", "port", "pin", "header", "connector", "label", "screw", "wire end", "switch", "light", "chip",
         "battery", "other"]

INDEX_PROMPT = """These are photos of one real object from a scan{name}. List the components a person might ask \
about that you can actually see: buttons, ports and jacks, pins and pin headers, connectors, switches, lights, \
screws, chips, batteries, printed labels, and each visible wire end (with its colour and what it is plugged into). \
For each give: label (its printed text when legible, e.g. "A2", "5V", "GND", "RESET", else a short name like "X \
button" or "red wire end"; left and right mean the object's own sides as its user holds or faces it, never the \
photo's, and the object may lie turned round in a photo), kind, the photo it is clearest in (its name, like s4), x and y (fractions of that photo, \
0,0 top left, on the component itself; read them from the faint grid, whose ticks mark every tenth), and a short \
note (what it is for, or for a wire what it connects to as far \
as you can see). Only list what you can see; read printed text only when it is legible. At most 40, the most useful \
first."""

REFINE_PROMPT = """Each crop above is a full-resolution close-up from a photo of one real object{name}. For each \
crop, find the named component and give its centre: x and y as fractions of that crop (0,0 top left), read from \
the faint grid (its ticks mark every tenth). seen is false when that component is not in the crop; do not guess."""

REFINE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {
    "items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["crop", "seen", "x", "y"],
        "properties": {"crop": {"type": "integer"}, "seen": {"type": "boolean"}, "x": {"type": "number"},
                       "y": {"type": "number"}}}}}}

INDEX_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["parts"], "properties": {
    "parts": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["label", "kind", "photo", "x", "y", "note"],
        "properties": {"label": {"type": "string"}, "kind": {"type": "string", "enum": KINDS},
                       "photo": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
                       "note": {"type": "string"}}}}}}


class Evidence:
    """Per gateway process: reads a scan's data once and keeps it in the views module's caches."""

    def __init__(self, request_app: Any, root: Path):
        self.app, self.root = request_app, root
        self._building: set[str] = set()

    # ------------------------------------------------------------------ scan data
    def image_of(self, still: dict) -> bytes | None:
        ref = still.get("image_file")
        blobs = self.app.state.blobs
        try:
            return blobs.get(ref) if ref and blobs.exists(ref) else None
        except Exception:  # noqa: BLE001 - a missing still is skipped
            return None

    async def stills(self, scan_id: str) -> list[dict]:
        rows = await self.app.state.store.list("still", scan_id=scan_id)
        return [r.data for r in rows if r.data]

    async def surface(self, scan_id: str) -> V.Surface | None:
        hit = V.SURFACES.get(scan_id)
        if hit is not None:
            return hit
        st, blobs = self.app.state.store, self.app.state.blobs
        run = await st.get("pipeline-run", f"run_{scan_id}")
        rec = ((run.data if run else {}) or {}).get("recreation") or {}
        pkg_row = await st.get("scan-package", scan_id)
        pkg = pkg_row.data if pkg_row else {}

        def load() -> V.Surface | None:
            ref = rec.get("ref")
            if ref and rec.get("format") == "usdz" and blobs.exists(ref):
                s = V.surface_from_usdz(blobs.get(ref))
                if s is not None:
                    return s.focus(rec.get("focus_center"), rec.get("focus_radius"))
            ref = pkg.get("points_file")
            if ref and blobs.exists(ref):
                from repairos.locate.cloud import read_ply

                with tempfile.NamedTemporaryFile(suffix=".ply", delete=False) as f:
                    f.write(blobs.get(ref))
                try:
                    return V.surface_from_cloud(read_ply(f.name), pkg.get("crop_box"), pkg.get("voxel_mm"))
                finally:
                    os.unlink(f.name)
            return None
        surface = await asyncio.to_thread(load)
        if surface is not None:
            V.SURFACES.put(scan_id, surface)
        return surface

    # ------------------------------------------------------------------ a turn
    async def views(self, scan_id: str, question: str, rows: list[dict]) -> list[V.View]:
        """The images for a report turn: [] when the scan has no surface or no posed stills."""
        surface = await self.surface(scan_id)
        if surface is None:
            return []
        stills = await self.stills(scan_id)
        return await asyncio.to_thread(V.build_views, surface, stills, self.image_of, question, rows, scope=scan_id)

    async def spot_photo(self, scan_id: str, point: list[float], radius_m: float, prefer: str | None = None
                         ) -> str | None:
        """The ringed photo of a spot as a blob the phone fetches (GET /v1/blobs/<sha>), its sha; None when there
        is nothing to show it on."""
        surface = await self.surface(scan_id)
        if surface is None:
            return None
        stills = await self.stills(scan_id)
        key = (scan_id, "spot", tuple(round(v, 3) for v in point), round(radius_m, 3), prefer)
        sha = V.VIEWS.get(key)
        if sha is None:
            jpeg, _ = await asyncio.to_thread(V.spot_photo, surface, stills, self.image_of, point, radius_m,
                                              scope=scan_id, prefer=prefer)
            sha = self.app.state.blobs.put(jpeg, "image/jpeg").split("sha256:")[-1]
            V.VIEWS.put(key, sha)
        return sha

    async def refine_live(self, scan_id: str, item: dict, key: str, model: str, *, http_client: Any = None
                          ) -> dict | None:
        """A spot Lea pointed at this turn, refined on one full-resolution crop (one call): {point, radius,
        precision}, or None when there is nothing to refine on."""
        import anthropic

        surface = await self.surface(scan_id)
        if surface is None:
            return None
        stills = await self.stills(scan_id)
        kw = {"http_client": http_client} if http_client is not None else {}
        client = anthropic.AsyncAnthropic(api_key=key, max_retries=1, timeout=30.0, **kw)
        out, _ = await refine_points(surface, stills, self.image_of, [item], client=client, model=model,
                                     scan_id=scan_id, sides=(LIVE_REFINE_SIDE,), verify=False)
        return out[0] if out and out[0]["precision"] != "coarse" else None

    # ------------------------------------------------------------------ the part index
    def _index_path(self, scan_id: str) -> Path:
        return self.root / (re.sub(r"[^A-Za-z0-9_.-]", "_", scan_id)[:120] + ".json")

    def index(self, scan_id: str) -> list[dict]:
        """The scan's index entries (id ix_N, label, kind, note, point, radius, still_id); [] while none."""
        try:
            return json.loads(self._index_path(scan_id).read_text(encoding="utf-8")).get("entries") or []
        except (OSError, ValueError):
            return []

    async def ensure_index(self, scan_id: str, talker_key: str | None, model: str, identity: dict | None, *,
                           http_client: Any = None) -> None:
        """Starts the index once per scan (never twice at once, never again once written): its photos are read
        here, then the vision call and the file write run on their own thread, so the answer never waits for it
        and it outlives the request."""
        if not talker_key or scan_id in self._building or self._index_path(scan_id).exists():
            return
        self._building.add(scan_id)
        try:
            views, why = await self._index_views(scan_id)
        except Exception as e:  # noqa: BLE001
            views, why = {}, f"{type(e).__name__}: {e}"
        if not views:
            self._write(scan_id, {"status": "skipped", "reason": why, "entries": []})
            self._building.discard(scan_id)
            return
        name = (identity or {}).get("name") or " ".join(
            str(x) for x in ((identity or {}).get("brand"), (identity or {}).get("model")) if x)

        surface, stills = V.SURFACES.get(scan_id), await self.stills(scan_id)

        def run() -> None:
            t0 = time.perf_counter()
            try:
                out = asyncio.run(self.call_index(scan_id, views, name, talker_key, model, http_client=http_client,
                                                  surface=surface, stills=stills, image_of=self.image_of))
            except Exception as e:  # noqa: BLE001 - the index is a speed-up; answers work without it
                log.warning("voice index %s failed: %s", scan_id, e)
                out = {"status": "failed", "reason": f"{type(e).__name__}: {e}"[:300], "entries": []}
            out["seconds"] = round(time.perf_counter() - t0, 2)
            self._write(scan_id, out)
            self._building.discard(scan_id)
            log.info("voice index %s: %s, %d parts in %.1f s", scan_id, out.get("status"),
                     len(out.get("entries") or []), out["seconds"])
        threading.Thread(target=run, name=f"voice-index-{scan_id}", daemon=True).start()

    def _write(self, scan_id: str, out: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        p = self._index_path(scan_id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    async def _index_views(self, scan_id: str) -> tuple[dict[str, V.View], str]:
        """The best-coverage photos at INDEX_PX, each with its point map; ({}, why) when there are none."""
        surface = await self.surface(scan_id)
        stills = [s for s in await self.stills(scan_id) if V.usable(s)]
        if surface is None or not stills:
            return {}, "no 3D surface or no posed photos"
        views = {}
        for st in V.pick_photos(stills, "", [], surface.center, k=INDEX_PHOTOS):
            img = self.image_of(st)
            v = await asyncio.to_thread(V.photo_view, surface, st, img, V.short_key(st["still_id"]), INDEX_PX) \
                if img else None
            if v is not None:
                views[v.key] = v
        return views, "" if views else "no readable photos"

    @staticmethod
    async def call_index(scan_id: str, views: dict[str, V.View], name: str, key: str, model: str, *,
                         http_client: Any = None, surface: V.Surface | None = None, stills: list[dict] | None = None,
                         image_of: Any = None) -> dict:
        """One vision call listing what the photos show, placed on the model (index_entries), then each place
        refined on full-resolution crops (refine_points) when the surface and stills are given."""
        import anthropic

        content: list[dict] = []
        for v in views.values():
            content += [{"type": "text", "text": f"Photo {v.key}:"}, {"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(v.jpeg).decode()}}]
        content.append({"type": "text", "text": INDEX_PROMPT.format(name=f" (it looks like a {name})" if name else "")})
        kw = {"http_client": http_client} if http_client is not None else {}
        client = anthropic.AsyncAnthropic(api_key=key, max_retries=1, timeout=90.0, **kw)
        est = charge("anthropic", key=key_of("voice_index", scan_id))
        extra = {} if "haiku" in model else {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        r = await client.beta.messages.create(
            model=model, max_tokens=8000, messages=[{"role": "user", "content": content}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": INDEX_SCHEMA}}, **extra)
        record_claude((getattr(r, "model", None), model), getattr(r, "usage", None), est)
        if r.stop_reason == "refusal":
            return {"status": "failed", "reason": "refused", "entries": []}
        text = next((b.text for b in r.content if getattr(b, "type", None) == "text"), "{}")
        entries = index_entries(json.loads(text).get("parts") or [], views)
        calls = 1
        if entries and surface is not None and stills and image_of is not None:
            refined, n = await refine_points(surface, stills, image_of, entries, client=client, model=model,
                                             scan_id=scan_id, name=name)
            calls += n
            for e, r_ in zip(entries, refined):
                e["coarse_point"] = e["point"]
                e.update(point=[round(float(c), 5) for c in r_["point"]], radius=r_["radius"],
                         precision=r_["precision"], still_id=r_["still_id"] or e.get("still_id"),
                         x=r_.get("xy", (e.get("x"), e.get("y")))[0], y=r_.get("xy", (e.get("x"), e.get("y")))[1])
            pins, n = await header_pins(surface, stills, image_of, entries, client=client, model=model,
                                        scan_id=scan_id, name=name)
            entries += pins
            calls += n
        return {"status": "done", "model": model, "photos": list(views), "calls": calls, "entries": entries}


def index_entries(parts: list[dict], views: dict[str, V.View]) -> list[dict]:
    """The model's listed parts placed on the surface: each pixel through its photo's point map. A part whose pixel
    is not on the model (or names no photo it was given) is dropped."""
    out = []
    for p in parts[:MAX_INDEX]:
        v = views.get(str(p.get("photo", "")).strip().lower())
        try:
            x, y = float(p.get("x")), float(p.get("y"))
        except (TypeError, ValueError):
            continue
        pt = V.point_at(v, x, y) if v is not None else None
        label = " ".join(str(p.get("label") or "").split())[:40]
        if pt is None or not label:
            continue
        out.append({"id": f"ix_{len(out) + 1}", "label": label, "part": label, "kind": p.get("kind") or "other",
                    "note": " ".join(str(p.get("note") or "").split())[:140], "point": [round(float(c), 5) for c in pt],
                    "radius": INDEX_RADIUS_M, "still_id": v.still_id, "x": round(x, 4), "y": round(y, 4),
                    "where": "read from the scan's photos"})
    return out


async def ask_crops(client: Any, model: str, scan_id: str, crops: list[tuple[str, bytes]], name: str
                    ) -> list[tuple[float, float] | None]:
    """One call: for each (label, crop) where that component's centre is in the crop (fractions), or None."""
    content: list[dict] = []
    for k, (label, jpeg) in enumerate(crops, 1):
        content += [{"type": "text", "text": f"Crop {k}: find the {label}."}, {"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}}]
    content.append({"type": "text", "text": REFINE_PROMPT.format(name=f" (a {name})" if name else "")})
    est = charge("anthropic", key=key_of("voice_refine", scan_id, *[c[0] for c in crops], len(crops)))
    extra = {} if "haiku" in model else {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    r = await client.beta.messages.create(
        model=model, max_tokens=4000, messages=[{"role": "user", "content": content}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": REFINE_SCHEMA}}, **extra)
    record_claude((getattr(r, "model", None), model), getattr(r, "usage", None), est)
    out: list[tuple[float, float] | None] = [None] * len(crops)
    if r.stop_reason == "refusal":
        return out
    text = next((b.text for b in r.content if getattr(b, "type", None) == "text"), "{}")
    for it in json.loads(text).get("items") or []:
        try:
            k, x, y = int(it["crop"]) - 1, float(it["x"]), float(it["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if it.get("seen") and 0 <= k < len(crops) and 0 <= x <= 1 and 0 <= y <= 1:
            out[k] = (x, y)
    return out


PINS_PROMPT = """This is a full-resolution close-up of the {header} on one real object{name}. List every pin or \
contact whose printed label you can read (e.g. "A2", "5V", "GND", "~3"), each with x and y: the centre of that pin \
or contact itself (not the printed text), as fractions of the crop (0,0 top left), read from the faint grid. Only \
labels you can actually read; none when you can read none."""

PINS_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["pins"], "properties": {
    "pins": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["label", "x", "y"],
        "properties": {"label": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"}}}}}}
PINS_SIDE = 0.22            # the header crop, as a fraction of the photo's shorter side
MAX_PINS = 40


async def list_pins(client: Any, model: str, scan_id: str, header: str, crop: bytes, name: str
                    ) -> list[tuple[str, float, float]]:
    """One call: the printed pin labels readable in a header's close-up and where each pin is (crop fractions)."""
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.b64encode(crop).decode()}},
               {"type": "text", "text": PINS_PROMPT.format(header=header, name=f" (a {name})" if name else "")}]
    est = charge("anthropic", key=key_of("voice_pins", scan_id, header))
    extra = {} if "haiku" in model else {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    r = await client.beta.messages.create(
        model=model, max_tokens=4000, messages=[{"role": "user", "content": content}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": PINS_SCHEMA}}, **extra)
    record_claude((getattr(r, "model", None), model), getattr(r, "usage", None), est)
    if r.stop_reason == "refusal":
        return []
    text = next((b.text for b in r.content if getattr(b, "type", None) == "text"), "{}")
    out = []
    for p_ in (json.loads(text).get("pins") or [])[:MAX_PINS]:
        try:
            x, y, label = float(p_["x"]), float(p_["y"]), " ".join(str(p_["label"]).split())[:12]
        except (KeyError, TypeError, ValueError):
            continue
        if label and 0 <= x <= 1 and 0 <= y <= 1:
            out.append((label, x, y))
    return out


def ring_for(own: Any, offset_m: float | None, refined: bool) -> tuple[Any, float, str]:
    """(point, ring radius, precision word) from a refined point and how far from it (metres) a second photo shows
    the part, when one was checked. Tight only when the second photo confirms it."""
    if own is not None and offset_m is not None:
        if offset_m < AGREE_M:
            return own, max(TIGHT_MIN_M, offset_m), "confirmed in a second photo"
        return own, float(np.clip(offset_m, REFINED_RADIUS_M, 0.04)), "second photo disagrees"
    if refined:
        return own, REFINED_RADIUS_M, "refined in one photo"
    return own, COARSE_RADIUS_M, "coarse"


async def refine_points(surface: V.Surface, stills: list[dict], image_of: Any, items: list[dict], *, client: Any,
                        model: str, scan_id: str, name: str = "", sides: tuple[float, ...] = REFINE_SIDES,
                        verify: bool = True) -> tuple[list[dict], int]:
    """Each item ({label, point, and optionally still_id + x, y: its pixel as upright fractions}) refined on
    full-resolution crops of its photo, one batched call per photo per pass (windows `sides`), and with `verify`
    found again in a second photo that sees it (in the last pass's call for that photo). Returns
    ([{point, radius, precision}], calls made). Spots off the model or not seen keep their earlier point."""
    usable = [s for s in stills if V.usable(s)]
    by_id = {s["still_id"]: s for s in usable}
    state = []
    for it in items:
        st = by_id.get(it.get("still_id"))
        xy = (it["x"], it["y"]) if st is not None and it.get("x") is not None else None
        if xy is None:
            cands = V.photo_candidates(usable, np.asarray(it["point"], float))
            st = cands[0][1] if cands else None
            xy = V.to_upright(st, cands[0][2], cands[0][3]) if st is not None else None
        state.append({"still": st, "xy": xy, "point": np.asarray(it["point"], float), "ok": 0, "check": None})

    def view_of(st: dict) -> V.View | None:
        key = (scan_id, "photo", st.get("still_id"))
        v = V.VIEWS.get(key)
        if v is None:
            img = image_of(st)
            v = V.photo_view(surface, st, img, V.short_key(str(st.get("still_id")))) if img else None
            if v is not None:
                V.VIEWS.put(key, v)
        return v

    calls = 0
    for n, side in enumerate(sides):
        last = n == len(sides) - 1
        jobs: dict[str, list[tuple[str, int, tuple[float, float]]]] = {}
        for i, s_ in enumerate(state):
            if s_["still"] is not None and s_["xy"] is not None:
                jobs.setdefault(s_["still"]["still_id"], []).append(("own", i, s_["xy"]))
        if last and verify:   # the refined point projected into another photo: is the part really there?
            for i, s_ in enumerate(state):
                if not s_["ok"] or s_["still"] is None:
                    continue
                for _, st2, u, v, d in V.photo_candidates(usable, s_["point"]):
                    if st2["still_id"] != s_["still"]["still_id"]:
                        x2, y2 = V.to_upright(st2, u, v)
                        s_["check_at"] = (st2, x2, y2, d)
                        jobs.setdefault(st2["still_id"], []).append(("check", i, (x2, y2)))
                        break

        async def one(sid: str, job: list) -> None:
            nonlocal calls
            st, img = by_id[sid], image_of(by_id[sid])
            if not img:
                return
            crops, rects = [], []
            for kind, i, (x, y) in job:
                jpeg, rect = await asyncio.to_thread(V.refine_crop, st, img, x, y, side * (1.6 if kind == "check" else 1))
                crops.append((items[i]["label"], jpeg))
                rects.append(rect)
            answers = await ask_crops(client, model, scan_id, crops, name)
            calls += 1
            view = await asyncio.to_thread(view_of, st)
            for (kind, i, _), rect, a in zip(job, rects, answers):
                if a is None or view is None:
                    continue
                x, y = V.from_crop(rect, *a)
                if kind == "check":   # how far the part is from where the refined point projects, in mm there
                    st2, x2, y2, depth = state[i]["check_at"]
                    w2, h2 = float(st2.get("width_px") or 1), float(st2.get("height_px") or 1)
                    if V.quarter_turns(st2) % 2:
                        w2, h2 = h2, w2
                    fx = float(st2["intrinsics"]["fx"])
                    state[i]["check"] = float(np.hypot((x - x2) * w2, (y - y2) * h2)) * depth / fx
                    continue
                pt = V.point_at(view, x, y)
                if pt is None:
                    continue
                state[i].update(xy=(x, y), point=pt, ok=state[i]["ok"] + 1)
        await asyncio.gather(*(one(sid, job) for sid, job in jobs.items()))
    out = []
    for s_ in state:
        pt, radius, word = ring_for(s_["point"], s_["check"] if s_["ok"] else None, s_["ok"] > 0)
        out.append({"point": pt, "radius": round(float(radius), 4), "precision": word,
                    "still_id": (s_["still"] or {}).get("still_id"), "xy": s_["xy"]})
    return out, calls


async def header_pins(surface: V.Surface, stills: list[dict], image_of: Any, entries: list[dict], *, client: Any,
                      model: str, scan_id: str, name: str = "") -> tuple[list[dict], int]:
    """For each header in the index (a row of pins), a full-resolution close-up and one call listing its printed
    pin labels, each placed on the model through the photo's point map: "where's A2" becomes a lookup. A pin's ring
    is tight (a model reading its own crop at full resolution), never the size of the whole header."""
    by_id = {s["still_id"]: s for s in stills if V.usable(s)}
    out, calls = [], 0
    for e in [x for x in entries if x.get("kind") == "header"][:4]:
        st = by_id.get(e.get("still_id"))
        if st is None or e.get("x") is None or not (img := image_of(st)):
            continue
        crop, rect = await asyncio.to_thread(V.refine_crop, st, img, e["x"], e["y"], PINS_SIDE, 1024)
        found = await list_pins(client, model, scan_id, e["label"], crop, name)
        calls += 1
        view = V.VIEWS.get((scan_id, "photo", st["still_id"]))
        if view is None:
            view = await asyncio.to_thread(V.photo_view, surface, st, img, V.short_key(st["still_id"]))
            if view is not None:
                V.VIEWS.put((scan_id, "photo", st["still_id"]), view)
        for label, cx, cy in found:
            if any(o["label"].lower() == label.lower() for o in out):
                continue   # read already from another header's close-up (crops can overlap): the first reading stays
            x, y = V.from_crop(rect, cx, cy)
            pt = V.point_at(view, x, y) if view is not None else None
            if pt is None:
                continue
            out.append({"id": f"ix_{len(entries) + len(out) + 1}", "label": label, "part": f"{label} pin",
                        "kind": "pin", "note": f"on the {e['label']}", "point": [round(float(c), 5) for c in pt],
                        "radius": REFINED_RADIUS_M, "precision": "pin read in a close-up", "still_id": st["still_id"],
                        "x": round(x, 4), "y": round(y, 4), "where": f"on the {e['label']}"})
    return out, calls
