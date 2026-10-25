# Live state in SpacetimeDB (big screen)

The Python backend stays the source of truth and does all compute. SpacetimeDB holds a live, in-memory mirror of
the current scans that a web page subscribes to. If SpacetimeDB is down or not configured, the backend and the phone
work exactly as before.

```
gateway SSE (/v1/sessions/{id}/events) ──> bridge (backend/repairos/live/spacetime_bridge.py) ──reducers over HTTP──>
SpacetimeDB module (spacetime/module) ──subscriptions──> viewer (spacetime/viewer)
```

- `module/`: tables `scan`, `object_identity`, `progress`, `still` (thumbnail URL only, never image bytes),
  `finding` (with the 3D point in the ARKit frame), `report`, `mesh`, `focus`, `bridge`. Reducers write; only the
  bridge identity (env `WRITER_IDENTITY`) or the publisher may call them. `set_report` swaps the live findings for
  the report's ranked findings in one transaction.
- `viewer/`: one static page (TypeScript client SDK + three.js). It follows the newest scan; `?scan=<id>` pins one,
  `?host=` and `?db=` point it elsewhere.

## Run locally

```
spacetime start                                    # local server on :3000
cd backend && SPACETIME_HOST=http://127.0.0.1:3000 python -m repairos.live.spacetime_bridge --mint-identity ~/.config/lea/stdb-local.json
cd spacetime/module && npm ci && WRITER_IDENTITY=<identity printed above> spacetime publish lea-live --server local --yes
cd backend && SPACETIME_HOST=http://127.0.0.1:3000 SPACETIME_DB=lea-live SPACETIME_TOKEN_FILE=~/.config/lea/stdb-local.json \
  SPACETIME_MEDIA_PORT=8790 python -m repairos.live.spacetime_bridge
cd spacetime/viewer && npm ci && npm start         # http://127.0.0.1:5173/
```

## Maincloud

```
spacetime login                                    # browser
cd backend && WRITER_IDENTITY=$(SPACETIME_HOST=https://maincloud.spacetimedb.com \
  python -m repairos.live.spacetime_bridge --mint-identity ~/.config/lea/stdb-maincloud.json)
cd spacetime/module && WRITER_IDENTITY=$WRITER_IDENTITY spacetime publish <db-name> --server maincloud --yes
cd spacetime/viewer && SPACETIME_HOST=https://maincloud.spacetimedb.com SPACETIME_DB=<db-name> npm run build
```

`viewer/dist/` is the whole site: serve it with `npm start` (no rebuild needed: `node serve.mjs`) or any static host.
`dist/config.js` holds the host and database name.

After a module schema change: `cd viewer && npm run bindings`.

## Bridge settings

See the docstring of `backend/repairos/live/spacetime_bridge.py`. Thumbnails and the mesh are served by the bridge at
signed, expiring URLs only when `SPACETIME_MEDIA_PORT` is set; `SPACETIME_MEDIA_BASE` is that port's public URL.
