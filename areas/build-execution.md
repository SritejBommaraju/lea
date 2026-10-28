# Build execution (stream `build`)

**Who.** Rohan, driving coding agents.

**Delivers.** Everything that is not the app or the data: contracts and codegen, config and registry, the gateway, state
and blobs, sessions, the design engine client and the report built from its answer, the import fallback, CI and the
licence check. The Phase 1 pipeline, Jev fusion and the LLM slot were removed on 2026-10-28 (PLAN.md §10).

**Owns.** `backend/**` except the modeling paths ([modeling-benchmark.md](modeling-benchmark.md)); `build/**`,
`config/**`, `tools/**`, `spec/contracts/**` (a contract change is its own PR), `.github/**`,
`samples/README.md`, `samples/session.schema.json`.

**Contracts.** Receives `scan-package`, `still`, `meter-reading` from the app. Produces `finding`s (source `grain`, or
`repairos.meter` for a row a reading settled) and the `report` (B9).

## Tasks, in order

| Id | What | Deps |
|---|---|---|
| B1-B6 | Scaffold, contracts, config, state and blobs, gateway, sessions | done |
| B9 | `report` contract | B2 |
| B12 | Import fallback: Stray Scanner first; 3D Scanner App after a terms check| B6 |
| B14 | CI: `make check` on every PR | B1 |
| B15 | Licence check against the L2 allowlist | B14 |
| B16 | Design engine (Grain): client, report from its answer, readings, Fix it | B5 |

B7, B8, B10, B11 and B13 are removed (PLAN.md §7).

"Done when" for each: [PLAN.md](../PLAN.md) §7. Milestone MS3 (report on the phone) is shared with the app stream.

**Setup (doctor items).** `dev_tools`, `repo_access`, `grain_key`, `anthropic_key`; `test_products` for B12.

**Done when.** A completed scan is judged by the design engine and the gateway serves the report to the app;
`make check` runs on every PR.
