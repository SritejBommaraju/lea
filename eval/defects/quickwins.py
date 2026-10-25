"""Detection quick wins, measured: checklist prompt, VLM routing, budget re-ranking, unknown-part path.

    PY=backend/.venv/bin/python
    $PY eval/defects/quickwins.py vlm --arm sonnet_cl          # one VLM arm on the seeded 300 held-out crops
    $PY eval/defects/quickwins.py vlm --arm sonnet_cl --dev 40 # the same arm on 40 non-held-out crops (prompt check)
    $PY eval/defects/quickwins.py detect --set dev|heldout     # OWLv2 with per-box damage scores on whole stills
    $PY eval/defects/quickwins.py rank --set dev|heldout       # defects inside the inspected set, per ranking
    $PY eval/defects/quickwins.py filter --set dev|heldout     # region floor + cap (vision.inspect.select_regions)
    $PY eval/defects/quickwins.py report                       # results/quickwins-<date>.json + tables

VLM arms run on exactly the crops of eval/defects/run.py (`vlm_sample.json`, 300 held-out detector crops: VisA pcb4
100 defect + 100 ok, Wikimedia 60 defect + 40 ok), asked as vision/inspect.py asks (crop + context view + the
detector label as data). Recorded real answers are kept under data/datasets/work/vlm_<arm>/ and reused; an answer
whose request would be identical in another arm is reused from that arm instead of asked again.
Held-out data is never used to choose a prompt, a weight or a threshold: those were set before the held-out runs,
or chosen on the `dev` stills (VisA pcb1-3 and webcam close-ups, train/val split only).
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import random
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run as R  # noqa: E402  (also puts training/datasets on the path)

D, M = R.D, R.M
SONNET, HAIKU = "claude-sonnet-5-5", "claude-haiku-5-5"

# arm -> (models, checklists, family rule). family rule: None = no family (old prompt), "label" = the family the
# detector label suggests, "unknown" = unknown_part for weak labels (vision/checklists.crop_family).
ARMS: dict[str, tuple[tuple[str, ...], bool, str | None]] = {
    "haiku_ladder": ((HAIKU, SONNET), False, None),      # the shipped M3 policy; cached in work/vlm (run.py)
    "sonnet": ((SONNET,), False, None),                  # 60 cached by run.py in work/vlm_claude-sonnet-5-5
    "haiku_ladder_cl": ((HAIKU, SONNET), True, "label"),
    "sonnet_cl": ((SONNET,), True, "label"),
    "sonnet_cl_unknown": ((SONNET,), True, "unknown"),
    # the same requests as sonnet_cl asked again on every third crop: the run-to-run noise floor of one arm
    "sonnet_cl_repeat": ((SONNET,), True, "label"),
    # the shipped arm (sonnet_cl_unknown) with the report-fix prompt: severity + part_words asked per crop
    "sonnet_cl_unknown_sev": ((SONNET,), True, "unknown"),
}
REPEAT_EVERY = 3
ARM_DIR = {"haiku_ladder": D.WORK / "vlm", "sonnet": D.WORK / f"vlm_{SONNET}"}
REUSE = {"sonnet_cl_unknown": "sonnet_cl"}
PAIRS = (("haiku_ladder", "haiku_ladder_cl", "checklist at the Haiku ladder"),
         ("sonnet", "sonnet_cl", "checklist at Sonnet"),
         ("haiku_ladder", "sonnet", "Sonnet instead of the ladder, no checklist"),
         ("haiku_ladder_cl", "sonnet_cl", "Sonnet instead of the ladder, with checklist"),
         ("sonnet_cl", "sonnet_cl_unknown", "unknown_part checklist for weak labels"),
         ("haiku_ladder", "sonnet_cl_unknown", "all kept changes vs M3"),
         ("sonnet_cl_unknown", "sonnet_cl_unknown_sev", "severity and part words in the prompt"))  # crops whose request is identical are copied, not re-asked


def arm_dir(arm: str, dev: bool = False) -> Path:
    if dev:
        return D.WORK / f"qw_dev_{arm}"
    return ARM_DIR.get(arm, D.WORK / f"qw_{arm}")


def family_of(rule: str | None, row: dict) -> str | None:
    from repairos.vision.checklists import crop_family, family_for

    if rule is None:
        return None
    if rule == "label":
        return family_for(row["det_label"])
    return crop_family(row["det_label"], row["det_score"])


def dev_rows(n: int) -> list[dict]:
    """n non-held-out detector crops (half defect), seeded: for checking a prompt before any held-out run."""
    rows = [r for r in D.load_crops() if r["split"] in ("test", "val") and r["kind"] == "det"
            and r["source"] in ("visa", "pcb_defect_webcam")]
    rng = random.Random("quickwins-dev")
    out = []
    for lab in ("defect", "ok"):
        pool = [r for r in rows if r["label"] == lab]
        rng.shuffle(pool)
        out += pool[: n // 2]
    return out


def sample_rows() -> list[dict]:
    ids = json.loads(R.SAMPLE.read_text())
    by = {r["crop_id"]: r for r in D.load_crops()}
    return [by[i] for i in ids]


def cmd_vlm(a) -> None:
    from repairos.registry import Registry
    from repairos.vision.crops import context_view, load_still

    models, cl, rule = ARMS[a.arm]
    rows = dev_rows(a.dev) if a.dev else sample_rows()
    if a.arm.endswith("_repeat"):
        rows = rows[::REPEAT_EVERY]
    if a.only_differing:  # sonnet_cl_unknown: only the crops whose family changes
        rows = [r for r in rows if family_of(rule, r) != family_of("label", r)]
    out_dir = arm_dir(a.arm, bool(a.dev))
    out_dir.mkdir(parents=True, exist_ok=True)
    src = REUSE.get(a.arm)
    if src and not a.dev:
        for r in sample_rows():
            if family_of(rule, r) == family_of(ARMS[src][2], r) and (arm_dir(src) / f"{r['crop_id']}.json").exists() \
                    and not (out_dir / f"{r['crop_id']}.json").exists():
                shutil.copy(arm_dir(src) / f"{r['crop_id']}.json", out_dir / f"{r['crop_id']}.json")
    judge = Registry().load("VLM", "anthropic")
    judge.models, judge.checklists = models, cl
    judge.effort = {HAIKU: "low", SONNET: "medium"}  # the M3 settings, unchanged in every arm
    judge.record_dir = out_dir / "raw"
    todo = [r for r in rows if not (out_dir / f"{r['crop_id']}.json").exists()]
    if a.limit:
        todo = todo[: a.limit]
    print(f"arm {a.arm}: {len(rows)} crops, {len(todo)} to ask", flush=True)

    def ask(r: dict) -> None:
        im = load_still(D.image_path(r["image_id"]))
        crop = D.cut_row(r)
        ctx = context_view(im, tuple(r["box"]))
        fam = family_of(rule, r)
        t = time.perf_counter()
        s = judge.inspect_crop(crop.image, ctx, part_hint=r["det_label"], family=fam)
        (out_dir / f"{r['crop_id']}.json").write_text(json.dumps({
            "crop_id": r["crop_id"], "state": s.state, "part_class": s.part_class, "evidence": s.evidence_text,
            "confidence": s.confidence_word, "model": s.model, "escalated": s.escalated, "family": fam,
            "severity": s.severity, "part_words": s.part_words,
            "models": [x["model"] for x in s.attempts], "latency_ms": s.latency_ms,
            "wall_s": round(time.perf_counter() - t, 2), "usage": [x.get("usage") for x in s.attempts],
            "label": "recorded real VLM answer"}))

    errors = 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs = [pool.submit(ask, r) for r in todo]
        for k, f in enumerate(futs):
            try:
                f.result()
            except Exception as e:  # noqa: BLE001  recorded and counted; a missing answer is never filled in
                errors += 1
                print("error", type(e).__name__, str(e)[:200], flush=True)
            if k % 25 == 0:
                print(k, flush=True)
    print(f"done, {errors} errors")


# -- cost ---------------------------------------------------------------------------------------------------------
def cost_usd(v: dict) -> float:
    """List-price cost of one recorded answer, every attempt, with prompt-cache writes (1.25x) and reads (0.1x)."""
    models = v.get("models") or ([v["model"]] if not v.get("escalated") else [HAIKU, v["model"]])
    total = 0.0
    for m, u in zip(models, v.get("usage") or []):
        pi, po = R.PRICE[m]
        total += (u["input_tokens"] * pi + u.get("cache_creation_input_tokens", 0) * pi * 1.25
                  + u.get("cache_read_input_tokens", 0) * pi * 0.1 + u["output_tokens"] * po) / 1e6
    return total


# -- metrics ------------------------------------------------------------------------------------------------------
def paired_boot(y, a, b, seed: int = 0) -> dict:
    """95 % bootstrap intervals of (b - a) for recall, precision and F1, crops resampled together (paired)."""
    import numpy as np

    rng = np.random.default_rng(seed)
    y, a, b = np.asarray(y), np.asarray(a), np.asarray(b)
    out = {"recall": [], "precision": [], "f1": []}
    for _ in range(R.BOOT):
        i = rng.integers(0, len(y), len(y))
        ra, rb = M.prf(y[i].tolist(), a[i].tolist()), M.prf(y[i].tolist(), b[i].tolist())
        for k in out:
            if ra[k] is not None and rb[k] is not None:
                out[k].append(rb[k] - ra[k])
    return {k: [float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))] if v else None for k, v in out.items()}


def arm_block(rows: list[dict], ans: dict, base: dict | None) -> dict:
    import numpy as np

    y = [int(r["label"] == "defect") for r in rows]
    v = [ans[r["crop_id"]] for r in rows]
    pred = [R.vlm_flag(x) for x in v]
    lat = [x["latency_ms"] for x in v]
    out = M.prf(y, pred) | {"n": len(rows), "defect": sum(y), "ci": R.boot_ci(y, pred),
                            "cannot_tell": sum(x["state"] == "cannot_tell" for x in v),
                            "cannot_tell_share": sum(x["state"] == "cannot_tell" for x in v) / len(v),
                            "cost_usd": sum(cost_usd(x) for x in v),
                            "cost_per_crop_usd": sum(cost_usd(x) for x in v) / len(v),
                            "latency_median_ms": float(np.median(lat)), "latency_p90_ms": float(np.percentile(lat, 90)),
                            "escalated": sum(bool(x.get("escalated")) for x in v)}
    if base is not None:
        bp = [R.vlm_flag(base[r["crop_id"]]) for r in rows]
        out["vs_baseline"] = paired_boot(y, bp, pred)
    if any("severity" in x for x in v):
        # A flag judged cosmetic goes to the report's cosmetic section and never makes the verdict: count what the
        # verdict would still see, and how the flags split by severity.
        fpred = [int(p and x.get("severity") != "cosmetic") for p, x in zip(pred, v)]
        out["functional"] = M.prf(y, fpred) | {"ci": R.boot_ci(y, fpred)}
        out["severity_of_flags"] = {
            "defect": dict(collections.Counter(x.get("severity") for p, x, t in zip(pred, v, y) if p and t)),
            "ok": dict(collections.Counter(x.get("severity") for p, x, t in zip(pred, v, y) if p and not t))}
    return out


# -- whole stills: what the crop budget covers --------------------------------------------------------------------
STILL_SETS = {"dev": lambda it: it.split in ("train", "val") and it.anomalous and it.source in ("visa",
                                                                                              "pcb_defect_webcam"),
              "heldout": lambda it: it.split == "heldout" and it.anomalous}
DEV_STILLS = 160


def still_items(name: str) -> list:
    items = [it for it in D.all_items() if STILL_SETS[name](it)]
    if name == "dev":
        rng = random.Random("quickwins-dev-stills")
        rng.shuffle(items)
        items = items[:DEV_STILLS]
    return items


def _dmg_cache(it) -> Path:
    return D.WORK / "boxes_dmg" / (hashlib.sha1(it.image_id.encode()).hexdigest()[:16] + ".json")


def cmd_detect(a) -> None:
    from repairos.registry import Registry

    det = Registry().load("DET", "owlv2")
    items = still_items(a.set)
    todo = [it for it in items if not _dmg_cache(it).exists()]
    print(f"{len(items)} stills, {len(todo)} to detect", flush=True)
    _dmg_cache(items[0]).parent.mkdir(parents=True, exist_ok=True)
    same = diff = 0
    for k, it in enumerate(todo):
        im = it.load()
        dets = det.detect(im)
        _dmg_cache(it).write_text(json.dumps({"image_id": it.image_id, "det": det.model_id, "size": list(im.size),
                                              "boxes": [[list(d.box), d.label, d.score, d.damage_score]
                                                        for d in dets]}))
        old = D._box_cache(it)
        if old.exists():  # the new damage score must not change the boxes the cached M7 run used
            ob = [[b, lab, s] for b, lab, s in json.loads(old.read_text())["boxes"]]
            nb = [[list(d.box), d.label, d.score] for d in dets]
            same, diff = (same + 1, diff) if ob == nb else (same, diff + 1)
        if k % 25 == 0:
            print(k, f"boxes identical to the M7 cache: {same}, different: {diff}", flush=True)
    print(f"boxes identical to the M7 cache: {same}, different: {diff}")


def visible(region, rect) -> bool:
    """The defect region counts as seen in a crop (the same rule as the crop labels in training/datasets)."""
    area = (rect[2] - rect[0]) * (rect[3] - rect[1])
    o = region.overlap(rect)
    return o >= max(D.MIN_DEFECT_PX, D.MIN_OF_CROP * area) and \
        (o >= D.VISIBLE_FRAC * region.area or o >= D.VISIBLE_OF_CROP * area)


def strategies() -> dict[str, dict]:
    """Rankings compared on the dev stills. The held-out run uses the baseline and the one chosen on dev."""
    s = {"score": {"by": "score", "merge_iou": None}, "score+merge0.7": {"by": "score", "merge_iou": 0.7},
         "score+merge0.5": {"by": "score", "merge_iou": 0.5}}
    for bonus in (0.0, 0.02, 0.05, 0.1, 1.0):
        for m in (None, 0.7, 0.5):
            s[f"likelihood(bonus {bonus})" + (f"+merge{m}" if m else "")] = {
                "by": "likelihood", "merge_iou": m, "prior_bonus": {"high": bonus, "medium": 0.0, "low": -bonus}}
    return s


# Chosen on the dev stills before any held-out ranking run: merging at crop IoU 0.5 was the best dev variant; the
# likelihood order (damage evidence + family prior) is the research hypothesis, kept as a candidate because it tied
# on dev at budget 12 and was within noise at 4 and 8. "likelihood(bonus 0.1)" alone isolates the ranking effect.
HELDOUT_STRATEGIES = ("score", "score+merge0.5", "likelihood(bonus 0.1)", "likelihood(bonus 0.1)+merge0.5")


def cmd_rank(a) -> None:
    import numpy as np

    from repairos.slots.detector import Detection
    from repairos.vision.crops import crop_region
    from repairos.vision.inspect import MAX_BOX_FRACTION
    from repairos.vision.ranking import rank_regions

    items = [it for it in still_items(a.set) if _dmg_cache(it).exists()]
    strat = strategies()
    if a.set == "heldout":  # only the baseline and the candidates chosen on dev (declared before this run)
        strat = {k: v for k, v in strat.items() if k in HELDOUT_STRATEGIES}
    budgets = (4, 8, 12, 999)  # 999 = every candidate: what the detector boxes cover at all
    per = {k: {b: [] for b in budgets} for k in strat}   # per still: (defects seen, defects, crops used)
    src: list[str] = []
    for it in items:
        c = json.loads(_dmg_cache(it).read_text())
        w, h = c["size"]
        regions = [r for r in D.regions_of(it) if r.source_label != "ignore"]
        if not regions:
            continue
        src.append(it.source)
        dets = [Detection(tuple(b), lab, s, d) for b, lab, s, d in c["boxes"]]
        cand = [i for i, d in enumerate(dets) if (d.box[2] - d.box[0]) * (d.box[3] - d.box[1])
                <= MAX_BOX_FRACTION * w * h and d.box[2] > d.box[0] and d.box[3] > d.box[1]]
        rects = {i: crop_region(dets[i].box, w, h) for i in cand}
        for name, kw in strat.items():
            order, _ = rank_regions(dets, cand, rects, **kw)
            for b in budgets:
                look = [rects[i] for i in order[:b]]
                seen = sum(any(visible(r, x) for x in look) for r in regions)
                per[name][b].append((seen, len(regions), len(look)))
    out = {"set": a.set, "groups": {}}
    groups = {"all": [True] * len(src)} | {g: [x == g for x in src] for g in sorted(set(src))}
    for g, mask in groups.items():
        m = np.asarray(mask)
        rng = np.random.default_rng(0)
        base = {b: np.asarray(per["score"][b])[m] for b in budgets}
        block = {"stills": int(m.sum()), "defects": int(base[12][:, 1].sum()), "strategies": {}}
        for name in strat:
            row = {}
            for b in budgets:
                arr = np.asarray(per[name][b])[m]
                seen, tot = arr[:, 0], arr[:, 1]
                diffs = []
                for _ in range(R.BOOT):  # stills resampled, paired against the detector-score order
                    i = rng.integers(0, len(arr), len(arr))
                    diffs.append(seen[i].sum() / tot[i].sum() - base[b][i, 0].sum() / base[b][i, 1].sum())
                row[str(b)] = {"defects_seen": int(seen.sum()), "share": float(seen.sum() / tot.sum()),
                               "stills_with_a_defect_seen": int((seen > 0).sum()),
                               "crops_mean": float(arr[:, 2].mean()),
                               "vs_score_ci": [float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))]}
            block["strategies"][name] = row
        out["groups"][g] = block
    path = D.WORK / f"qw_rank_{a.set}.json"
    path.write_text(json.dumps(out, indent=1))
    for g, block in out["groups"].items():
        print(f"== {g}: {block['stills']} stills, {block['defects']} defects")
        for name, row in block["strategies"].items():
            print(f"{name:34s} " + "  ".join(f"@{b}: {r['defects_seen']:3d} ({100 * r['share']:.1f}%) "
                                             f"[{100 * r['vs_score_ci'][0]:+.1f},{100 * r['vs_score_ci'][1]:+.1f}] "
                                             f"crops {r['crops_mean']:.1f}" for b, r in row.items()))


# -- region floor and cap (vision.inspect.select_regions, the shipped code) ---------------------------------------
FLOOR_GRID = (0.0, 0.01, 0.0125, 0.015, 0.0175, 0.02, 0.025)
CAP_GRID = (4, 6, 8, 10, 11, 12, 16, 24)
VLM_BUDGET = 12


def _box_rel(d, w, h) -> float:
    from repairos.vision.inspect import box_side

    return box_side(d.box, w, h)


def _filter_stills(name: str) -> list[tuple]:
    from repairos.slots.detector import Detection

    out = []
    for it in still_items(name):
        if not _dmg_cache(it).exists():
            continue
        c = json.loads(_dmg_cache(it).read_text())
        regions = [r for r in D.regions_of(it) if r.source_label != "ignore"]
        if regions:
            out.append((it, c["size"][0], c["size"][1], regions,
                        [Detection(tuple(b), lab, sc, dm) for b, lab, sc, dm in c["boxes"]]))
    return out


def _coverage(stills: list[tuple], floor: float, cap: int | None) -> dict:
    """Defects inside the VLM's crops (the first VLM_BUDGET regions) and inside every kept region, per still."""
    from repairos.vision.inspect import select_regions

    per = []
    for it, w, h, regions, dets in stills:
        sel = select_regions(dets, w, h, budget=1, min_box_side=floor,   # budget 1: the cap alone sets the list
                             max_regions=cap if cap is not None else 10 ** 6)
        vlm = [sel.regions[i] for i in sel.ordered[:VLM_BUDGET]]
        kept = [sel.regions[i] for i in sel.ordered]
        per.append((sum(any(visible(r, x) for x in vlm) for r in regions),
                    sum(any(visible(r, x) for x in kept) for r in regions), len(regions), len(vlm),
                    len(sel.ordered) + len(sel.merged), sum(len(v) for v in sel.dropped.values()), it.source))
    return {"per_still": per}


