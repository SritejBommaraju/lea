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

## Why we're different

AI assistants can describe a device, and 3D scanning apps can capture one, but as far as we know nothing puts the two
together on the phone you already own. LEA does:

- **Just an iPhone.** No special hardware, no CAD files, no setup. The phone's LiDAR camera and photos are enough.
- **Your object, not a stock picture.** The 3D model is built from your own scan, so Lea is looking at the exact
  device in front of you: your board, your wiring, your damage.
- **AI that shows, not just tells.** Ask a question and Lea answers out loud while she turns the 3D model to the part,
  circles it and brings up the real photo of that spot.
- **Works on anything.** Controllers, circuit boards, appliances, chargers: nothing is built for one product.

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

**Design engine.** Set `GRAIN_URL`, `GRAIN_API_KEY` and, optionally, `GRAIN_PROJECT` (the design to judge against when the
phone names none) in `backend/.env`. Each completed scan is then judged by Grain against the product's own design instead of
by the pipeline, meter readings go through it, and the report offers "Fix it with Grain". Type the design id printed on the
product in the app's Settings, under Product. Every Grain answer is saved in the session's `grain/` folder.

**Host it** on one Runpod GPU pod: the start command runs `deploy/runpod/bootstrap.sh`, which clones the repo,
installs the locked dependencies, caches the detector weights and starts everything. See `deploy/runpod/README.md`.

**App** (Xcode 27, an ARKit iPhone; LiDAR models give the 3D scans): copy `ios/Config/Signing.example.xcconfig` to
`ios/Config/Signing.xcconfig`, set your team, then build and install. Steps are in `ios/README.md`. Pair from the
app's Settings with the gateway URL and pairing code.

**Tests:** `make test` for the backend; the `RepairOS` scheme's tests in Xcode for the app.

## Team

Rohan Kilaru, Sritej Bommaraju, Jayden Lee, Samik Thusoo.

## License and credits

See `LICENSE` and `NOTICE`. Third-party notices for the app are in `ios/THIRD_PARTY_NOTICES.md`. The Alegreya Sans font
is under the SIL Open Font License.
