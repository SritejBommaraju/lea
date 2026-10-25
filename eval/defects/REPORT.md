# Defect classifier benchmark (M7), 2026-10-24

CLS=dinov2 (frozen `facebook/dinov2-base` at revision `f9e44c8`, linear heads, head `015921955e`) against the Claude VLM (`VLM=anthropic`: Haiku 5.5, Sonnet 5.5 on `cannot_tell`) and the two combined, on crops cut exactly as `vision/inspect.py` cuts them. Raw numbers: [results/2026-10-24.json](results/2026-10-24.json). Code: [run.py](run.py), [metrics.py](metrics.py). Data and splits: [training/datasets/MANIFEST.json](../../training/datasets/MANIFEST.json).

## Verdict

**Question:** does a trained classifier add value, or would Claude + Jev alone do the job?

**Decision rule** (applied to HELD-OUT data only: VisA pcb4 and the Wikimedia photos). Keep the classifier only if at least one holds: (a) it beats Claude-alone on defect recall at comparable precision; (b) classifier -> VLM routing beats VLM-alone; (c) it matches VLM accuracy while inspecting far more regions at a fraction of the latency and cost.

**Answer: it does not clear the bar. Phase 1 stays VLM-only (`CLS: none`).** The classifier is built, wired and selectable (`REPAIROS_SLOT_CLS=dinov2`). It is worth keeping as an opt-in, cheap triage step for board-like crops only.

| Rule | Result on held-out (300 VLM-judged crops, 95 % bootstrap CI) | Met? |
|---|---|---|
| (a) recall at comparable precision | Classifier recall 61 % [54-69] vs VLM 36 % [28-43], but precision 74 % [66-81] vs 98 % [94-100]. The precision is not comparable. | No |
| (b) routing beats VLM-alone | The shipped routing (the VLM settles every crop it sees) gives exactly the VLM's precision and recall, with 11 % fewer VLM calls (268 of 300; USD 0.352 vs 0.363). The pre-registered "either flags" variant raises recall to 67 % [59-75] and F1 from 0.52 to 0.73, but drops precision to 79 % [72-86]: a trade, not a win. | No (trade only) |
| (c) same accuracy, many more regions, far cheaper | 21 ms per crop on the Mac's GPU vs 1.6 s (Haiku) or 2.5 s (Sonnet), and no API cost. But accuracy is not the same (AUROC 0.73 [0.67-0.79] held-out). On pcb4 stills the 12-crop budget never binds (median 4 regions). On the real-world photos where it would bind (p90 33 regions), the classifier abstains on 93 % of crops. | No |

What the numbers do say:
- **In distribution (PCB photos like the training data), the classifier is strong.** Test AUROC is 0.97 and precision 97 %. On held-out board crops it found defects the VLM missed (pcb4 recall 67 % vs 18 %).
- **Claude misses subtle board defects.** On pcb4 the Haiku escalation flagged 18 of 100 defect crops (dirt, small scratches, missing small parts), and almost never raised a false alarm (precision 100 %). Sonnet alone, on an even 60-crop subset, caught 17 of 32 defects vs 10 of 32 for the Haiku escalation at the same precision (89-91 %), at about 5 times the cost per crop.
- **Outside board photos, the classifier knows it is lost.** It abstains on 93 % of Wikimedia detector crops and the VLM decides; where it did answer, it was no better than the VLM.

Recommendation: Phase 1 runs VLM-only and spends effort on the VLM side (Sonnet for crops, or a defect checklist in the crop prompt; both measurable with this benchmark). Re-run MS5 when M4 gives labelled LEA sessions: retrain the classifier on our own crops and re-judge it on held-out products. The next variant to test is "disagree -> insufficient evidence" (the classifier flags, the VLM says ok), decided on a fresh split rather than on these held-out crops.

## Setup

