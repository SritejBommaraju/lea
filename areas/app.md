# App (stream `app`)

Alias lanes: L1 (iOS capture), plus the iOS screens from L4, L5, L6. **Who:** one person (the owner assigns; §0.B
had Elysi with Sritej on L1). The owner also builds iOS on this Mac, so this stream overlaps build execution.

**Delivers.** The iPhone 16 Pro / Pro Max app. Phase 1: LiDAR + photo capture of **any** product into a scan
package (binary PLY in metres, full-resolution JPEG stills with pose and intrinsics, a manifest), uploaded to our
backend; the findings, parts and plan screens. Phase 2: AR labels and the meter wizard on Grain's reference.

**Owns.** `ios/RepairOS/**` and `ios/RepairOSTests/**` except the generated `ios/RepairOS/Contracts/**`
(`make codegen`); `tools/convert_stray.py`, `tools/convert_polycam.py`; `samples/sessions/demo-*/**`.

**Contracts.** Writes: `scan-package`, `still`, `meter-reading`, `part-on-hand` (to the gateway, S2.1).
Reads: `finding`, `suspect`, `next-action`, `repair-plan`, `shopping-quote` (from the gateway's live events).
The phone never holds a key and never talks to Grain or a model (A6).

**Cards, in order.** Loop: [AGENTS.md](../AGENTS.md). `P1` = Phase 1 (no Grain), `P2` = needs Grain ([BUILD.md Phases](../BUILD.md#phases-phase-1-now-no-grain-phase-2-with-grain)); `←` = depends on; links go to the card text.
- **Capture: shell, probe, Mode A, dense fusion + PLY, upload (L1)**: [C1.1](../BUILD.md#c11-ios-app-shell) P1 2 h · [C1.2](../BUILD.md#c12-device-capability-probe) P1 0.5 h · [C1.3](../BUILD.md#c13-mode-a-capture) P1 3 h ← C1.1 · [C1.4](../BUILD.md#c14-point-fusion-crop-ply-export-scanpackage) P1 2.5 h ← C1.3 · [C1.5](../BUILD.md#c15-upload-client) P1 1 h ← C1.1, S2.1 · [C1.7](../BUILD.md#c17-day-0-converters-stray-scanner-polycam-raw) P1 1.5 h ← B0.7
- **Mode B stills and AR (L1)**: [I5.1](../BUILD.md#i51-mode-b-guided-close-ups) P1 3 h ← C1.3 · [I5.2](../BUILD.md#i52-ar-labels) P2 1.5 h ← I5.1
- **Screens: findings, wizard, parts, plan (L6, L4, L5)**: [I5.3](../BUILD.md#i53-findings-and-suspects-ui) P1 1.5 h ← C1.1 · [I5.4](../BUILD.md#i54-meter-wizard-ui) P2 2.5 h ← C1.1, F4.5 · [I5.5](../BUILD.md#i55-parts-on-hand-ui) P1 1.5 h ← C1.1 · [I5.6](../BUILD.md#i56-plan-and-confirm-ui) P1 1.5 h ← C1.1
- **Demo captures (L1)**: [C1.9](../BUILD.md#c19-demo-captures-through-grain-t12) P2 1 h ← C1.4, C1.5
- **Later track** (kept, not in the 24 h). P1: C1.6, M.OCRD.0, M.OCRD.1, M.SPEECH.0, M.SPEECH.1, M.SPEECH.2. P2: none.

**Setup (doctor items).** `dev_tools`, `xcode`, `iphone`, `apple_signing`, `repo_access`.

**What's ready.** `python3 -I build/next.py --stream app` (or `--lane L1`; add `--phase1` for Phase 1 only).

**Hands off / receives.** Sends scan packages and stills to build execution (gateway upload, S2.1); the same
recorded sessions feed the benchmark (modeling). Receives findings, next actions and plans over SSE. Without a
reference (Phase 1), Mode B targets come from detector boxes (modeling), not from projected parts.

**Done when.** A scan of a product the team never tuned on uploads as a valid `scan-package` + `still` set
(`python3 -I spec/contracts/check.py` green on a recorded example), resumes after a dropped connection, and the
findings screen shows words, evidence crops, `not_inspected` and `insufficient_evidence`; Phase 2 adds AR labels and
the wizard.
