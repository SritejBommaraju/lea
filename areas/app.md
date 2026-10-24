# App (stream `app`)

**Who.** One person (the owner assigns); Rohan also builds iOS on his Mac. **Delivers.** The iPhone app: a guided LiDAR scan of any object, guided full-resolution close-ups, a
resumable upload to our gateway, the report screen and an optional meter-reading entry.

**Owns.** `ios/RepairOS/**` and `ios/RepairOSTests/**`, except the generated `ios/RepairOS/Contracts/**`
(`make codegen`).

**Contracts.** Writes `scan-package`, `still`, `meter-reading` to the gateway. Reads `report`, `finding`,
`suspect`, `next-action` from it. The phone never holds a key and talks only to the gateway (A6).

## Capture base (decided 2026-10-24)

Our own ARKit pipeline, not `ObjectCaptureSession`: it exposes no depth or poses and owns the camera.

- `ARWorldTrackingConfiguration` with `sceneReconstruction = .mesh` for the live overlay.
- `sceneDepth` and confidence into a voxel hash with per-voxel hit counts: the coverage heat map.
- Our own keyframe picker, scored on sharpness.
- Close-ups with `captureHighResolutionFrame` in the **same AR session and world frame**. 12 MP is the base;
  default to 48 MP when the device and iOS allow it in the AR session (probe at startup); ask the user only on a
  trade-off such as speed.
- Drift: correct on the phone in real time first (ARKit anchor updates plus our keyframe-level correction).
- Scan time: as fast as possible; more only where quality needs it, with the target set from measurements.
- Coaching cues in Apple's Feedback vocabulary: moving too fast, too close, too far, low light, out of view.
- Borrow from MIT repos: `Dominik-Hagmann/lidar-scanner-ios` (voxel accumulation, confidence gate, binary PLY
  writer; keep ARKit Y up), `strayrobots/scanner` (raw RGB-D format), `cedanmisquith/SwiftUI-LiDAR` (mesh
  preview). Apple sample code is allowlisted.
- Never `arkit-3dgs-scanner` (non-commercial), the `record3d` library (LGPL), or GPL or unlicensed repos.

If on-phone drift correction falls short, the backend refines poses (B13). If our capture underperforms, sessions
can come from Stray Scanner (MIT) exports (B12).

## Tasks, in order

| Id | What | Deps |
|---|---|---|
| A1 | App shell, device probe, pairing | - |
| A2 | LiDAR scan with overlay, coverage heat map, keyframes, coaching cues | A1 |
| A3 | Guided close-ups in the same AR session | A2 |
| A4 | Resumable upload | A1 |
| A5 | Report screen | A4, B11 |
| A6 | Optional meter reading entry | A5 |
| A7 | First-run usability pass | A5 |
| A8 | StandardCyborg evaluation: LiDAR depth into its fusion for live drift correction; optional TrueDepth small-object mode | A2 |

"Done when" for each: [PLAN.md](../PLAN.md) §7. Milestones MS2 and MS3 are this stream's.
**Setup (doctor items).** `dev_tools`, `xcode`, `iphone`, `apple_signing`, `test_products`; `multimeter` for A6.
**Done when.** On a product nobody developed against, the app scans, guides close-ups and uploads a session that
passes `make sessions-check`, then shows its report with crops, confidence words, `not_inspected` and
`insufficient_evidence` reasons.
