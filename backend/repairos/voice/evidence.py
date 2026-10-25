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
import hashlib
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
INDEX_RETRY_S = 60.0       # a failed index is tried again after this, doubling per attempt
INDEX_MAX_ATTEMPTS = 3     # ... at most this many times for the same photos
KINDS = ["button", "port", "pin", "header", "connector", "label", "screw", "wire end", "switch", "light", "chip",
         "battery", "other"]

INDEX_PROMPT = """These are photos of one real object from a scan{name}. First, in orientation, say how the object \
lies in each photo (for example "photo s2: its front faces away from the camera, so its own left is on the photo's \
right"). Then list the components a person might ask \
about that you can actually see: buttons, ports and jacks, pins and pin headers, connectors, switches, lights, \
screws, chips, batteries, printed labels, and each visible wire end (with its colour and what it is plugged into). \
For each give: label (its printed text when legible, e.g. "A2", "5V", "GND", "RESET", else a short name like "power \
button" or "red wire end"; left and right mean the object's own sides as its user holds or faces it, never the \
photo's, and the object may lie turned round in a photo), kind, the photo it is clearest in (its name, like s4), x and y (fractions of that photo, \
0,0 top left, on the component itself; read them from the faint grid, whose ticks mark every tenth), and a short \
note (what it is for, or for a wire what it connects to as far \
as you can see). Only list what you can see; read printed text only when it is legible. At most 40, the most useful \
first. Last, in mirror_pairs, for each pair of listed parts named by side (left and right, top and bottom), say from \
what you know of this product which other listed part sits nearer to which one of the pair (for example the right \
port sits nearer the power button); none when there are no such pairs."""

REFINE_PROMPT = """Each crop above is a full-resolution close-up from a photo of one real object{name}. For each \
crop, find the named component and give its centre: x and y as fractions of that crop (0,0 top left), read from \
the faint grid (its ticks mark every tenth). seen is false when that component is not in the crop; do not guess."""

REFINE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {
    "items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["crop", "seen", "x", "y"],
        "properties": {"crop": {"type": "integer"}, "seen": {"type": "boolean"}, "x": {"type": "number"},
                       "y": {"type": "number"}}}}}}

