"""Looking turns, measured: the owner's real stills plus spoken-style questions, sent through the gateway's own prompt
path (PERSONA, context_block, the <phone> live line with LiDAR facts, look_content's photos, Talker.stream with its
spend guard), for each model; then graded against labelled expectations with a rubric.

    PY=/Users/rohankilaru/lea/backend/.venv/bin/python
    $PY eval/live_vision/run.py --dry-run                 # build every prompt, no calls
    $PY eval/live_vision/run.py --env /path/backend/.env  # answers for both models, then the judge
    $PY eval/live_vision/run.py --report results/2026-10-24.json

Data is read only: the state database opened with mode=ro and the still blobs. Photos are prepared the way the phone
sends them (Voice/LeaSight.capture): turned upright, 768 px on the long side, JPEG quality 65, the person's pointer
drawn as a magenta ring. Nothing here writes images anywhere. Every Claude call (answers and judge) goes through
repairos.voice.spend_guard (charge before, measured cost after), so the spend caps apply, and `--max-calls` stops the
run before it passes its budget.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import math
import os
import random
import sqlite3
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "backend"))

MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5"]
JUDGE = "claude-opus-5-5"
GRADES = ["correct", "honest_unsure", "wrong", "overclaimed"]
LIVE = {"screen": "scan", "stage": "scanning: walking around it; photos and close-ups take themselves",
        "progress": "55%", "close-ups taken": "3"}

RUBRIC = """You grade the spoken answers of Lea, a repair assistant that sees the person's camera photo while they \
scan an object. You do not see the photo: a person who did labelled what it shows. Grade each answer against that \
label only.

Grades (pick exactly one per answer):
- correct: gives the labelled answer (hedging is fine), or, for an item marked answerable=false, says honestly that \
it can't tell and why, in the way the label describes. Nothing it states contradicts the label.
- honest_unsure: says it can't tell or asks for a better view on an item that IS answerable from the photo (a miss, \
but honest), and claims nothing wrong.
- wrong: states something the label contradicts (hedged or not), on the main point of the question.
- overclaimed: states as fact something the label says the photo can't support: a confident claim on an \
answerable=false item, a brand or model it could not read, a number it made up, where a wire goes or which way \
round something goes from colour or looks, a fault declared as proven. Use this over wrong when the claim is \
unsupported rather than contradicted; when both apply, choose overclaimed.

Judge the main point of the question; ignore style, length and extra coaching unless they contain a claim of the \
kinds above. Markers like [mark: ...] or [measure: ...] are what Lea pointed at or asked the phone to measure, \
not spoken words."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["A", "B"], "properties": {
    k: {"type": "object", "additionalProperties": False, "required": ["grade", "reason"],
        "properties": {"grade": {"type": "string", "enum": GRADES}, "reason": {"type": "string"}}}
    for k in ("A", "B")}}


# ------------------------------------------------------------------------------------------------ data (read only)
class Data:
    def __init__(self, root: Path):
        self.root = root
        self.db = sqlite3.connect(f"file:{root / 'state.sqlite3'}?mode=ro", uri=True)

    def row(self, kind: str, id: str) -> dict | None:
        r = self.db.execute("SELECT json FROM rows WHERE kind=? AND id=?", (kind, id)).fetchone()
        return json.loads(r[0]) if r else None

    def blob(self, ref: str) -> bytes:
        h = ref.split("sha256:")[-1]
        return (self.root / "blobs" / h[:2] / h).read_bytes()


def phone_jpeg(raw: bytes, ring: list[float] | None = None, long_side: int = 768, quality: int = 65) -> bytes:
    """The photo as LeaSight sends it: sensor landscape turned right (portrait), 768 px long side, JPEG 0.65, the
    person's pointer as a magenta ring (radius 6% of the width)."""
    from PIL import Image, ImageDraw

    im = Image.open(io.BytesIO(raw)).convert("RGB").rotate(-90, expand=True)
    im.thumbnail((long_side, long_side), Image.Resampling.LANCZOS)
    if ring:
        w, h = im.size
        r = w * 0.06
        x, y = ring[0] * w, ring[1] * h
        ImageDraw.Draw(im).ellipse([x - r, y - r, x + r, y + r], outline=(255, 0, 255), width=max(3, round(r * 0.18)))
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=quality)
    return out.getvalue()


def lidar_facts(pkg: dict | None, still: dict) -> dict[str, str]:
    """The LiDAR facts the phone sends with a looking turn (ScanSession.lidarFacts): the object's box (footprint and
    height, mm) and the camera's distance to its centre (cm)."""
    box = (pkg or {}).get("crop_box") or {}
    ext, ctr = box.get("extent_m"), box.get("center_m")
    out = {}
    if ext and len(ext) == 3:
        out["object box (LiDAR)"] = f"{round(ext[0] * 1000)} x {round(ext[2] * 1000)} mm footprint, " \
                                    f"{round(ext[1] * 1000)} mm tall"
        cam = ((still.get("camera") or {}).get("position_m"))
        if ctr and cam:
            out["camera distance (LiDAR)"] = f"{round(math.dist(cam, ctr) * 100)} cm"
    return out


