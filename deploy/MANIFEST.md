# What runs where (Cal Hacks demo, 2026-10-25)

One page for the demo: every moving part, where it runs, and a one-line check. Secrets are listed **by name only**.
Setup steps are in [`runpod/README.md`](runpod/README.md) and [`../spacetime/README.md`](../spacetime/README.md).

```
iPhone app ──HTTPS──> Runpod pod :8000 gateway ──> worker (Claude, Jev, OWLv2 on the GPU; Bright Data for Fix it)
                              │ SSE
                              └──> bridge (same pod) ──reducers──> SpacetimeDB Maincloud `lea-calhacks`
                                   :8790 thumbnails + mesh (signed URLs)          │ subscriptions
                                                                                  └──> big-screen viewer (laptop browser)
Fallback: the same three processes on the Mac (gateway :8000 on the LAN), the phone re-paired to the Mac.
```

## 1. Runpod pod `lea-demo` (primary backend)

| | |
|---|---|
| Image | `ghcr.io/sritejbommaraju/lea-backend:demo` (`deploy/runpod/Dockerfile`, linux/amd64, ~4.5 GB compressed; private GHCR package, pulled with a Runpod registry credential) |
| Hardware | 1x RTX A5000 24 GB, Secure Cloud, on demand (~$0.28/hr with disks); fallbacks in `runpod/README.md` |
| Processes | `start.sh` supervises: `[gateway]` uvicorn `repairos.main:app` on :8000; `[worker]` `repairos.pipeline.worker`; `[bridge]` `repairos.live.spacetime_bridge` (only when `SPACETIME_DB` is set). Each restarts on a crash; SIGTERM stops all |
| Ports | `8000/http` → `https://<pod_id>-8000.proxy.runpod.net` (gateway); `8790/http` → `https://<pod_id>-8790.proxy.runpod.net` (bridge media, HMAC-signed expiring URLs, 403 otherwise) |
| Secrets (Runpod secret → env) | `anthropic_api_key` → `ANTHROPIC_API_KEY`; `typesafe_key` → `TYPESAFE_KEY`; `repairos_pairing_code` → `REPAIROS_PAIRING_CODE` (new, long, random for the cloud); `brightdata_api_key` → `BRIGHTDATA_API_KEY`; `brightdata_serp_zone` → `BRIGHTDATA_SERP_ZONE` (zone `serp_api1`); `cartesia_api_key` → `CARTESIA_API_KEY`; `spacetime_token` → `SPACETIME_TOKEN` |
| Plain env | `REPAIROS_DATA_DIR=/workspace/lea-data`, `SPACETIME_DB=lea-calhacks`, `SPACETIME_HOST=https://maincloud.spacetimedb.com`; start.sh derives `SPACETIME_MEDIA_PORT=8790` and `SPACETIME_MEDIA_BASE=https://$RUNPOD_POD_ID-8790.proxy.runpod.net` |
| Volume | pod volume disk 20 GB at `/workspace` (SQLite `state.sqlite3`, blobs, sessions, `cache/fix` + `cache/hints` 24 h caches, the bridge's device token in `live/`); container disk 30 GB, wiped on stop. Survives stop/restart, deleted on terminate. Leave it running for the demo |
| Create | `RUNPOD_API_KEY=… deploy/runpod/create_pod.sh --image ghcr.io/sritejbommaraju/lea-backend:demo --registry-auth <id> --all --spacetime lea-calhacks` (or the console form, `runpod/README.md` step 2) |
| Optional features | Fix it + reference hints need both `BRIGHTDATA_*`; voice needs `CARTESIA_API_KEY`; the big screen needs `SPACETIME_DB` + `SPACETIME_TOKEN`. Missing ones are logged by name at start (`[start] note: … is not set`) and the feature is off |

Verify:
- Gateway: `curl -fsS https://<pod_id>-8000.proxy.runpod.net/healthz` → `{"ok":true}`
- End to end (pair, upload, report, step latencies): `REPAIROS_PAIRING_CODE=… deploy/smoke.sh https://<pod_id>-8000.proxy.runpod.net` → `PASS`
- Processes and missing keys: Runpod console > Pods > lea-demo > Logs, look for `[start] live mirror to https://maincloud.spacetimedb.com database lea-calhacks` and no `WARNING`
- Media port: `curl -s -o /dev/null -w '%{http_code}\n' https://<pod_id>-8790.proxy.runpod.net/m/still/x.jpg` → `403` (up; unsigned URLs refused)

## 2. SpacetimeDB Maincloud module (live mirror)

| | |
|---|---|
| Database | `lea-calhacks` on `https://maincloud.spacetimedb.com` (published; owner identity `c20025ee…`) |
| Module | `spacetime/module` (TypeScript, spacetimedb 2.13.0): tables `scan`, `object_identity`, `progress`, `still`, `finding`, `report`, `mesh`, `focus`, `bridge`; reducers callable only by `WRITER_IDENTITY` (the bridge, identity `c200c7dd…`, token file `~/.config/lea/stdb-maincloud.json` on the Mac) or the publisher |
| Writer | the bridge in the pod (`SPACETIME_TOKEN` = the `token` field of that file); checked 2026-10-24: a `heartbeat` call from the Mac with it returned HTTP 200 in 269 ms |
| What it holds | a copy only: no image bytes, thumbnails and mesh are signed URLs to the pod's :8790; the backend stays the source of truth |
| Republish after a schema change | `cd spacetime/module && WRITER_IDENTITY=<bridge identity> spacetime publish lea-calhacks --server maincloud --yes`, then `cd ../viewer && npm run bindings` |

Verify: `spacetime sql lea-calhacks "SELECT id, backend_ok, note FROM bridge" -s maincloud` → one row, `backend_ok = true`,
note `following N session(s), … events`, updated within the last 10 s while the pod's bridge runs.

## 3. Big-screen viewer

| | |
|---|---|
| Code | `spacetime/viewer` (static page: TypeScript client SDK + three.js); `dist/` is the whole site |
| Hosted | on the demo laptop: `cd spacetime/viewer && node serve.mjs` → `http://127.0.0.1:5173/` (any static host works; nothing secret in it) |
| Pointing it at Maincloud | no rebuild: open `http://127.0.0.1:5173/?host=https://maincloud.spacetimedb.com&db=lea-calhacks` (or build once with `SPACETIME_HOST=https://maincloud.spacetimedb.com SPACETIME_DB=lea-calhacks npm run build`) |
| Behaviour | follows the newest scan (`?scan=<id>` pins one); one live card per place on the scan (the bridge keys live rows by the backend's place identity), the report's rows replace them; model and pins share one frame and centring |

Verify: `curl -fsS http://127.0.0.1:5173/ | grep -c '<title>'` → `1`, then the page's status chip reads **Live** (bridge
heartbeat younger than 30 s).

## 4. iPhone app

| | |
|---|---|
| Build | Release, installed over the top on UDID `00008140-0019798C1AE8801C` (never uninstall: the scans and the pairing live in the app) |
| Config | Settings > "Connect to your Mac": **Address** `https://<pod_id>-8000.proxy.runpod.net` (with `https://`), **Pairing code** = the pod's `repairos_pairing_code`, **Pair**, then **Check connection** → Online. Or over USB: `xcrun devicectl device process launch --device 00008140-0019798C1AE8801C com.rohankilaru.repairos --gateway https://<pod_id>-8000.proxy.runpod.net --pairing-code <code>` |
| Holds | a device token only; never an API key |
| Report | "Fix it" card under "Most likely problems": the part and up to 3 offers (price, seller; tap opens Safari), "Searching for parts…" until the live `fix_ready`, "Parts search is not available right now." at the spend cap, nothing when nothing was found |

Verify: `xcrun devicectl device info apps --device 00008140-0019798C1AE8801C | grep com.rohankilaru.repairos` → one line
(installed); in the app, Settings > Check connection → Online.

## 5. The Mac (fallback, and what runs on it today)

| | |
|---|---|
| Processes | gateway `cd ~/lea-demo/backend && REPAIROS_DATA_DIR=~/lea/data ~/lea/backend/.venv/bin/python -m uvicorn repairos.main:app --host 0.0.0.0 --port 8000`; worker `… -m repairos.pipeline.worker` (same dir and env); bridge `… -u -m repairos.live.spacetime_bridge` with `SPACETIME_HOST`, `SPACETIME_DB`, `SPACETIME_TOKEN_FILE`, `SPACETIME_MEDIA_PORT=8790`, `REPAIROS_DATA_DIR=~/lea/data` |
| Today (2026-10-24) | the bridge mirrors into a **local** SpacetimeDB (`spacetimedb-standalone` on 127.0.0.1:3000, database `lea-live`) and the viewer on :5173 follows it. For the fallback on the big screen, run the bridge with `SPACETIME_HOST=https://maincloud.spacetimedb.com SPACETIME_DB=lea-calhacks SPACETIME_TOKEN_FILE=~/.config/lea/stdb-maincloud.json` instead (only one bridge per database at a time) |
| Secrets | `~/lea/backend/.env` (symlinked as `~/lea-demo/backend/.env`): `ANTHROPIC_API_KEY`, `TYPESAFE_KEY`, `REPAIROS_PAIRING_CODE`, `BRIGHTDATA_API_KEY`, `BRIGHTDATA_SERP_ZONE`, `CARTESIA_API_KEY` |
| Data | `~/lea/data` (state, blobs, sessions, caches) |
| Phone config | Address `http://<Mac LAN IP>:8000` (same Wi-Fi), the Mac's pairing code; re-pair when switching between the pod and the Mac (each has its own STATE) |

Verify: `curl -fsS http://127.0.0.1:8000/healthz && pgrep -fl 'repairos.pipeline.worker|spacetime_bridge'` → `{"ok":true}`
and two process lines.

## The person's guess (story → leads)

- The phone or the voice agent posts what the person says is wrong: `POST /v1/sessions/{id}/story`
  `{text, source: typed|voice, scan_id?}` (device auth, 202). The worker reads it into a hypothesis (one quick Claude
  call, `quick_model` in `config/vlm.yaml`), sends the person's own suspect as the first live `lead` at once, then up
  to three research leads (two Bright Data searches at once, 24 h cache in `cache/leads`, spend guard).
- A lead only reorders which regions are cropped first; it never reaches the vision model and never makes a
  finding. The report's `user_lead` says what the photos showed (confirmed, not_seen, could_not_check).
- Verify: `curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"text":"I think the button stopped working"}' http://127.0.0.1:8000/v1/sessions/<session_id>/story` → `202`

## Spend and limits

- Bright Data: each SERP request passes the spend guard (`repairos.spend.charge("brightdata", key=<query hash>)`)
  after the 24 h cache; at the cap the search is skipped, not retried. Three requests per (object, part) for Fix it,
  one per identified product for hints, each at most once a day.
- Runpod: ~$0.28/hr while the pod runs. Stop the pod after the demo; terminate deletes the volume.
- The pod's proxy URL is public: the pairing code is the only gate. Keep it long.
