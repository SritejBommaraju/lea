# RepairOS build status

Generated 2026-10-24 09:21 UTC by `python3 -I build/status.py`: profile **hackathon**, track **demo**, **24 h left**. Do not edit by hand; regenerate and commit through your card's PR.

## The 24-hour demo path

| Cards (counted) | Done | In progress | Blocked | Todo | Remaining | Optional / stretch (separate) |
|---|---|---|---|---|---|---|
| 67 | 0 | 1 | 0 | 66 | 95.5 h | 1 |

**Critical path: 16.5 h**: `B0.1` (0.5) -> `B0.2` (1.5) -> `M.GRAIN.0` (1) -> `S2.3` (1.5) -> `S2.4` (2.5) -> `S2.5` (1.5) -> `S2.6` (1.5) -> `F4.8` (2) -> `K6.7` (1.5) -> `O9.5` (3)

**Fits**: 16.5 h of chained work in 24 h left, 7.5 h slack (the demo dry run, backup video and final run sit in that slack; BUILD.md §0.A).

Load per lane (hours of unfinished demo cards; a lane is one person plus agents):

| Lane | People | Cards left | Hours left | In progress | Fits |
|---|---|---|---|---|---|
| L1 | Elysi (with Sritej) | 9 | 16 | - | yes |
| L2 | Rohan | 7 | 13 | - | yes |
| L3 | Sritej + Elysi (Sritej lands the Grain-side cards S2.7-S2.11) | 14 | 18.5 | - | yes |
| L4 | Jayden | 8 | 14 | - | yes |
| L5 | Samik | 11 | 16 | - | yes |
| L6 | Agents, coordinated by Sritej; Rohan runs the H0 scaffold cards; Samik + Jayden take the demo script and video from H16 | 18 | 18 | B0.8 | yes |

**Demo path complete: NOT YET.**

## Ready now (deps done, todo; human checks applied)

| Card | Lane | Est. h | Title |
|---|---|---|---|
| B0.10 | L3 | 1 | Hour 0: decisions, demo product, Grain server up with a key, LAN or tunnel URL, backend/.env |
| B0.11 | L3 | 0.5 | Hour 0: Jev (TypeSafe) key in backend/.env and one working Jev call on a hand-written RepairOS word state |
| S2.7 | L3 | 0.5 | Grain side, H0: the running Grain serves current scan diagnosis (`not_visible` parts) and the pilot CAD endpoints; private fixtures re-recorded |
| B0.1 | L6 | 0.5 | Repo scaffold, Makefile, LICENSE, NOTICE, .gitignore |

## Waiting, and on what

