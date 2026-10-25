"""Defect benchmark (task M7): CLS=dinov2 vs the Claude VLM vs the two combined, on crops never trained on.

    PY=backend/.venv/bin/python
    $PY eval/defects/run.py classify   # head on every test/held-out crop (cached DINOv2 features)
    $PY eval/defects/run.py sample     # pick the VLM sample (<= VLM_CAP crops, seeded, held-out only)
    $PY eval/defects/run.py vlm        # VLM=anthropic on the sample, as inspect.py asks it (crop + context)
    $PY eval/defects/run.py report     # eval/defects/results/<date>.json + REPORT.md tables

Splits: `test` (same sources as training, unseen boards / capture days), `heldout` (VisA pcb4: another board design
with defect kinds never trained on; Wikimedia defective-capacitor photos). Crops are runtime crops: `det` = a
detector box cut by `vision.crops.cut`, `gt` = a crop centred on an annotated defect.
Answers per crop are kept under data/datasets/work/ (VLM answers are recorded real answers, reusable).
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "training" / "datasets"))
sys.path.insert(0, str(HERE))

import defects as D  # noqa: E402
import metrics as M  # noqa: E402

SCORES = D.WORK / "scores.jsonl"
SAMPLE = D.WORK / "vlm_sample.json"
VLM_DIR = D.WORK / "vlm"
VLM_CAP = 300
SAMPLE_PLAN = {("visa", "defect"): 100, ("visa", "ok"): 100,
               ("wikimedia_defective_capacitors", "defect"): 60, ("wikimedia_defective_capacitors", "ok"): 40}


def cmd_classify(a) -> None:
    import numpy as np

    from repairos.slots.cls_dinov2 import DEFAULT_HEAD_DIR, Head
    from repairos.vision.generic_classes import DEFECT_CLASSES

    sys.path.insert(0, str(ROOT / "training" / "defects"))
    import train as T

    head = Head(DEFAULT_HEAD_DIR)
    rows, x = T.load_xy(head.resolution, head.pools)
    keep = [i for i, r in enumerate(rows) if r["split"] in ("test", "heldout", "val")]
    p_def, p_kind = head.predict(x[keep])
    odd = head.unfamiliar(x[keep])
    with SCORES.open("w") as f:
        for j, i in enumerate(keep):
            k = int(p_kind[j].argmax())
            f.write(json.dumps({"crop_id": rows[i]["crop_id"], "p_defect": float(p_def[j]),
                                "kind": DEFECT_CLASSES[k], "p_kind": float(p_kind[j][k]),
                                "abstain": bool(odd[j])}) + "\n")
    print(f"scored {len(keep)} crops with head {head.meta['head_id']}")


def cmd_sample(a) -> None:
    rows = [r for r in D.load_crops() if r["split"] == "heldout" and r["kind"] == "det"]
    rng = random.Random("vlm-sample")
    picked = []
    for (src, lab), n in SAMPLE_PLAN.items():
        pool = [r for r in rows if r["source"] == src and r["label"] == lab]
        rng.shuffle(pool)
        picked += pool[:n]
    picked = picked[:VLM_CAP]
    SAMPLE.write_text(json.dumps([r["crop_id"] for r in picked], indent=0))
    print(len(picked), collections.Counter((r["source"], r["label"]) for r in picked))


def cmd_vlm(a) -> None:
    from repairos.registry import Registry
    from repairos.vision.crops import context_view, load_still

    judge = Registry().load("VLM", "anthropic")
    out_dir = VLM_DIR
    ids = json.loads(SAMPLE.read_text())
    if a.model:  # one model only, no escalation (latency and accuracy of that model), on the first `limit` crops
        judge.models = (a.model,)
        out_dir = D.WORK / f"vlm_{a.model}"
        ids = ids[:: max(1, len(ids) // a.limit)][: a.limit] if a.limit else ids  # spread over the groups
    judge.record_dir = out_dir / "raw"
    ids = set(ids)
    rows = [r for r in D.load_crops() if r["crop_id"] in ids]
    out_dir.mkdir(parents=True, exist_ok=True)
    todo = [r for r in rows if not (out_dir / f"{r['crop_id']}.json").exists()]
    print(f"{len(rows)} sampled, {len(todo)} to ask", flush=True)

    def ask(r: dict) -> None:
        im = load_still(D.image_path(r["image_id"]))
        crop = D.cut_row(r)
        ctx = context_view(im, tuple(r["box"]))
        t = time.perf_counter()
        s = judge.inspect_crop(crop.image, ctx, part_hint=r["det_label"])
        (out_dir / f"{r['crop_id']}.json").write_text(json.dumps({
            "crop_id": r["crop_id"], "state": s.state, "part_class": s.part_class, "evidence": s.evidence_text,
            "confidence": s.confidence_word, "model": s.model, "escalated": s.escalated,
            "latency_ms": s.latency_ms, "wall_s": round(time.perf_counter() - t, 2),
            "usage": [x.get("usage") for x in s.attempts], "label": "recorded real VLM answer"}))

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        for k, _ in enumerate(pool.map(ask, todo)):
            if k % 25 == 0:
                print(k, flush=True)


# -- report -------------------------------------------------------------------------------------------------------
def _fmt(v, pct=True) -> str:
    if v is None:
        return "n/a"
    return f"{100 * v:.1f}" if pct else f"{v:.3f}"


def classifier_block(rows: list[dict], sc: dict, head_meta: dict) -> dict:
    ok_below, flag_at = head_meta["thresholds"]["ok_below"], head_meta["thresholds"]["flag_at"]
    y = [int(r["label"] == "defect") for r in rows]
    p = [sc[r["crop_id"]]["p_defect"] for r in rows]
    ab = [sc[r["crop_id"]]["abstain"] for r in rows]
    pr = M.prf(y, [int(q >= flag_at) for q in p])  # the raw classifier, abstention ignored
    answered = [i for i, a in enumerate(ab) if not a]
    pa = M.prf([y[i] for i in answered], [int(p[i] >= flag_at) for i in answered])
    routed_ok = [q < ok_below and not a for q, a in zip(p, ab)]
    missed = sum(1 for t, o in zip(y, routed_ok) if t and o)
    return {"n": len(rows), "defect": sum(y), "auroc": M.auroc(y, p), "ece": M.ece(y, p), **pr,
            "abstain_share": sum(ab) / len(rows) if rows else None,
            "answered": {"n": len(answered), "auroc": M.auroc([y[i] for i in answered], [p[i] for i in answered]),
                         "precision": pa["precision"], "recall": pa["recall"]},
            "routed_ok_share": sum(routed_ok) / len(rows) if rows else None,
            "defects_routed_ok": missed, "defects_routed_ok_share": missed / sum(y) if sum(y) else None}


def kind_block(rows: list[dict], sc: dict) -> dict:
    from repairos.vision.generic_classes import DEFECT_CLASSES

    rows = [r for r in rows if r["label"] == "defect" and r["dtype"]]
    return M.per_class([r["dtype"] for r in rows], [sc[r["crop_id"]]["kind"] for r in rows], DEFECT_CLASSES) | \
        {"n": len(rows)}


def vlm_flag(v: dict) -> int:
    return int(v["state"] not in ("ok", "cannot_tell"))


# Claude API list prices, USD per million tokens (claude-api skill table, cached 2026-10-06)
PRICE = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00)}
BOOT = 1000


def cost_usd(v: dict) -> float:
    """Cost of one recorded VLM answer: every attempt (Haiku, then Sonnet on escalation) at list price."""
    models = [v["model"]] if not v.get("escalated") else ["claude-haiku-5-5", v["model"]]
    total = 0.0
    for m, u in zip(models, v.get("usage") or []):
        pi, po = PRICE[m]
        total += (u["input_tokens"] * pi + u["output_tokens"] * po) / 1e6
    return total


def boot_ci(y: list[int], pred: list[int], score: list[float] | None = None, seed: int = 0) -> dict:
    """95 % percentile bootstrap intervals (crops resampled with replacement) for precision, recall, AUROC."""
    import numpy as np

    rng = np.random.default_rng(seed)
    y, pred = np.asarray(y), np.asarray(pred)
    sc = np.asarray(score) if score is not None else None
    out = {"precision": [], "recall": [], "auroc": []}
    for _ in range(BOOT):
        i = rng.integers(0, len(y), len(y))
        r = M.prf(y[i].tolist(), pred[i].tolist())
        for k in ("precision", "recall"):
            if r[k] is not None:
                out[k].append(r[k])
        if sc is not None:
            a = M.auroc(y[i], sc[i])
            if a is not None:
                out["auroc"].append(a)
    return {k: [float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))] if v else None for k, v in out.items()}


def systems(rows: list[dict], sc: dict, vlm: dict, meta: dict) -> dict:
    """Classifier alone, VLM alone (Haiku, Sonnet on cannot_tell), classifier -> VLM routing (the inspect.py
    policy), and 'either flags' on the same crops, with bootstrap intervals."""
    ok_below, flag_at = meta["thresholds"]["ok_below"], meta["thresholds"]["flag_at"]
    y = [int(r["label"] == "defect") for r in rows]
    p = [sc[r["crop_id"]]["p_defect"] for r in rows]
    odd = [sc[r["crop_id"]]["abstain"] for r in rows]
    v = [vlm[r["crop_id"]] for r in rows]
    cls = [int(q >= flag_at) for q in p]
    vl = [vlm_flag(x) for x in v]
    to_vlm = [q >= ok_below or o for q, o in zip(p, odd)]
    combined = [f if t else 0 for t, f in zip(to_vlm, vl)]
    either = [f if o else int(c or f) for c, f, o in zip(cls, vl, odd)]
    ct = [x["state"] == "cannot_tell" for x in v]
    out = {"n": len(rows), "defect": sum(y), "classifier_abstained": sum(odd)}
    every = [True] * len(rows)
    for name, pred, score, calls in (("classifier", cls, p, [False] * len(rows)), ("vlm", vl, None, every),
                                     ("combined", combined, None, to_vlm), ("either", either, None, every)):
        out[name] = M.prf(y, pred) | {"ci": boot_ci(y, pred, score), "vlm_calls": sum(calls),
                                      "cannot_tell": sum(c for c, k in zip(ct, calls) if k),
                                      "cost_usd": sum(cost_usd(x) for x, k in zip(v, calls) if k)}
        if score is not None:
            out[name]["auroc"] = M.auroc(y, score)
    return out


def latency(vlm: dict, sonnet: dict) -> dict:
    import numpy as np

    def stats(xs):
        xs = sorted(xs)
        return {"n": len(xs), "median_ms": float(np.median(xs)) if xs else None,
                "p90_ms": float(np.percentile(xs, 90)) if xs else None}

    haiku = [v["latency_ms"] for v in vlm.values() if not v["escalated"]]
    ladder = [v["latency_ms"] for v in vlm.values()]
    cost = [cost_usd(v) for v in vlm.values()]
    sc = [cost_usd(v | {"escalated": False}) for v in sonnet.values()]
    return {"haiku_only": stats(haiku), "haiku_then_sonnet_ladder": stats(ladder),
            "sonnet_only": stats([v["latency_ms"] for v in sonnet.values()]),
            "escalated_share": sum(v["escalated"] for v in vlm.values()) / len(vlm) if vlm else None,
            "cost_per_crop_ladder_usd": sum(cost) / len(cost) if cost else None,
            "cost_per_crop_sonnet_usd": sum(sc) / len(sc) if sc else None}


def classifier_latency(n: int = 64) -> dict:
    """Wall time of CLS=dinov2 on real held-out crops (MPS on this Mac), per crop, after the backbone is loaded."""
    import time as _t

    from repairos.registry import Registry

    cls = Registry().load("CLS", "dinov2")
    rows = [r for r in D.load_crops() if r["split"] == "heldout"][:n]
    cache: dict = {}
    crops = [D.cut_row(r, cache).image for r in rows]
    cls.classify(crops[:4])  # warm-up: weights, MPS kernels
    t = _t.perf_counter()
    cls.classify(crops)
    batch = (_t.perf_counter() - t) / len(crops) * 1000
    t = _t.perf_counter()
    for c in crops[:16]:
        cls.classify([c])
    single = (_t.perf_counter() - t) / 16 * 1000
    return {"device": cls.backbone._device, "per_crop_batched_ms": batch, "per_crop_one_at_a_time_ms": single,
            "backbone_load_s": cls.backbone.load_s}


def regions_per_still(image_prefixes: tuple[str, ...], max_crops: int = 12, max_classified: int = 48) -> dict:
    """Detector regions per still that inspect.py would look at (boxes under half the still), and how many get
    a model's look under each strategy."""
    import numpy as np

    from repairos.vision.inspect import MAX_BOX_FRACTION

    cand = []
    for f in (D.WORK / "boxes").glob("*.json"):
        c = json.loads(f.read_text())
        if not c["image_id"].startswith(image_prefixes):
            continue
        w, h = c["size"]
        cand.append(sum(1 for b, _, _ in c["boxes"] if (b[2] - b[0]) * (b[3] - b[1]) <= MAX_BOX_FRACTION * w * h))
    cand = np.asarray(cand)
    return {"stills": len(cand), "candidates_median": float(np.median(cand)),
            "candidates_p90": float(np.percentile(cand, 90)),
            "vlm_only_inspected_mean": float(np.minimum(cand, max_crops).mean()),
            "with_classifier_inspected_mean": float(np.minimum(cand, max_classified).mean()),
            "vlm_only_not_inspected_mean": float(np.maximum(cand - max_crops, 0).mean()),
            "with_classifier_not_inspected_mean": float(np.maximum(cand - max_classified, 0).mean())}


