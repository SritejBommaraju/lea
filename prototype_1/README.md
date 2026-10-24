# Lea prototype 1 (Samik)

A working prototype of the copilot flow, kept in its own folder so it does not touch the Phase 1 code on `main`.
It is a different stack from `main` (a TypeScript bridge over SpacetimeDB rather than the Python gateway) and it
calls Grain, which Phase 1 does not. Treat it as a reference for what to fold into the plan, not as Phase 1 work.

| Folder | What |
|---|---|
| `ios/` | The SwiftUI app (`Lea.xcodeproj`, sources in `ios/MyApp/`) |
| `services/` | Bridge, SpacetimeDB module, reconstruction + Open3D QA, whiteboard bundle, wake-word trainer; see `services/README.md` |

## What it does

Guided capture (Apple Object Capture on LiDAR iPhones, orbit photos + Mac reconstruction elsewhere), Open3D scale /
holdout / coverage / reference-fit gating, evidence-linked candidates, a Claude tool-using voice copilot that sees
the camera, Jev live coaching and part location, VEL's turn-taking and whiteboard, safe checks with code-judged
ranges, measured feature extraction (planes, walls, holes) into a design request, and versioned design turns.

## Where it departs from AGENTS.md

- **Stand-ins in tests.** Some tests use a local stand-in Jev server, a scripted stand-in for Claude that calls the
  real tools, and a synthetic mesh in place of Object Capture (labelled in each file). That breaks "nothing fake";
  they test Lea's plumbing and gates only. Replace them with recorded real responses when keys and captures exist.
- **Grain calls.** The bridge uses Grain's chat, files and events endpoints (API surface only).
- **Guard false positives.** `tools/guard_proprietary.py` flags four imports of SpacetimeDB's own test-utils
  subpath as a Grain source path. They are a third-party package path, not Grain.

## Not verified yet

Nothing has run on an iPhone; SpacetimeDB (live server), Claude, Jev, Cartesia and the "Hey Leah" model have not run
for real. `services/README.md` lists the bring-up steps and what each test does and does not prove.
