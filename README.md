# LEA: scan anything, ask Lea, see the answer on your object

**LEA turns an iPhone into a repair assistant: you scan a device, get a 3D model of your actual object with a report on
what looks wrong, then ask Lea anything about it. She answers out loud while she turns the model to the part, circles
it and shows the real photo of that spot.**

Built at Cal Hacks. [How it works](#how-it-works) · [Run it](#run-it) · [Repo map](#repo-map) ·
[Write-up](docs/devpost.md)

## What it does

1. **Scan.** Walk around the object with the iPhone. LiDAR depth, keyframes and full-resolution close-ups are captured
   as you go. Flip the object over and scan its underside too; the server lines the two scans up.
2. **See.** A few seconds later you get a 3D model of your object, built from your own scan. Thin objects like circuit
   boards, which LiDAR can't resolve, get a model built from the photos, with raised parts (headers, jacks,
   capacitors) measured by multi-view stereo. A report lists what looks wrong, how sure it is, and safety issues
   first.
3. **Ask Lea.** Tap and ask: "where's A2?", "what does this wire do?", "is the main chip soldered?". Lea identifies the
   object (for example ELEGOO UNO R3, read off the board), answers with web search when needed, and finds any named
   part in the photos. The camera flies to it, the part lights up, and the close-up photo fades in.

Nothing is built for one product. We tested on a game controller, an Arduino board, a mouse, a toaster oven, a hair
trimmer and a phone charger.

## Why it's hard

- **Any object, no CAD.** The model and every circle Lea draws come from the user's own scan and photos.
- **Thin boards.** A 1.6 mm board sits inside LiDAR's noise. We rectify the photos onto the measured table plane,
  extrude the measured outline, and recover part heights by plane-sweep stereo. A quality gate shows a flat board
  instead of guessed towers when the photos don't agree.
- **Flipped objects.** The underside scan is registered to the top: geometrically for objects with sides, and for
  boards from their outline, through-holes and connector edges.
- **A voice that drives 3D.** Lea's answers carry markers that become camera moves, timed to the sentence she is
  speaking, with the matching real photo.
- **Honest.** Lea never calls a part fine if no photo shows it, and parts nobody saw are never marked OK.

## How it works

```
iPhone app (SwiftUI, ARKit, Metal, SceneKit)
   │  scan package, stills, voice turns                 HTTPS
   ▼
Gateway (FastAPI) ── SQLite + blob store ── Pipeline worker
   │                                         ├─ registration: flips and continued scans
   │                                         ├─ 3D: textured mesh, or photo slab + stereo relief for thin objects
   │                                         ├─ inspect: OWLv2 detector, crops, Claude vision, OCR
   │                                         ├─ locate: findings ray-cast onto the model
   │                                         └─ report
   ├─ Voice: Claude (answers, part index) + Cartesia (speech in and out)
   └─ Live mirror → SpacetimeDB (big-screen viewer)
```

| Component | Job | Tech | Folder |
|---|---|---|---|
| App | Capture, 3D stage, report, talk to Lea | Swift, SwiftUI, ARKit, SceneKit, Metal | `ios/RepairOS/` |
| Gateway | The only server the phone talks to | Python, FastAPI | `backend/repairos/api/` |
| Pipeline | Registration, 3D, inspection, report | PyTorch, Open3D, OWLv2, Claude | `backend/repairos/pipeline/`, `recon/`, `recreation/`, `vision/`, `report/` |
| Voice | Lea's answers and 3D moves | Claude, Cartesia | `backend/repairos/voice/` |
| Live mirror | Scans and models on a big screen | SpacetimeDB | `spacetime/` |
| Hosting | One GPU pod running everything | Runpod | `deploy/runpod/` |

The phone holds no API keys. Every paid call goes through a spend guard with per-scan and per-day caps.

## Repo map

| Folder | What |
|---|---|
| **Product** | |
| `ios/` | The iPhone app (`RepairOS/`) and its Xcode project |
| `backend/` | The gateway, pipeline worker, 3D, vision, report and voice (`repairos/`) |
| `config/` | Pipeline, provider, verdict and vision-model settings |
| `spec/contracts/` | JSON schemas for every message between phone and server |
| `deploy/runpod/` | Hosting on a Runpod GPU pod |
| `spacetime/` | SpacetimeDB module and live viewer |
| **Tests and evaluation** | |
| `backend/tests/` | Backend unit and conformance tests (about 1,150) |
| `ios/RepairOSTests/` | App tests (about 270) |
| `eval/` | Evaluations on real scans (Lea's answers, part pointing) |
| `training/`, `samples/` | Training notes and recorded sample sessions |
| **Design and docs** | |
| `docs/design/` | Figma exports, the design system and product notes |
| `docs/devpost.md` | The Devpost write-up |
| `docs/DEVELOPING.md` | Developer workflow |
| `PLAN.md`, `areas/` | The full plan, architecture decisions and work streams |
| **Developer tools** | |
| `tools/` | Code generation, replay, import and guard scripts |
| `build/` | The project tracker (task status and doctor), not build output |

## Run it

**Backend** (Python 3.12):

```
cd backend
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.lock && .venv/bin/pip install -e .
cp .env.example .env     # ANTHROPIC_API_KEY, TYPESAFE_KEY, CARTESIA_API_KEY, REPAIROS_PAIRING_CODE
.venv/bin/uvicorn repairos.main:app --host 0.0.0.0 --port 8000     # gateway
.venv/bin/python -m repairos.pipeline.worker                       # pipeline worker, second terminal
```

**Host it** on one Runpod GPU pod: the start command runs `deploy/runpod/bootstrap.sh`, which clones the repo,
installs the locked dependencies, caches the detector weights and starts everything. See `deploy/runpod/README.md`.

**App** (Xcode 27, an ARKit iPhone; LiDAR models give the 3D scans): copy `ios/Config/Signing.example.xcconfig` to
`ios/Config/Signing.xcconfig`, set your team, then build and install. Steps are in `ios/README.md`. Pair from the
app's Settings with the gateway URL and pairing code.

**Tests:** `make test` for the backend; the `RepairOS` scheme's tests in Xcode for the app.

## Honest limits

- A flipped board registers only when its outline and a confident connector cue agree; otherwise its underside shows
  as photos.
- Stereo depth misses shiny parts (a metal USB shell) and stays flat there rather than guessing.
- Pointing at a tiny part needs a sharp close photo; without one Lea circles the region and names the part in words.

## What's next

1. Register flipped boards from more cues (silkscreen text, pad patterns).
2. Live part pointing while scanning, not only in the report.
3. Repair steps and replacement parts linked to each finding.

## Team

Rohan Kilaru, Sritej Bommaraju, Jayden, Samik.

## License and credits

See `LICENSE` and `NOTICE`. Third-party notices for the app are in `ios/THIRD_PARTY_NOTICES.md`. The Alegreya Sans font
is under the SIL Open Font License.