def _load_dir(d: Path) -> dict:
    return {json.loads(p.read_text())["crop_id"]: json.loads(p.read_text()) for p in d.glob("*.json")}


def cmd_report(a) -> None:
    from repairos.slots.cls_dinov2 import DEFAULT_HEAD_DIR

    meta = json.loads((DEFAULT_HEAD_DIR / "head.json").read_text())
    rows = D.load_crops()
    sc = {json.loads(line)["crop_id"]: json.loads(line) for line in SCORES.open()}
    sample = set(json.loads(SAMPLE.read_text()))
    vlm = {k: v for k, v in _load_dir(VLM_DIR).items() if k in sample}
    sonnet = _load_dir(D.WORK / "vlm_claude-sonnet-5-5")
    groups = {
        "val (threshold split)": [r for r in rows if r["split"] == "val"],
        "test, all sources": [r for r in rows if r["split"] == "test"],
        "test, VisA pcb1-3": [r for r in rows if r["split"] == "test" and r["source"] == "visa"],
        "test, webcam close-ups": [r for r in rows if r["split"] == "test" and r["source"] == "pcb_defect_webcam"],
        "test, component boards (ok only)": [r for r in rows if r["split"] == "test"
                                             and r["source"] == "rf100_printed_circuit_board"],
        "held-out VisA pcb4, detector crops": [r for r in rows if r["split"] == "heldout" and r["source"] == "visa"
                                               and r["kind"] == "det"],
        "held-out VisA pcb4, defect-centred + same place on normal boards":
            [r for r in rows if r["split"] == "heldout" and r["source"] == "visa" and r["kind"] != "det"],
        "held-out Wikimedia photos, detector crops":
            [r for r in rows if r["split"] == "heldout" and r["source"] == "wikimedia_defective_capacitors"
             and r["kind"] == "det"],
        "held-out Wikimedia photos, defect-centred crops":
            [r for r in rows if r["split"] == "heldout" and r["source"] == "wikimedia_defective_capacitors"
             and r["kind"] != "det"],
    }
    sampled = [r for r in rows if r["crop_id"] in vlm and r["crop_id"] in sc]
    cmp = {
        "held-out VisA pcb4": [r for r in sampled if r["source"] == "visa"],
        "held-out Wikimedia photos": [r for r in sampled if r["source"] != "visa"],
        "held-out, both": sampled,
        "held-out, classifier in distribution": [r for r in sampled if not sc[r["crop_id"]]["abstain"]],
        "held-out, classifier abstained (unfamiliar)": [r for r in sampled if sc[r["crop_id"]]["abstain"]],
    }
    res = {"head": {k: meta[k] for k in ("head_id", "backbone", "resolution", "pools", "thresholds", "calibration",
                                         "ood", "defect_head", "kind_head", "train_seconds")},
           "classifier": {k: classifier_block([r for r in g if r["crop_id"] in sc], sc, meta)
                          for k, g in groups.items() if g},
           "kinds": {k: kind_block([r for r in g if r["crop_id"] in sc], sc)
                     for k, g in groups.items() if k.startswith(("test, all", "held-out VisA pcb4, defect"))},
           "compare": {k: systems(g, sc, vlm, meta) for k, g in cmp.items() if g},
           "latency": latency(vlm, sonnet) | {"classifier": classifier_latency()},
           "regions_per_still": {"held-out VisA pcb4": regions_per_still(("visa/pcb4/",)),
                                 "held-out Wikimedia photos": regions_per_still(("wikimedia/",)),
                                 "webcam close-ups": regions_per_still(("kerem/",))},
           "sonnet_only_subset": sonnet_subset(sonnet, rows, sc, vlm),
           "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    out = HERE / "results" / f"{datetime.now(UTC).strftime('%Y-%m-%d')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(res, indent=1) + "\n")
    print(tables(res))
    print(f"\nwrote {out}")


def sonnet_subset(sonnet: dict, rows: list[dict], sc: dict, vlm: dict) -> dict:
    """Sonnet alone vs the Haiku ladder on the same evenly spread subset of the VLM sample."""
    by = {r["crop_id"]: r for r in rows}
    ids = [k for k in sonnet if k in vlm]
    y = [int(by[k]["label"] == "defect") for k in ids]
    return {"n": len(ids), "defect": sum(y), "sonnet": M.prf(y, [vlm_flag(sonnet[k]) for k in ids]),
            "haiku_ladder": M.prf(y, [vlm_flag(vlm[k]) for k in ids])}


def _ci(c, k) -> str:
    v = (c or {}).get(k)
    return f" [{100 * v[0]:.0f}-{100 * v[1]:.0f}]" if v else ""


def tables(res: dict) -> str:
    L = ["| Split | Crops | Defect | AUROC | ECE | Precision | Recall | Abstains (unfamiliar) | AUROC answered | "
         "Routed ok (no VLM) | Defects routed ok |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, b in res["classifier"].items():
        L.append(f"| {k} | {b['n']} | {b['defect']} | {_fmt(b['auroc'], False)} | {_fmt(b['ece'], False)} | "
                 f"{_fmt(b['precision'])} | {_fmt(b['recall'])} | {_fmt(b['abstain_share'])} | "
                 f"{_fmt(b['answered']['auroc'], False)} | {_fmt(b['routed_ok_share'])} | "
                 f"{b['defects_routed_ok']} ({_fmt(b['defects_routed_ok_share'])}) |")
    L += ["", "| Held-out VLM sample | System | Precision [95% CI] | Recall [95% CI] | F1 | Specificity | AUROC | "
          "VLM calls | cannot_tell | API cost USD |", "|---|---|---|---|---|---|---|---|---|---|"]
    for k, b in res["compare"].items():
        for name in ("classifier", "vlm", "combined", "either"):
            s = b[name]
            L.append(f"| {k} ({b['n']} crops, {b['defect']} defect, {b['classifier_abstained']} abstained) | {name} | "
                     f"{_fmt(s['precision'])}{_ci(s['ci'], 'precision')} | {_fmt(s['recall'])}{_ci(s['ci'], 'recall')} "
                     f"| {_fmt(s['f1'])} | {_fmt(s['specificity'])} | "
                     f"{_fmt(s.get('auroc'), False)}{_ci(s['ci'], 'auroc') if s.get('auroc') else ''} | "
                     f"{s['vlm_calls']} | {s['cannot_tell']} | {s['cost_usd']:.3f} |")
    first = next(iter(res["kinds"].values()))["classes"]
    L += ["", "| Kinds | Crops | Accuracy | " + " | ".join("P/R " + c for c in first) + " |",
          "|---|---|---|" + "---|" * len(first)]
    for k, b in res["kinds"].items():
        cells = [f"{_fmt(c['precision'])}/{_fmt(c['recall'])} (n={c['support']})" for c in b["classes"].values()]
        L.append(f"| {k} | {b['n']} | {_fmt(b['accuracy'])} | " + " | ".join(cells) + " |")
    L += ["", "```", json.dumps({"latency": res["latency"], "regions_per_still": res["regions_per_still"],
                                 "sonnet_only_subset": res["sonnet_only_subset"]}, indent=1), "```"]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["classify", "sample", "vlm", "report"])
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--model", default=None, help="vlm: ask only this model (no escalation), answers kept apart")
    ap.add_argument("--limit", type=int, default=None, help="vlm --model: N sampled crops, evenly spread")
    a = ap.parse_args(argv)
    {"classify": cmd_classify, "sample": cmd_sample, "vlm": cmd_vlm, "report": cmd_report}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
