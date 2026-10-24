# AGENTS.md: start here

For every person on the team and every coding agent working for them.

## What this is

LEA (RepairOS in the code) is an iPhone app plus a backend. It scans any hardware object with LiDAR, guides
full-resolution close-ups, finds defects with our models and writes a report with evidence. Phase 1 makes no
Grain calls. The plan, the invariants and every task are in [PLAN.md](PLAN.md).

## Read order

1. This file.
2. [PLAN.md](PLAN.md) §5 (invariants) and §7 (tasks).
3. Your stream: [areas/app.md](areas/app.md), [areas/modeling-benchmark.md](areas/modeling-benchmark.md) or
   [areas/build-execution.md](areas/build-execution.md).
4. [spec/contracts/README.md](spec/contracts/README.md) when a task reads or writes a contract.

## One-time setup

```
python3 -I build/doctor.py              # prerequisites; HUMAN ACTION NEEDED = ask your human for that step
cp backend/.env.example backend/.env    # then fill in the keys (never commit it)
make bootstrap                          # backend venv from the lock file
make hooks                              # pre-commit guard
```

## The loop

1. `python3 -I build/next.py --stream <app|modeling|build>`. Pick a ready task.
2. Say the task id and your branch in your stream's channel. That is the claim.
3. `git fetch origin && git switch -c <you>/<id>-<slug> origin/main` (next.py prints the slug).
4. Build test-first, inside the task's `owns` paths (PLAN.md §7, `build/tasks.yaml`).
5. `make test`, `make contracts` and, if you touched a schema, `make codegen` then `make codegen-check`.
6. `git fetch origin && git rebase origin/main`, then a PR to `main`. The title starts with the task id
   (`B10 Pipeline orchestrator`); squash-merge keeps it and the tools count the task done.

Stuck on a missing key, device or service: say so in the channel and pick another task. Never fake around it.

## Rules

- **PRs only.** Never push to `main` or to someone else's branch.
- **Grain is proprietary.** Only the API surface we call goes in the repo: endpoints, field shapes, units,
  frames. Never Grain source paths, algorithms, thresholds, model names, timings or recorded responses. Write
  a need ("Grain must return a verdict per reading") or point to `private/grain/` (ask Sritej). Never touch or
  commit `private/`. The guard (`tools/guard_proprietary.py`) enforces it.
- **Nothing fake.** No fakes, stubs or mocks of missing services, devices or data. Recorded real captures and
  responses in tests are fine.
- **Any product.** No product names, refs or class lists in code; the one generic vocabulary is
  `backend/repairos/vision/generic_classes.py`.
- **Honest output.** Vision is a suspect and only a meter reading is proof; not looked at is `not_inspected`,
  never `ok`; `insufficient_evidence` always has a reason; Jev judges and code decides.
- **Secrets only in `backend/.env`.** The phone never holds a key.
- **Licence-clean.** Permissive code, weights and data only (PLAN.md invariant L2).
- **Stay in your owned paths.** A contract change is its own PR. A behaviour change updates PLAN.md in the
  same PR.

If a task would break an invariant, stop and say so.

## Commands

| Task | Command |
|---|---|
| What is ready | `python3 -I build/next.py [--stream app]` |
| Prerequisites | `python3 -I build/doctor.py [--offline] [--stream app] [--tasks A2,M7]` |
| Status | `python3 -I build/status.py` (writes `build/STATUS.md`) |
| Tests | `make test`; tooling self-test `python3 -I build/test_tools.py` |
| Contracts | `make contracts`, `make codegen`, `make codegen-check` |
| Proprietary guard | `make guard` or `python3 -I tools/guard_proprietary.py <paths>` |
| Swap a provider | edit `config/providers.yaml` or set `REPAIROS_SLOT_<SLOT>=<impl>` |
