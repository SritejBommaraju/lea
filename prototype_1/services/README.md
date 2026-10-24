# Lea services

The laptop half of Lea. The iPhone app (`../MyApp`, project `../Lea.xcodeproj`) talks only to the bridge.

```
iPhone (Lea) --HTTP /v1 + SSE--> bridge (TypeScript) --SDK--> SpacetimeDB (module/, local)
                                   |-- assets on disk (sha256/, immutable originals)
                                   |-- Object Capture (reconstruction/objcap) -> Open3D QA (reconstruction/lea_geometry)
                                   |-- Claude close-up comparison (bridge/src/jobs/closeup.ts)
                                   |-- Grain: existing chat/files/events API, unmodified (bridge/src/grain.ts)
                                   `-- Cartesia token minting (phone streams STT/TTS with the token)
```

| Path | What it is |
|---|---|
| `module/` | SpacetimeDB 2.13 module: private tables, bridge-only views, reducers that enforce the gates (mesh QA verdict, evidence, ranges, one auto CAD turn per scan, pre/post versions) |
| `bridge/` | The only SpacetimeDB client: phone API, job runner, Grain adapter, voice routing |
| `reconstruction/` | `objcap/` Apple Object Capture CLI; `lea_geometry/` Open3D scale/holdout/coverage/fit QA; `step/` STEP re-import check |
| `contract-swift/` | The app's `BridgeClient.swift` compiled for macOS and run against the bridge (`bridge/test/swift-contract.test.ts`) |

## From VEL (the VEL repo, read only)

| VEL | Lea |
|---|---|
| `app/jev.mjs` (Jev typed decisions on images) | `bridge/src/jev.ts`; `bridge/src/look.ts` asks Jev about each coaching frame: usable, what's wrong, which part, where, same area as the reference, looks damaged. `POST /v1/sessions/:sid/look`; frames are never stored |
| `app/look.mjs` (look before teaching) | `MyApp/Capture/CaptureCoach.swift`: Coach in Capture sends a frame every 1.5 s, draws Jev's box and speaks the tip |
| `app/route.mjs` (Jev first, patterns when it does not answer) | `routeTranscriptWithJev` in `bridge/src/voice.ts`; a design change still needs explicit words |
| `app/speech.mjs`, `workers/vel-stt/src/endpoint.rs` (turn-taking) | `MyApp/Voice/TurnTaking.swift`, checked with VEL's own test cases (`contract-swift/turntaking`) |
| `app/voice/stt.mjs` (one event protocol, many recognizers) | `MyApp/Voice/SpeechProviders.swift`: Apple on-device recognition (no key) or Cartesia; system voice or Sonic |
| hands-free barge-in (`main.mjs`) | Hands-free in the voice bar: talk to interrupt; echo and backchannels ignored; "go on" resumes |

Not ported: VEL's Rust Parakeet/Kokoro workers (Rust isn't installed here and they would run on the laptop, not the phone).

## The copilot flow

| You say | What happens | Where |
|---|---|---|
| "Hey Leah, make a render of this" | LiDAR iPhones: Apple's guided Object Capture with spoken feedback, model built on the phone (USDZ to look at, OBJ to measure). Other iPhones: guided orbit photos, model built on the Mac. Either way the model must pass scale/holdout/coverage/fit before it is used | `MyApp/Capture/ObjectScanner.swift`, `ScanView.swift`; `bridge/src/jobs/reconstruction.ts` (`meshAssetId` = phone-built) |
| "What seems to be the issue here?" | Claude (`claude-opus-5-5`) sees the camera frame and session state, answers in 1-3 spoken sentences; candidates stay unverified until a check | `bridge/src/agent.ts` |
| "What's that? Zoom in on it" | Jev locates the part; the phone zooms (as far as keeps it in view), focuses, takes a full-resolution close-up and compares it with the reference photo of that area | `agent.ts` tools, `RootView.swift` `takeCloseup`, `CameraService.zoom` |
| "Open the whiteboard and explain" | VEL's stage (bundled as `MyApp/Whiteboard/vel-stage.js`, rebuilt with `cd whiteboard && npm run build`) draws each step while Lea says it | `MyApp/Whiteboard/` |
| "Let's make a solution for this" | Open3D fits planes, wall thicknesses and holes on the validated scan; the measured spec goes through Grain's own chat (Grain reads text only); versions saved before and after | `reconstruction/lea_geometry/features.py`, `bridge/src/features.ts` |

Without `ANTHROPIC_API_KEY` the bridge answers with the rule-based router instead of the agent.

"Hey Leah": record the phrase (many takes) into `<dir>/hey_leah/` and room noise / other speech into `<dir>/background/`, then
`swift wake/train.swift <dir> ../MyApp/HeyLeah.mlmodel` and rebuild the app. The toggle appears once the model is bundled.

## Tests (run on this Mac, 24 Oct 2026)

- `cd module && npm test`: 23 reducer invariants, run in SpacetimeDB's portable datastore (wasm).
- `cd bridge && npm test`: 23 tests (set `LEA_TEST_GRAIN_PROJECTS` to the local Grain projects folder to include the STEP and model.glb cases) (look/intent use a stand-in Jev; the agent test uses a scripted stand-in for Claude that calls the real tools) over real HTTP, with the module's reducers, the **live local Grain** and **real Open3D**. Object Capture is replaced by a synthetic mesh there, because no real photo set exists yet.
- `cd reconstruction && .venv/bin/python -m pytest tests`: 7 geometry tests (QA, ambiguity, holes, walls, slots, cylinders) on a **synthetic** asymmetric bracket.

None of these is the live SpacetimeDB server, a real photo reconstruction, or the phone.

## Bring-up

1. Install SpacetimeDB (official installer): `curl -sSf https://install.spacetimedb.com | sh`, then `spacetime version install 2.13.0 && spacetime version use 2.13.0`.
2. `spacetime start` (leave running), then `cd module && spacetime publish --server local lea`.
3. No codegen: the bridge builds its database client from `module/src/shapes.ts`, the same shapes the module uses.
4. `cp .env.example .env` and fill in the keys you have. Grain's design agent is configured on the Grain side (ask Sritej for the service URL and key).
5. `cd bridge && npm start` (first run creates the bridge identity), stop it, run `npm run authorize`, then start it again.
6. `npm run pair "iPhone"`. In Lea: Session → gear → the bridge URL it prints plus the token.
7. Xcode: open `Lea.xcodeproj`, choose your team under Signing, then run on the iPhone.

## Maintenance

- `cd bridge && npm run verify`: re-hashes every stored asset (files are named by their SHA-256).
- `cd bridge && npm run backup -- <folder>`: copies the bridge data and SpacetimeDB's data together (stop `spacetime start` first).
- The bridge trims events older than 7 days every 6 hours, clears partial uploads older than an hour at start, and warns when FileVault is off.

## Owner inputs still required

- Physical fixture and two caliper measurements, plus QA limits (typed in Lea's measurement policy; the code has no defaults).
- A reference mesh of the intact object, with known units.
- A Jev key for coaching; a Cartesia key and voice id (optional now: Apple on-device speech works without it); a licensed "Hey Leah" detector implementing `WakeWordDetector`.
- Anthropic key for close-up comparison; a model key for Grain's design agent.