def _cov_block(per: list[tuple], base: list[tuple] | None) -> dict:
    import numpy as np

    a = np.asarray([p[:6] for p in per], dtype=float)
    out = {"stills": len(per), "defects": int(a[:, 2].sum()), "seen_by_vlm": int(a[:, 0].sum()),
           "share_vlm": float(a[:, 0].sum() / a[:, 2].sum()), "seen_in_kept": int(a[:, 1].sum()),
           "share_kept": float(a[:, 1].sum() / a[:, 2].sum()), "crops_mean": float(a[:, 3].mean()),
           "regions_mean": float(a[:, 4].mean()), "regions_max": int(a[:, 4].max()),
           "dropped_mean": float(a[:, 5].mean())}
    if base is not None:
        b = np.asarray([p[:6] for p in base], dtype=float)
        rng = np.random.default_rng(0)
        diffs = []
        for _ in range(R.BOOT):
            i = rng.integers(0, len(a), len(a))
            diffs.append(a[i, 0].sum() / a[i, 2].sum() - b[i, 0].sum() / b[i, 2].sum())
        out["vlm_vs_baseline_ci"] = [float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))]
    return out


# Selection rule, declared before the held-out run: the floor is the largest FLOOR_GRID value that loses no dev
# defect, either inside the VLM's crops or inside every kept region; the cap is the smallest CAP_GRID value at which
# dev coverage of every kept region equals the uncapped coverage, rounded up to the VLM budget.

