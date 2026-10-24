# Modeling + benchmark (stream `modeling`)

Alias lanes: the vision part of L3, the VLM part of L5, E8.6 from L6. **Who:** one person (the owner assigns).

**Delivers.** Perception slots that turn full-resolution stills into evidence for **any** product: the detector
(Phase 1: Grounding DINO tiny, zero-shot, no training; OWLv2 the alternative), crops from detector boxes (SEG=none),
VLM crop checks (Claude Haiku, `cannot_tell` escalates to Sonnet), OCR, depth refinement and fine-tunes when the
benchmark shows the need. And the benchmark: held-out products, metrics, generality gates, recorded real sessions.

**Owns.** `backend/repairos/slots/{detector,segmenter,vlm,ocr,renderer,depth,trainer}.py` and their implementation
folders (`det_*`, `seg_*`, `vlm_*`, `ocr_*`, `render_*`, `depth_*`, `train_*`), `backend/repairos/vision/**`,
`backend/repairos/compare/**`, `backend/repairos/render/**`, the matching `backend/tests/conformance/*/**`,
`eval/**`, `training/**`, `samples/sessions/**` (data campaign), `licenses/weights.json`.

**Contracts.** Reads `still` (+ `scan-package` poses). Returns to the pipeline: `Box{xyxy_px, score, prompt}` from
DET (M.DET.0 interface), a structured state per crop from VLM (M.VLM.0), both turned into `finding`
(`source: repairos.vision.*`, never proof, I1). Prompts: one generic part-class list in
`backend/repairos/vision/generic_classes.py` (GEN1), plus BOM text when a reference exists (Phase 2).
Benchmark consumes recorded sessions (`samples/session.schema.json`, B0.7) and `eval/truth.schema.json` (E8.1).

**Cards, in order.** Loop: [AGENTS.md](../AGENTS.md). `P1` = Phase 1 (no Grain), `P2` = needs Grain ([BUILD.md Phases](../BUILD.md#phases-phase-1-now-no-grain-phase-2-with-grain)); `←` = depends on; links go to the card text.
- **Detector, Phase 1 zero-shot (L3)**: [M.DET.0](../BUILD.md#mdet0-det-slot-interface-prompts-from-generic-classes--bom-text-expected-part-matching) P1 1 h ← B0.9 · [M.DET.1](../BUILD.md#mdet1-grounding-dino-default) P1 2 h ← M.DET.0
- **VLM crop checks (L5)**: [M.VLM.0](../BUILD.md#mvlm0-vlm-slot-interface) P1 0.5 h ← B0.9 · [M.VLM.1](../BUILD.md#mvlm1-anthropic-default--mvlm2-openai--mvlm3-qwen3-vl-self-hosted) P1 1.5 h ← M.VLM.0
- **Backend OCR off for now (L6)**: [M.OCRB.3](../BUILD.md#mocrb3-ocrb-none-hackathon) P1 0.25 h ← B0.9
- **Render, masks and per-part compare on the reference (L3, L2)**: [M.RENDER.0](../BUILD.md#mrender0-render-slot-interface-and-expected-view-assembly) P2 1.5 h ← B0.9, S2.3 · [M.RENDER.1](../BUILD.md#mrender1-trimesh-ray-cast-renderer-default) P2 1.5 h ← M.RENDER.0 · [M.SEG.0](../BUILD.md#mseg0-seg-slot-interface-and-full-resolution-crops) P2 1 h ← B0.9, M.RENDER.0 · [M.SEG.4](../BUILD.md#mseg4-seg-none-hackathon) P2 0.25 h ← M.SEG.0 · [V3.5](../BUILD.md#v35-per-part-compare-silhouette-edges-vlm-crop-pair) P2 1.5 h ← M.SEG.0, M.VLM.0, S2.5
- **Benchmark sanity check (L6)**: [E8.6](../BUILD.md#e86-one-held-out-sanity-check) P2 1 h ← F4.8, C1.9
- **Later track** (kept, not in the 24 h). P1: C1.8, T7.1, T7.4, T7.5, E8.1, E8.2, E8.4, M.VLM.2, M.VLM.3, M.DET.2, M.DET.4, M.OCRB.0, M.OCRB.1, M.OCRB.2, M.TRAIN.0, M.TRAIN.1, M.TRAIN.2, M.TRAIN.3, M.TRAIN.4. P2: V3.4, V3.7, T7.2, T7.3, T7.6, E8.3, E8.5, M.DET.3, M.DET.5, M.SEG.1, M.SEG.2, M.SEG.3, M.DEPTH.0, M.DEPTH.1, M.DEPTH.2, M.RENDER.2, M.RENDER.3, M.RENDER.4.

**Phase 1 gap (owner to decide).** Crops from detector boxes live in `vision/crops.py`, owned by M.SEG.0, which is
Phase 2 (it depends on M.RENDER.0). Phase 1 needs that crop step without the render: move it to M.DET.0 or add a card.

**Setup (doctor items).** `dev_tools`, `anthropic_key` (VLM), `test_products` and `iphone` for captures;
`gpu_box` only for later fine-tunes.

**What's ready.** `python3 -I build/next.py --stream modeling` (`--track all` shows the later P1 benchmark cards
E8.1, E8.2, E8.4, C1.8).

**Hands off / receives.** Receives stills and scan packages from the app (via build execution's gateway and blob
store) and the slot registry (B0.9). Hands findings to fusion (build execution, F4.1/F4.4) and scores to the team.

**Done when.** On recorded real sessions of products never tuned on, every still yields boxes and crop verdicts
through the slots, conformance suites pass, unknown stays `insufficient_evidence`, and the benchmark prints per-split
metrics; no product name, class list or threshold is hard-coded outside the one class file and `thresholds.yaml`.
