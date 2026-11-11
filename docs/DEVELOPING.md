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
- **Detection pipeline**: OWLv2 zero-shot detector (Grounding DINO as the alternative), full-resolution crops, a
  trained defect classifier (planned), Claude VLM for explanations and unclear cases, OCR for labels, Jev
  judgments with the decisions made in code, then the report.
- **Benchmark**: labelled real captures with held-out products. Every target comes from its measurements.

Built so far: the gateway, state and blobs, recorded sessions and replay, the detector slots, crops, the VLM and
LLM slots, Jev fusion, contracts with codegen, the provider registry and config, the iOS app, pipeline orchestrator, 
and report generation. Live status: [build/STATUS.md](../build/STATUS.md).

## Quickstart

Needs Python 3.12+, git, gh and Node 22+ (`uv` is used if present). The iOS work also needs Xcode 27.2 and an iPhone
with iOS 18+ (LiDAR models like iPhone 12 Pro or later give full 3D scans; non-LiDAR models capture photos only).

```bash
make bootstrap                          # backend/.venv from backend/requirements.lock
# Or directly:
python3.12 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.lock
backend/.venv/bin/pip install -e backend

make hooks                              # pre-commit guard: blocks private files
cp backend/.env.example backend/.env    # then add TYPESAFE_KEY and ANTHROPIC_API_KEY (never commit it)

make test                               # backend unit + conformance tests
# Or directly:
backend/.venv/bin/pytest backend/tests

make run-gateway                        # FastAPI gateway on :8000, the only server the phone talks to
# Or directly:
cd backend && .venv/bin/uvicorn repairos.main:app --reload --host 0.0.0.0 --port 8000
```

Keys live only in `backend/.env`; the phone never holds one. `python3 -I build/doctor.py` says what is missing
and prints HUMAN ACTION NEEDED for steps only a person can do.

## Working here

| Command | What |
|---|---|
| `python3 -I build/next.py [--stream app\|modeling\|build]` | Tasks ready to start, per stream |
| `python3 -I build/doctor.py [--offline]` | Prerequisites for the open tasks |
| `python3 -I build/status.py` | Rewrites `build/STATUS.md` |
| `make contracts` or `python3 -I spec/contracts/check.py` | Schemas vs examples |
| `make codegen-check` or `python3 -I tools/codegen/codegen.py --check` | Generated code has no drift |
| `make replay SESSION=<id>` or `backend/.venv/bin/python tools/replay.py --session <id>` | Replay a recorded session |
| `make sessions-check` or `backend/.venv/bin/python tools/replay.py --check --all` | Validate all sessions |
| `make check` | Contracts, codegen drift, guard and tests |
| `make test` or `backend/.venv/bin/pytest backend/tests` | Backend unit tests |

Three streams: [App](../areas/app.md), [Modeling + benchmark](../areas/modeling-benchmark.md) and
[Build execution](../areas/build-execution.md). Start with [AGENTS.md](../AGENTS.md); it applies to people and coding
agents.

Licence: Apache-2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)).
