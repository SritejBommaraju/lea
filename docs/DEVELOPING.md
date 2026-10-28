# LEA (RepairOS)

Scan any hardware object with an iPhone's LiDAR, take the close-ups the app asks for, and get a report on what
looks wrong: wiring faults, chip and board issues, breakage, defects, with the labels and markings around them.

Each finding comes with its photo evidence and a confidence in words. The report says "looks good" or "potential
issues", and it is honest about its limits: parts nobody looked at are `not_inspected`, never ok, and anything
the models cannot decide is `insufficient_evidence` with the reason. A multimeter reading, if you have one, turns
a suspected electrical fault into a proven one.

The code calls the product RepairOS. **[PLAN.md](../PLAN.md) is the plan**: scope, architecture, invariants,
milestones and every task.

## Phase 1 (now)

- **iOS app**: guided LiDAR scan with live coverage on our own ARKit pipeline, guided full-resolution close-ups, resumable
  upload, the report screen.
- **Design engine**: the product was designed in Grain, which judges the scan, photos and meter readings against that
  design; the report is built from its answer, with the fix (what to buy, remake or reseat) and the next meter check.
  "Fix it with Grain" asks it to change the design from the phone.
- **Benchmark**: labelled real captures with held-out products. Every target comes from its measurements.

Built so far: the gateway, state and blobs, recorded sessions, contracts with codegen, the provider registry and config,
the design engine client and the report built from its answer, Lea's voice. Live status: [build/STATUS.md](../build/STATUS.md).

## Quickstart

Needs Python 3.12+, git, gh and Node 22+ (`uv` is used if present). The iOS work also needs Xcode 26 and a LiDAR
iPhone.

```
make bootstrap                          # backend/.venv from backend/requirements.lock
make hooks                              # pre-commit guard: blocks private files
cp backend/.env.example backend/.env    # then add GRAIN_URL, GRAIN_API_KEY and ANTHROPIC_API_KEY (never commit it)
make test                               # backend unit + conformance tests
make run-gateway                        # FastAPI gateway on :8000, the only server the phone talks to
```

Keys live only in `backend/.env`; the phone never holds one. `python3 -I build/doctor.py` says what is missing
and prints HUMAN ACTION NEEDED for steps only a person can do.

## Working here

| Command | What |
|---|---|
| `python3 -I build/next.py [--stream app\|modeling\|build]` | Tasks ready to start, per stream |
| `python3 -I build/doctor.py [--offline]` | Prerequisites for the open tasks |
| `python3 -I build/status.py` | Rewrites `build/STATUS.md` |
| `make contracts`, `make codegen-check` | Schemas vs examples; generated code has no drift |
| `make replay SESSION=<id>`, `make sessions-check` | Load or validate recorded real sessions |
| `make check` | Contracts, codegen drift, guard and tests |

Three streams: [App](../areas/app.md), [Modeling + benchmark](../areas/modeling-benchmark.md) and
[Build execution](../areas/build-execution.md). Start with [AGENTS.md](../AGENTS.md); it applies to people and coding
agents.

Licence: Apache-2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)).
