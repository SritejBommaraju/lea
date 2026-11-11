# Sample Sessions

This directory contains recorded real capture sessions for testing and development.

## Getting Sample Sessions

Sample sessions are created by:

1. **Running the iOS app**: Every capture is automatically saved to the gateway's `data/sessions/` directory
2. **Importing photos**: Use `tools/import_photos.py` to convert plain iPhone photos into a session:
   ```bash
   python3 -I tools/import_photos.py <photo-folder> --out samples/sessions/<session-id> --symptom "description"
   ```
3. **Importing from Stray Scanner**: Use `tools/import_capture.py` to convert a Stray Scanner export:
   ```bash
   backend/.venv/bin/python tools/import_capture.py <export-folder>
   ```

## No Sample Sessions Yet?

If this directory is empty, that's expected for a fresh clone. To test the pipeline:

1. Follow the quickstart in the root README to set up the backend and iOS app
2. Capture an object with the app (any hardware item)
3. The session will be saved in the gateway's `data/sessions/` directory
4. Copy a session from `data/sessions/` to `samples/sessions/` to preserve it for testing

See [samples/README.md](../README.md) for the full session format specification.

## Important: No Fake Data

Per PLAN.md invariant GEN3, we never create synthetic or fake sessions. All samples must come from real captures of physical objects.