def build(item: dict, data: Data) -> tuple[list[str], dict[str, str]]:
    """(the photos as base64 JPEG, oldest first; the live dict) for one item."""
    photos = []
    for i, sid in enumerate(item["frames"]):
        st = data.row("still", sid)
        last = i == len(item["frames"]) - 1
        photos.append(base64.b64encode(phone_jpeg(data.blob(st["image_file"]), item.get("ring") if last else None)).decode())
    now = data.row("still", item["frames"][-1])
    live = dict(LIVE)
    live.update(lidar_facts(data.row("scan-package", now["scan_id"]), now))
    if item.get("ring"):
        live["pointed"] = "yes: the magenta ring in the photo"
    return photos, live


# ------------------------------------------------------------------------------------------------ one looking turn
async def ask(talker, model: str, photos: list[str], live: dict, question: str, scope: str) -> dict:
    """The gateway's converse path for one turn, timed: first token and first sentence from the request.
    `model` may end in "@nothink" (the same model with thinking disabled, a latency probe)."""
    from repairos.voice.conversation import (SentenceSplitter, context_block, live_note, look_content, messages_for,
                                             parse_live)

    msgs = messages_for({}, context_block(None, None, None), live_note(parse_live(json.dumps(live))) + question)
    msgs[-1]["content"] = look_content(photos, msgs[-1]["content"])
    sp, text, used = SentenceSplitter(), [], {}
    t0, first_token, first_sentence = time.perf_counter(), None, None

    def on_usage(m, u):
        used["model"], used["usage"] = m, u
    try:
        name, _, variant = model.partition("@")
        async for delta in talker.stream(scope, msgs, model=name, on_usage=on_usage,
                                         thinking={"type": "disabled"} if variant == "nothink" else None):
            first_token = first_token or time.perf_counter() - t0
            text.append(delta)
            if sp.feed(delta) and first_sentence is None:
                first_sentence = time.perf_counter() - t0
        rest = sp.flush()
        if rest and first_sentence is None:
            first_sentence = time.perf_counter() - t0
        error = None
    except Exception as e:  # noqa: BLE001 - recorded, the run goes on
        error = f"{type(e).__name__}: {e}"
    total = time.perf_counter() - t0
    u = used.get("usage")
    counts = {k: getattr(u, k, 0) or 0 for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                                   "cache_read_input_tokens")} if u else {}
    from repairos import spend
    usd = spend.anthropic_usd(used.get("model") or model.partition("@")[0], counts) if counts else None
    return {"model": model, "answered_by": used.get("model"), "raw": "".join(text), "error": error,
            "marks": sp.marks, "measure": sp.measure, "first_token_s": first_token, "first_sentence_s": first_sentence,
            "total_s": total, "usage": counts, "usd": usd}


def spoken(a: dict) -> str:
    """What the person hears, plus what Lea pointed at, for the judge."""
    from repairos.voice.conversation import SentenceSplitter
    sp = SentenceSplitter()
    words = " ".join(sp.feed(a["raw"]) + sp.flush())
    extra = "".join(f" [mark: {m['label']}]" for m in a["marks"])
    if a["measure"]:
        extra += " [measure: two points in the photo]"
    return (words + extra).strip() or f"(no answer: {a['error']})"


async def judge(client, item: dict, live: dict, answers: dict[str, dict], rng: random.Random) -> dict:
    from repairos.voice.spend_guard import charge, key_of, record_claude

    order = list(answers)
    rng.shuffle(order)
    label = {k: v for k, v in item.items() if k in ("expect", "correct_if", "overclaim_if", "wrong_if")}
    label["answerable"] = item.get("answerable", True)
    body = (f"Question the person asked out loud: {item['question']}\n"
            f"<phone> line Lea was given (LiDAR numbers she may repeat): {json.dumps(live)}\n"
            f"Label: {json.dumps(label, ensure_ascii=False)}\n\n"
            f"Answer A: {spoken(answers[order[0]])}\n\nAnswer B: {spoken(answers[order[1]])}")
    est = charge("anthropic", key=key_of("live_vision_judge", item["id"], body))
    r = await client.beta.messages.create(
        model=JUDGE, max_tokens=4000, system=RUBRIC, messages=[{"role": "user", "content": body}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    record_claude((getattr(r, "model", None), JUDGE), getattr(r, "usage", None), est)
    out = json.loads(next(b.text for b in r.content if b.type == "text"))
    return {order[0]: out["A"], order[1]: out["B"]}


# ------------------------------------------------------------------------------------------------ report
def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, math.ceil(q * len(xs)) - 1))] if xs else float("nan")


