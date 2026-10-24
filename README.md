# RepairOS

Point an iPhone 16 Pro at a broken hardware product. RepairOS scans it with LiDAR and the camera, compares it
with the product's design, finds the parts and wires that look wrong, walks you through multimeter checks that
**prove** the fault, tells you whether the parts you have lying around will do, and gives you the repair steps.

**Grain**, our design engine, supplies the design: geometry, wiring harness, nets, simulated expected values and BOM. It also
judges a scan part by part and meter readings net by net. RepairOS detects first and asks Grain second.

**Two phases** ([BUILD.md Phases](BUILD.md#phases-phase-1-now-no-grain-phase-2-with-grain)). Phase 1, now: capture any
product into a scan package and find what is broken ourselves, with no Grain calls. Phase 2: Grain's design,
cross-check and electrical check.

Status (2026-10-24): planning is done, nothing is implemented yet, and the build has **less than 24 hours**,
including the demo. This README is the front door. It is short on purpose; every section links to the detail.

| Read | For |
|---|---|
| **This README** | What we are building in 24 hours, the demo, the flow, the decisions |
| [BUILD.md](BUILD.md) §0 | The hour-by-hour plan, lanes, task cards; `python3 -I build/next.py` lists what to start |
| [SPEC.md](SPEC.md) | The rules: invariants, contracts, decision log |
| [spec/contracts/](spec/contracts/) | JSON Schemas; `check.py` validates our examples and, when present, the private recorded Grain responses |
| [AGENTS.md](AGENTS.md) | **Start here** (people and coding agents): setup, the loop, the rules |
| [areas/README.md](areas/README.md) | The three work streams (app, modeling + benchmark, build-plan execution): pick yours |
| `private/grain/GRAIN_SPEC.md`, `private/grain/GRAIN_STATUS.md` | Grain spec sheet and status. **Private, not in the repo** (Grain is proprietary; get them from Sritej) |

---

## 1. The demo (what the judges see)

One product, chosen at hour 0 (§6, D1), with two faults staged before the demo: **one external part removed**
and **one connector unplugged inside**.

1. **Pick the product and say the symptom.** "It won't turn on."
2. **Scan it (under 2 minutes).** RepairOS aligns the scan to the design and shows the missing part in red;
   Grain's verdict agrees (`not_seen`). Interior parts are shown as "not inspected", never as fine.
3. **Open it and take close-ups.** The plan gives the opening steps (power off first). The app rings three
   targets on the screen; you fill each ring and hold still. RepairOS crops
   each connector at full resolution and asks the vision model one question: "Is this connector seated?" The
   unplugged one comes back **suspect**, not proven.
4. **The meter proves it.** Power off. AR labels show where the probes go. One continuity check: no beep.
   RepairOS and Grain both name the open: the harness row, its gauge, length and colour.
5. **"I have X."** You offer two things you have. One is a yes with the reasons and the datasheet; one is a no
   (for example a fuse with the wrong rating), with the number that rules it out.
6. **The plan.** Numbered steps with safety gates: re-seat the connector, print or buy the missing part (STEP
   from Grain, or the BOM row with price and link).
7. **Confirm.** Re-measure: beep. Rescan: the part is back.

If a stage cannot decide, it says **insufficient evidence** and why, and the meter settles it. That is a feature
of the demo, not a failure. A recorded backup video of the full run is made at hour ~20.

---

## 2. The 24-hour plan in one table

Full timeline, lanes, cards and the cut list: [BUILD.md](BUILD.md) §0. Owners are a proposal; reassign freely.

| Lane | Builds | People (owner can reassign) |
|---|---|---|
| **L1 Capture** | ARKit app: scan to dense PLY (metres, gravity, poses), stills with pose and intrinsics, upload, AR labels | Elysi (with Sritej) |
| **L2 Backend + Grain client** | FastAPI, SQLite state, Grain client (H0–H5 handshakes), reference cache, ICP with gravity + fit gate, cross-check | Rohan |
| **L3 Fusion + Grain side** | Jev fusion, connector targets projected into stills, full-res crops; the Grain-side API changes (S2.7–S2.11, Sritej) | Sritej + Elysi |
| **L4 Meter wizard** | Test plan on the power path, bisection, range check via Grain, wizard UI, confirm | Jayden |
| **L5 Parts + plan** | LLM and VLM, datasheets, "I have X" quick check, simulation check, plan writer | Samik |
| **L6 Demo + integration** | Hour-0 setup, scaffold, pipeline glue, demo script, staging, backup video | agents, coordinated by Sritej; Samik + Jayden on the script and video from H16 |

The demo critical path is 16.5 h of the 24 (`python3 -I build/status.py`); it fits only if each lane runs about
two agent sessions in parallel.

**Checkpoints:** H6 first real scan through Grain · H12 end to end on recorded data · H16 feature freeze · H18
dry run · H20 backup video · H22 final run. **If late, cut in this order:** Grain `/look` second opinion →
AR labels (use photos of the product with the pins circled) → simulation check live (show the cached one) →
rescan confirm (keep re-measure).

**Grain side, inside the same 24 hours** (Sritej, cards S2.7–S2.11; detail in `private/grain/GRAIN_STATUS.md` §4):
the running Grain serves current scan diagnosis and the pilot; a range verdict for every voltage reading; a
verdict on every meter reading; structured fix subjects; then the first real phone scans, with a removed part never
`ok` at phone noise. These must land in the 24 hours: there is no
fallback path, the demo runs on the real thing.

---

## 3. Architecture (hackathon profile)

```
 iPhone 16 Pro (Swift, ARKit)                RepairOS backend (FastAPI, the laptop)              Grain
 ┌──────────────────────────────┐  PLY (m),   ┌────────────────────────────────────────┐  key   ┌─────────────────┐
 │ scan: depth → points, gravity │  stills +   │ reference cache per product model      │ ─────▶ │ files, bom,     │
 │ stills: 12 MP + pose + K      │  pose,      │ ICP (gravity, fit gate), Y-up → Z-up   │        │ electrical      │
 │ AR rings, AR probe labels     │  readings,  │ targets → crops → VLM yes/no → findings│        │ scans, diagnose │
 │ meter wizard, parts, plan UI  │  "I have X" │ Jev fusion → next action               │        │ /electrical/    │
 │ Vision OCR (on device)        │ ◀────────── │ meter planner + range check            │ ◀───── │   check, /call  │
 └──────────────────────────────┘  plan, next │ parts check, plan text (Claude)        │        │ chat (detached) │
                                    step      │ SQLite state, files on disk            │        └─────────────────┘
                                              └────────────────────────────────────────┘
```

Hackathon providers (`config/providers.yaml`, profile `hackathon`): Grain live (Phase 2 only), Jev fusion (TypeSafe
key at hour 0), Claude Sonnet 5.5 for the assistant and plan, Claude Haiku 5.5 for crop yes/no, Grounding DINO tiny
zero-shot as the detector (Phase 1 has no design to say where to look; no training), no segmenter (crops from the
detector boxes), Apple Vision OCR on the phone, trimesh rendering, SQLite state, local disk. SpacetimeDB, SAM 2,
training and hosting are the `full` profile, after the hackathon.

Rules that hold everywhere (SPEC §7): the phone never holds a key; vision findings are suspects and only the
meter is proof; an unseen part is "not inspected", never "ok"; polarity is never inferred from wire colour;
protective parts are judged on their trip ratings or the answer is no; a Grain rebuild is a "simulation check",
never "verified"; no product-specific code.

---

## 4. The flow, with the Grain calls

Handshake names (H0–H7) are in the private Grain spec (`private/grain/GRAIN_SPEC.md` §5, from Sritej).

| Step | RepairOS | Grain |
|---|---|---|
| 0. Boot | Check Grain, snapshot the tool schema | H0: `GET /health`, `GET /call/schema` |
| 1. Product | Saved, typed, or the label read on the phone | H1: reference files, `/electrical`, `/bom`, cached. Not a Grain design → H5: Grain designs a reference by chat (minutes, detached, cached; `design_trust: low`) |
| 2. Scan | Our ICP with gravity; **fit gate** (no verdicts on a bad fit: rescan); Y-up → Z-up; crop on the AR box | H2: upload (`name=<id>.ply&units=m`), `diagnose?align=none&crop=false`, as a cross-check of our own coverage |
| 3. Close-ups | Connector targets from the design projected into each still; full-res crops; one VLM question per crop | H3 (optional, grey-band parts only): photos + `/look` |
| 4. Fuse | Jev: ranked suspects with probabilities, next action | — |
| 5. Meter | Test plan along the power path, suspects first, bisection; our `[min, max]` range check | H4: `/electrical/check` with all readings so far |
| 6. Parts | "I have X" quick check; simulation check pre-run for the demo | H5 on a copy: "Replace <ref> with <X>" |
| 7. Plan | Steps, safety gates, have it / print it / buy it | `/call` with scan id + readings (no photos) for Grain's fixes, `buy`, `remake` |
| 8. Confirm | Re-measure, rescan | H4, H2 |

---

## 5. Details that matter

**Frames and units.** ARKit is metres, Y up. Grain's design frame is `model.glb`: **millimetres, Z up**. Upload
with `units=m` after our ICP and the Y-up → Z-up turn, then `align=none`. Grain's own `align=icp` (it reports
`inlier_share`) runs on the raw scan as a second opinion.

**What LiDAR can and cannot see.** About 1 cm accuracy: enough for a missing, broken or shifted part, never for
a wire or a solder joint. Wires come from the stills, and the design says where to look. iPhone LiDAR
noise (about 5–10 mm per point) is where a removed part risks reading `ok` (G29), so Grain must hold at phone
noise (T13) and we send dense high-confidence points averaged over many frames (no coarse voxels), with a staged
fault that leaves open space behind it. The first real capture at H6 decides it.

**Where to look, without pin positions.** Grain does not export 3D pin positions yet (G25). For the demo the
targets are connectors, located by their nodes in `model.glb` (e.g. `J1`), and the question is "is it
seated", not "which pin". Pin-level targets come with Grain's `pins.json` (target T7).

**Meter readings.** Send net-style pins (e.g. `J1.1`), the names in Grain's nets. Every voltage is judged
against the expected `[min, max]`: in our code always, and by Grain once T1 lands (by H3). An open on a harness wire comes back with the
wire to replace: gauge, length, colour, both ends.

**"I have X."** Quick check: identify X, find its datasheet, compare with the BOM row, the net's voltage range
and peak current, and the fit. Answer yes, yes with changes, or no, with reasons and the link. Protective parts
(fuse, polyfuse, TVS, BMS, thermal cut-off) are judged on hold and trip current, trip time or I²t, voltage
rating and breaking capacity, or the answer is no. Any step that wires a supply says: power off, measure polarity
with the meter, confirm on the design. The simulation check (Grain rebuild on a copy) is a limited check, pre-run
for the demo because a design turn takes minutes.

**Print it.** Grain exports `out/model.step` for every made part. Grain's pilot gives a rough PLA estimate for
simple brackets and enclosures; nothing in Grain slices or drives a printer, so printing at the venue is manual.

---

## 6. Decisions (owner answers kept; proposals marked)

| # | Decision |
|---|---|
| D1 | **Demo product: picked at hour 0.** Low-voltage DC, a removable connector or harness, a clear battery or USB power path, fits on a table; ideally one Grain already designed, otherwise Grain designs a reference by chat at H0–H2. Two units if possible. Product facts live only in `samples/`, never in code. (Proposal.) |
| D2 | Any product, not only Grain designs (owner). Detect first, Grain second (owner). |
| D3 | Shopping: deterministic selection and budget, AI only for discovery (owner). BOM prices are treated as current (owner). |
| D4 | A 3D printer is at the venue (owner). Grain cannot drive it; print by hand from `model.step`. |
| D5 | RepairOS's ICP is primary; Grain's `align=icp` is the cross-check (proposal; owner was unsure). |
| D6 | Models: Claude Sonnet 5.5 (assistant, plan), Claude Haiku 5.5 (crop yes/no); keys on the backend (proposal). |
| D7 | No model training in the 24 hours; no SpacetimeDB (proposal). No deterministic fallback paths: Jev fuses, Grain's demo-critical changes land, and if a piece is down it is fixed, not degraded (owner). |
| D8 | One phone, signed with a free Apple ID; no TestFlight (proposal). |

The full log and the questions resolved for the 24 hours are in SPEC §8. What is still unknown is a **test** in
the plan with the hour it gets answered: real-scan verdict quality (H6), hi-res still formats on the device
(H1), the VLM's accuracy on our crops (H8).

---

## 7. Working on this repo

Pull requests only; never push to `main` or someone else's branch. Pick your stream in
[areas/README.md](areas/README.md), then follow [AGENTS.md](AGENTS.md): `python3 -I build/doctor.py` →
`next.py` → branch → build → `make test` + `python3 -I spec/contracts/check.py` → PR. Grain is proprietary: only
the API surface goes in the repo. If a Grain shape changes, Sritej re-records the private fixtures
(`private/grain/`) and you run `python3 -I spec/contracts/check.py`.
