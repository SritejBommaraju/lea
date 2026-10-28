# samples/: recorded real sessions

One folder per **real** capture of one physical unit (PLAN.md task B6). The backend is developed and regression-tested
by replaying these. No synthetic product, no made-up readings (GEN3). Product facts live here, never in code (GEN1).

```
make replay SESSION=<session_id | folder path>   # validate, load into a fresh STATE/BLOB, run the pipeline, score
make sessions-check                              # validate every non-held-out session (CI)
```

Replay writes `data/replay/<session_id>/` (its own SQLite state and blob store), `summary.json` with per-stage
timing, the findings, the score against `truth.json` and each scan's report, and `report_<scan_id>.json` (validated
against `spec/contracts/report.schema.json`), `.md` and `.html`. The pipeline (B10, `repairos.pipeline.run_session`)
calls the configured detector, Claude and Jev; a stage that fails fails the replay with the stage and the error.

## Where sessions come from

- **The gateway** writes every object it receives into `data/sessions/<session_id>/` in this layout (`source:
  gateway`), so any real run on the laptop is replayable as is. To keep one as a sample: copy the folder to
  `samples/sessions/`, fill in `object_id`, `category`, `operator`, `lighting`, `meter`, write `truth.json`, and
  run `make sessions-check`.
- The app upload (A4) and the importers (B12) write the same layout with
  `repairos.sessions.writer.SessionWriter`.
- **Importers** (B12): `backend/.venv/bin/python tools/import_capture.py <export> [--device-model iPhone15,2 --ios
  18.1 --symptom "..."]` converts a **Stray Scanner** export folder (MIT app, LiDAR: fused PLY in metres, a mesh when
  Open3D is installed, keyframes from the video as `unguided` stills) or a **NeRFCapture** offline export, folder
  or zip (MIT app, any ARKit iPhone: posed stills, `depth_source: none`, no PLY) into `samples/sessions/<id>/`
  (`source: converter`) and validates it. Needs `ffmpeg` for Stray videos; `backend[geometry]` (Open3D) for TSDF
  fusion and the mesh, else a numpy voxel fusion runs. `import.json` records the source, the fusion method and
  measured spacing, and every assumption (tracking state not recorded, distortion not applied, etc.). Real exports
  for the importer tests go in `samples/imports/<name>/` (backend/tests/replay/test_imports.py).
- **Plain iPhone photos** (PLAN.md §6a step 1): put 5 to 10 Camera photos (JPEG or HEIC, any size) of one unit in a
  folder, then

  ```
  python3 -I tools/import_photos.py ~/Desktop/unit-photos --out samples/sessions/sess_20261024_unit1 \
      --symptom "won't turn on"
  make replay SESSION=sess_20261024_unit1
  ```

  The folder name after `--out` is the session id (`sess_...`; leave `--out` out for a new random one). HEIC is
  converted with macOS `sips`; every photo is turned upright and re-encoded once at full resolution, JPEG quality
  92, with **all EXIF, GPS, XMP and other metadata removed** (only the ICC colour profile stays). Photos have no
  AR session, so the scan has `depth_source: none` and no keyframes, and each photo is an `unguided` still with
  `pose_reliable: false` and no `camera`: detection, Jev and the report run, and every finding's location says
  "not computed". Intrinsics are estimated from the EXIF 35 mm-equivalent focal length when it is there (else left
  out). `import.json` lists each photo's source file, capture time and its source, the intrinsics source (`exif`
  or `unknown`), what metadata was removed (GPS included), and what was skipped (videos, other files, duplicates,
  subfolders). Fill in `object_id`, `truth.json` etc. by hand afterwards, as for any session.

## Layout

```
samples/sessions/<session_id>/
  session.json                         samples/session.schema.json
  truth.json                           expected outcomes: samples/session.schema.json#/$defs/truth
  scans/<scan_id>/scan_package.json    ScanPackage (spec/contracts/scan-package.schema.json)
  scans/<scan_id>/points_arkit_world.ply  absent when depth_source is none (no-LiDAR capture, A8)
  scans/<scan_id>/mesh_arkit_world.ply    optional triangle mesh (mesh_file)
  scans/<scan_id>/<keyframe_id>.jpg, <keyframe_id>_depth.png, <keyframe_id>_conf.png
  stills/<still_id>.json               Still (spec/contracts/still.schema.json)
  stills/<still_id>.jpg, <still_id>_depth.png, <still_id>_conf.png
  readings/<reading_id>.json           MeterReading, typed by the operator from the real meter
  parts/<item_id>.json                 PartOnHand, as the user described it
  import.json                          importers only: where the session came from and what was assumed
  grain/<kind>-<scan_id>-<timestamp>.json   gateway only, when a design engine is configured: every answer it gave,
                                       as it came (kind: scan, call, plan, chat, bom-<ref>, reading). Data, not code;
                                       it is never sent back to the design engine and never read by `make replay`.
```

Every file path inside a JSON object is **relative to the session folder** (for example
`scans/scan_1a2b3c4d/kf_000.jpg`). On the wire (gateway) the same fields hold blob refs instead
(`blob://sha256:<hex>`); the writer turns refs into files and replay turns files back into refs.


## Files

