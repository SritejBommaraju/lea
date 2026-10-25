"""Train the CLS=dinov2 head (task M7): frozen DINOv2 features -> defect head + kind head, calibrated on val.

    PY=backend/.venv/bin/python
    $PY training/defects/train.py features   # DINOv2 features for every crop in data/datasets/work/crops.jsonl
    $PY training/defects/train.py train      # heads -> training/models/cls_dinov2/{head.npz, head.json}
    $PY training/defects/train.py all

Only the `train` split is fitted; `val` picks the head variant, fits the calibration (Platt for the defect head,
temperature for the kind head) and the routing thresholds. `test` and `heldout` are never looked at here: the
benchmark (eval/defects/run.py) scores them.

Routing thresholds (chosen on val, calibrated probabilities):
- `ok_below`: the largest threshold under which at most MISS_BUDGET of val defect crops fall (those would skip the
  VLM as `ok`);
- `flag_at`: 0.5 of the calibrated probability.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "training" / "datasets"))
sys.path.insert(0, str(ROOT / "eval" / "defects"))

import defects as D  # noqa: E402
import metrics as M  # noqa: E402

ALL_POOLS = ("cls", "mean", "max")   # cached per crop; a head uses a subset
HIDDEN = 768                         # dinov2-base width

OUT = ROOT / "training" / "models" / "cls_dinov2"
MISS_BUDGET = 0.02
SEED = 0


def features_path(resolution: int) -> Path:
    return D.WORK / f"features_{resolution}.npz"


def cmd_features(a) -> None:
    import numpy as np

    from repairos.slots.cls_dinov2 import Backbone

    rows = D.load_crops()
    path = features_path(a.resolution)
    have = {}
    if path.exists():
        with np.load(path, allow_pickle=False) as z:
            have = dict(zip(z["crop_id"].tolist(), z["x"]))
    todo = [r for r in rows if r["crop_id"] not in have]
    todo.sort(key=lambda r: r["image_id"])
    print(f"{len(rows)} crops, {len(todo)} to embed at {a.resolution} px", flush=True)
    if have and len(next(iter(have.values()))) != len(ALL_POOLS) * HIDDEN:
        have = {}  # cache from another pooling layout: rebuild
        todo = sorted(rows, key=lambda r: r["image_id"])
    bb = Backbone(batch_size=32)
    cache: dict = {}
    t = time.perf_counter()
    batch, ids = [], []

    def flush():
        if batch:
            for cid, f in zip(ids, bb.embed(batch, a.resolution, ALL_POOLS)):
                have[cid] = f
            batch.clear()
            ids.clear()

    for k, r in enumerate(todo):
        batch.append(D.cut_row(r, cache).image)
        ids.append(r["crop_id"])
        if len(batch) == 64:
            flush()
        if k % 2000 == 0:
            print(f"{k}/{len(todo)} {time.perf_counter() - t:.0f}s", flush=True)
    flush()
    keys = sorted(have)
    np.savez(path, crop_id=np.asarray(keys), x=np.stack([have[k] for k in keys]).astype(np.float32))
    print(f"embedded {len(todo)} crops in {time.perf_counter() - t:.0f}s (backbone load {bb.load_s:.1f}s)")


def load_xy(resolution: int, pools=("cls", "mean")):
    import numpy as np

    rows = D.load_crops()
    with np.load(features_path(resolution), allow_pickle=False) as z:
        idx = {c: i for i, c in enumerate(z["crop_id"].tolist())}
        x = z["x"]
    cols = np.concatenate([np.arange(ALL_POOLS.index(p) * HIDDEN, (ALL_POOLS.index(p) + 1) * HIDDEN) for p in pools])
    rows = [r for r in rows if r["crop_id"] in idx]
    return rows, x[[idx[r["crop_id"]] for r in rows]][:, cols]


# -- heads (torch, CPU: features are small) -----------------------------------------------------------------------
def fit(x, y, n_out: int, hidden: int, weight_decay: float, epochs: int = 300, class_weight=None):
    import torch

    torch.manual_seed(SEED)
    xt = torch.from_numpy(x)
    yt = torch.from_numpy(y)
    d = x.shape[1]
    if hidden:
        net = torch.nn.Sequential(torch.nn.Dropout(0.2), torch.nn.Linear(d, hidden), torch.nn.ReLU(),
                                  torch.nn.Dropout(0.2), torch.nn.Linear(hidden, n_out))
    else:
        net = torch.nn.Sequential(torch.nn.Dropout(0.2), torch.nn.Linear(d, n_out))
    opt = torch.optim.AdamW(net.parameters(), lr=1e-3, weight_decay=weight_decay)
    cw = torch.as_tensor(class_weight, dtype=torch.float32) if class_weight is not None else None
    loss_fn = (torch.nn.BCEWithLogitsLoss(pos_weight=cw) if n_out == 1 else torch.nn.CrossEntropyLoss(weight=cw))
    g = torch.Generator().manual_seed(SEED)
    for _ in range(epochs):
        net.train()
        perm = torch.randperm(len(xt), generator=g)
        for i in range(0, len(xt), 256):
            b = perm[i:i + 256]
            out = net(xt[b])
            loss = loss_fn(out[:, 0], yt[b].float()) if n_out == 1 else loss_fn(out, yt[b])
            opt.zero_grad()
            loss.backward()
            opt.step()
    net.eval()
    return net


def weights_of(net, part: str) -> dict:
    lin = [m for m in net if hasattr(m, "weight")]
    w = {}
    if len(lin) == 2:
        w[f"{part}_w1"] = lin[0].weight.detach().numpy().T.copy()
        w[f"{part}_b1"] = lin[0].bias.detach().numpy().copy()
    w[f"{part}_w"] = lin[-1].weight.detach().numpy().T.copy()
    w[f"{part}_b"] = lin[-1].bias.detach().numpy().copy()
    return w


def logits(net, x):
    import torch

    with torch.inference_mode():
        return net(torch.from_numpy(x)).numpy()


def platt(z, y):
    """(a, b) minimising the log loss of sigmoid(a z + b) on val (Newton steps)."""
    import numpy as np

    a, b = 1.0, 0.0
    for _ in range(100):
        p = 1 / (1 + np.exp(-(a * z + b)))
        g = np.array([((p - y) * z).sum(), (p - y).sum()])
        w = p * (1 - p) + 1e-9
        h = np.array([[(w * z * z).sum(), (w * z).sum()], [(w * z).sum(), w.sum()]]) + 1e-6 * np.eye(2)
        step = np.linalg.solve(h, g)
        a, b = a - step[0], b - step[1]
        if np.abs(step).max() < 1e-8:
            break
    return float(a), float(b)


def temperature(z, y):
    import numpy as np

    best = (1e9, 1.0)
    for t in np.exp(np.linspace(np.log(0.2), np.log(10), 200)):
        q = z / t
        q = q - q.max(axis=1, keepdims=True)
        nll = -(q[np.arange(len(y)), y] - np.log(np.exp(q).sum(axis=1))).mean()
        best = min(best, (float(nll), float(t)))
    return best[1]


OOD_COMPONENTS = 64      # principal components of the standardised train features
OOD_VAL_QUANTILE = 0.99  # cutoff: 99th percentile of the val distance (about 1 % of in-distribution crops abstain)


def ood_fit(xs_train):
    """PCA of the standardised train features: (components (D, k), variances (k,), residual variance)."""
    import numpy as np

    c = xs_train - xs_train.mean(0)
    _, sv, vt = np.linalg.svd(c, full_matrices=False)
    var = sv ** 2 / (len(c) - 1)
    k = OOD_COMPONENTS
    return vt[:k].T.astype(np.float32), var[:k].astype(np.float32), float(var[k:].mean()), xs_train.mean(0)


def ood_distance(xs, comps, var, resid_var, centre):
    """Squared Mahalanobis distance in the top components plus the residual outside them, per dimension."""
    import numpy as np

    c = xs - centre
    z = c @ comps
    resid = (c * c).sum(1) - (z * z).sum(1)
    return ((z * z) / var).sum(1) / len(var) + resid / resid_var / (c.shape[1] - len(var))


def log_loss(y, p) -> float:
    import numpy as np

    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def cmd_train(a) -> None:
    import numpy as np

    from repairos.slots.cls_dinov2 import MODEL_REPO, MODEL_REVISION
    from repairos.vision.generic_classes import DEFECT_CLASSES

    t0 = time.perf_counter()
    pools = tuple(a.pools.split(","))
    rows, x = load_xy(a.resolution, pools)
    split = np.asarray([r["split"] for r in rows])
    y = np.asarray([r["label"] == "defect" for r in rows], dtype=np.int64)
    tr, va = split == "train", split == "val"
    mean, std = x[tr].mean(0), x[tr].std(0) + 1e-6
    xs = ((x - mean) / std).astype(np.float32)

    # defect head: pick the variant on val log loss after calibration
    pos_w = (y[tr] == 0).sum() / max(1, (y[tr] == 1).sum())
    trials = []
    for hidden in (0, 256):
        for wd in (1e-4, 1e-2, 1e-1):
            net = fit(xs[tr], y[tr], 1, hidden, wd, epochs=a.epochs, class_weight=[pos_w])
            zv = logits(net, xs[va])[:, 0]
            ab = platt(zv, y[va])
            pv = 1 / (1 + np.exp(-(ab[0] * zv + ab[1])))
            trials.append({"hidden": hidden, "weight_decay": wd, "val_log_loss": log_loss(y[va], pv),
                           "val_auroc": M.auroc(y[va], pv), "net": net, "platt": ab, "pv": pv})
            print({k: v for k, v in trials[-1].items() if k not in ("net", "pv")}, flush=True)
    best = min(trials, key=lambda t: t["val_log_loss"])
    net_d, ab, pv = best["net"], best["platt"], best["pv"]

    # kind head: defect crops with exactly one known kind
    kind = np.asarray([DEFECT_CLASSES.index(r["dtype"]) if r["label"] == "defect" and r["dtype"] else -1
                       for r in rows])
    ktr, kva = tr & (kind >= 0), va & (kind >= 0)
    counts = np.bincount(kind[ktr], minlength=len(DEFECT_CLASSES))
    cw = np.where(counts > 0, counts.sum() / np.maximum(counts, 1) / len(DEFECT_CLASSES), 0.0)
    ktrials = []
    for hidden in (0, 256):
        for wd in (1e-3, 1e-1):
            net = fit(xs[ktr], kind[ktr], len(DEFECT_CLASSES), hidden, wd, epochs=a.epochs, class_weight=cw)
            zk = logits(net, xs[kva])
            acc = float((zk.argmax(1) == kind[kva]).mean())
            ktrials.append({"hidden": hidden, "weight_decay": wd, "val_accuracy": acc, "net": net})
            print({k: v for k, v in ktrials[-1].items() if k != "net"}, flush=True)
    kbest = max(ktrials, key=lambda t: t["val_accuracy"])
    net_k = kbest["net"]
    temp = temperature(logits(net_k, xs[kva]), kind[kva])

    # thresholds on val
    pdef = np.sort(pv[y[va] == 1])
    ok_below = float(pdef[int(np.floor(MISS_BUDGET * len(pdef)))]) if len(pdef) else 0.0
    comps, var, resid_var, centre = ood_fit(xs[tr])
    d_val = ood_distance(xs[va], comps, var, resid_var, centre)
    ood_cut = float(np.quantile(d_val, OOD_VAL_QUANTILE))
    flag_at = 0.5
    train_s = time.perf_counter() - t0

    OUT.mkdir(parents=True, exist_ok=True)
    w = {"mean": mean.astype(np.float32), "std": std.astype(np.float32)} | weights_of(net_d, "defect") | \
        weights_of(net_k, "kind") | {"ood_components": comps, "ood_var": var, "ood_centre": centre.astype(np.float32)}
    np.savez(OUT / "head.npz", **w)
    manifest = (ROOT / "training" / "datasets" / "MANIFEST.json").read_bytes()
    head_id = hashlib.sha1((OUT / "head.npz").read_bytes()).hexdigest()[:10]
    counts_split = {s: {"crops": int((split == s).sum()), "defect": int(((split == s) & (y == 1)).sum())}
                    for s in ("train", "val")}
    meta = {
        "head_id": head_id,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "backbone": {"repo": MODEL_REPO, "revision": MODEL_REVISION, "licence": "Apache-2.0", "frozen": True},
        "features": "concat of the chosen pools of the last layer: [CLS], mean and/or max of patch tokens",
        "resolution": a.resolution,
        "pools": list(pools),
        "classes": list(DEFECT_CLASSES),
        "defect_head": {"hidden": best["hidden"], "weight_decay": best["weight_decay"],
                        "val_auroc": best["val_auroc"], "val_log_loss": best["val_log_loss"],
                        "val_ece": M.ece(y[va], pv)},
        "kind_head": {"hidden": kbest["hidden"], "weight_decay": kbest["weight_decay"],
                      "val_accuracy": kbest["val_accuracy"], "train_counts": dict(zip(DEFECT_CLASSES,
                                                                                       counts.tolist()))},
        "calibration": {"defect_platt": list(ab), "kind_temperature": temp, "fitted_on": "val"},
        "ood": {"components": OOD_COMPONENTS, "residual_var": resid_var, "cutoff": ood_cut,
                "rule": f"abstain when the distance to the train features exceeds the {OOD_VAL_QUANTILE:.0%} "
                        "quantile of the val distances (Mahalanobis in the top principal components of the "
                        "standardised train features plus the residual outside them)",
                "val_abstain_share": float((d_val > ood_cut).mean())},
        "thresholds": {"ok_below": ok_below, "flag_at": flag_at,
                       "rule": f"ok_below: at most {MISS_BUDGET:.0%} of val defect crops below it; flag_at: 0.5"},
        "data": {"manifest_sha1": hashlib.sha1(manifest).hexdigest(), "splits_used": ["train", "val"],
                 "counts": counts_split},
        "licence": "Head weights derived from CC BY 4.0 data (training/datasets/MANIFEST.json): attribution "
                   "required, see NOTICE.",
        "train_seconds": round(train_s, 1),
        "seed": SEED,
    }
    (OUT / "head.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps({k: meta[k] for k in ("head_id", "defect_head", "kind_head", "calibration", "thresholds", "ood",
                                           "train_seconds")}, indent=1))


def cmd_select(a) -> None:
    """Pick resolution and pooling by new-board generalisation without touching held-out data: train a linear
    defect head on the train split minus every VisA pcb3 crop, score VisA pcb3 (all its splits; a board design the
    head has not seen). pcb4 stays held out. Also prints the in-distribution val AUROC."""
    import numpy as np

    out = []
    for res in a.resolutions:
        if not features_path(res).exists():
            print(f"no features at {res} px: run `features --resolution {res}`")
            continue
        for pools in (("cls", "mean"), ("cls", "mean", "max")):
            rows, x = load_xy(res, pools)
            split = np.asarray([r["split"] for r in rows])
            pcb3 = np.asarray([r["image_id"].startswith("visa/pcb3/") for r in rows])
            y = np.asarray([r["label"] == "defect" for r in rows], dtype=np.int64)
            res_row = {"resolution": res, "pools": ",".join(pools)}
            for name, tr, te in (("new_board_pcb3_auroc", (split == "train") & ~pcb3, pcb3 & (split != "heldout")),
                                 ("val_auroc", split == "train", split == "val")):
                mean, std = x[tr].mean(0), x[tr].std(0) + 1e-6
                xs = ((x - mean) / std).astype(np.float32)
                pw = (y[tr] == 0).sum() / max(1, (y[tr] == 1).sum())
                net = fit(xs[tr], y[tr], 1, 0, 1e-4, epochs=a.epochs, class_weight=[pw])
                res_row[name] = M.auroc(y[te], logits(net, xs[te])[:, 0])
            print(res_row, flush=True)
            out.append(res_row)
    best = max(out, key=lambda r: (round(r["new_board_pcb3_auroc"], 3), r["val_auroc"]))
    print("best:", best)
    (D.WORK / "select.json").write_text(json.dumps({"trials": out, "best": best}, indent=1))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["features", "select", "train", "all"])
    ap.add_argument("--resolution", type=int, default=224)
    ap.add_argument("--resolutions", type=int, nargs="+", default=[224, 448])
    ap.add_argument("--pools", default="cls,mean,max", help="comma list from cls,mean,max (default: `select` winner)")
    ap.add_argument("--epochs", type=int, default=60)
    a = ap.parse_args(argv)
    if a.cmd in ("features", "all"):
        cmd_features(a)
    if a.cmd == "select":
        cmd_select(a)
    if a.cmd in ("train", "all"):
        cmd_train(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
