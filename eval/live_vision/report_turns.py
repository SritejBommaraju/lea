"""Report turns, measured on the owner's real scans: Lea answering about a finished scan through the gateway's own
pieces (PERSONA, context_block with <parts>, repairos.voice.views for the photos and 3D renders, the part index,
the marker -> 3D point resolution and the ringed spot photo), Haiku answering.

    PY=/Users/rohankilaru/lea/backend/.venv/bin/python
    $PY eval/live_vision/report_turns.py --index            # build the part index for the scans (one call each)
    $PY eval/live_vision/report_turns.py                    # every item in report_questions.json
    $PY eval/live_vision/report_turns.py --only xbox_xb_here --out ...

Item modes: `after` (the gateway now), `before` (the persona and words-only context at git HEAD), `heldout` (a
located finding is taken out of <parts>; Lea must point at it in a photo or view, and the point's distance to the
finding's own located point is the accuracy), `sparse` (only `keep` stills are left: renders carry the rest).
Data is read only (state DB mode=ro, blobs). Ringed spot photos go to --shots (the scratchpad), never the repo.
Every Claude call goes through the spend guard.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(HERE))

from run import Data, load_key  # noqa: E402

MODEL = "claude-haiku-5-5"


def old_persona() -> str:
    """The persona at git HEAD (before this change)."""
    src = subprocess.run(["git", "-C", str(ROOT), "show", "HEAD:backend/repairos/voice/conversation.py"],
                         capture_output=True, text=True, check=True).stdout
    ns: dict = {}
    start = src.index('PERSONA = """')
    end = src.index('"""', start + len('PERSONA = """')) + 3
    exec(src[start:end], ns)  # noqa: S102 - our own source, read from git
    return ns["PERSONA"]


class Scan:
    """One scan's evidence from the read-only data dir, the way repairos.voice.evidence reads it from the store."""

    def __init__(self, data: Data, scan_id: str, keep: list[str] | None = None):
        from repairos.locate.cloud import read_ply
        from repairos.voice import views as V

        self.id, self.data = scan_id, data
        self.report = data.row("report", f"rep_{scan_id}")
        run = data.row("pipeline-run", f"run_{scan_id}") or {}
        pkg = data.row("scan-package", scan_id) or {}
        self.identity = run.get("identity") or self.report.get("object")
        sess = data.row("session", self.report.get("session_id") or "") or {}
        self.story = sess.get("symptom") or pkg.get("symptom")
        rows = data.db.execute("SELECT json FROM rows WHERE kind='still'").fetchall()
        self.stills = [s for s in (json.loads(r[0]) for r in rows) if s.get("scan_id") == scan_id]
        if keep is not None:
            self.stills = [s for s in self.stills if V.short_key(s["still_id"]) in keep]
        rec = run.get("recreation") or {}
        if rec.get("ref") and rec.get("format") == "usdz":
            self.surface = V.surface_from_usdz(data.blob(rec["ref"])).focus(rec.get("focus_center"), rec.get("focus_radius"))
        else:
            with tempfile.NamedTemporaryFile(suffix=".ply", delete=False) as f:
                f.write(data.blob(pkg["points_file"]))
            self.surface = V.surface_from_cloud(read_ply(f.name), pkg.get("crop_box"), pkg.get("voxel_mm"))
            os.unlink(f.name)

    def image_of(self, st):
        try:
            return self.data.blob(st["image_file"])
        except OSError:
            return None


async def index_scan(scan: Scan, key: str, out: Path) -> dict:
    from repairos.voice import views as V
    from repairos.voice.evidence import INDEX_PHOTOS, INDEX_PX, Evidence

    views = {}
    for st in V.pick_photos([s for s in scan.stills if V.usable(s)], "", [], scan.surface.center, k=INDEX_PHOTOS):
        v = V.photo_view(scan.surface, st, scan.image_of(st), V.short_key(st["still_id"]), INDEX_PX)
        if v is not None:
            views[v.key] = v
            (out / f"index_{scan.id}_{v.key}.jpg").write_bytes(v.jpeg)
    ident = scan.identity or {}
    name = ident.get("name") or " ".join(str(x) for x in (ident.get("brand"), ident.get("model")) if x)
    t0 = time.perf_counter()
    res = await Evidence.call_index(scan.id, views, name, key, "claude-sonnet-5-5", surface=scan.surface,
                                    stills=scan.stills, image_of=scan.image_of)
    res["seconds"] = round(time.perf_counter() - t0, 1)
    return res


