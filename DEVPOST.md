# LEA: scan anything, ask Lea, see the answer on your object

LEA (code name RepairOS) turns an iPhone into a repair assistant. You scan a device with the phone's LiDAR camera and
take the close-ups the app asks for. A few seconds later you get a 3D model of your actual object and a report on what
looks wrong. Then you ask Lea, the voice assistant, anything about it. She answers out loud and moves the 3D model while
she talks: she turns it to the part, circles it and shows the real photo of that spot.

It works on any object; nothing is built for one product. We tested it on a game controller, an Arduino board, a mouse,
a toaster oven, a hair trimmer and a phone charger.

## What it does

- **Scan.** The phone captures LiDAR depth, keyframes and full-resolution close-ups while you walk around the object.
  You can flip the object and scan its underside; the server lines the two scans up.
- **3D model of your object.** The server rebuilds a textured mesh from the scan. Thin objects such as circuit boards,
  which LiDAR can't resolve, get a model built from the photos: the board's outline on the table, textured at full
  resolution, with raised parts (headers, jacks, capacitors) measured from the photos by multi-view stereo. A flipped
  board gets its real underside.
- **Report.** Detectors and a vision model look at every close-up. The report lists what looks wrong, how sure it is
  in words, and safety problems first. Parts nobody could see are never called fine.
- **Ask Lea.** Tap the talk button and ask. Lea identifies the object (for example "ELEGOO UNO R3", read off the board),
  answers general questions with web search when needed, and finds any named part ("where is A2?", "where's the X
  button?") from the photos, mapped onto the 3D model. The camera flies to it, the part lights up, and the close-up
  photo fades in.

## How it works

```
iPhone app (SwiftUI, ARKit, Metal, SceneKit)
   │  scan package, stills, voice turns        HTTPS
   ▼
Gateway (FastAPI)  ──  SQLite + blob store  ──  Pipeline worker
   │                                             ├─ registration: flips and continued scans (segment_align, flat_flip)
   │                                             ├─ 3D: textured mesh, or photo slab + stereo relief for thin objects
   │                                             ├─ inspect: OWLv2 detector, crops, Claude vision, OCR
   │                                             ├─ locate: findings ray-cast onto the model
   │                                             └─ report
   ├─ Voice: Claude (answers, part index) + Cartesia (speech in and out)
   └─ Live mirror → SpacetimeDB (big-screen viewer)
```

- The phone holds no API keys. It talks only to the gateway.
- Every paid call (Claude, Cartesia, web search) goes through a spend guard with per-scan and per-day caps.
- Lea's answers carry inline markers (`<show>`, `<view>`, `<zoom>`, `<point>`) that the app turns into camera moves,
  synced to the sentence she is speaking.

## Repository map

| Path | What |
|---|---|
| `ios/RepairOS/` | The iPhone app: capture, 3D viewer, report, Lea's voice and the 3D stage |
| `backend/repairos/` | Gateway (`api/`), pipeline (`pipeline/`), 3D (`recon/`, `recreation/`), vision (`vision/`), report (`report/`), voice (`voice/`) |
| `config/` | Pipeline, provider, verdict and vision-model settings |
| `spec/contracts/` | JSON schemas for every message between phone and server (loaded at runtime) |
| `deploy/runpod/` | Hosting on a Runpod GPU pod |
| `spacetime/` | SpacetimeDB module and the live viewer for a big screen |

## Run the backend locally

Needs Python 3.12.

```
cd backend
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock && .venv/bin/pip install -e .
cp .env.example .env            # add ANTHROPIC_API_KEY, TYPESAFE_KEY, CARTESIA_API_KEY, REPAIROS_PAIRING_CODE
.venv/bin/uvicorn repairos.main:app --host 0.0.0.0 --port 8000   # the gateway
.venv/bin/python -m repairos.pipeline.worker                     # the pipeline worker (second terminal)
```

Check it with `curl http://localhost:8000/healthz`.

## Host it on Runpod

The demo runs on one Runpod GPU pod (an RTX A5000) with the gateway, the worker and the live mirror together.

1. Create a pod from Runpod's official PyTorch image, with a volume at `/workspace`, and expose HTTP ports 8000 and
   8790.
2. Add the keys as Runpod secrets (`anthropic_api_key`, `cartesia_api_key`, `typesafe_key`, `repairos_pairing_code`,
   and optionally `spacetime_token`). Map them to environment variables in the pod.
3. Set the start command to fetch and run `deploy/runpod/bootstrap.sh`. It clones the repository, installs the
   locked dependencies into a venv on the volume, caches the detector weights and starts everything.
4. The phone connects to `https://<pod_id>-8000.proxy.runpod.net`.

Details are in `deploy/runpod/README.md`.

## Build the iPhone app

Needs Xcode 27 and an ARKit iPhone; a LiDAR iPhone (Pro models) gives the 3D scans.

1. Copy `ios/Config/Signing.example.xcconfig` to `ios/Config/Signing.xcconfig` and set your `DEVELOPMENT_TEAM`.
2. Build and install:
   ```
   cd ios
   xcodebuild -project RepairOS.xcodeproj -scheme RepairOS -configuration Release \
     -destination 'generic/platform=iOS' -allowProvisioningUpdates build
   xcrun devicectl device install app --device <udid> <DerivedData>/Build/Products/Release-iphoneos/RepairOS.app
   ```
3. On the phone, open Settings in the app and pair with the gateway URL and pairing code.

## Built with

Swift, SwiftUI, ARKit, SceneKit, Metal, Python, FastAPI, PyTorch, Open3D, OWLv2, Claude (Anthropic), Cartesia,
SpacetimeDB, Runpod.

## What we're proud of

- The model you see is your object, built from your scan, and every circle Lea draws comes from a real photo of it.
- Lea is confident but never invents: she won't call a part fine if no photo shows it.
- Flipping a circuit board over just works: the server finds the flip from the board's own connectors and holes, and
  the 3D model shows the real solder side.
