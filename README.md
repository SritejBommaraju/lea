# LEA: scan anything, ask Lea, see the answer on your object

**LEA turns an iPhone into a repair assistant for hardware you were sent: you scan the device, the design engine
(Grain) compares the scan, photos and any meter readings with the product's own design, and you get a report of what
looks wrong and how to fix it. Ask Lea anything about it, or ask Grain to change the design from your phone.**

Built at Cal Hacks. [How it works](#how-it-works) · [Run it](#run-it) · [Repo map](#repo-map) ·
[Write-up](docs/devpost.md)

## What it does

1. **Scan.** Walk around the object with the iPhone. LiDAR depth, keyframes and full-resolution close-ups are captured
   as you go.
2. **Judge.** The product was designed in Grain (geometry, nets, harness, SPICE, BOM). The gateway sends the scan to
   Grain, which judges it with that design as context: which parts are missing, shifted or damaged, which parts the scan
   could not see (listed as not looked at, never as fine), and what to buy, remake or reseat. The report says so in
   words, with the fix.
3. **Measure.** The report asks for the next meter check (net, probe points, power state, expected range) and opens the
   meter sheet filled in. A reading, typed or read over Bluetooth from a Pokit Meter, goes back through Grain and the
   report is re-made. Only a meter reading proves an electrical fault.
4. **Ask Lea, fix it from the phone.** Tap and ask: "what does this wire do?", "is the main chip soldered?". Lea answers
   from the report and, inside a conversation, from what the camera shows. "Fix it with Grain" sends your words, with
   the context (symptom, flagged parts, readings, fixes so far), to Grain's agent, which changes the design and tells
   you whether it rebuilt cleanly.

Nothing is built for one product: the design names the parts.

## Why we're different

AI assistants can describe a device, and 3D scanning apps can capture one, but as far as we know nothing puts the two
together on the phone you already own. LEA does:

- **Just an iPhone.** No special hardware, no CAD files, no setup. The phone's LiDAR camera and photos are enough.
- **Your object, judged against its own design.** Every product a customer receives was designed in Grain, so the
  judgement has the geometry, nets, harness and SPICE behind it, not a guess from a photo.
- **Fixes, not just findings.** Buy this part, remake that one from its file, reseat this connector; and from the phone
  you can ask Grain to change the design.
- **Works on anything with a design.** Nothing is built for one product.

## How it works

```
iPhone app (SwiftUI, ARKit, Metal, SceneKit)
   │  scan package, stills, meter readings, voice turns        HTTPS
   ▼
Gateway (FastAPI) ── SQLite + blob store
   ├─ complete scan ──zip of the session folder──> Grain (judges against the product's design) ──> report
   ├─ meter reading ──/call──> Grain ──> report re-made
   ├─ Fix it ──chat──> Grain's agent (changes the design)
   └─ Voice: Claude (answers) + Cartesia (speech in and out)
```

| Component | Job | Tech | Folder |
|---|---|---|---|
| App | Capture, 3D stage, report, meter, talk to Lea, Fix it | Swift, SwiftUI, ARKit, SceneKit, Metal | `ios/RepairOS/` |
| Gateway | The only server the phone talks to | Python, FastAPI | `backend/repairos/api/` |
| Design engine client | Upload, judgement, report from its answer | Python, httpx2 | `backend/repairos/grain/`, `report/` |
| Voice | Lea's answers | Claude, Cartesia | `backend/repairos/voice/` |
| Hosting | One CPU host (or a laptop) | uvicorn | `deploy/MANIFEST.md` |

The phone holds no API keys. Every paid call the gateway makes itself (Claude, Cartesia) goes through a spend guard with
per-minute and per-day caps; Grain bills on its own side.

## Repo map

| Folder | What |
|---|---|
| **Product** | |
| `ios/` | The iPhone app (`RepairOS/`) and its Xcode project |
| `backend/` | The gateway, the design engine client, report and voice (`repairos/`) |
| `config/` | Provider and spend-cap settings |
| `spec/contracts/` | JSON schemas for every message between phone and server |
| `deploy/` | What runs where (a CPU host) |
| **Tests and evaluation** | |
| `backend/tests/` | Backend unit and conformance tests |
| `ios/RepairOSTests/` | App tests |
| `eval/` | Evaluations on real scans (Lea's answers while scanning) |
| `samples/` | Recorded sample sessions |
| **Design and docs** | |
| `docs/design/` | Figma exports, the design system and product notes |
| `docs/devpost.md` | The Devpost write-up |
| `docs/DEVELOPING.md` | Developer workflow |
| `PLAN.md`, `areas/` | The full plan, architecture decisions and work streams |
| **Developer tools** | |
| `tools/` | Code generation, session replay, import and guard scripts |
| `build/` | The project tracker (task status and doctor), not build output |

## Run it

**Backend** (Python 3.12):

```
cd backend
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.lock && .venv/bin/pip install -e .
cp .env.example .env     # GRAIN_URL, GRAIN_API_KEY, ANTHROPIC_API_KEY, CARTESIA_API_KEY, REPAIROS_PAIRING_CODE
.venv/bin/uvicorn repairos.main:app --host 0.0.0.0 --port 8000     # gateway
```

**Design engine.** Set `GRAIN_URL`, `GRAIN_API_KEY` and, optionally, `GRAIN_PROJECT` (the design to judge against when the
phone names none) in `backend/.env`. Without `GRAIN_URL` nothing judges a scan: `POST /v1/scans/{id}/complete` answers
409. Each completed scan is then judged by Grain against the product's own design, meter readings go through it, and the
report offers "Fix it with Grain". Type the design id printed on the product in the app's Settings, under Product. Every
Grain answer is saved in the session's `grain/` folder.

**Host it** on any CPU machine: the gateway is one uvicorn process with SQLite and a blob folder under `REPAIROS_DATA_DIR`
(see `deploy/MANIFEST.md`); there is no GPU and no worker.

**App** (Xcode 27, an ARKit iPhone; LiDAR models give the 3D scans): copy `ios/Config/Signing.example.xcconfig` to
`ios/Config/Signing.xcconfig`, set your team, then build and install. Steps are in `ios/README.md`. Pair from the
app's Settings with the gateway URL and pairing code.

**Tests:** `make test` for the backend; the `RepairOS` scheme's tests in Xcode for the app.

## Team

Rohan Kilaru, Sritej Bommaraju, Jayden Lee, Samik Thusoo.

## License and credits

See `LICENSE` and `NOTICE`. Third-party notices for the app are in `ios/THIRD_PARTY_NOTICES.md`. The Alegreya Sans font
is under the SIL Open Font License.