def load_still_size(image_id: str) -> tuple[int, int]:
    from repairos.vision.crops import load_still

    return load_still(D.image_path(image_id)).size


def _sample_recall(floor: float, cap: int | None, arm: str = "sonnet_cl_unknown") -> dict:
    """The shipped VLM arm's recorded answers on the 300 held-out crops; a crop whose box the floor drops, or that
    is not among the first VLM_BUDGET regions of its still, counts as not flagged. Nothing is asked again."""
    from repairos.slots.detector import Detection
    from repairos.vision.inspect import select_regions

    rows, ans = sample_rows(), load_arm(arm)
    y, pred, kept_n = [], [], 0
    cache: dict[str, tuple] = {}
    for r in rows:
        if r["image_id"] not in cache:   # the M7 detector cache the sample crops were cut from
            c = json.loads((D.WORK / "boxes" / (hashlib.sha1(r["image_id"].encode()).hexdigest()[:16] + ".json"))
                           .read_text())
            size = c.get("size") or list(load_still_size(r["image_id"]))
            cache[r["image_id"]] = (size, [Detection(tuple(b), lab, s) for b, lab, s in c["boxes"]])
        (w, h), dets = cache[r["image_id"]]
        idx = next((i for i, d in enumerate(dets) if all(abs(a - b) < 0.05 for a, b in zip(d.box, r["box"]))), None)
        sel = select_regions(dets, w, h, budget=VLM_BUDGET, min_box_side=floor,
                             max_regions=cap if cap is not None else 10 ** 6)
        vlm = set(sel.ordered[:VLM_BUDGET])
        looked = idx is not None and (idx in vlm or sel.merged.get(idx) in vlm)
        kept_n += looked
        y.append(int(r["label"] == "defect"))
        pred.append(R.vlm_flag(ans[r["crop_id"]]) if looked else 0)
    return M.prf(y, pred) | {"n": len(rows), "inspected": kept_n, "ci": R.boot_ci(y, pred), "pred": pred, "y": y}


