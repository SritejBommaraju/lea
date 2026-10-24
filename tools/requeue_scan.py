#!/usr/bin/env python3
"""Run a scan already in STATE through the live gateway and workers again, as a phone upload would (task B10).

    backend/.venv/bin/python tools/requeue_scan.py --scan <scan_id> [--during-scan] [--gateway URL]
        [--token-file F] [--data-dir DIR] [--timeout 900] [--upload] [--wait-recreation S]

The scan package and its stills are copied under a new scan id (same session, same blobs: nothing is uploaded
again) and posted to the gateway like the app does:
  default        the old app order: the scan package, every still, then POST /complete (the upload is done);
  --during-scan  the app order from 2026-10-24: each still as it was taken (the original spacing between captures),
                 before the scan package exists (a pending scan), then the scan package and /complete at Done.
The clock starts when /complete returns (the last upload) and stops when the report is published. Printed (JSON):
the new scan id, seconds from the last upload to the report, the run row's per-stage and per-still timings, the
Claude crop calls, and the report's most likely findings. The device token is read from --token-file or
REPAIROS_DEVICE_TOKEN and never printed.

--upload: the target gateway is ANOTHER machine (the Runpod pod): a new session is made there (POST /v1/sessions with
the local session's product, make/model and symptom), every blob the scan package and its stills name is uploaded
first (PUT /v1/blobs/<sha256>, in chunks; a blob already there is skipped), the copies carry the new session id, and
the results are read from the gateway (GET .../pipeline, .../report) instead of the local STATE.
--wait-recreation S: after the report, wait up to S seconds for the recreation (and, when the pod's engine is
`splat`, the splat) and print their records from GET .../pipeline.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _call(base: str, token: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path}: HTTP {e.code}: {e.read()[:800].decode(errors='replace')}") from None


def _copy(scan: dict, stills: list[dict], new_id: str, session_id: str | None = None) -> tuple[dict, list[dict]]:
    sess = {"session_id": session_id} if session_id else {}
    pkg = {**scan, "scan_id": new_id, **sess}
    out = []
    for st in stills:
        n = st["still_id"].rpartition("_")[2]
        out.append({**st, "scan_id": new_id, "still_id": f"{new_id}_{n}", **sess})
    return pkg, out


def blob_refs(scan: dict, stills: list[dict]) -> list[str]:
    """Every blob ref the scan package and its stills name (in order, once each)."""
    sys.path.insert(0, str(ROOT / "backend"))
    from repairos.sessions.layout import map_files

    seen: dict[str, None] = {}

    def note(v: str, _base: str) -> str:
        seen.setdefault(v, None)
        return v
    map_files("scan-package", scan, note)
    for st in stills:
        map_files("still", st, note)
    return list(seen)


def _upload(base: str, token: str, blobs: object, ref: str, chunk: int = 8 << 20) -> int:
    """PUT one local blob to the gateway in chunks (Content-Range), resuming from the gateway's staged offset (HEAD,
    as the phone does); 0 when it was already there, else the bytes sent."""
    sha = ref.rpartition(":")[2]
    size = blobs.size(ref)
    auth = {"Authorization": f"Bearer {token}"}
    head = urllib.request.Request(f"{base}/v1/blobs/{sha}", method="HEAD", headers=auth)
    try:
        with urllib.request.urlopen(head, timeout=60) as r:
            if r.headers.get("Upload-Complete") == "1":
                return 0
            start = int(r.headers.get("Upload-Offset") or 0)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HEAD blob {sha[:12]}: HTTP {e.code}") from None
    sent = 0
    with blobs.open(ref) as f:
        f.seek(start)
        while True:
            data = f.read(chunk)
            headers = {**auth, "Content-Type": "application/octet-stream"}
            if size:
                headers["Content-Range"] = f"bytes {start}-{start + max(len(data), 1) - 1}/{size}"
            req = urllib.request.Request(f"{base}/v1/blobs/{sha}", data=data, method="PUT", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    if r.status in (200, 201):
                        return sent + len(data)
            except urllib.error.HTTPError as e:
                raise SystemExit(f"PUT blob {sha[:12]}: HTTP {e.code}: {e.read()[:400].decode(errors='replace')}") from None
            start += len(data)
            sent += len(data)
            if not data or start >= size:
                raise SystemExit(f"PUT blob {sha[:12]}: the gateway did not commit it after {start} bytes")


def _t(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


async def _load(data_dir: Path, scan_id: str) -> tuple[dict, list[dict], object]:
    sys.path.insert(0, str(ROOT / "backend"))
    from repairos.slots.state_sqlite import SqliteState

    state = SqliteState(data_dir / "state.sqlite3")
    row = await state.get("scan-package", scan_id)
    if row is None:
        raise SystemExit(f"no scan-package {scan_id} in {data_dir}")
    stills = sorted((r.data for r in await state.list("still", scan_id=scan_id)),
                    key=lambda s: (s.get("captured_at") or "", int(s["still_id"].rpartition("_s")[2] or 0)
                                   if s["still_id"].rpartition("_s")[2].isdigit() else 0))
    return row.data, stills, state


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scan", required=True)
    ap.add_argument("--during-scan", action="store_true", help="post each still as it was taken, before the package")
    ap.add_argument("--gateway", default="http://127.0.0.1:8000")
    ap.add_argument("--token-file", default=None)
    ap.add_argument("--data-dir", default=os.environ.get("REPAIROS_DATA_DIR") or str(ROOT / "data"))
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--upload", action="store_true", help="the gateway is another machine: upload session + blobs")
    ap.add_argument("--wait-recreation", type=float, default=0.0, help="seconds to wait for recreation/splat")
    a = ap.parse_args(argv)
    token = (Path(a.token_file).read_text().strip() if a.token_file else os.environ.get("REPAIROS_DEVICE_TOKEN", ""))
    if not token:
        raise SystemExit("a paired device token is needed: --token-file or REPAIROS_DEVICE_TOKEN")
    data_dir = Path(a.data_dir).expanduser()
    scan, stills, state = asyncio.run(_load(data_dir, a.scan))
    new_id = f"scan_{secrets.token_hex(8)}"
    base = a.gateway.rstrip("/")
    session_id = None
    uploaded = {}
    if a.upload:
        from repairos.slots.blob_local import LocalBlobStore

        sess = asyncio.run(state.get("session", scan["session_id"]))
        meta = {k: (sess.data if sess else {}).get(k) for k in ("product_id", "make_model", "symptom")}
        session_id = _call(base, token, "POST", "/v1/sessions", meta)["session_id"]
        blobs = LocalBlobStore(data_dir / "blobs")
        refs = blob_refs(scan, stills)
        t_up = time.time()
        sent = sum(_upload(base, token, blobs, r) for r in refs)
        uploaded = {"session_id": session_id, "blobs": len(refs), "mb_sent": round(sent / 1e6, 1),
                    "seconds": round(time.time() - t_up, 1)}
        print(json.dumps({"uploaded": uploaded}), file=sys.stderr)
    pkg, copies = _copy(scan, stills, new_id, session_id)
    t_first = time.time()
    if a.during_scan:
        t0 = _t(copies[0]["captured_at"]) if copies and copies[0].get("captured_at") else None
        for st in copies:
            if t0 is not None and st.get("captured_at"):
                time.sleep(max(0.0, (_t(st["captured_at"]) - t0) - (time.time() - t_first)))
            r = _call(base, token, "POST", "/v1/stills", st)
            if not r.get("scan_pending"):
                raise SystemExit(f"still {st['still_id']}: the gateway did not take it as a pending scan: {r}")
        _call(base, token, "POST", "/v1/scans", pkg)
    else:
        _call(base, token, "POST", "/v1/scans", pkg)
        for st in copies:
            _call(base, token, "POST", "/v1/stills", st)
    _call(base, token, "POST", f"/v1/scans/{new_id}/complete")
    t_done = time.time()
    status = {}
    while time.time() - t_done < a.timeout:
        status = _call(base, token, "GET", f"/v1/scans/{new_id}/pipeline")
        if status.get("status") in ("done", "failed"):
            break
        time.sleep(0.25)
    t_report = time.time()
    if a.upload:
        run = status
        try:
            rep = type("Row", (), {"data": _call(base, token, "GET", f"/v1/scans/{new_id}/report")})()
        except SystemExit:
            rep = None
    else:
        run = asyncio.run(state.get("pipeline-run", f"run_{new_id}"))
        rep = asyncio.run(state.get("report", f"rep_{new_id}"))
        run = run.data if run else {}
    stills_rec = run.get("stills") or {}
    timing = {sid.rpartition("_")[2]: {**(st.get("timing") or {}), "covered": bool(st.get("covered")),
                                       "seconds": st.get("seconds")} for sid, st in stills_rec.items()}
    out = {
        "source_scan": a.scan, "scan_id": new_id, "mode": "during_scan" if a.during_scan else "at_done",
        "status": status.get("status"), "error": status.get("error"),
        "upload_seconds": round(t_done - t_first, 1),
        "seconds_last_upload_to_report": round(t_report - t_done, 1),
        "stages": {k: {kk: v.get(kk) for kk in ("seconds", "vlm_calls", "reused_regions", "detector_seconds",
                                                 "inspected", "kept", "covered", "decided_during_scan",
                                                 "decided_now", "jev_requests") if kk in v}
                   for k, v in (run.get("stages") or {}).items()},
        "stills": timing,
        "claude_crop_calls": sum(int(t.get("vlm_calls") or 0) for t in timing.values()),
    }
    if rep:
        r = rep.data
        by = {e["finding_id"]: e for e in r["findings"]}
        out["verdict"] = r["verdict"]
        out["most_likely"] = [{"part": by[f]["part_class"], "issue": by[f]["issue"], "severity": by[f].get("severity"),
                               "confidence": by[f]["confidence_word"], "evidence": by[f]["evidence"][:3]}
                              for f in r["most_likely"]]
        out["counts"] = {"findings": len(r["findings"]), "issues": sum(1 for e in r["findings"] if e["issue"] != "none"),
                         "cosmetic": len(r["cosmetic"]), "not_inspected_rows": len(r["not_inspected"]),
                         "insufficient_evidence": len(r["insufficient_evidence"])}
        out["summary"] = r["summary"]["text"]
    if uploaded:
        out["uploaded"] = uploaded
    if a.wait_recreation > 0:
        t_rec = time.time()
        while time.time() - t_rec < a.wait_recreation:
            view = _call(base, token, "GET", f"/v1/scans/{new_id}/pipeline")
            rec, sp = view.get("recreation") or {}, view.get("splat") or {}
            rec_settled = rec.get("status") in ("done", "unavailable", "failed")
            sp_settled = sp.get("status") in ("done", "failed") or (sp.get("status") == "unavailable"
                                                                   and time.time() - t_rec > 30)
            if rec_settled and (sp_settled or rec.get("status") != "done"):
                break
            time.sleep(2)
        out["recreation"] = {k: rec.get(k) for k in ("status", "kind", "rev", "bytes", "seconds", "reason")}
        out["splat"] = {k: sp.get(k) for k in ("status", "rev", "bytes", "seconds", "stats", "reason")} if sp else None
        out["recreation_wait_s"] = round(time.time() - t_rec, 1)
    print(json.dumps(out, indent=1))
    return 0 if status.get("status") == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