- **Crops.** The configured detector (OWLv2 base, revision pinned) runs on every full image. Each box under half the image is cut by `vision.crops.cut` with the runtime padding rule. Up to 20 crops per image, deduplicated at IoU 0.7. In addition, one crop is centred on every annotated defect (what a `DAMAGE_CLASSES` box gives at runtime). For VisA, the same place on a normal board of the same design is added as a hard `ok`.
- **Labels** come from the source's own annotation (VisA masks, COCO boxes, our Wikimedia boxes). A crop is `defect` when an annotated defect is visible in it: half of the defect is inside the crop, or the defect covers 2 % of the crop, and it is not a speck under 0.3 % of the crop. A crop is `ok` when no annotated pixel is inside it. Anything else is dropped.
- **Splits** are by board or capture session, never by crop. VisA pcb4 (another design; the defect types extra, dirt, wrong place, damage and burnt were never trained on) and the Wikimedia photos are held out. They were never used for training, thresholds, the abstention cutoff or model selection.
- **Model selection** used "leave VisA pcb3 out" AUROC: 224 px with CLS+mean+max pooling scored 0.865, the best of four. The hidden-layer head overfit, so the linear head was kept.
- **Calibration** was fitted on val: Platt scaling for p(defect) (val ECE 0.017) and a temperature for the type head.
- **Thresholds** were set on val: `ok_below` 0.145 (at most 2 % of val defects fall below it) and `flag_at` 0.5.
- **Unfamiliar-crop abstention.** The distance of a crop's features from the training features (Mahalanobis distance in the top 64 principal components of the standardised train features, plus the residual outside them). The cutoff is the 99th percentile of val distances, so 1.0 % of val crops abstain. An abstaining crop goes to the VLM.
- **VLM sample.** 300 held-out detector crops, seeded: pcb4 100 defect + 100 ok, and Wikimedia 60 defect + 40 ok. Each was asked exactly as `inspect.py` asks (the crop, a context view, and the detector label passed as data). `cannot_tell` counts as not flagged (it becomes `insufficient_evidence`).

## Classifier on every split

| Split | Crops | Defect | AUROC | ECE | Precision | Recall | Abstains | AUROC answered | Routed ok | Defects routed ok |
|---|---|---|---|---|---|---|---|---|---|---|
| val | 1285 | 396 | 0.992 | 0.017 | 96.0 | 91.4 | 1.0 | 0.991 | 63.8 | 7 (1.8) |
| test, all | 1317 | 316 | 0.969 | 0.058 | 97.3 | 67.7 | 2.9 | 0.969 | 73.6 | 40 (12.7) |
| test, VisA pcb1-3 | 783 | 182 | 0.975 | 0.050 | 100.0 | 70.9 | 1.9 | 0.975 | 74.5 | 19 (10.4) |
| test, webcam close-ups | 146 | 134 | 0.670 | 0.340 | 96.6 | 63.4 | 3.4 | 0.646 | 17.8 | 21 (15.7) |
| test, component boards (ok only) | 388 | 0 | n/a | 0.024 | n/a | n/a | 4.6 | n/a | 92.8 | 0 |
| held-out pcb4, detector crops | 999 | 164 | 0.736 | 0.257 | 30.6 | 64.0 | 0.3 | 0.735 | 15.3 | 13 (7.9) |
| held-out pcb4, defect-centred + same place on normal | 525 | 257 | 0.788 | 0.141 | 65.0 | 76.7 | 12.6 | 0.787 | 14.7 | 13 (5.1) |
| held-out Wikimedia, detector crops | 146 | 93 | 0.628 | 0.397 | 69.6 | 41.9 | 93.2 | 0.917 (10 crops) | 1.4 | 0 |
| held-out Wikimedia, defect-centred | 31 | 31 | n/a | 0.466 | 100.0 | 54.8 | 87.1 | n/a | 0.0 | 0 |

Precision and recall are measured at `flag_at` with abstention ignored. "Defects routed ok" counts defect crops that would be answered `ok` without a VLM look: 1.8 % on val by construction, and 8-13 % on test and pcb4.

## Held-out: classifier vs Claude vs combined