| Card | Lane | Waiting on |
|---|---|---|
| C1.1 | L1 | human: xcode, apple_signing |
| C1.2 | L1 | human: xcode, apple_signing |
| C1.3 | L1 | deps: C1.1 |
| C1.4 | L1 | deps: C1.3 |
| C1.5 | L1 | deps: C1.1, S2.1 |
| C1.7 | L1 | deps: B0.7 |
| C1.9 | L1 | deps: C1.4, C1.5 |
| I5.1 | L1 | deps: C1.3 |
| I5.2 | L1 | deps: I5.1 |
| F4.7 | L2 | deps: M.GRAIN.1, S2.4, S2.9 / human: grain_key, grain_project |
| M.GRAIN.0 | L2 | deps: B0.9, B0.2 |
| M.GRAIN.1 | L2 | deps: M.GRAIN.0 / human: grain_key, grain_project |
| M.RENDER.1 | L2 | deps: M.RENDER.0 |
| S2.1 | L2 | deps: B0.2, B0.6, B0.9, M.STATE.0 |
| S2.3 | L2 | deps: M.GRAIN.0 / human: grain_key, grain_project |
| S2.4 | L2 | deps: B0.7, S2.3 |
| M.DET.0 | L3 | deps: B0.9 |
| M.DET.1 | L3 | deps: M.DET.0 |
| M.FUSION.1 | L3 | deps: M.FUSION.0, B0.11 |
| M.RENDER.0 | L3 | deps: B0.9, S2.3 |
| M.SEG.0 | L3 | deps: B0.9, M.RENDER.0 |
| S2.10 | L3 | deps: S2.7 |
| S2.11 | L3 | deps: S2.7 |
| S2.5 | L3 | deps: S2.4 |
| S2.8 | L3 | deps: S2.7 |
| S2.9 | L3 | deps: S2.7 |
| V3.5 | L3 | deps: M.SEG.0, M.VLM.0, S2.5 |
| F4.1 | L4 | deps: B0.2 |
| F4.4 | L4 | deps: M.FUSION.0, F4.1 |
| F4.5 | L4 | deps: S2.3 |
| F4.6 | L4 | deps: S2.3, S2.8 / human: grain_key |
| I5.4 | L4 | deps: C1.1, F4.5 |
| K6.7 | L4 | deps: K6.6, F4.8 |
| M.FUSION.0 | L4 | deps: B0.9, F4.1 |
| S2.6 | L4 | deps: S2.5, F4.5 |
| I5.5 | L5 | deps: C1.1 |
| I5.6 | L5 | deps: C1.1 |
| K6.1 | L5 | deps: S2.1 |
| K6.2 | L5 | deps: S2.1, M.LLM.0 |
| K6.3 | L5 | deps: K6.1, K6.2, M.FUSION.0 |
| K6.4 | L5 | deps: M.GRAIN.0, K6.3 / human: grain_key, grain_project |
| K6.6 | L5 | deps: F4.4, F4.7, K6.3, M.LLM.0 |
| K6.8 | L5 | deps: B0.10 / human: grain_key |
| M.LLM.0 | L5 | deps: B0.9 |
| M.LLM.1 | L5 | deps: M.LLM.0 |
| M.VLM.0 | L5 | deps: B0.9 |
| M.VLM.1 | L5 | deps: M.VLM.0 |
| B0.2 | L6 | deps: B0.1 |
| B0.6 | L6 | deps: B0.1 |
| B0.7 | L6 | deps: B0.2 |
| B0.9 | L6 | deps: B0.6 |
| E8.6 | L6 | deps: F4.8, C1.9 |
| F4.8 | L6 | deps: F4.4, F4.7, S2.6, V3.5 +4 |
| I5.3 | L6 | deps: C1.1 |
| M.BLOB.0 | L6 | deps: B0.9 |
| M.BLOB.1 | L6 | deps: M.BLOB.0 |
| M.GRAIN.2 | L6 | deps: M.GRAIN.0 |
| M.HOST.3 | L6 | deps: B0.1 |
| M.OCRB.3 | L6 | deps: B0.9 |
| M.SEG.4 | L6 | deps: M.SEG.0 |
| M.STATE.0 | L6 | deps: B0.9, B0.2 |
| M.STATE.11 | L6 | deps: M.STATE.0 |
| O9.5 | L6 | deps: K6.7, C1.9 |

## Grain changes and targets the demo uses (S2.7-S2.11 must land; BUILD.md §0.D)

- G20: quick-tunnel URL changes on restart: GRAIN_URL lives in backend/.env, never in code; prefer Grain on the demo laptop: B0.10
- G6: must land at H0 (diagnose returns not_visible for parts the scan did not cover; pilot endpoints reachable with our key): S2.7
- Reference data per product model: I5.2
- T1 + T2 (G2, G22): must land by ~H4; Sritej re-records the private fixtures and check.py's gap ledger shrinks: S2.8
- T1 range verdicts and T2 verdict on every row: land in S2.8 by ~H4 (Grain side, must land): F4.6
- T10 version + Idempotency-Key (until it lands: snapshot /call/schema and fail on drift; list scans before re-upload; N5, N11): M.GRAIN.1
- T11 last_build.hash (until it lands: cache key = project id + last_build.seconds + artifacts; Q7): S2.3
- T11 project copy (until it lands: copy the project through the project files API into a new project; G16): K6.4
- T12 + T13 (G1, G29): first real capture through diagnose at H6, must hold by ~H10: S2.10
- T12 + T13 (S2.10): these captures go through diagnose at H6; the staged fault is a part that leaves open space behind it (G1, G29): C1.9
- T12 + T13 real-scan tuning and the free-space missing-part test land in S2.10 by ~H10 (must land): F4.7
- T3 (G23): must land by ~H5; Sritej re-records the private /call fixture: S2.9
- T3 fix subject (S2.9): K6.6
- T3 fix subject + sources: lands in S2.9 by ~H5 (Grain side, must land): F4.7
- T4 findings in /call and T5 photo references: after the demo path; we merge suspects with fixes on our side and send nothing photo-shaped in /call: F4.7
- T5 photo metadata (until it lands: our own full-res crops and our own VLM question per crop; G7, G8, G9): V3.5
- T6 detach (until it lands: chat with detach: true, poll GET /projects/{id}; pre-run before the demo, Q8): K6.4
- T6 detached /call (until it lands: no photos and no rebuild in /call; G3): M.GRAIN.1
- T7 (G25, G26): lands inside the 24 hours (~H14): S2.11
- T7 pin positions land in S2.11 (~H14): project pins when out/pins.json exists, node bounds always (G25): M.RENDER.0
- T7 pin positions land in S2.11 (~H14): targets stay one per connector node; pins refine the AR probe labels (G25, G26): S2.6
- T7 pins.json + one pin naming lands in S2.11 (~H14); connector-level targets from GLB node bounds are the demo question either way (G25, G26): S2.3
- T8 expected values on every power-path net (until it lands: ~0 ohm for wires, fuses, closed switches; datasheet ranges; G13): F4.5
- T8 expected values on every power-path net: later; the wizard uses ~0 ohm for wires, fuses, closed switches and datasheet ranges (G13): F4.6
- T9 gravity on upload (until it lands: our ICP with gravity + fit gate, upload in the design frame with align=none; G4, G5): S2.4