INDEX_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["orientation", "parts", "mirror_pairs"],
                "properties": {
    "orientation": {"type": "string"},
    "parts": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["label", "kind", "photo", "x", "y", "note"],
        "properties": {"label": {"type": "string"}, "kind": {"type": "string", "enum": KINDS},
                       "photo": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
                       "note": {"type": "string"}}}},
    "mirror_pairs": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["a", "b", "anchor", "nearer"],
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}, "anchor": {"type": "string"},
                       "nearer": {"type": "string", "enum": ["a", "b"]}}}}}}


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
        """The scan's stills, each marked `in_frame` (views.usable): segment 0, a continuation in its frame
        (segments.same_frame_segments), or a flipped bottom the server registered for this package revision and
        whose merge passed the gate (segments.merged_transforms, the bottoms the model is built with; its camera
        restated in segment 0's frame). A bottom the gate refused stays out of frame: its photos are shown as
        photos, never placed on the model."""
        from repairos.pipeline.segments import merged_transforms, same_frame_segments

        st = self.app.state.store
        rows = await st.list("still", scan_id=scan_id)
        stills = [r.data for r in rows if r.data]
        pkg_row = await st.get("scan-package", scan_id)
        if pkg_row is None:
            return stills
        run = await st.get("pipeline-run", f"run_{scan_id}")
        rec = ((run.data if run else {}) or {}).get("segments") or {}
        moved = merged_transforms(rec) if rec.get("scan_rev") == pkg_row.rev else {}
        return V.in_scan_frame(stills, same_frame_segments(pkg_row.data or {}), moved)

    async def surface_key(self, scan_id: str) -> str:
        """Which surface the scan has now (the recreation's ref and rev, the mesh rev, the cloud): a new one (a
        better mesh, a recreation landing) gets a fresh surface and re-places the index (`index`)."""
        st = self.app.state.store
        run = await st.get("pipeline-run", f"run_{scan_id}")
        data = (run.data if run else {}) or {}
        pkg_row = await st.get("scan-package", scan_id)
        rec, mesh = data.get("recreation") or {}, data.get("mesh") or {}
        return "|".join(str(x) for x in (rec.get("ref"), rec.get("rev"), mesh.get("rev"),
                                         (pkg_row.data if pkg_row else {}).get("points_file")))

    async def surface(self, scan_id: str) -> V.Surface | None:
        key = await self.surface_key(scan_id)
        hit = V.SURFACES.get((scan_id, key))
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
            V.SURFACES.put((scan_id, key), surface)
            for k in [k for k in list(V.VIEWS.items) if isinstance(k, tuple) and k and k[0] == scan_id]:
                V.VIEWS.items.pop(k, None)   # point maps of the old surface
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
        """The scan's index entries as written (id ix_N, label, kind, note, point or None, radius, still_id, x, y);
        [] while none. `entries` also re-places them on a newer surface."""
        try:
            return json.loads(self._index_path(scan_id).read_text(encoding="utf-8")).get("entries") or []
        except (OSError, ValueError):
            return []

    async def entries(self, scan_id: str) -> list[dict]:
        """The index entries on the scan's current surface: when the surface changed since they were placed (a mesh
        revision, a recreation), every entry's 3D point is worked out again from its photo and pixel (the pixel is
        the model's reading, independent of the mesh), and the file is updated. Photo-only spots get a 3D point here
        once the surface can take it."""
        try:
            data = json.loads(self._index_path(scan_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        entries = data.get("entries") or []
        key = await self.surface_key(scan_id)
        if not entries or data.get("surface_key") == key:
            return entries
        surface = await self.surface(scan_id)
        if surface is None:
            return entries
        by_id = {s["still_id"]: s for s in await self.stills(scan_id)}

        def place() -> None:
            for e in entries:
                st = by_id.get(e.get("still_id"))
                if st is None or e.get("x") is None:
                    continue
                v = V.VIEWS.get((scan_id, "photo", st["still_id"]))
                if v is None and (img := self.image_of(st)):
                    v = V.photo_view(surface, st, img, V.short_key(st["still_id"]))
                    if v is not None:
                        V.VIEWS.put((scan_id, "photo", st["still_id"]), v)
                pt = V.point_at(v, e["x"], e["y"]) if v is not None else None
                e["point"] = None if pt is None else [round(float(c), 5) for c in pt]
        await asyncio.to_thread(place)
        data.update(entries=entries, surface_key=key)
        self._write(scan_id, data)
        return entries

    async def spot_photo_at(self, scan_id: str, still_id: str, x: float, y: float, ring_frac: float) -> str | None:
        """The ringed photo of a photo-only spot (no 3D point), as a blob sha; None without that photo."""
        st = next((s for s in await self.stills(scan_id) if s.get("still_id") == still_id), None)
        img = self.image_of(st) if st else None
        if not img:
            return None
        jpeg = await asyncio.to_thread(V.photo_spot, st, img, x, y, ring_frac)
        return self.app.state.blobs.put(jpeg, "image/jpeg").split("sha256:")[-1]

    async def index_inputs(self, scan_id: str) -> str:
        """What the index reads: its usable photos (a continuation's new close-ups change it), as a short hash."""
        ids = sorted(str(s.get("still_id")) for s in await self.stills(scan_id) if V.usable(s))
        return hashlib.sha1("|".join(ids).encode()).hexdigest()[:16]

    def _read(self, scan_id: str) -> dict | None:
        try:
            return json.loads(self._index_path(scan_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def index_due(data: dict | None, inputs: str, surface_key: str, now: float) -> bool:
        """Whether the index should be (re)built: none yet; a finished one whose photos changed (one written before
        inputs were recorded counts as unchanged: its entries are re-placed on a new surface by `entries`); a skipped
        one whose photos or surface changed; a failed attempt once its backoff (INDEX_RETRY_S, doubling) has passed,
        at most INDEX_MAX_ATTEMPTS times for the same photos."""
        if data is None:
            return True
        status = data.get("status")
        if status == "done" and data.get("inputs") in (None, inputs):
            return False
        if status == "skipped" and data.get("inputs") == inputs and data.get("surface_key") == surface_key:
            return False
        a = data.get("attempt") or {}
        if a.get("inputs") == inputs:
            n = int(a.get("n") or 0)
            if n >= INDEX_MAX_ATTEMPTS or now - float(a.get("at") or 0) < INDEX_RETRY_S * 2 ** max(0, n - 1):
                return False
        return True

    def _record(self, scan_id: str, out: dict, inputs: str) -> None:
        """Writes an index result. A failed or skipped one is an attempt (retried later, `index_due`) and never
        replaces an earlier finished index's entries."""
        old = self._read(scan_id) or {}
        if out.get("status") == "done":
            self._write(scan_id, {**out, "inputs": inputs})
            return
        prev = old.get("attempt") or {}
        n = int(prev.get("n") or 0) + 1 if prev.get("inputs") == inputs else 1
        attempt = {"inputs": inputs, "at": time.time(), "n": n, "status": out.get("status"),
                   "reason": str(out.get("reason") or "")[:300]}
        if old.get("status") == "done":
            self._write(scan_id, {**old, "attempt": attempt})
        else:
            self._write(scan_id, {**out, "inputs": inputs, "attempt": attempt})

    async def ensure_index(self, scan_id: str, talker_key: str | None, model: str, identity: dict | None, *,
                           http_client: Any = None) -> None:
        """Starts the index when it is due (`index_due`: once per set of photos, failures retried with backoff; never
        twice at once): its photos are read here, then the vision call and the file write run on their own thread,
        so the answer never waits for it and it outlives the request."""
        if not talker_key or scan_id in self._building:
            return
        self._building.add(scan_id)
        key, inputs = None, ""
        try:
            key = await self.surface_key(scan_id)
            inputs = await self.index_inputs(scan_id)
            if not self.index_due(self._read(scan_id), inputs, key, time.time()):
                self._building.discard(scan_id)
                return
            views, why = await self._index_views(scan_id)
            status = "skipped"
        except Exception as e:  # noqa: BLE001 - reading the scan failed: an attempt, retried later
            views, why, status = {}, f"{type(e).__name__}: {e}", "failed"
        if not views:
            self._record(scan_id, {"status": status, "reason": why, "entries": [], "surface_key": key}, inputs)
            self._building.discard(scan_id)
            return
        name = (identity or {}).get("name") or " ".join(
            str(x) for x in ((identity or {}).get("brand"), (identity or {}).get("model")) if x)

        surface, stills = V.SURFACES.get((scan_id, key)), await self.stills(scan_id)

        def run() -> None:
            t0 = time.perf_counter()
            try:
                out = asyncio.run(self.call_index(scan_id, views, name, talker_key, model, http_client=http_client,
                                                  surface=surface, stills=stills, image_of=self.image_of))
            except Exception as e:  # noqa: BLE001 - the index is a speed-up; answers work without it
                log.warning("voice index %s failed: %s", scan_id, e)
                out = {"status": "failed", "reason": f"{type(e).__name__}: {e}"[:300], "entries": []}
            out["seconds"] = round(time.perf_counter() - t0, 2)
            out["surface_key"] = key
            try:
                self._record(scan_id, out, inputs)
            finally:
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
        listing = json.loads(text)
        entries = index_entries(listing.get("parts") or [], views)
        calls, stats = 1, {"refine_errors": 0}
        if entries and surface is not None and stills and image_of is not None:
            # Refinement and pin reading are optional: whatever fails, the listed parts stay at their coarse points.
            try:
                refined, n = await refine_points(surface, stills, image_of, entries, client=client, model=model,
                                                 scan_id=scan_id, name=name, stats=stats)
                calls += n
                for e, r_ in zip(entries, refined):
                    e["coarse_point"] = e["point"]
                    xy = r_.get("xy") or (e.get("x"), e.get("y"))
                    e.update(point=None if r_["point"] is None else [round(float(c), 5) for c in r_["point"]],
                             radius=r_["radius"], precision=r_["precision"],
                             still_id=r_["still_id"] or e.get("still_id"), x=xy[0], y=xy[1])
            except Exception as err:  # noqa: BLE001
                log.warning("voice index %s: refining failed, coarse points kept: %s", scan_id, err)
                stats["refine_errors"] += 1
            try:
                pins, n = await header_pins(surface, stills, image_of, entries, client=client, model=model,
                                            scan_id=scan_id, name=name, stats=stats)
                entries += pins
                calls += n
            except Exception as err:  # noqa: BLE001
                log.warning("voice index %s: header pins failed: %s", scan_id, err)
                stats["refine_errors"] += 1
        swapped = check_mirrors(entries, listing.get("mirror_pairs") or [])
        return {"status": "done", "model": model, "photos": list(views), "calls": calls, "entries": entries,
                "refine_errors": stats["refine_errors"],
                "orientation": str(listing.get("orientation") or "")[:400],
                "mirror_pairs": (listing.get("mirror_pairs") or [])[:12], "swapped": swapped}


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
        label = " ".join(str(p.get("label") or "").split())[:40]
        if v is None or not label or not (0 <= x <= 1 and 0 <= y <= 1):
            continue
        pt = V.point_at(v, x, y)
        # A part on a thin or missing mesh is kept as a photo-only spot (its photo and pixel): it is shown in its
        # photo, and gets its 3D point once a better surface lands (Evidence.index re-resolves on a new surface).
        out.append({"id": f"ix_{len(out) + 1}", "label": label, "part": label, "kind": p.get("kind") or "other",
                    "note": " ".join(str(p.get("note") or "").split())[:140],
                    "point": None if pt is None else [round(float(c), 5) for c in pt],
                    "radius": INDEX_RADIUS_M, "still_id": v.still_id, "x": round(x, 4), "y": round(y, 4),
                    "where": "read from the scan's photos"})
    return out


def check_mirrors(entries: list[dict], pairs: list[dict]) -> list[str]:
    """A pair of parts named by side (left / right) whose places contradict the product's own layout (the model's
    knowledge: which listed part sits nearer to which of the pair) is swapped back, in 3D. Returns the swapped
    pairs' labels. Generic: the relations come from the model, nothing here knows a product."""
    placed = [e for e in entries if e.get("point")]

    def find(name: Any) -> dict | None:   # the listed part a relation names: same words, else one inside the other
        want = " ".join(str(name or "").lower().split())
        if not want:
            return None
        exact = [e for e in placed if " ".join(e["label"].lower().split()) == want]
        loose = [e for e in placed if want in e["label"].lower() or e["label"].lower() in want]
        return (exact or loose or [None])[0] if len(exact) == 1 or (not exact and len(loose) == 1) else None
    swapped = []
    for pr in pairs or []:
        a, b, anchor = (find(pr.get(k)) for k in ("a", "b", "anchor"))
        if not (a and b and anchor) or pr.get("nearer") not in ("a", "b") or len({id(a), id(b), id(anchor)}) < 3:
            continue
        da = float(np.linalg.norm(np.asarray(a["point"]) - anchor["point"]))
        db = float(np.linalg.norm(np.asarray(b["point"]) - anchor["point"]))
        if (pr["nearer"] == "a" and da > db * 1.15) or (pr["nearer"] == "b" and db > da * 1.15):
            for k in ("point", "still_id", "x", "y", "radius", "precision", "coarse_point"):
                a[k], b[k] = b.get(k), a.get(k)
            swapped.append(f"{a['label']} / {b['label']}")
    return swapped


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
PIN_RADIUS_M = 0.006        # a pin read on its own close-up: about one socket (2.54 mm pitch headers)
MAX_PINS = 40
SAME_PIN_M = 0.008          # two readings of one label closer than this are the same pin (overlapping crops)
SAME_PIN_PX = 0.02          # ... or, without 3D points, this close in the same photo (fractions)


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
                        verify: bool = True, stats: dict | None = None) -> tuple[list[dict], int]:
    """Each item ({label, point, and optionally still_id + x, y: its pixel as upright fractions}) refined on
    full-resolution crops of its photo, one batched call per photo per pass (windows `sides`), and with `verify`
    found again in a second photo that sees it (in the last pass's call for that photo). Returns
    ([{point, radius, precision}], calls made). Spots off the model or not seen keep their earlier point (and its
    coarse ring); a call that fails leaves its parts as they were and is counted in `stats["refine_errors"]`."""
    usable = [s for s in stills if V.usable(s)]
    by_id = {s["still_id"]: s for s in usable}
    state = []
    for it in items:
        st = by_id.get(it.get("still_id"))
        xy = (it["x"], it["y"]) if st is not None and it.get("x") is not None else None
        if xy is None and it.get("point") is None:
            state.append({"still": None, "xy": None, "point": None, "ok": 0, "placed": 0, "check": None})
            continue
        if xy is None:
            cands = V.photo_candidates(usable, np.asarray(it["point"], float))
            st = cands[0][1] if cands else None
            xy = V.to_upright(st, cands[0][2], cands[0][3]) if st is not None else None
        state.append({"still": st, "xy": xy, "point": None if it.get("point") is None else np.asarray(it["point"], float),
                      "ok": 0, "placed": 0, "check": None})

    def view_of(st: dict) -> V.View | None:
        key = (scan_id, "photo", st.get("still_id"))
        v = V.VIEWS.get(key)
        if v is None:
            img = image_of(st)
            v = V.photo_view(surface, st, img, V.short_key(str(st.get("still_id")))) if img else None
            if v is not None:
                V.VIEWS.put(key, v)
        return v

    calls, errors = 0, 0
    for n, side in enumerate(sides):
        last = n == len(sides) - 1
        jobs: dict[str, list[tuple[str, int, tuple[float, float]]]] = {}
        for i, s_ in enumerate(state):
            if s_["still"] is not None and s_["xy"] is not None:
                jobs.setdefault(s_["still"]["still_id"], []).append(("own", i, s_["xy"]))
        if last and verify:   # the refined point projected into another photo: is the part really there?
            for i, s_ in enumerate(state):
                if not s_["placed"] or s_["still"] is None or s_["point"] is None:
                    continue
                for _, st2, u, v, d in V.photo_candidates(usable, s_["point"]):
                    if st2["still_id"] != s_["still"]["still_id"]:
                        x2, y2 = V.to_upright(st2, u, v)
                        s_["check_at"] = (st2, x2, y2, d)
                        jobs.setdefault(st2["still_id"], []).append(("check", i, (x2, y2)))
                        break

        async def one(sid: str, job: list) -> None:
            nonlocal calls, errors
            st, img = by_id[sid], image_of(by_id[sid])
            if not img:
                return
            crops, rects = [], []
            for kind, i, (x, y) in job:
                jpeg, rect = await asyncio.to_thread(V.refine_crop, st, img, x, y, side * (1.6 if kind == "check" else 1))
                crops.append((items[i]["label"], jpeg))
                rects.append(rect)
            try:
                answers = await ask_crops(client, model, scan_id, crops, name)
            except Exception as e:  # noqa: BLE001 - a refinement is optional: its parts keep their earlier points
                errors += 1
                log.warning("voice refine %s on %s failed: %s", scan_id, sid, e)
                return
            calls += 1
            view = await asyncio.to_thread(view_of, st)
            for (kind, i, _), rect, a in zip(job, rects, answers):
                if a is None or view is None:
                    continue
                x, y = V.from_crop(rect, *a)
                if kind == "check":   # where the second photo shows the part (measured against the final point)
                    state[i]["check_xy"] = (x, y)
                    continue
                pt = V.point_at(view, x, y)
                # The refined pixel counts for the photo even where the surface is thin (a photo-only spot keeps no
                # 3D point); a 3D point is refined only when this pixel placed it, never an older guess kept.
                state[i].update(xy=(x, y), point=pt if pt is not None else state[i]["point"], ok=state[i]["ok"] + 1,
                                placed=state[i]["placed"] + (pt is not None))
        await asyncio.gather(*(one(sid, job) for sid, job in jobs.items()))
    for s_ in state:   # the second photo against the final point (the last pass may have moved it)
        if s_.get("check_xy") is not None and s_["point"] is not None and s_["placed"]:
            s_["check"] = check_offset(s_["check_at"][0], s_["point"], *s_["check_xy"])
    out = []
    for s_ in state:
        refined = s_["placed"] > 0 if s_["point"] is not None else s_["ok"] > 0
        pt, radius, word = ring_for(s_["point"], s_["check"] if refined else None, refined)
        out.append({"point": pt, "radius": round(float(radius), 4), "precision": word,
                    "still_id": (s_["still"] or {}).get("still_id"), "xy": s_["xy"]})
    if stats is not None:
        stats["refine_errors"] = stats.get("refine_errors", 0) + errors
    return out, calls


def check_offset(st2: dict, point: Any, x: float, y: float) -> float | None:
    """How far (metres, at the part's depth) the part found at upright fractions (x, y) of still st2 is from where
    `point` projects in st2; None when it does not project there."""
    proj = V.project_upright(st2, point)
    if proj is None:
        return None
    x2, y2, depth = proj
    w2, h2 = float(st2.get("width_px") or 1), float(st2.get("height_px") or 1)
    if V.quarter_turns(st2) % 2:
        w2, h2 = h2, w2
    fx = float(st2["intrinsics"]["fx"])
    return float(np.hypot((x - x2) * w2, (y - y2) * h2)) * depth / fx


def same_pin(a: dict, b: dict) -> bool:
    """Two pin readings with the same printed label are one pin only when they are in the same place (overlapping
    header crops read it twice): 3D points within SAME_PIN_M, or without 3D points the same photo and pixel.
    Boards repeat GND or 5V on different headers: those are different pins."""
    if a["label"].lower() != b["label"].lower():
        return False
    if a.get("point") is not None and b.get("point") is not None:
        return float(np.linalg.norm(np.asarray(a["point"], float) - np.asarray(b["point"], float))) < SAME_PIN_M
    return a.get("still_id") == b.get("still_id") and \
        float(np.hypot(a["x"] - b["x"], a["y"] - b["y"])) < SAME_PIN_PX


async def header_pins(surface: V.Surface, stills: list[dict], image_of: Any, entries: list[dict], *, client: Any,
                      model: str, scan_id: str, name: str = "", stats: dict | None = None) -> tuple[list[dict], int]:
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
        try:
            found = await list_pins(client, model, scan_id, e["label"], crop, name)
        except Exception as err:  # noqa: BLE001 - a header's pins are optional: the listed parts stay
            log.warning("voice pins %s on %s failed: %s", scan_id, e["label"], err)
            if stats is not None:
                stats["refine_errors"] = stats.get("refine_errors", 0) + 1
            continue
        calls += 1
        view = V.VIEWS.get((scan_id, "photo", st["still_id"]))
        if view is None:
            view = await asyncio.to_thread(V.photo_view, surface, st, img, V.short_key(st["still_id"]))
            if view is not None:
                V.VIEWS.put((scan_id, "photo", st["still_id"]), view)
        for label, cx, cy in found:
            x, y = V.from_crop(rect, cx, cy)
            text = V.text_likeness(st, img, x, y)
            pt = V.point_at(view, x, y) if view is not None else None
            pin = {"label": label, "part": f"{label} pin", "kind": "pin", "note": f"on the {e['label']}",
                   "point": None if pt is None else [round(float(c), 5) for c in pt], "radius": PIN_RADIUS_M,
                   "precision": "pin read in a close-up", "still_id": st["still_id"], "x": round(x, 4),
                   "y": round(y, 4), "where": f"on the {e['label']}", "_text": text}
            prev = next((o for o in out if same_pin(o, pin)), None)
            if prev is not None:   # read in two close-ups (crops overlap): keep the one on the socket, not the text
                if text >= prev["_text"]:
                    continue
                out.remove(prev)
            out.append(pin)
    for i, o in enumerate(out):
        o.pop("_text", None)
        o["id"] = f"ix_{len(entries) + i + 1}"
    return out, calls
