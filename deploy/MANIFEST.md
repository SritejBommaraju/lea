# What runs where

One page: every moving part, where it runs, and a one-line check. Secrets are listed **by name only**.

The GPU pod (Runpod), the SpacetimeDB live mirror and the pipeline worker were removed on 2026-10-28 (PLAN.md §10): the
design engine (Grain) judges scans, so the gateway needs no GPU and no worker.

```
iPhone app ──HTTPS──> gateway :8000 (CPU host or laptop) ──HTTPS──> Grain (the design engine, hosted separately)
                              ├─ SQLite state + blob folder + session folders under REPAIROS_DATA_DIR
                              └─ Claude (Lea's answers), Cartesia (her voice)
```

## 1. The gateway (CPU host)

| | |
|---|---|
| Hardware | any machine that runs Python 3.12 (a small VM or a laptop); no GPU |
| Install | `make bootstrap` (backend venv from `backend/requirements.lock`) |
| Process | one process: `cd backend && .venv/bin/uvicorn repairos.main:app --host 0.0.0.0 --port 8000` (`make run-gateway` for development) |
| Port | `8000/http`, put TLS in front of it (a reverse proxy or the host's HTTPS ingress) |
| Secrets (env or `backend/.env`) | `GRAIN_API_KEY`, `ANTHROPIC_API_KEY`, `CARTESIA_API_KEY` (optional), `REPAIROS_PAIRING_CODE` (long, random) |
| Plain env | `GRAIN_URL`, `GRAIN_PROJECT` (optional default design), `REPAIROS_DATA_DIR` (state, blobs, sessions, voice conversations) |
| Disk | grows with scans: each session folder holds the PLY, keyframes and stills it received, plus `grain/` answers |

Without `GRAIN_URL` the gateway still runs, takes uploads and serves voice, but `POST /v1/scans/{id}/complete` answers
409 "no design engine configured": nothing else judges a scan.

Verify:
- Gateway: `curl -fsS http://<host>:8000/healthz` returns `{"ok":true}`
- Design engine reachable and keyed: pair a phone, scan, and the report arrives (`report_ready`); a wrong key shows as
  `pipeline_failed` with Grain's own error in the run's `error`
- Missing keys: the voice routes answer 503 `voice_unavailable` / `answer_unavailable` naming the key

## 2. The phone

| | |
|---|---|
| Build | `ios/README.md` (Xcode, or the iOS workflow on GitHub Actions from Windows) |
| Config | Settings > Connect: gateway address and pairing code; Settings > Product: the design id printed on the product |
| Holds | no key, ever (A6) |

## 3. Spend

Claude and Cartesia calls the gateway makes itself are capped in `config/spend.yaml` (`python -m repairos.spend status`).
Grain bills on its own side and is not counted there.