## Human steps outstanding (this profile and track)

- **apple_signing** (NOT MET): Apple ID in Xcode with a development team for the RepairOS target. Blocks C1.1, C1.2. How: Xcode > Settings > Accounts > add Apple ID; put DEVELOPMENT_TEAM in ios/Config/Signing.xcconfig (gitignored).
- **grain_key** (NOT MET): Grain URL and key in backend/.env as GRAIN_URL and GRAIN_KEY. Blocks F4.6, F4.7, K6.4, K6.8, M.GRAIN.1, S2.3. How: Sritej starts Grain (private/grain/GRAIN_SPEC.md §2) and issues a RepairOS API key (sent as `Authorization: Bearer <key>`). GRAIN_URL = the Grain laptop's LAN URL or its tunnel URL (a quick tunnel changes on restart, G20). backend/.env only; never commit.
- **grain_project** (NOT MET): The demo product's Grain project id (with model.glb, harness.csv, elec.json, bom) as GRAIN_TEST_PROJECT in backend/.env. Blocks F4.7, K6.4, M.GRAIN.1, S2.3. How: Owner picks at H0 (Q1). Until then use the test project Sritej names (the one behind the private fixtures in private/grain/observed/). Set GRAIN_TEST_PROJECT=<id> in backend/.env
- **iphone** (unverified): An iPhone 16 Pro or Pro Max, developer mode on, connected to the Mac. Blocks C1.1, C1.2, C1.3, C1.4, C1.7, C1.9, I5.1, I5.2, O9.5. How: Settings > Privacy & Security > Developer Mode on; plug in via USB; trust the computer; check `xcrun devicectl list devices`
- **multimeter** (unverified): A multimeter with continuity (beep), DC volts and ohms. Blocks C1.9, O9.5. How: Any basic DMM. Note its continuity threshold (ohms) in session.json
- **test_products** (unverified): The demo product on the table (Q1, picked at H0; two units if possible); later 3+ categories for evaluation. Blocks C1.9, E8.6, O9.5. How: Owner picks (Q1). Record it in samples/sessions/<id>/session.json. Never put product facts in code.
- **xcode** (NOT MET): Xcode 26.x with Swift 6.x and the iOS 26 SDK on the dev Mac. Blocks C1.1, C1.2. How: Install Xcode from the App Store, open it once, run `xcode-select --install`, then `xcodebuild -version`

## Module slots (profile `hackathon`)