def report(res: dict) -> str:
    rows = ["| model | n | correct | honest-unsure | wrong | overclaimed | first token p50 / p95 | "
            "first sentence p50 / p95 | cost per turn |", "|---|---|---|---|---|---|---|---|---|"]
    for m in res["models"]:
        turns = [it["answers"][m] for it in res["items"] if m in it["answers"]]
        grades = [it["grades"][m]["grade"] for it in res["items"] if m in it.get("grades", {})]
        ft = [t["first_token_s"] for t in turns if t["first_token_s"]]
        fs = [t["first_sentence_s"] for t in turns if t["first_sentence_s"]]
        usd = [t["usd"] for t in turns if t["usd"] is not None]
        c = {g: grades.count(g) for g in GRADES}
        rows.append(f"| {m} | {len(grades)} | {c['correct']} | {c['honest_unsure']} | {c['wrong']} | "
                    f"{c['overclaimed']} | {pct(ft, .5):.2f} / {pct(ft, .95):.2f} s | {pct(fs, .5):.2f} / "
                    f"{pct(fs, .95):.2f} s | ${statistics.mean(usd):.5f} |")
    kinds = sorted({it["kind"] for it in res["items"]})
    rows += ["", "| kind | n | " + " | ".join(f"{m} correct" for m in res["models"]) + " |",
             "|---|---|" + "---|" * len(res["models"])]
    for k in kinds:
        its = [it for it in res["items"] if it["kind"] == k]
        rows.append(f"| {k} | {len(its)} | " + " | ".join(
            str(sum(1 for it in its if it.get("grades", {}).get(m, {}).get("grade") == "correct"))
            for m in res["models"]) + " |")
    return "\n".join(rows)


# ------------------------------------------------------------------------------------------------ main
def load_key(env: Path) -> None:
    """ANTHROPIC_API_KEY from a .env file into this process only (never printed)."""
    if os.environ.get("ANTHROPIC_API_KEY") or not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        k, _, v = line.partition("=")
        if k.strip() == "ANTHROPIC_API_KEY" and v.strip():
            os.environ["ANTHROPIC_API_KEY"] = v.strip().strip('"').strip("'")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("/Users/rohankilaru/lea/data"))
    ap.add_argument("--env", type=Path, default=ROOT / "backend" / ".env")
    ap.add_argument("--models", nargs="+", default=MODELS)
    ap.add_argument("--only", nargs="*", help="item ids")
    ap.add_argument("--max-calls", type=int, default=150)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--out", type=Path, default=HERE / "results" / f"{datetime.now(UTC):%Y-%m-%d}.json")
    ap.add_argument("--report", type=Path)
    ap.add_argument("--reuse", type=Path, help="an earlier results file: its answers for these models are reused")
    a = ap.parse_args()
    if a.report:
        print(report(json.loads(a.report.read_text(encoding="utf-8"))))
        return
    items = json.loads((HERE / "questions.json").read_text(encoding="utf-8"))["items"]
    if a.only:
        items = [it for it in items if it["id"] in a.only]
    reused = {it["id"]: it["answers"] for it in json.loads(a.reuse.read_text(encoding="utf-8"))["items"]} if a.reuse else {}
    fresh = sum(1 for it in items for m in a.models if m not in reused.get(it["id"], {}))
    calls = fresh + (0 if a.no_judge else len(items))
    if calls > a.max_calls:
        sys.exit(f"{calls} Claude calls planned, over --max-calls {a.max_calls}")
    data = Data(a.data)
    built = {it["id"]: build(it, data) for it in items}
    if a.dry_run:
        for it in items:
            photos, live = built[it["id"]]
            print(it["id"], len(photos), "photos", sum(len(p) * 3 // 4 for p in photos) // 1024, "KB", live)
        print(f"{calls} calls would be made")
        return

    load_key(a.env)
    from repairos.voice.conversation import Talker
    from repairos.voice.settings import VoiceSettings
    import anthropic

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set (pass --env)")
    settings = VoiceSettings()
    talker = Talker(settings, key)
    client = anthropic.AsyncAnthropic(api_key=key, max_retries=1, timeout=60.0)
    rng = random.Random(20261024)
    res = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "models": a.models, "judge": JUDGE,
           "effort": settings.lea_voice_answer_effort, "items": []}
    for n, it in enumerate(items):
        photos, live = built[it["id"]]
        order = a.models if n % 2 == 0 else list(reversed(a.models))   # alternate who goes first
        answers = {}
        for m in order:
            answers[m] = reused.get(it["id"], {}).get(m) or \
                await ask(talker, m, photos, live, it["question"], f"eval_{it['id']}_{m}")
        row = {"id": it["id"], "kind": it["kind"], "answerable": it.get("answerable", True), "live": live,
               "answers": answers}
        if not a.no_judge:
            try:
                row["grades"] = await judge(client, it, live, answers, rng)
            except Exception as e:  # noqa: BLE001
                row["judge_error"] = f"{type(e).__name__}: {e}"
        res["items"].append(row)
        print(it["id"], {m: (round(answers[m]["first_sentence_s"] or -1, 2), row.get("grades", {}).get(m, {}).get("grade"))
                         for m in a.models}, flush=True)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print(report(res))
    print("saved", a.out)


if __name__ == "__main__":
    asyncio.run(main())
