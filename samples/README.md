# samples/: recorded real sessions

One folder per **real** capture of one physical unit (PLAN.md task B6). The backend is developed and regression-tested
by replaying these. No synthetic product, no made-up readings (GEN3). Product facts live here, never in code (GEN1).

```
make replay SESSION=<session_id | folder path>   # validate, load into a fresh STATE/BLOB, run the pipeline, score
make sessions-check                              # validate every non-held-out session (CI)
```

Replay writes `data/replay/<session_id>/` (its own SQLite state and blob store) and `summary.json` with per-stage
timing, the findings, and the score against `truth.json`. Until the pipeline exists (task B10) the `pipeline`
stage reports `not_built`; nothing stands in for it. Phase 1 replay makes no Grain calls.

## Where sessions come from

- **The gateway** writes every object it receives into `data/sessions/<session_id>/` in this layout (`source:
  gateway`), so any real run on the laptop is replayable as is. To keep one as a sample: copy the folder to
  `samples/sessions/`, fill in `object_id`, `category`, `operator`, `lighting`, `meter`, write `truth.json`, and
  run `make sessions-check`.
- The app upload (A4) and the importers (B12) write the same layout with
  `repairos.sessions.writer.SessionWriter`.

## Layout

```
samples/sessions/<session_id>/
  session.json                         samples/session.schema.json
  truth.json                           expected outcomes: samples/session.schema.json#/$defs/truth
  scans/<scan_id>/scan_package.json    ScanPackage (spec/contracts/scan-package.schema.json)
  scans/<scan_id>/points_arkit_world.ply
  scans/<scan_id>/<keyframe_id>.jpg, <keyframe_id>_depth.png, <keyframe_id>_conf.png
  stills/<still_id>.json               Still (spec/contracts/still.schema.json)
  stills/<still_id>.jpg, <still_id>_depth.png, <still_id>_conf.png
  readings/<reading_id>.json           MeterReading, typed by the operator from the real meter
  parts/<item_id>.json                 PartOnHand, as the user described it
```

Every file path inside a JSON object is **relative to the session folder** (for example
`scans/scan_1a2b3c4d/kf_000.jpg`). On the wire (gateway) the same fields hold blob refs instead
(`blob://sha256:<hex>`); the writer turns refs into files and replay turns files back into refs.

Phase 2 adds `grain/` (recorded real Grain responses; gitignored, never committed).

## Files

| File | Format | Checked by the validator |
|---|---|---|
| `points_arkit_world.ply` | binary little-endian PLY, ARKit world frame, **metres**, Y up; `float x, y, z` + `uchar red, green, blue`, optional `uchar confidence, views` | `format binary_little_endian 1.0`, float x/y/z, vertex count = `point_count`, body size |
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
(eval/categories.yaml id), `product_id`, `make_model` (as typed), `symptom`, `operator`, `lighting`
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
2. `POST /v1/sessions {"symptom": "..."}` returns the `session_id`.
3. For each file (PLY, keyframe JPEGs, depth, confidence, stills): `PUT /v1/blobs/<sha256 of the whole file>`,
   whole or in chunks with `Content-Range: bytes <start>-<end>/<total>`. A partial chunk answers 202 with
   `Upload-Offset`; after a dropped connection `HEAD /v1/blobs/<sha256>` gives `Upload-Offset` to resume from. The
   last chunk answers 201 `{ref}` only when the bytes hash to the sha256 (else 422, staging discarded). An upload
   of a blob that already exists answers 200 with the same ref.
4. `POST /v1/scans` with the ScanPackage whose file fields are those refs; then `POST /v1/stills`, `POST
   /v1/readings`. Bodies are validated against the contracts and the files against the table above (422 with the
   schema path or the list of problems). Posting the same object again is a no-op.
5. `GET /v1/sessions/<id>/events` (SSE; reconnect with `Last-Event-ID`) streams every row change: findings,
   suspects, next actions. `GET /v1/sessions/<id>`, `/v1/scans/<id>`, `/v1/findings/<id>`, `/v1/suspects/<id>`
   read state. OpenAPI at `/openapi.json`.