def cmd_filter(a) -> None:
    from repairos.vision.inspect import MAX_REGIONS, MIN_BOX_SIDE

    stills = _filter_stills(a.set)
    res: dict = {"set": a.set, "vlm_budget": VLM_BUDGET, "shipped": {"min_box_side": MIN_BOX_SIDE,
                                                                     "max_regions": MAX_REGIONS}}
    base = _coverage(stills, 0.0, None)["per_still"]
    if a.set == "dev":   # the sweep the values were chosen on
        res["floor"] = {str(f): _cov_block(_coverage(stills, f, None)["per_still"], base) for f in FLOOR_GRID}
        res["cap"] = {str(k): _cov_block(_coverage(stills, 0.0, k)["per_still"], base) for k in CAP_GRID}
    groups = {"all": None} | {g: g for g in sorted({p[6] for p in base})}
    shipped = _coverage(stills, MIN_BOX_SIDE, MAX_REGIONS)["per_still"]
    res["compare"] = {g: {"baseline": _cov_block([p for p in base if src is None or p[6] == src], None),
                          "shipped": _cov_block([p for p in shipped if src is None or p[6] == src],
                                                [p for p in base if src is None or p[6] == src])}
                      for g, src in groups.items()}
    if a.set == "heldout":
        old, new = _sample_recall(0.0, None), _sample_recall(MIN_BOX_SIDE, MAX_REGIONS)
        res["sample_recall"] = {"baseline": {k: v for k, v in old.items() if k not in ("pred", "y")},
                                "shipped": {k: v for k, v in new.items() if k not in ("pred", "y")},
                                "delta_ci": paired_boot(old["y"], old["pred"], new["pred"])}
    path = HERE / "results" / f"regionfilter-{a.set}-{datetime.now(UTC).strftime('%Y-%m-%d')}.json"
    path.write_text(json.dumps(res, indent=1) + "\n")
    print(f"wrote {path}")
    for key in ("floor", "cap"):
        for k, b in (res.get(key) or {}).items():
            print(f"{key} {k:>7}: vlm {b['seen_by_vlm']}/{b['defects']} kept {b['seen_in_kept']} "
                  f"regions {b['regions_mean']:.1f} (max {b['regions_max']}) dropped {b['dropped_mean']:.1f}")
    for g, c in res["compare"].items():
        for name, b in c.items():
            ci = b.get("vlm_vs_baseline_ci")
            print(f"{g:32s} {name:9s} vlm {b['seen_by_vlm']}/{b['defects']} ({100 * b['share_vlm']:.1f}%) kept "
                  f"{b['seen_in_kept']} ({100 * b['share_kept']:.1f}%) crops {b['crops_mean']:.1f} regions "
                  f"{b['regions_mean']:.1f} max {b['regions_max']} dropped {b['dropped_mean']:.1f}"
                  + (f" d[{100 * ci[0]:+.1f},{100 * ci[1]:+.1f}]" if ci else ""))
    if "sample_recall" in res:
        sr = res["sample_recall"]
        for name in ("baseline", "shipped"):
            b = sr[name]
            print(f"sample {name}: recall {100 * b['recall']:.1f} precision {100 * b['precision']:.1f} "
                  f"inspected {b['inspected']}/{b['n']}")
        print("delta", sr["delta_ci"])


