# Build execution (stream `build`)

**Who.** Rohan, driving coding agents.

**Delivers.** Everything that is not the app or the models: contracts and codegen, config and registry, the
gateway, state and blobs, sessions and replay, Jev fusion (all built); the report contract, the Phase 1 pipeline
orchestrator, report generation, the import fallback, pose-graph refinement, CI and the licence check (to build).

**Owns.** `backend/**` except the modeling paths ([modeling-benchmark.md](modeling-benchmark.md)); `build/**`,
`config/**`, `tools/**`, `spec/contracts/**` (a contract change is its own PR), `.github/**`,
`samples/README.md`, `samples/session.schema.json`.

**Contracts.** Receives `scan-package`, `still`, `meter-reading` from the app and vision `finding`s from the
modeling slots. Produces `suspect`, `next-action` and the `report` (B9). No Grain calls in Phase 1.

## Tasks, in order

| Id | What | Deps |
|---|---|---|
| B1-B8 | Scaffold, contracts, config, state and blobs, gateway, sessions, Jev fusion, LLM slot | done |
| B9 | `report` contract | B2 |
| B10 | Phase 1 pipeline orchestrator (no Grain) | B9 |
| B11 | Report generation: verdict in code, plain words by Claude, route and SSE | B9, B10 |
| B12 | Import fallback: Stray Scanner first; 3D Scanner App after a terms check| B6 |
| B13 | Backend pose-graph refinement with Open3D and OpenCV, only if on-phone drift correction falls short | B6 |
| B14 | CI: `make check` on every PR | B1 |
| B15 | Licence check against the L2 allowlist | B14 |

"Done when" for each: [PLAN.md](../PLAN.md) §7. Milestone MS1 (replay to report) is this stream's.

**Setup (doctor items).** `dev_tools`, `repo_access`, `jev_key`, `anthropic_key`; `test_products` for B12.

**Done when.** `make replay` on a recorded real session writes a valid report with zero honesty violations and
zero Grain calls; the gateway serves it to the app; `make check` runs on every PR.
