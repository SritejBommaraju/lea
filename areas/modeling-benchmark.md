# Modeling + benchmark (stream `modeling`)

**Who.** One person (the owner assigns).

**Delivers.** Perception that turns full-resolution stills into evidence for any product, and the benchmark that
measures it. Detector (OWLv2, Grounding DINO swap-in), crops, Claude VLM crop checks (built); the defect
classifier and its dataset, OCR of labels and markings, and `make eval` (to build).

**Owns.** Under `backend/repairos/`: `slots/{detector,vlm,classifier,ocr}.py` and their `det_*`, `vlm_*`,
`cls_*`, `ocrb_*` folders, `vision/**`; the matching `backend/tests/conformance/*/`; `eval/**`, `training/**`,
`samples/sessions/**`, `licenses/datasets.json`.

**Contracts.** Reads `still` (and `scan-package` poses). Returns boxes, crop states, classifier scores and OCR
text, which the pipeline turns into `finding`s: always suspects, never proof (I1). Class names live only in
`vision/generic_classes.py` (GEN1). The benchmark reads recorded sessions (`samples/session.schema.json`).

## Tasks, in order

| Id | What | Deps |
|---|---|---|
| M1 | Detector slot, generic vocabulary, prompts, tiling | done |
| M2 | Full-res crops from detector boxes | done |
| M3 | VLM crop checks and per-still inspection | done |
| M4 | Benchmark data: labelled real sessions, held-out products | - |
| M5 | Benchmark runner `make eval` | M4, B10 |
| M6 | Defect dataset, licence-clean | M4 |
| M7 | Defect classifier (CLS slot): frozen DINOv2 + trained head; in progress on `rohan/defect-classifier` | M5, M6 |
| M8 | OCR of labels and markings | M2 |
| M9 | Detector choice measured | M5 |

"Done when" for each: [PLAN.md](../PLAN.md) §7. Milestones MS4 (baseline) and MS5 (classifier) are this stream's.

**Rules that bite here.** No target number before MS4 measures the baseline. Held-out products never reach
tuning or training. Captures from closed third-party apps are never used for training or benchmarking. Training data and weights pass the licence allowlist (L2). The classifier is selected in
`config/providers.yaml` only after it beats the baseline on held-out products (MS5).

**Setup (doctor items).** `dev_tools`, `anthropic_key`, `test_products`, `iphone`; `train_compute` for M7.

**Done when.** `make eval` prints per-split metrics on held-out products, honesty violations are zero, and every
default in config follows the numbers.
