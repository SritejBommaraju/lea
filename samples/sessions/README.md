# Sample Sessions

This directory holds recorded real capture sessions for regression testing and replay. Each session is a subfolder containing:

- `session.json` – session metadata
- `truth.json` – expected outcomes (faults, negatives)
- `scans/<scan_id>/` – scan packages with LiDAR point clouds, meshes, and keyframes
- `stills/<still_id>.jpg` – full-resolution photos
- `readings/<reading_id>.json` – meter readings (optional)

See `samples/README.md` for the full layout and how to create sessions.

## Getting Started

There are three ways to create a session in this directory:

### 1. From iPhone Photos (Quickest)

If you have 5-10 iPhone photos of a broken device:

```bash
python3 -I tools/import_photos.py ~/path/to/photos --out samples/sessions/sess_YYYYMMDD_demo \
    --symptom "won't turn on"
make replay SESSION=sess_YYYYMMDD_demo
```

This runs detection and generates a report (no 3D mesh, as there's no LiDAR data).

### 2. From the Gateway

Real captures from the iOS app are automatically saved in `data/sessions/<session_id>/`. Copy one here:

```bash
cp -r data/sessions/sess_YYYYMMDD_abcd samples/sessions/
# Fill in object_id, truth.json, etc.
make sessions-check
```

### 3. From Stray Scanner or NeRFCapture

Import an export from the MIT-licensed Stray Scanner or NeRFCapture apps:

```bash
python3 -I tools/import_capture.py ~/path/to/export --device-model "iPhone15,2" \
    --ios "18.1" --symptom "screen cracked"
make sessions-check
```

## Why No Sample Sessions Yet?

This directory is empty by default because:
- Real capture sessions contain photos of actual hardware, which may include proprietary designs
- The team will add sanitized sample sessions after verifying they contain no proprietary content
- Creating a sample requires either a real device scan or importing from a third-party capture app

To test the backend pipeline without samples, use the photo import method above with your own device photos.
