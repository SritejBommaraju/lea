# Areas: three streams

Each stream is run by one person, mostly with coding agents. Pick yours, open its file, run its `next.py` line.
Tasks and milestones: [PLAN.md](../PLAN.md) §6-§7; machine twin: [build/tasks.yaml](../build/tasks.yaml).

| Stream | Who | Owns (short) | Next |
|---|---|---|---|
| [App](app.md) | one person (owner assigns) | `ios/**` except generated contracts | `python3 -I build/next.py --stream app` |
| [Modeling + benchmark](modeling-benchmark.md) | one person (owner assigns) | perception slots, `vision/**`, `eval/**`, `training/**`, `samples/sessions/**` | `python3 -I build/next.py --stream modeling` |
| [Build execution](build-execution.md) | Rohan + agents | the rest of `backend/**`, `build/`, `config/`, `tools/`, contracts, CI | `python3 -I build/next.py --stream build` |

Phase 2 (Grain) has no stream yet: [PLAN.md](../PLAN.md) §9.

## Contracts at the boundaries

| From → to | What | Schema (`spec/contracts/`) |
|---|---|---|
| App → build (gateway) | scan package (binary PLY in metres, poses, keyframe depth), full-res stills with pose and intrinsics, meter readings | `scan-package`, `still`, `meter-reading` |
| Build → app (gateway, SSE) | the report, findings, suspects, next action, in words | `report` (B9), `finding`, `suspect`, `next-action` |
| Build → modeling | stills and crops from the blob store; the generic vocabulary | `still`; the DET, VLM, CLS, OCRB slot interfaces |
| Modeling → build | boxes, crop states, classifier scores, OCR text, as vision findings (never proof) | `finding` |
| Sessions → benchmark | real captures with `truth.json`, replayed by `make replay` and scored by `make eval` | `samples/session.schema.json` |
