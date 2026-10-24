# Areas: three streams

Each stream is run by one person, mostly one-shotting with agents. Pick yours, open its file, run its `next.py`
line. Lanes L1-L6 from [BUILD.md §0.B](../BUILD.md#0b-lanes-and-people) are aliases inside the streams; the mapping
of every card to a stream is `streams:` in [build/tasks.yaml](../build/tasks.yaml).

| Stream | People | Owns (short) | First card | Next |
|---|---|---|---|---|
| [App](app.md) | one person (owner assigns); Rohan builds iOS on this Mac too | `ios/**` (not generated contracts), capture converters | [C1.1](../BUILD.md#c11-ios-app-shell), [C1.2](../BUILD.md#c12-device-capability-probe) | `next.py --stream app` |
| [Modeling + benchmark](modeling-benchmark.md) | one person (owner assigns) | perception slots, `backend/repairos/vision/**`, `eval/**`, `training/**` | [M.DET.0](../BUILD.md#mdet0-det-slot-interface-prompts-from-generic-classes--bom-text-expected-part-matching), [M.VLM.0](../BUILD.md#mvlm0-vlm-slot-interface) (after B0.9) | `next.py --stream modeling` |
| [Build-plan execution](build-execution.md) | Rohan + agents | rest of `backend/**`, `build/`, `config/`, `tools/`, `demo/` | [B0.1](../BUILD.md#b01-repo-scaffold-makefile-license-notice-gitignore), [B0.8](../BUILD.md#b08-build-tooling-tasksyaml-doctorpy-nextpy-statuspy) | `next.py --stream build` |
| Grain side (Phase 2) | Sritej | the Grain API itself (not in this repo) | [S2.7](../BUILD.md#s27-grain-side-h0-the-running-grain-serves-current-scan-diagnosis-not_visible-parts-and-the-pilot-cad-endpoints-private-fixtures-re-recorded) | `next.py --stream grain` |

**Phases.** Phase 1 (now): capture any product, find broken things ourselves, no Grain calls. Phase 2: Grain.
Cards say `P1`/`P2`; `next.py --phase1` filters ([BUILD.md Phases](../BUILD.md#phases-phase-1-now-no-grain-phase-2-with-grain)).

## Contracts at the boundaries

| From → to | What | Schema (`spec/contracts/`) |
|---|---|---|
| App → build (gateway S2.1) | scan package: binary PLY in metres + manifest; full-res JPEG stills with pose and intrinsics; readings; parts on hand | `scan-package`, `still`, `meter-reading`, `part-on-hand` |
| Build → app (live events) | findings, suspects, next action, plan, quotes (words, not numbers) | `finding`, `suspect`, `next-action`, `repair-plan`, `shopping-quote` |
| Build → modeling | stills and crops from the blob store; prompts = the generic class list (+ BOM text in Phase 2) | `still`; slot interfaces M.DET.0, M.VLM.0 |
| Modeling → build (pipeline) | boxes `{xyxy_px, score, prompt}`, crop verdicts, OCR text, as vision findings (never proof) | `finding` |
| Recorded sessions → benchmark | real captures + truth labels, replayed by `make replay` / `make eval` | `samples/session.schema.json` (B0.7), `eval/truth.schema.json` (E8.1) |
| Build ↔ Grain (Phase 2) | upload, diagnose, `/electrical/check`, `/call`; API shapes only | `grain-*` |

**Grain side.** Sritej owns the Grain-side cards (S2.7-S2.11). They are stated as API capabilities RepairOS needs;
Grain internals stay out of the repo (`private/grain/`, ask Sritej). See [AGENTS.md](../AGENTS.md) rules.