async def turn(scan: Scan, item: dict, talker, index: list[dict], shots: Path) -> dict:
    from repairos.voice import conversation as C
    from repairos.voice import views as V
    from repairos.voice.answer import report_facts
    from repairos.voice.evidence import FINDING_RADIUS_M, POINT_RADIUS_M
    from repairos.voice.routes import _named_part

    mode = item.get("mode", "after")
    rows = C.part_rows(scan.report)
    held = None
    if mode in ("heldout", "heldout_index"):
        held = next(r for r in rows if r["id"] == item["heldout"])
        rows = [r for r in rows if r["id"] != held["id"]]
    extra = [] if mode in ("before", "heldout") else index
    views = []
    t0 = time.perf_counter()
    if mode == "before":
        obj = {k: (scan.identity or {}).get(k) for k in ("name", "category", "brand", "model")
               if (scan.identity or {}).get(k)}
        context = (f"<scan>{json.dumps(report_facts(scan.report), ensure_ascii=False, separators=(',', ':'))}</scan>\n"
                   f"<object>{json.dumps(obj) if obj else 'not identified'}</object>\n"
                   f"<story>{(scan.story or 'not given').strip()}</story>")
        msgs = C.messages_for({}, context, item["question"])
        C.PERSONA, saved = old_persona(), C.PERSONA
    else:
        report = scan.report
        if mode in ("heldout", "heldout_index"):   # the finding is not in <parts> (nor in the report she reads)
            report = {**report, "findings": [f for f in report["findings"] if f.get("finding_id") != held["id"]]}
        msgs = C.messages_for({}, C.context_block(report, scan.identity, scan.story, parts=True, extra_parts=extra),
                              item["question"])
        views = V.build_views(scan.surface, scan.stills, scan.image_of, item["question"], rows + extra, scope=scan.id)
        msgs[-1]["content"] = C.views_content(views, msgs[-1]["content"])
        saved = None
    prep = time.perf_counter() - t0
    tools = [C.web_search_tool(MODEL)] if item.get("search") else None
    sp, text, used = C.SentenceSplitter(), [], {}
    t1, first = time.perf_counter(), None

    def on_usage(m, u):
        used["usage"] = u
    try:
        async for d in talker.stream(f"eval_report_{item['id']}", msgs, model=MODEL, tools=tools, on_usage=on_usage):
            text.append(d)
            if sp.feed(d) and first is None:
                first = time.perf_counter() - t1
        sp.flush()
    finally:
        if saved is not None:
            C.PERSONA = saved
    raw = "".join(text)
    spoken_sp = C.SentenceSplitter()
    words = " ".join(spoken_sp.feed(raw) + spoken_sp.flush())
    known = {r["id"]: r for r in rows + extra}
    points = []
    for mv in sp.stage:
        pt, label, radius = None, "", 0.0
        if mv["type"] == "show" and mv["part"] in known:
            r = known[mv["part"]]
            pt, label, radius = r.get("point"), r.get("label") or r.get("part"), r.get("radius") or FINDING_RADIUS_M
        elif mv["type"] == "point" and _named_part(mv["label"], extra) is not None:   # as the gateway does
            r = _named_part(mv["label"], extra)
            pt, label, radius = r["point"], r["label"], r.get("radius") or POINT_RADIUS_M
            mv = {**mv, "part": r["id"]}
        elif mv["type"] == "point":
            v = next((x for x in views if x.key == mv["photo"]), None)
            got = V.point_at(v, mv["x"], mv["y"]) if v is not None else None
            pt, label, radius = (None if got is None else got.tolist()), mv["label"], POINT_RADIUS_M
        if pt is None:
            continue
        jpeg, sid = V.spot_photo(scan.surface, scan.stills, scan.image_of, pt, radius, scope=scan.id)
        name = f"{item['id']}_{len(points) + 1}.jpg"
        (shots / name).write_bytes(jpeg)
        points.append({"marker": mv, "label": label, "point": [round(float(c), 4) for c in pt], "photo": sid,
                       "shot": name, "source": "index" if str(mv.get("part", "")).startswith("ix_") else
                       ("point" if mv["type"] == "point" else "finding")})
    err = None
    if held is not None and points:
        err = round(min(float(np.linalg.norm(np.asarray(p["point"]) - np.asarray(held["point"]))) for p in points) * 1000, 1)
    u = used.get("usage")
    from repairos import spend
    counts = {k: getattr(u, k, 0) or 0 for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                                   "cache_read_input_tokens")} if u else {}
    if item.get("expect_label") and points:   # did she show the part she was asked for (not a neighbour)?
        want = item["expect_label"].lower()
        used_right = any(want in str(p["label"]).lower() for p in points)
    else:
        used_right = None
    return {"id": item["id"], "scan": scan.id, "mode": mode, "used_the_named_part": used_right, "kind": item.get("kind"), "question": item["question"],
            "spoken": words, "raw": raw, "moves": sp.stage, "points": points, "heldout_error_mm": err,
            "views": [v.key for v in views], "prep_s": round(prep, 2),
            "first_sentence_s": None if first is None else round(first, 2),
            "searches": C.searches_made(u), "usd": spend.anthropic_usd(MODEL, counts) if counts else None}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("/Users/rohankilaru/lea/data"))
    ap.add_argument("--env", type=Path, default=Path("/Users/rohankilaru/lea/backend/.env"))
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--index", action="store_true", help="build the part index for every scan in the items")
    ap.add_argument("--shots", type=Path, required=True, help="where ringed spot photos go (outside the repo)")
    ap.add_argument("--out", type=Path, default=HERE / "results" / "report-turns.json")
    ap.add_argument("--max-calls", type=int, default=30)
    a = ap.parse_args()
    load_key(a.env)
    key = os.environ.get("ANTHROPIC_API_KEY") or sys.exit("ANTHROPIC_API_KEY is not set")
    items = json.loads((HERE / "report_questions.json").read_text(encoding="utf-8"))["items"]
    if a.only:
        items = [it for it in items if it["id"] in a.only]
    a.shots.mkdir(parents=True, exist_ok=True)
    data = Data(a.data)
    index_file = a.shots / "index.json"
    indexes = json.loads(index_file.read_text()) if index_file.exists() else {}
    if a.index:
        for sid in sorted({it["scan"] for it in items}):
            indexes[sid] = await index_scan(Scan(data, sid), key, a.shots)
            print(sid, indexes[sid]["status"], [e["label"] for e in indexes[sid]["entries"]], indexes[sid]["seconds"], "s")
        index_file.write_text(json.dumps(indexes, indent=1))
        return
    if len(items) > a.max_calls:
        sys.exit(f"{len(items)} calls planned, over --max-calls {a.max_calls}")
    from repairos.voice.conversation import Talker
    from repairos.voice.settings import VoiceSettings
    talker = Talker(VoiceSettings(), key)
    scans: dict = {}
    out = []
    for it in items:
        k = (it["scan"], tuple(it.get("keep") or ()))
        if k not in scans:
            scans[k] = Scan(data, it["scan"], it.get("keep"))
        r = await turn(scans[k], it, talker, (indexes.get(it["scan"]) or {}).get("entries") or [], a.shots)
        out.append(r)
        print(json.dumps({x: r[x] for x in ("id", "mode", "spoken", "heldout_error_mm", "used_the_named_part",
                                            "first_sentence_s", "views")},
                         ensure_ascii=False), flush=True)
        for p in r["points"]:
            print("   ->", p["label"], p["photo"], p["shot"])
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print("saved", a.out)


if __name__ == "__main__":
    asyncio.run(main())