# -- report -------------------------------------------------------------------------------------------------------
def load_arm(arm: str, dev: bool = False) -> dict:
    d = arm_dir(arm, dev)
    return {json.loads(p.read_text())["crop_id"]: json.loads(p.read_text()) for p in d.glob("*.json")} \
        if d.exists() else {}


def cmd_report(a) -> None:
    rows = dev_rows(a.dev) if a.dev else sample_rows()
    base = load_arm("haiku_ladder", bool(a.dev))
    res = {"created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "sample": "dev" if a.dev else "heldout",
           "arms": {}}
    groups = {"both": rows, "pcb4": [r for r in rows if r["source"] == "visa"],
              "wikimedia": [r for r in rows if r["source"] != "visa"]}
    for arm in ARMS:
        ans = load_arm(arm, bool(a.dev))
        if not all(r["crop_id"] in ans for r in rows):
            print(f"{arm}: {sum(r['crop_id'] in ans for r in rows)}/{len(rows)} answered, skipped")
            continue
        res["arms"][arm] = {g: arm_block(rr, ans, base if base and all(r["crop_id"] in base for r in rr) else None)
                            for g, rr in groups.items() if rr}
    res["pairs"] = {}  # one change at a time, paired on the same crops
    for a_, b_, name in PAIRS:
        A_, B_ = load_arm(a_, bool(a.dev)), load_arm(b_, bool(a.dev))
        for g, rr in groups.items():
            if rr and all(r["crop_id"] in A_ and r["crop_id"] in B_ for r in rr):
                y = [int(r["label"] == "defect") for r in rr]
                pa, pb = [R.vlm_flag(A_[r["crop_id"]]) for r in rr], [R.vlm_flag(B_[r["crop_id"]]) for r in rr]
                res["pairs"][f"{name} | {g}"] = {"from": M.prf(y, pa), "to": M.prf(y, pb),
                                                  "delta_ci": paired_boot(y, pa, pb)}
    rep = load_arm("sonnet_cl_repeat", bool(a.dev))
    first = load_arm("sonnet_cl", bool(a.dev))
    rr = [r for r in rows[::REPEAT_EVERY] if r["crop_id"] in rep and r["crop_id"] in first]
    if rr:
        res["noise_floor"] = {"n": len(rr), "same_state": sum(rep[r["crop_id"]]["state"] == first[r["crop_id"]]["state"]
                                                             for r in rr),
                              "same_flag": sum(R.vlm_flag(rep[r["crop_id"]]) == R.vlm_flag(first[r["crop_id"]])
                                               for r in rr)}
    for name in ("dev", "heldout"):
        p = D.WORK / f"qw_rank_{name}.json"
        if p.exists():
            res[f"rank_{name}"] = json.loads(p.read_text())
    if not a.dev:
        out = HERE / "results" / f"quickwins-{datetime.now(UTC).strftime('%Y-%m-%d')}.json"
        out.write_text(json.dumps(res, indent=1) + "\n")
        print(f"wrote {out}")
    print(tables(res))


