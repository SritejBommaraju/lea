# Modeling + benchmark (stream `modeling`)

**Who.** One person (the owner assigns).

**Delivers.** The recorded real captures and, later, the benchmark that scores the design engine's judgement against
them. Everything perception-shaped that used to live here (the detector, crops, VLM crop checks, the defect classifier,
OCR, `make eval` for the pipeline) was removed on 2026-10-28 when Grain took over judging (PLAN.md §10).

**Owns.** `eval/**`, `samples/sessions/**`.

**Contracts.** The benchmark reads recorded sessions (`samples/session.schema.json`) and the report the gateway built
from the design engine's answer.

## Tasks, in order

| Id | What | Deps |
|---|---|---|
| M4 | Benchmark data: labelled real sessions, held-out products | - |

M1-M3 and M5-M9 are removed (PLAN.md §7). A new benchmark runner is an open question (PLAN.md §11).

**Rules that bite here.** No target number before a baseline is measured. Held-out products never reach tuning or
training. Captures from closed third-party apps are never used for training or benchmarking.

**Setup (doctor items).** `dev_tools`, `test_products`, `iphone`.

**Done when.** `make sessions-check` passes on every recorded session and the held-out ones are never read by anything
but the benchmark.
