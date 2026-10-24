# Build-plan execution (stream `build`)

Alias lanes: L2, L4 and L6, the backend parts of L3 and L5. **Who:** Rohan, driving agents through BUILD.md.

**Delivers.** Everything that is not the app or the models: scaffold, codegen, config, registry; the backend
gateway, state and blobs; fusion via Jev and the next action; parts on hand, datasheets and the quick check; the
pipeline orchestrator, integration and the demo script. Phase 2: the Grain client, reference cache, alignment,
cross-check, meter planner, electrical expectations, simulation check, plan writer and confirm loop.

**Owns.** `backend/**` except the modeling paths ([modeling-benchmark.md](modeling-benchmark.md)); `build/**`,
`config/**`, `tools/**` (not the app's converters), `deploy/**`, `demo/**`, `samples/README.md`,
`samples/session.schema.json`. Shared: `spec/contracts/**` and `SPEC.md` change only in their own PR (BUILD §0.4).

**Contracts.** Receives `scan-package`, `still`, `meter-reading`, `part-on-hand` from the app; vision `finding`s
from the modeling slots. Produces `suspect`, `next-action`, `repair-plan`, `shopping-quote`, `reference-design`.
Phase 2 talks to Grain only through the `grain-*` schemas and the GRAIN slot.

**Cards, in order.** Loop: [AGENTS.md](../AGENTS.md). `P1` = Phase 1 (no Grain), `P2` = needs Grain ([BUILD.md Phases](../BUILD.md#phases-phase-1-now-no-grain-phase-2-with-grain)); `←` = depends on; links go to the card text.
- **Scaffold, tooling, codegen, config, registry, hour 0 (L6)**: [B0.1](../BUILD.md#b01-repo-scaffold-makefile-license-notice-gitignore) P1 0.5 h · [B0.8](../BUILD.md#b08-build-tooling-tasksyaml-doctorpy-nextpy-statuspy) P1 0.5 h · [B0.2](../BUILD.md#b02-contract-codegen-pydantic-v2-swift-codable-typescript-types) P1 1.5 h ← B0.1 · [B0.6](../BUILD.md#b06-config-and-secrets) P1 0.5 h ← B0.1 · [B0.9](../BUILD.md#b09-provider-registry) P1 1 h ← B0.6 · [B0.7](../BUILD.md#b07-recorded-session-format-and-replay-runner) P1 1 h ← B0.2 · [M.HOST.3](../BUILD.md#mhost0--mhost1--mhost2--mhost3-hosting) P1 0.5 h ← B0.1 · [B0.11](../BUILD.md#b011-hour-0-jev-key-and-one-working-jev-call) P1 0.5 h · [B0.10](../BUILD.md#b010-hour-0-decisions-demo-product-grain-up-key-tunnel) P2 1 h
- **Backend: state, blobs, gateway (L6, L2)**: [M.STATE.0](../BUILD.md#mstate0-state-slot-interface-and-conformance-suite) P1 0.75 h ← B0.9, B0.2 · [M.STATE.11](../BUILD.md#mstate11-sqlite-state-implementation-hackathon) P1 2 h ← M.STATE.0 · [M.BLOB.0](../BUILD.md#mblob0--mblob1--mblob2-blob-store) P1 0.25 h ← B0.9 · [M.BLOB.1](../BUILD.md#mblob0--mblob1--mblob2-blob-store) P1 0.5 h ← M.BLOB.0 · [S2.1](../BUILD.md#s21-fastapi-gateway) P1 2 h ← B0.2, B0.6, B0.9, M.STATE.0
- **Fusion, Jev, next action (L4, L3)**: [F4.1](../BUILD.md#f41-evidence-to-words-the-state-builder) P1 1.5 h ← B0.2 · [M.FUSION.0](../BUILD.md#mfusion0-fusion-slot-interface) P1 0.5 h ← B0.9, F4.1 · [M.FUSION.1](../BUILD.md#mfusion1-jev-default--mfusion2-rules-registered-as-an-implementation) P1 1.5 h ← M.FUSION.0, B0.11 · [F4.4](../BUILD.md#f44-suspect-ranking-and-nextaction) P1 2 h ← M.FUSION.0, F4.1
- **Parts, I have X, LLM (L5)**: [M.LLM.0](../BUILD.md#mllm0--mllm1--mllm2--mllm3-text-llm) P1 0.5 h ← B0.9 · [M.LLM.1](../BUILD.md#mllm0--mllm1--mllm2--mllm3-text-llm) P1 1 h ← M.LLM.0 · [K6.1](../BUILD.md#k61-parts-on-hand-store-and-matching-order) P1 1 h ← S2.1 · [K6.2](../BUILD.md#k62-datasheet-fetch-and-extraction) P1 2 h ← S2.1, M.LLM.0 · [K6.3](../BUILD.md#k63-quick-check-in-code-safety-rules-s1s5-protective-part-logic) P1 2.5 h ← K6.1, K6.2, M.FUSION.0
- **Grain client, reference, alignment, cross-check (L2; Phase 2)**: [M.GRAIN.0](../BUILD.md#mgrain0-grain-slot-interface-and-conformance-suite) P2 1 h ← B0.9, B0.2 · [M.GRAIN.1](../BUILD.md#mgrain1-live-grain-client) P2 2.5 h ← M.GRAIN.0 · [M.GRAIN.2](../BUILD.md#mgrain2-replay-double-ci-and-dev-only) P2 1 h ← M.GRAIN.0 · [S2.3](../BUILD.md#s23-reference-cache-unknown-product-brief-no-harness-degradation) P2 1.5 h ← M.GRAIN.0 · [S2.4](../BUILD.md#s24-alignment) P2 2.5 h ← B0.7, S2.3 · [S2.5](../BUILD.md#s25-per-part-geometry-compare) P2 1.5 h ← S2.4 · [S2.6](../BUILD.md#s26-mode-b-target-list) P2 1.5 h ← S2.5, F4.5 · [F4.7](../BUILD.md#f47-grain-cross-check-and-merge) P2 2 h ← M.GRAIN.1, S2.4, S2.9
- **Meter planner + electrical (L4; Phase 2)**: [F4.5](../BUILD.md#f45-meter-test-planner) P2 2.5 h ← S2.3 · [F4.6](../BUILD.md#f46-electrical-expectations-and-local-range-check) P2 2 h ← S2.3, S2.8
- **Plan, simulation check, confirm, pilot CAD (L5, L4; Phase 2)**: [K6.4](../BUILD.md#k64-simulation-check-via-grain) P2 1.5 h ← M.GRAIN.0, K6.3 · [K6.6](../BUILD.md#k66-assistant-tool-loop-and-plan-writer) P2 2.5 h ← F4.4, F4.7, K6.3, M.LLM.0 · [K6.7](../BUILD.md#k67-confirm-loop) P2 1.5 h ← K6.6, F4.8 · [K6.8](../BUILD.md#k68-stretch-grain-pilot-cad-for-a-made-part) P2 3 h stretch ← B0.10
- **Pipeline, integration, demo (L6)**: [F4.8](../BUILD.md#f48-pipeline-orchestrator) P2 2 h ← F4.4, F4.7, S2.6, V3.5, M.RENDER.0, S2.1, M.STATE.11, M.FUSION.1 · [O9.5](../BUILD.md#o95-demo-script-dry-run-backup-video) P2 3 h ← K6.7, C1.9
- **Later track** (kept, not in the 24 h). P1: B0.3, B0.4, B0.5, F4.3, K6.5, O9.1, O9.3, O9.4, M.STATE.1, M.STATE.2, M.STATE.3, M.STATE.4, M.STATE.5, M.STATE.6, M.STATE.7, M.STATE.8, M.STATE.9, M.STATE.10, M.BLOB.2, M.FUSION.2, M.LLM.2, M.LLM.3, M.SUPPLIER.0, M.SUPPLIER.1, M.SUPPLIER.2, M.SUPPLIER.3, M.HOST.0, M.HOST.1, M.HOST.2. P2: O9.2, M.GRAIN.3.

**Phase 1 gap (owner to decide).** No Grain-free pipeline card exists yet: F4.8 depends on the Grain cross-check
(F4.7), alignment and render. Phase 1 needs one (scan package → detector boxes → full-res crops → VLM/OCR →
findings → Jev → next action), as a new card or a Phase 1 split of F4.8.

**Setup (doctor items).** `dev_tools`, `repo_access`, `jev_key`, `anthropic_key`; Phase 2 adds `grain_key`,
`grain_project`; the demo adds `multimeter`, `test_products`, `iphone`.

**What's ready.** `python3 -I build/next.py --stream build` (`--phase1` to skip Grain work).

**Hands off / receives.** Gives the app the gateway (S2.1) and live events; gives modeling the registry, blob store
and recorded-session format (B0.7, B0.9); receives Grain API capabilities from Sritej's Grain-side cards.

**Done when.** A recorded real session replays end to end (`make replay`) into findings, suspects and a next action
with Jev on the real word state; the meter overrides any engine answer in code (I2); Phase 2 adds the Grain
cross-check and a plan that passes the validator. `make test` and `check.py` green.