def _p(v) -> str:
    return "n/a" if v is None else f"{100 * v:.1f}"


def _c(ci) -> str:
    return f" [{100 * ci[0]:.0f}-{100 * ci[1]:.0f}]" if ci else ""


def _d(ci) -> str:
    return f"[{100 * ci[0]:+.1f}, {100 * ci[1]:+.1f}]" if ci else "n/a"


def tables(res: dict) -> str:
    L = ["| Arm | Sample | Precision [CI] | Recall [CI] | F1 | Δ recall vs baseline [CI] | Δ precision [CI] | "
         "cannot_tell | USD/crop | median / p90 s |", "|---|---|---|---|---|---|---|---|---|---|"]
    for arm, gs in res["arms"].items():
        for g, b in gs.items():
            vs = b.get("vs_baseline") or {}
            L.append(f"| {arm} | {g} ({b['n']}, {b['defect']} defect) | {_p(b['precision'])}{_c(b['ci']['precision'])} "
                     f"| {_p(b['recall'])}{_c(b['ci']['recall'])} | {_p(b['f1'])} | {_d(vs.get('recall'))} | "
                     f"{_d(vs.get('precision'))} | {b['cannot_tell']} ({_p(b['cannot_tell_share'])}%) | "
                     f"{b['cost_per_crop_usd']:.4f} | {b['latency_median_ms'] / 1000:.1f} / "
                     f"{b['latency_p90_ms'] / 1000:.1f} |")
    sev = [(arm, g, b) for arm, gs in res["arms"].items() for g, b in gs.items() if "functional" in b]
    if sev:
        L += ["", "| Arm | Sample | Functional precision [CI] | Functional recall [CI] | Flags on defects by severity | "
              "Flags on ok crops by severity |", "|---|---|---|---|---|---|"]
        for arm, g, b in sev:
            f = b["functional"]
            L.append(f"| {arm} | {g} | {_p(f['precision'])}{_c(f['ci']['precision'])} | "
                     f"{_p(f['recall'])}{_c(f['ci']['recall'])} | {b['severity_of_flags']['defect']} | "
                     f"{b['severity_of_flags']['ok']} |")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["vlm", "detect", "rank", "filter", "report"])
    ap.add_argument("--arm", choices=list(ARMS), default="sonnet_cl")
    ap.add_argument("--dev", type=int, default=0, help="use N non-held-out crops instead of the held-out sample")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only-differing", action="store_true")
    ap.add_argument("--set", choices=list(STILL_SETS), default="dev")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args(argv)
    {"vlm": cmd_vlm, "detect": cmd_detect, "rank": cmd_rank, "filter": cmd_filter, "report": cmd_report}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