| File | Format | Checked by the validator |
|---|---|---|
| `points_arkit_world.ply` | binary little-endian PLY, ARKit world frame, **metres**, Y up; `float x, y, z` + `uchar red, green, blue`, optional `uchar confidence, views` | `format binary_little_endian 1.0`, float x/y/z, vertex count = `point_count`, body size |
| `mesh_arkit_world.ply` | binary little-endian PLY triangle mesh, arkit_world metres; `float x, y, z` (+ `uchar red, green, blue`) vertices, `uchar int vertex_indices` faces | header (binary LE, float x/y/z) and body size |
| keyframe `.jpg` | JPEG q85, 1920x1440 | JPEG, size = keyframe `intrinsics.width_px/height_px` |
| still `.jpg` | full-resolution JPEG (q >= 85; HEIC converted on the phone) | JPEG, size = `width_px x height_px` |
| `*_depth.png`, `*_conf.png` | 256x192 ARKit depth (uint16 mm) / confidence (uint8 0-2) | present when named |

The validator (`repairos.sessions.validate.validate_session`, `tools/replay.py --check`) also checks every JSON
object against its schema, that stills and readings belong to a scan in the same session, and that ids match their
file names.

## Ids

`session_id = sess_<yyyymmdd>_<4 hex>` (the gateway makes it), `scan_id = scan_<8 hex>`, `still_id = s<n>`,
`object_id = obj_<8 hex>` (stable per physical unit). Ids are random, never a product name
(`repairos.sessions.ids`).

## `session.json`

Required: `session_id` (= folder name), `device.model`. Recorded when known: `object_id`, `category`
(eval/categories.yaml id), `product_id`, `product` (the design id the scan is judged against), `make_model` (as typed), `symptom`, `operator`, `lighting`
(`room | torch | polarised`), `device {model, ios, app_version}`, `meter {model, continuity_ohms_threshold}`,
`source` (`gateway | phone_export | converter`), `split` (`unassigned | train | val | test | heldout`, written by
eval/assign_split.py), `created_at`, `notes`.

**Held-out sessions** (`split: heldout`) are read only by the evaluation (`make eval`, M5). `open_session()`
raises `HeldoutSession` for anyone else, so no test or tuning run can see them.

## `truth.json`: expected outcomes

```json
{
  "faults": [{"subject": {"kind": "wire", "ref": "<harness wire name>"}, "fault": "unseated", "magnitude_mm": 2,
              "notes": "pulled 2 mm out of its housing"}],
  "not_inspected_expected": [{"kind": "part", "ref": "<part hidden inside the housing>"}],
  "negatives": false
}
```

Written by the person who induced (or ruled out) faults on the real unit. `subject` and `fault` use the Finding
contract's `subject` and `state`. `negatives: true` means no induced fault: every non-ok finding is a false alarm.
Replay scores `caught`, `missed`, `fault_kind_match`, `not_inspected_reported`, `not_inspected_missing`,
`false_alarms`.

## Gateway upload protocol (what the phone does, A4)

1. `POST /v1/devices/pair {"code": "<REPAIROS_PAIRING_CODE>"}` returns `{device_id, token}`; send
   `Authorization: Bearer <token>` from then on.
2. `POST /v1/sessions {"symptom": "...", "product": "<design id>"}` returns the `session_id` (`product` is optional).
3. For each file (PLY, keyframe JPEGs, depth, confidence, stills): `PUT /v1/blobs/<sha256 of the whole file>`,
   whole or in chunks with `Content-Range: bytes <start>-<end>/<total>`. A partial chunk answers 202 with
   `Upload-Offset`; after a dropped connection `HEAD /v1/blobs/<sha256>` gives `Upload-Offset` to resume from. The
   last chunk answers 201 `{ref}` only when the bytes hash to the sha256 (else 422, staging discarded). An upload
   of a blob that already exists answers 200 with the same ref.
4. `POST /v1/scans` with the ScanPackage whose file fields are those refs; then `POST /v1/stills`, `POST
   /v1/readings`. Bodies are validated against the contracts and the files against the table above (422 with the
   schema path or the list of problems). Posting the same object again is a no-op.
5. `POST /v1/scans/<scan_id>/complete` once the scan and every still are posted: with a design engine configured
   (GRAIN_URL) it uploads the scan's session folder as a zip, asks for the judgement against the product's design and
   publishes the report (no pipeline run); otherwise it queues the pipeline run (stills
   are already being inspected as they arrive; `make run-workers` runs the worker). `POST /v1/readings` then forwards
   the reading to the design engine and the report is re-made; `POST /v1/scans/<scan_id>/fix {"message": "..."}`
   relays the customer's request to the design engine's agent with the scan's context and returns its reply. `GET /v1/scans/<id>/pipeline`
   gives the stage statuses and, after a failure, the stage and the error. `GET /v1/scans/<id>/report` is the
   report (404 while it is made, 409 when the pipeline failed); `GET /v1/scans/<id>/report/export?format=html|md`
   a printable copy.
6. `GET /v1/sessions/<id>/events` (SSE; reconnect with `Last-Event-ID`) streams every row change: findings,
   suspects, next actions, the pipeline run, the report; plus `event: report_ready` when a report is published and
   `event: pipeline_failed` when a run fails. `GET /v1/sessions/<id>`, `/v1/scans/<id>`, `/v1/findings/<id>`, `/v1/suspects/<id>`
   read state. OpenAPI at `/openapi.json`.