| Sample | System | Precision [CI] | Recall [CI] | F1 | Specificity | AUROC | VLM calls | cannot_tell | USD |
|---|---|---|---|---|---|---|---|---|---|
| pcb4 (200, 100 defect, 1 abstained) | classifier | 74.4 [65-83] | 67.0 [58-76] | 70.5 | 77.0 | 0.787 [0.72-0.85] | 0 | 0 | 0 |
| | VLM | 100 [100-100] | 18.0 [11-26] | 30.5 | 100 | | 200 | 2 | 0.083 |
| | combined (shipped) | 100 | 18.0 | 30.5 | 100 | | 168 | 2 | 0.072 |
| | either flags | 74.4 | 67.0 | 70.5 | 77.0 | | 200 | 2 | 0.083 |
| Wikimedia (100, 60 defect, 92 abstained) | classifier | 72.1 [58-85] | 51.7 [40-65] | 60.2 | 70.0 | 0.728 [0.63-0.83] | 0 | 0 | 0 |
| | VLM | 97.5 [91-100] | 65.0 [53-77] | 78.0 | 97.5 | | 100 | 38 | 0.279 |
| | combined (shipped) | 97.5 | 65.0 | 78.0 | 97.5 | | 100 | 38 | 0.279 |
| | either flags | 88.9 [79-97] | 66.7 [55-78] | 76.2 | 87.5 | | 100 | 38 | 0.279 |
| Both (300, 160 defect, 93 abstained) | classifier | 73.7 [66-81] | 61.3 [54-69] | 66.9 | 75.0 | 0.732 [0.67-0.79] | 0 | 0 | 0 |
| | VLM | 98.3 [94-100] | 35.6 [28-43] | 52.3 | 99.3 | | 300 | 40 | 0.363 |
| | combined (shipped) | 98.3 | 35.6 | 52.3 | 99.3 | | 268 | 40 | 0.352 |
| | either flags | 79.3 [72-86] | 66.9 [59-75] | 72.5 | 80.0 | | 300 | 40 | 0.363 |
| In distribution (207, 104 defect) | classifier | 72.4 [63-81] | 68.3 [60-77] | 70.3 | 73.8 | 0.779 | 0 | 0 | 0 |
| | VLM | 100 | 20.2 [13-28] | 33.6 | 100 | | 207 | 1 | 0.085 |
| | combined | 100 | 20.2 | 33.6 | 100 | | 175 | 1 | 0.075 |
| Out of distribution, abstained (93, 56 defect) | classifier (abstention ignored) | 77.1 | 48.2 | 59.3 | 78.4 | 0.748 | 0 | 0 | 0 |
| | VLM = combined = either | 97.3 [90-100] | 64.3 [52-76] | 77.4 | 97.3 | | 93 | 39 | 0.277 |

In the shipped routing, confident-ok crops skip the VLM and the VLM settles every other crop. "Either flags" counts a crop as a defect when the classifier or the VLM flags it; abstained crops use the VLM only. The `cannot_tell` answers on Wikimedia come mostly from shallow-focus macro photos.

## Latency, cost, regions per still

| Per crop | Median | p90 | USD per crop |
|---|---|---|---|
| CLS=dinov2, MPS (M4), batched | 21 ms | | 0 |
| CLS=dinov2, one at a time | 38 ms | | 0 |
| Haiku 5.5, not escalated (n=253) | 1.61 s | 2.08 s | |
| Haiku then Sonnet escalation as shipped (n=300, 15.7 % escalated) | 1.65 s | 4.10 s | 0.0012 |
| Sonnet 5.5 alone (n=60) | 2.50 s | 5.69 s | 0.0063 |

Prices are list prices (Haiku 5.5 $0.10/$0.50, Sonnet 5.5 $2/$10 per million input/output tokens), at about 3.5k input tokens per crop. Cost per 20-still scan with 12 VLM crops per still: about $0.29 VLM-only, and about $1.50 Sonnet-only. With the classifier, VLM calls fall by about 11 % on held-out data (up to 74 % on in-distribution board crops), at about 1 s of local compute per still.

