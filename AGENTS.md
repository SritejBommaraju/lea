# AGENTS.md: start here

The single entry point for every person on the team and every Claude or agent working for them.

## 1. What this is

LEA (RepairOS in the code and docs) is an iPhone 16 Pro app plus a backend that scans a broken hardware product with
LiDAR and the cameras, finds what is broken, proves electrical faults with a multimeter and writes a repair plan.
It must work for **any** product. Phase 1 (now) runs without Grain; Phase 2 adds Grain, the owner's design engine.

## 2. Read order

1. This file.
2. Your stream's file in [areas/](areas/README.md).
3. [BUILD.md](BUILD.md): the Phases section, §0 (the 24-hour plan), then your card in §5.
4. [SPEC.md](SPEC.md) when a card touches a rule: §7 invariants, §10 what we need from Grain.
5. [spec/contracts/README.md](spec/contracts/README.md) when a card reads or writes a contract.

## 3. Pick your stream, open its file

| Stream | File | Who |
|---|---|---|
| App (iOS capture and screens) | [areas/app.md](areas/app.md) | one person |
| Modeling + benchmark (perception slots, eval) | [areas/modeling-benchmark.md](areas/modeling-benchmark.md) | one person |
| Build-plan execution (everything else) | [areas/build-execution.md](areas/build-execution.md) | Rohan + agents |
| Grain side (Phase 2) | [areas/README.md](areas/README.md) | Sritej |

Each file lists the paths you own, the contracts at your boundary, your cards in order with their phase, the setup
you need and "done when". Lanes L1-L6 in BUILD.md are aliases.

## 4. One-time setup

```
python3 -I build/doctor.py              # prerequisites; HUMAN ACTION NEEDED = ask your human for that step
cp backend/.env.example backend/.env    # then fill in the keys (never commit it)
make bootstrap                          # backend venv from the lock file
make hooks                              # pre-commit guard: blocks Grain internals and private files
```

Grain material (spec sheet, status, recorded responses) is private: get `private/grain/` from Sritej. Everything
works without it; `check.py` then skips the private fixtures.

## 5. The loop

1. `python3 -I build/next.py --stream <app|modeling|build>` (add `--phase1` for Phase 1 only). Pick a ready card.
2. Say the card id and your branch in your stream's channel. That is the claim; no issue, no status edit.
3. `git fetch origin && git switch -c <you>/<card-id>-<slug> origin/main` (next.py prints the slug).
4. Build test-first, inside your stream's owned paths and the card's files (BUILD.md §5, `owns` in tasks.yaml).
5. `make test` and `python3 -I spec/contracts/check.py`.
6. `git fetch origin && git rebase origin/main`, then a PR to `main`:
   - title starts with the card id: `S2.4 Alignment: gravity-aware ICP`;
   - body: what changed, why, how you tested (the template asks).
   Squash-merge keeps the title, and `next.py` counts a card whose id leads a commit on `main` as done.

Stuck on a missing key, device or service: say so in the channel and pick another card. Never fake around it.

## 6. Rules

- **PRs only.** Never push to `main` or to someone else's branch.
- **Grain is proprietary.** Only the API surface we call goes in the repo: endpoints, request and response field
  shapes, units and frames we must send. Never Grain source paths or module names, its algorithms, thresholds or
  parameters, the model it calls, internal timings, recorded raw responses or notes on its code. Write a need
  ("Grain must return a verdict per reading") or a pointer (`private/grain/GRAIN_SPEC.md`, ask Sritej). The
  pre-commit guard (`tools/guard_proprietary.py`) enforces it.
- **Nothing fake.** No fakes, stubs or mocks of missing services. Recorded real responses in tests are fine
  (recorded Grain responses stay private).
- **No product-specific code.** No product names, refs, nets, colours or class lists in code (SPEC GEN1); product
  facts live in `samples/` and the reference.
- **Secrets only in `backend/.env`.** The phone never holds a key.
- **Stay in your owned paths.** A contract change (`spec/contracts/`) is its own PR. A behaviour change updates
  SPEC.md with a decision-log row in the same PR.

The safety and evidence invariants that code must keep (vision is a suspect, only a meter reading is proof,
unseen is `not_inspected`, `insufficient_evidence` is a real answer, power state before probe points) are in
SPEC.md §7. If a card would break one, stop and say so.

## Commands

| Task | Command |
|---|---|
| What is ready | `python3 -I build/next.py [--stream app] [--phase1] [--track all]` |
| Prerequisites | `python3 -I build/doctor.py [--offline]` |
| Status and critical path | `python3 -I build/status.py` (writes `build/STATUS.md`) |
| Tests | `make test`; tooling self-test `python3 -I build/test_tools.py` |
| Contracts | `python3 -I spec/contracts/check.py` |
| Proprietary guard | `python3 -I tools/guard_proprietary.py [paths]` (no paths = staged files) |
| Swap a provider | edit the profile in `config/providers.yaml` or set `REPAIROS_SLOT_<SLOT>=<impl>` |
