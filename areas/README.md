# Areas: three streams

Each stream is run by one person, mostly with coding agents. Pick yours, open its file, run its `next.py` line.
Tasks and milestones: [PLAN.md](../PLAN.md) §6-§7; machine twin: [build/tasks.yaml](../build/tasks.yaml).

| Stream | Who | Owns (short) | Next |
|---|---|---|---|
| [App](app.md) | one person (owner assigns) | `ios/**` except generated contracts | `python3 -I build/next.py --stream app` |
| [Modeling + benchmark](modeling-benchmark.md) | one person (owner assigns) | the recorded captures and the benchmark: `eval/**`, `samples/sessions/**` | `python3 -I build/next.py --stream modeling` |
| [Build execution](build-execution.md) | Rohan + agents | the rest of `backend/**`, `build/`, `config/`, `tools/`, contracts, CI | `python3 -I build/next.py --stream build` |

Phase 2 has no stream yet: [PLAN.md](../PLAN.md) §9.

## Contracts at the boundaries

| From → to | What | Schema (`spec/contracts/`) |
|---|---|---|
| App → build (gateway) | scan package (binary PLY in metres, poses, keyframe depth), full-res stills with pose and intrinsics, meter readings | `scan-package`, `still`, `meter-reading` |
| Build → app (gateway, SSE) | the report built from the design engine's answer: findings, the next meter step, the fix, in words | `report` (B9), `finding` |
| Sessions → benchmark | real captures with `truth.json`, loaded by `make replay` | `samples/session.schema.json` |