| Regions per still (boxes under half the still) | Stills | Median | p90 | Looked at, VLM-only (budget 12) | Looked at, with classifier (budget 48) |
|---|---|---|---|---|---|
| pcb4 | 300 | 4 | 5 | 3.4 (all) | 3.4 (all) |
| Wikimedia | 24 | 7 | 34 | 7.0 (10.7 not inspected) | 13.0 (4.6 not inspected), mostly abstentions |
| webcam close-ups | 189 | 4 | 17 | 5.1 (1.1 not inspected) | 6.2 (0) |

## Defect types

On test (270 crops) the type head is 65.9 % accurate. Precision/recall by type: burnt or melted 84/95, bent or deformed 100/90, missing part 77/81, scratch or surface damage 57/62, solder fault 96/25, misplaced or extra part 26/100. On held-out pcb4 (75 crops) accuracy is 25.3 %. The type does not transfer to a new board design, so the report should use the VLM's explanation.

## Coverage and limits

**Covered** (train + val: 7,287 crops, 1,767 defect and 5,520 ok):
- **Defect boards:** 3 small hobby modules (VisA pcb1-3: 640 photos, 930 defect / 3,633 ok crops), and webcam close-ups of assembled boards (155 photos over 4 days, 837 / 346).
- **Ok-only boards:** 105 boards (182 photos, 1,541 crops, assumed defect-free).
- **Defect types:** misplaced or extra part 577, burnt or melted 436, missing part 167, solder fault 167, bent or deformed 133, scratch 110, unknown 177.
- **Parts (detector labels, often wrong):** diode-like leads 975, inductor 876, resistor 563, IC 479, connectors and headers about 450, potentiometer 203, fuse holder 194, switch 179, buzzer 132, solder joint 128, battery 51, cable 21.
- **Image conditions:** studio-lit top-down board photos (about 1400 px), 640x480 webcam close-ups, 640 px whole boards. No phone or 12 MP close-ups, no oblique views, no blur, no enclosures.

**Not covered** (0 crops unless stated):
- consumer device interiors (phones, laptops, chargers, appliances, tools, toys, vehicles);
- wire harnesses and in-situ wiring or insulation damage (21 cable crops and 69 ribbon-cable crops, all ok);
- capacitor failures: bulging, venting or leaking electrolytics, tantalum, film and ceramic (Wikimedia is evaluation only);
- motors, fans, gears, belts, springs, cracked housings;
- batteries (51 crops, all ok): swelling, leakage, contact corrosion;
- corrosion and contamination;
- iPhone lighting, focus and clutter.

Measured consequence: on the Wikimedia photos of real failures, the classifier abstains on 93 % of crops, and where it answers it is no better than the VLM. Its "no visible defect" means something only for board-like crops in good light.

## Datasets and licences

- **VisA pcb1-4** (CC BY 4.0, Zou et al. 2022): pcb4 is held out.
- **keremberke pcb-defect-segmentation** (CC BY 4.0, Roboflow user diplom-qz7q6).
- **RF100 printed-circuit-board** (CC BY 4.0).
- **Wikimedia Commons, Defective capacitors:** 24 labelled photos; 13 CC0, 2 PD, 10 CC BY 2.0/3.0/4.0; share-alike excluded. The boxes were drawn by eye by the M7 agent and are not yet reviewed (`wikimedia_boxes.json`). Per-file licence and author are in `wikimedia_files.json`.
- **Excluded:** RF100 cable-damage, RF100 corrosion, the MVTec family, Real-IAD, MANTA, KolektorSDD, MPDD, Innodisk, DefectSpectrum, SolDef_AI, HRIPCB/DeepPCB, and any closed-app capture.

## Reproduce

```
PY=backend/.venv/bin/python
$PY training/datasets/defects.py detect     # OWLv2, 1,563 images, about 15 min (M4)
$PY training/datasets/defects.py crops
$PY training/defects/train.py features      # 10,365 crops at 224 px, about 4 min
$PY training/defects/train.py select        # leave-pcb3-out choice of resolution and pooling
$PY training/defects/train.py train         # 21 s
$PY eval/defects/run.py classify
$PY eval/defects/run.py sample
$PY eval/defects/run.py vlm                 # 300 crops, about USD 0.36
$PY eval/defects/run.py vlm --model claude-sonnet-5-5 --limit 60
$PY eval/defects/run.py report
```