| Slot | Required | Selected | Interface cards | Selected impl cards | Optional extras (not counted) | Slot done |
|---|---|---|---|---|---|---|
| STATE | yes | `sqlite` | M.STATE.0 (todo) | M.STATE.11 (todo) | - | no |
| BLOB | yes | `local` | M.BLOB.0 (todo) | M.BLOB.1 (todo) | - | no |
| GRAIN | yes | `live` | M.GRAIN.0 (todo) | M.GRAIN.1 (todo) | - | no |
| FUSION | yes | `jev` | M.FUSION.0 (todo) | M.FUSION.1 (todo) | - | no |
| LLM | yes | `anthropic` | M.LLM.0 (todo) | M.LLM.1 (todo) | - | no |
| VLM | yes | `anthropic` | M.VLM.0 (todo) | M.VLM.1 (todo) | - | no |
| DET | yes | `gdino` | M.DET.0 (todo) | M.DET.1 (todo) | - | no |
| SEG | yes | `none` | M.SEG.0 (todo) | M.SEG.4 (todo) | - | no |
| OCRB | yes | `none` | - | M.OCRB.3 (todo) | - | no |
| OCRD | no | `apple_vision` | - | - | - | no |
| DEPTH | no | `none` | - | - | - | no |
| RENDER | yes | `trimesh` | M.RENDER.0 (todo) | M.RENDER.1 (todo) | - | no |
| SUPPLIER | yes | `bom_and_web` | - | - | - | no |
| TRAIN | no | `none` | - | - | - | no |
| HOST | yes | `local_mac` | - | M.HOST.3 (todo) | - | no |
| SPEECH | no | `typed_only` | - | - | - | no |

## Optional and stretch work (tracked separately; never keeps the build unfinished)

- K6.8 (stretch, todo): Stretch: Grain pilot CAD: mount or enclosure edit by text, STEP + STL export, rough print estimate, phone-desktop room

## Later track (after the hackathon; all cards stay in the registry)

74 cards: 0 done, 74 todo, 0 blocked. Run `python3 -I build/status.py --track all --profile full` for the full build.

Full build critical path (profile `full`, all tracks): about **15.5 agent-days**: `B0.1` -> `B0.6` -> `B0.9` -> `M.GRAIN.0` -> `S2.3` -> `S2.4` -> `S2.5` -> `V3.5` -> `F4.8` -> `K6.7` -> `O9.5`

## Fully works checklist (global definition of done, BUILD.md §9; after the hackathon)

| # | Item | Met | Missing |
|---|---|---|---|
| D1 | Scan any product with a cached reference and get aligned per-part findings, with insufficient_evidence where fit or coverage is missing | no | C1.4, C1.5, S2.1, S2.3, S2.4, S2.5, F4.8 |
| D2 | Mode B close-ups give wire-check and connector findings with evidence crops and renders | no | I5.1, V3.4, V3.5 |
| D3 | Fusion ranks suspects and picks the next action, with the selected fusion engine; words not numbers | no | F4.1, F4.3, F4.4 |
| D4 | The meter wizard proves or clears an electrical fault by bisection; power state stated before probe points | no | F4.5, F4.6, I5.4 |
| D5 | Grain cross-check runs against the real Grain API with schema validation and version pinning; the plan says when it was skipped | no | F4.7 |
| D6 | Parts on hand get yes / yes_with_changes / no / need_simulation_check with S1-S5 in code; shopping is deterministic within budget | no | K6.1, K6.2, K6.3, K6.5 |
| D7 | A validated RepairPlan (I2, S3) is shown with AR labels and confirmed by rescan or re-reading | no | K6.6, K6.7, I5.6, I5.2 |
| D8 | Unknown product: reference built by a real Grain design turn, cached, design_trust=low, discovery mode; no harness degrades to discovery + symptom-driven meter guide | no | S2.3, V3.7 |
| D9 | Generalisation measured: held-out categories at or above the gate; guard green (no product names, refs or provider names hard-coded) | no | B0.4, E8.1, E8.2, E8.3; evidence file eval/results/latest.json |
| D10 | Calibration measured per product class; probabilities stay words until validated | no | E8.4; evidence file eval/results/calibration.json |
| D11 | Safety suite green: polarity never from colour, protective parts no when unconfirmed, power state before probes | no | E8.5 |
| D12 | Release ready: LICENSE, NOTICE, licence manifest green, no secrets on the phone, replay and dev-only providers locked out, evidence trail per scan | no | B0.5, O9.2, O9.3, O9.4 |
| D13 | Live state: findings, suspects, next actions and plan appear on the phone as they are written, through the selected STATE implementation | no | S2.1 |

**RepairOS fully works: NOT YET.**
