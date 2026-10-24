"""Defect crops for the CLS slot (task M7, data part of M6): sources -> detector boxes -> runtime crops -> labels.

Crops are made exactly as at runtime: the configured detector (DET) finds parts on the full image, and
`vision.crops.cut` cuts each box with the runtime padding rule. A crop is labelled from the source's own defect
annotation (VisA pixel masks, COCO boxes): `defect` when an annotated defect is visible in the crop, `ok` when no
annotated defect pixel is inside the crop, and dropped when it is in between (a sliver of defect at the edge).
On top of detector crops, every annotated defect also gets a crop centred on it (the case where the detector boxes
the damage itself, DAMAGE_CLASSES) and, for aligned VisA boards, the same box on a normal board (a hard `ok`).

Nothing is synthesised: every crop is a real photo region. Licences and splits: training/datasets/MANIFEST.json.

    PY=backend/.venv/bin/python
    $PY training/datasets/defects.py detect      # DET boxes per image, cached (data/datasets/work/boxes/)
    $PY training/datasets/defects.py crops       # crop index: data/datasets/work/crops.jsonl
    $PY training/datasets/defects.py manifest    # refresh counts in MANIFEST.json

Splits are by board or capture session, never by crop: VisA pcb1-3 by board photo (each photo is its own board),
the webcam set by capture day, the component-board set by board name. VisA pcb4 (another board design, defect kinds
never seen in training) is held out: never trained on, never used to pick thresholds.
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import io
import json
import random
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "datasets"
WORK = DATA / "work"
MANIFEST = Path(__file__).with_name("MANIFEST.json")

# Source label -> generic defect type (vision/generic_classes.py DEFECT_CLASSES). None: a defect whose kind has no
# training class (counted for the binary head only).
TYPE_MAP = {
    "melt": "burnt or melted", "burnt": "burnt or melted",
    "bent": "bent or deformed",
    "missing": "missing part",
    "scratch": "scratch or surface damage", "pcb_damage": "scratch or surface damage",
    "damage": "scratch or surface damage",
    "dry_joint": "solder fault", "short_circuit": "solder fault",
    "incorrect_installation": "misplaced or extra part", "extra": "misplaced or extra part",
    "wrong place": "misplaced or extra part",
    "dirt": None,
    "bulging_or_leaking_capacitor": None, "burnt_part": "burnt or melted",
}

MAX_DET_PER_IMAGE = 20       # detector crops kept per image, by score (runtime VLM budget is 12; CLS sees more)
DEDUP_IOU = 0.7              # crops whose regions overlap this much are one crop
MIN_DEFECT_PX = 25           # fewer annotated pixels than this inside a crop is too small to call
VISIBLE_FRAC = 0.5           # a defect region counts as visible when half of it is inside the crop ...
VISIBLE_OF_CROP = 0.02       # ... or it covers this fraction of the crop (a large defect cut by the crop edge)
MIN_OF_CROP = 0.003          # and it is not a speck in a large crop (unseen once the crop is resized): else dropped
NORMALS_PER_BOARD = {"pcb1": 150, "pcb2": 150, "pcb3": 150, "pcb4": 200}
PCB_BOARD_IMAGES = 300       # images of the component-board set used (ok crops from many real boards)


@dataclass
class Region:
    """One annotated defect: a pixel set (mask component) or a box, with its generic type (None = no class)."""
    box: tuple[float, float, float, float]
    dtype: str | None
    source_label: str
    pixels: object = None    # numpy (N, 2) array of (y, x) for mask regions; None for boxes

    @property
    def area(self) -> float:
        return float(len(self.pixels)) if self.pixels is not None else \
            max(0.0, self.box[2] - self.box[0]) * max(0.0, self.box[3] - self.box[1])

    def overlap(self, rect: tuple[int, int, int, int]) -> float:
        x0, y0, x1, y1 = rect
        if self.pixels is not None:
            p = self.pixels
            return float(((p[:, 1] >= x0) & (p[:, 1] < x1) & (p[:, 0] >= y0) & (p[:, 0] < y1)).sum())
        w = min(self.box[2], x1) - max(self.box[0], x0)
        h = min(self.box[3], y1) - max(self.box[1], y0)
        return max(0.0, w) * max(0.0, h)


@dataclass
class Item:
    """One source image."""
    image_id: str            # unique, stable
    source: str              # MANIFEST source id
    group: str               # board / session: the split unit
    split: str               # train | val | test | heldout
    load: object             # () -> PIL RGB image
    anomalous: bool
    labels: list[str] = field(default_factory=list)
    mask_path: Path | None = None
    coco_boxes: list[tuple[tuple[float, float, float, float], str]] = field(default_factory=list)
    aligned_group: str | None = None   # VisA: images of one board design are aligned (hard negatives)


def _bucket(key: str, val: float = 0.15, test: float = 0.15) -> str:
    h = int(hashlib.sha1(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "val" if h < val else "test" if h < val + test else "train"


def _pil(path: Path):
    from repairos.vision.crops import load_still

    return lambda: load_still(path)


# -- sources ------------------------------------------------------------------------------------------------------
def visa_items() -> list[Item]:
    raw = DATA / "visa" / "raw"
    out = []
    for board in ("pcb1", "pcb2", "pcb3", "pcb4"):
        rows = list(csv.DictReader((raw / board / "image_anno.csv").open()))
        normals = [r for r in rows if r["label"] == "normal"]
        random.Random(f"normals-{board}").shuffle(normals)
        keep = [r for r in rows if r["label"] != "normal"] + normals[:NORMALS_PER_BOARD[board]]
        for r in keep:
            path = raw.parent / "raw" / r["image"]
            iid = f"visa/{r['image']}"
            out.append(Item(
                image_id=iid, source="visa", group=iid, load=_pil(path),
                split="heldout" if board == "pcb4" else _bucket(iid), anomalous=r["label"] != "normal",
                labels=[s.strip() for s in r["label"].split(",")] if r["label"] != "normal" else [],
                mask_path=raw.parent / "raw" / r["mask"] if r["mask"] else None, aligned_group=board))
    return out


def _coco(dirpath: Path):
    for split in ("train", "valid", "test"):
        d = dirpath / split
        j = json.loads((d / "_annotations.coco.json").read_text())
        cats = {c["id"]: c["name"] for c in j["categories"]}
        anns = collections.defaultdict(list)
        for a in j["annotations"]:
            x, y, w, h = a["bbox"]
            anns[a["image_id"]].append(((x, y, x + w, y + h), cats[a["category_id"]]))
        for im in j["images"]:
            yield d / im["file_name"], anns.get(im["id"], [])


def webcam_items() -> list[Item]:
    """keremberke/pcb-defect-segmentation: webcam close-ups of assembled boards. Split by capture day."""
    day_split = {"20221017": "train", "20221030": "train", "20221102": "train", "20221023": "val",
                 "20221103": "test"}
    out = []
    for path, boxes in _coco(DATA / "hf" / "kerem" / "raw"):
        day = re.search(r"WIN_(\d{8})", path.name).group(1)
        out.append(Item(image_id=f"kerem/{path.name}", source="pcb_defect_webcam", group=day,
                        split=day_split[day], load=_pil(path), anomalous=bool(boxes),
                        labels=sorted({c for _, c in boxes}), coco_boxes=boxes))
    return out


def board_name(file_name: str) -> str:
    stem = file_name.split("_jpg.rf.")[0].split("_png.rf.")[0]
    stem = re.sub(r"_(png|JPG|jpg)$", "", stem)
    return re.sub(r"[-_ ]?(top|bottom)\d*$", "", stem, flags=re.I).lower()


def pcb_board_items() -> list[Item]:
    """RF100 printed-circuit-board: many real boards with component boxes and no defect annotation. Only `ok`
    crops come from it, assuming the photographed boards are not defective (recorded in the MANIFEST)."""
    items = []
    for path, _ in _coco(DATA / "hf" / "printed-circuit-board" / "raw"):
        b = board_name(path.name)
        items.append(Item(image_id=f"rf100pcb/{path.name}", source="rf100_printed_circuit_board", group=b,
                          split=_bucket(b), load=_pil(path), anomalous=False))
    random.Random("pcb-boards").shuffle(items)
    return items[:PCB_BOARD_IMAGES]


WIKIMEDIA_BOXES = ROOT / "eval" / "defects" / "wikimedia_boxes.json"


def wikimedia_items() -> list[Item]:
    """Wikimedia Commons defective-capacitor photos (CC0 / PD / CC BY per file): HELD OUT, evaluation only. Defect
    boxes were drawn by eye (eval/defects/wikimedia_boxes.json, in original pixels); every visibly failed part in
    the photo is boxed, so a crop with no box in it counts as `ok`."""
    if not WIKIMEDIA_BOXES.exists():
        return []
    from PIL import Image, ImageOps

    boxes = json.loads(WIKIMEDIA_BOXES.read_text())["files"]
    out = []
    for fn, b in sorted(boxes.items()):
        path = DATA / "wikimedia" / "raw" / fn
        if not path.exists():
            continue
        with Image.open(path) as im:  # boxes are in units of a 1000 px long side; scale to this file's pixels
            k = max(ImageOps.exif_transpose(im).size) / 1000
        out.append(Item(image_id=f"wikimedia/{fn}", source="wikimedia_defective_capacitors", group=fn,
                        split="heldout", load=_pil(path), anomalous=True, labels=[x["kind"] for x in b],
                        coco_boxes=[(tuple(v * k for v in x["box_1000"]), x["kind"]) for x in b]))
    return out


def all_items() -> list[Item]:
    return visa_items() + webcam_items() + pcb_board_items() + wikimedia_items()


# -- defect regions -----------------------------------------------------------------------------------------------
def components(mask, min_px: int = MIN_DEFECT_PX, join_px: int = 6):
    """Connected defect regions of a binary mask (8-connected after joining pixels within `join_px`), as (N,2)
    arrays of (y, x). Pure numpy + a BFS over a coarse grid: masks are small and scipy is not a dependency."""
    import numpy as np

    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return []
    cells = collections.defaultdict(list)
    for i, (y, x) in enumerate(zip(ys // join_px, xs // join_px)):
        cells[(int(y), int(x))].append(i)
    seen, out = set(), []
    for start in cells:
        if start in seen:
            continue
        stack, members = [start], []
        seen.add(start)
        while stack:
            c = stack.pop()
            members.extend(cells[c])
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    n = (c[0] + dy, c[1] + dx)
                    if n in cells and n not in seen:
                        seen.add(n)
                        stack.append(n)
        idx = np.asarray(members)
        if len(idx) >= min_px:
            out.append(np.stack([ys[idx], xs[idx]], axis=1))
    return out


def regions_of(item: Item) -> list[Region]:
    import numpy as np
    from PIL import Image

    if item.mask_path is not None:
        mask = np.asarray(Image.open(item.mask_path)) > 0
        single = TYPE_MAP.get(item.labels[0]) if len(item.labels) == 1 else None
        src = item.labels[0] if len(item.labels) == 1 else ",".join(item.labels)
        out = []
        for px in components(mask):
            box = (float(px[:, 1].min()), float(px[:, 0].min()), float(px[:, 1].max() + 1), float(px[:, 0].max() + 1))
            out.append(Region(box=box, dtype=single, source_label=src, pixels=px))
        return out
    return [Region(box=b, dtype=TYPE_MAP.get(c), source_label=c) for b, c in item.coco_boxes]


def label_crop(rect: tuple[int, int, int, int], regions: list[Region]) -> tuple[str, str | None, list[str]] | None:
    """('defect'|'ok', generic type or None, source labels) for a crop region, or None when ambiguous."""
    area = (rect[2] - rect[0]) * (rect[3] - rect[1])
    visible, touched = [], False
    for r in regions:
        o = r.overlap(rect)
        if o <= 0:
            continue
        if r.source_label == "ignore":  # a part nobody could judge: never counted, either way
            return None
        touched = True
        if o >= max(MIN_DEFECT_PX, MIN_OF_CROP * area) and (o >= VISIBLE_FRAC * r.area or o >= VISIBLE_OF_CROP * area):
            visible.append(r)
    if visible:
        types = {r.dtype for r in visible}
        return "defect", (types.pop() if len(types) == 1 else None), sorted({r.source_label for r in visible})
    return None if touched else ("ok", None, [])


def _iou(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    i = w * h
    return i / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i)


# -- detect -------------------------------------------------------------------------------------------------------
def _box_cache(item: Item) -> Path:
    return WORK / "boxes" / (hashlib.sha1(item.image_id.encode()).hexdigest()[:16] + ".json")


def cmd_detect(a) -> None:
    import time

    from repairos.registry import Registry

    reg = Registry()
    impl = a.det or reg.selected("DET")
    det = reg.load("DET", impl)
    items = all_items()
    todo = [it for it in items if not _box_cache(it).exists()]
    print(f"DET={impl}: {len(items)} images, {len(todo)} to detect", flush=True)
    (WORK / "boxes").mkdir(parents=True, exist_ok=True)
    t = time.perf_counter()
    for k, it in enumerate(todo):
        im = it.load()
        dets = det.detect(im)
        _box_cache(it).write_text(json.dumps({
            "image_id": it.image_id, "det": f"{impl}:{getattr(det, 'model_id', impl)}", "size": list(im.size),
            "boxes": [[list(d.box), d.label, d.score] for d in dets]}))
        if k % 50 == 0:
            print(f"{k}/{len(todo)} {time.perf_counter() - t:.0f}s", flush=True)


# -- crops --------------------------------------------------------------------------------------------------------
MAX_BOX_FRACTION = 0.5  # same rule as vision/inspect.py: a box over half the image is scene-level, not a part


def cmd_crops(a) -> None:
    from repairos.vision.crops import crop_region

    items = all_items()
    by_group = collections.defaultdict(list)
    for it in items:
        if it.aligned_group and not it.anomalous:
            by_group[it.aligned_group].append(it)
    rows, missing = [], 0
    for it in items:
        cache = _box_cache(it)
        if not cache.exists():
            missing += 1
            continue
        c = json.loads(cache.read_text())
        w, h = c["size"]
        regions = regions_of(it) if it.anomalous else []
        kept: list[tuple[int, int, int, int]] = []
        boxes = sorted(c["boxes"], key=lambda b: -b[2])
        for box, label, score in boxes:
            if (box[2] - box[0]) * (box[3] - box[1]) > MAX_BOX_FRACTION * w * h:
                continue
            try:
                rect = crop_region(tuple(box), w, h)
            except ValueError:
                continue
            if any(_iou(rect, k) > DEDUP_IOU for k in kept):
                continue
            kept.append(rect)
            lab = label_crop(rect, regions)
            if lab is not None:
                rows.append(_row(it, "det", box, rect, lab, det_label=label, det_score=score))
            if len(kept) >= MAX_DET_PER_IMAGE:
                break
        # a crop centred on every annotated defect (DAMAGE_CLASSES boxes at runtime)
        for r in (r for r in regions if r.source_label != "ignore"):
            rect = crop_region(r.box, w, h)
            lab = label_crop(rect, regions)
            if lab is not None and lab[0] == "defect":
                rows.append(_row(it, "gt", list(r.box), rect, lab))
            peers = [p for p in by_group.get(it.aligned_group or "", []) if p.split == it.split]
            if peers:  # the same place on a normal board of the same design: a hard ok
                p = random.Random(it.image_id + str(r.box)).choice(peers)
                rows.append(_row(p, "gt_same_place_normal", list(r.box), rect, ("ok", None, [])))
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / "crops.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    cnt = collections.Counter((r["source"], r["split"], r["label"]) for r in rows)
    for k in sorted(cnt):
        print(k, cnt[k])
    print(f"{len(rows)} crops; {missing} images without boxes (run `detect`)")


def _row(it: Item, kind: str, box, rect, lab, det_label: str | None = None, det_score: float | None = None) -> dict:
    label, dtype, src = lab
    return {"crop_id": hashlib.sha1(f"{it.image_id}|{kind}|{rect}".encode()).hexdigest()[:16],
            "image_id": it.image_id, "source": it.source, "group": it.group, "split": it.split, "kind": kind,
            "box": [float(v) for v in box], "region": list(rect), "label": label, "dtype": dtype,
            "source_labels": src, "det_label": det_label, "det_score": det_score}


def image_path(image_id: str) -> Path:
    src, rest = image_id.split("/", 1)
    if src == "visa":
        return DATA / "visa" / "raw" / rest
    if src == "wikimedia":
        return DATA / "wikimedia" / "raw" / rest
    base = DATA / "hf" / {"kerem": "kerem", "rf100pcb": "printed-circuit-board"}[src] / "raw"
    for split in ("train", "valid", "test"):
        if (base / split / rest).exists():
            return base / split / rest
    raise FileNotFoundError(image_id)


def load_crops(path: Path = WORK / "crops.jsonl") -> list[dict]:
    return [json.loads(line) for line in path.open()]


def cut_row(row: dict, cache: dict | None = None):
    """The crop for an index row, cut by the runtime `vision.crops.cut` from the full image."""
    from repairos.vision.crops import cut, load_still
    from repairos.vision.inspect import VLM_MAX_SIDE

    if cache is not None and row["image_id"] in cache:
        im = cache[row["image_id"]]
    else:
        im = load_still(image_path(row["image_id"]))
        if cache is not None:
            cache.clear()
            cache[row["image_id"]] = im
    crop = cut(im, tuple(row["box"]), still_id=row["image_id"], max_side=VLM_MAX_SIDE)  # as inspect.py cuts
    assert list(crop.region_px) == row["region"], (crop.region_px, row["region"])
    return crop


# -- manifest -----------------------------------------------------------------------------------------------------
def cmd_manifest(a) -> None:
    m = json.loads(MANIFEST.read_text())
    rows = load_crops()
    cnt = collections.defaultdict(collections.Counter)
    for r in rows:
        cnt[r["source"]][f"{r['split']}/{r['label']}"] += 1
    for s in m["sources"]:
        if s["id"] in cnt:
            s["crops"] = dict(sorted(cnt[s["id"]].items()))
    MANIFEST.write_text(json.dumps(m, indent=2) + "\n")
    print(json.dumps({k: dict(v) for k, v in cnt.items()}, indent=1))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("detect")
    d.add_argument("--det", default=None, help="DET implementation (default: the configured one)")
    sub.add_parser("crops")
    sub.add_parser("manifest")
    a = ap.parse_args(argv)
    {"detect": cmd_detect, "crops": cmd_crops, "manifest": cmd_manifest}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
