#!/usr/bin/env bash
# Smoke test of a running LEA backend (gateway + worker), local or on Runpod. Prints step latencies, never a secret.
#
#   deploy/smoke.sh <base_url> [--image photo.jpg] [--timeout 600] [--requeue <scan_id>]
#
#   base_url    e.g. https://<pod_id>-8000.proxy.runpod.net  or  http://127.0.0.1:8000
#   auth        REPAIROS_DEVICE_TOKEN (an already paired device token), else REPAIROS_PAIRING_CODE (pairs a new
#               device "smoke"), else the code is asked for without echo. Neither value is ever printed.
#   --image     a real JPEG to send instead of deploy/smoke-still.jpg (a small synthetic board picture: it proves
#               the plumbing, a real photo also exercises the detector boxes and the Claude crop calls)
#   --requeue   also replay a scan that already exists in this machine's STATE through the gateway with
#               tools/requeue_scan.py (it reads REPAIROS_DATA_DIR locally, so run it INSIDE the pod, base_url
#               http://127.0.0.1:8000)
#
# Steps: GET /healthz, pair, POST /v1/sessions, PUT the still's blob, POST /v1/scans (no-depth package), POST /v1/stills, POST .../complete, poll .../pipeline until done or failed, GET the
# report. Exit 0 only when the pipeline finished with a report. Needs curl, python3 (stdlib only) and shasum or sha256sum.
set -euo pipefail

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
[ $# -ge 1 ] || usage
BASE="${1%/}"; shift
case "$BASE" in http://*|https://*) ;; *) echo "base_url must start with http:// or https://"; exit 2;; esac
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="$HERE/smoke-still.jpg"; TIMEOUT=600; REQUEUE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE="$2"; shift 2;;
    --timeout) TIMEOUT="$2"; shift 2;;
    --requeue) REQUEUE="$2"; shift 2;;
    -h|--help) usage;;
    *) echo "unknown argument $1"; usage;;
  esac
done
[ -f "$IMAGE" ] || { echo "no image at $IMAGE"; exit 2; }
PY="$(command -v python3)"
sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

TMP="$(mktemp -d)"; chmod 700 "$TMP"
trap 'rm -rf "$TMP"' EXIT
AUTH="$TMP/auth.h"   # the Authorization header lives only in this 0600 file, never on a command line or in output
FAILED=0

ms() { "$PY" -c "import sys; print(round(float(sys.argv[1]) * 1000))" "$1"; }
step() { printf '  %-34s %7s ms  %s\n' "$1" "$2" "${3:-}"; }
die() { echo "FAIL: $*"; exit 1; }

# call <method> <path> [json-body-file] [extra curl args...] -> body in $TMP/out, status in $CODE, seconds in $SECS
call() {
  local method=$1 path=$2 body=${3:-}; shift $(( $# >= 3 ? 3 : $# ))
  local args=(-sS -X "$method" -o "$TMP/out" -w '%{http_code} %{time_total}' --max-time 90)
  [ -f "$AUTH" ] && args+=(-H "@$AUTH")
  [ -n "$body" ] && args+=(-H 'Content-Type: application/json' --data-binary "@$body")
  local res; res="$(curl "${args[@]}" "$@" "$BASE$path")" || die "$method $path: curl failed (network, TLS or proxy)"
  CODE=${res%% *}; SECS=${res##* }
}
field() { "$PY" -c "import functools,json,sys; d=json.load(open(sys.argv[1])); print(functools.reduce(lambda a, k: a[int(k)] if isinstance(a, list) else a[k], sys.argv[2].split('.'), d))" "$TMP/out" "$1"; }
excerpt() { head -c 400 "$TMP/out"; echo; }

echo "LEA smoke test against $BASE"
T0=$("$PY" -c 'import time; print(time.time())')

# 1. health
call GET /healthz
[ "$CODE" = 200 ] || { excerpt; die "GET /healthz returned $CODE"; }
step "GET /healthz" "$(ms "$SECS")" "200"

# 2. auth: an existing token, or pair with the code (sent from a file, never echoed)
if [ -n "${REPAIROS_DEVICE_TOKEN:-}" ]; then
  ( umask 077; printf 'Authorization: Bearer %s\n' "$REPAIROS_DEVICE_TOKEN" > "$AUTH" )
  step "device token" "-" "from REPAIROS_DEVICE_TOKEN"
else
  code="${REPAIROS_PAIRING_CODE:-}"
  if [ -z "$code" ]; then read -rsp "Pairing code (REPAIROS_PAIRING_CODE, not echoed): " code; echo; fi
  ( umask 077; CODE_IN="$code" "$PY" -c "import json,os; print(json.dumps({'code': os.environ['CODE_IN'], 'name': 'smoke'}))" > "$TMP/pair.json" )
  unset code
  call POST /v1/devices/pair "$TMP/pair.json"
  rm -f "$TMP/pair.json"
  [ "$CODE" = 201 ] || { excerpt; die "pairing returned $CODE (403 = wrong code, 503 = REPAIROS_PAIRING_CODE not set on the server)"; }
  ( umask 077; printf 'Authorization: Bearer %s\n' "$(field token)" > "$AUTH" )
  step "POST /v1/devices/pair" "$(ms "$SECS")" "201, device $(field device_id)"
fi

# 3. session
echo '{"symptom": "deploy smoke test"}' > "$TMP/session.json"
call POST /v1/sessions "$TMP/session.json"
[ "$CODE" = 201 ] || { excerpt; die "POST /v1/sessions returned $CODE"; }
SID="$(field session_id)"
step "POST /v1/sessions" "$(ms "$SECS")" "$SID"

# 4. the still's JPEG as a content-addressed blob
SHA="$(sha256 "$IMAGE")"
read -r W H < <("$PY" - "$IMAGE" <<'EOF'
import struct, sys
f = open(sys.argv[1], "rb"); assert f.read(2) == b"\xff\xd8", "not a JPEG"
while True:
    m, = struct.unpack(">H", f.read(2)); n, = struct.unpack(">H", f.read(2))
    if m in (0xFFC0, 0xFFC1, 0xFFC2):
        h, w = struct.unpack(">xHH", f.read(5)); print(w, h); break
    f.seek(n - 2, 1)
EOF
)
call PUT "/v1/blobs/$SHA" "" -H 'Content-Type: image/jpeg' --data-binary "@$IMAGE"
case "$CODE" in 200|201) ;; *) excerpt; die "PUT /v1/blobs returned $CODE";; esac
step "PUT /v1/blobs (still)" "$(ms "$SECS")" "$CODE, $(wc -c < "$IMAGE" | tr -d ' ') bytes, ${W}x${H}"

# 5. the scan package, then the still, then complete (every gateway version takes this order)
SCAN="scan_smoke$("$PY" -c 'import secrets; print(secrets.token_hex(6))')"
NOW="$("$PY" -c 'from datetime import datetime, UTC; print(datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"))')"
"$PY" - "$TMP" "$SCAN" "$SID" "$NOW" "$SHA" "$W" "$H" <<'EOF'
import json, sys
tmp, scan, sid, now, sha, w, h = sys.argv[1:]
json.dump({"still_id": f"{scan}_s1", "scan_id": scan, "session_id": sid, "kind": "unguided", "captured_at": now,
           "image_file": f"blob://sha256:{sha}", "width_px": int(w), "height_px": int(h), "pose_reliable": False,
           "targets": [], "tracking_state": "unknown"}, open(f"{tmp}/still.json", "w"))
json.dump({"scan_id": scan, "product_id": "unidentified", "session_id": sid, "captured_at": now,
           "device": {"model": "smoke-test", "ios": "n/a"}, "frame": "arkit_world", "units": "m",
           "depth_source": "none", "gravity_arkit_world": [0, -1, 0], "keyframes": [],
           "supporting_plane_removed": False}, open(f"{tmp}/scan.json", "w"))
EOF
call POST /v1/scans "$TMP/scan.json"
case "$CODE" in 200|201) ;; *) excerpt; die "POST /v1/scans returned $CODE";; esac
step "POST /v1/scans" "$(ms "$SECS")" "$CODE, $SCAN"
call POST /v1/stills "$TMP/still.json"
[ "$CODE" = 201 ] || { excerpt; die "POST /v1/stills returned $CODE"; }
step "POST /v1/stills" "$(ms "$SECS")" "201, jobs $(field jobs.0)"
call POST "/v1/scans/$SCAN/complete"
[ "$CODE" = 202 ] || { excerpt; die "POST .../complete returned $CODE"; }
step "POST .../complete" "$(ms "$SECS")" "202"
TC=$("$PY" -c 'import time; print(time.time())')

# 6. poll the pipeline (the app polls the same endpoint)
STATUS=""
while :; do
  call GET "/v1/scans/$SCAN/pipeline"
  [ "$CODE" = 200 ] || { excerpt; die "GET .../pipeline returned $CODE"; }
  STATUS="$(field status)"
  case "$STATUS" in done|failed) break;; esac
  ELAPSED=$("$PY" -c "import time, sys; print(int(time.time() - float(sys.argv[1])))" "$TC")
  [ "$ELAPSED" -lt "$TIMEOUT" ] || die "pipeline still '$STATUS' after ${TIMEOUT}s (is the worker running? pod logs: [worker] lines)"
  sleep 1
done
TR=$("$PY" -c "import time, sys; print(round(time.time() - float(sys.argv[1]), 1))" "$TC")
step "pipeline (complete -> $STATUS)" "$("$PY" -c "print(round($TR * 1000))")" "$TR s"
if [ "$STATUS" = failed ]; then excerpt; FAILED=1; fi

# 7. the report
call GET "/v1/scans/$SCAN/report"
if [ "$CODE" = 200 ]; then
  step "GET .../report" "$(ms "$SECS")" "verdict $(field verdict)"
else
  excerpt; FAILED=1; step "GET .../report" "$(ms "$SECS")" "HTTP $CODE"
fi

TOTAL=$("$PY" -c "import time, sys; print(round(time.time() - float(sys.argv[1]), 1))" "$T0")
echo "  total ${TOTAL} s; scan $SCAN in session $SID"

# 8. optional: replay a real recorded scan through the live gateway and worker (inside the pod)
if [ -n "$REQUEUE" ]; then
  ROOT="$(cd "$HERE/.." && pwd)"
  RPY=""; for c in /opt/venv/bin/python "${LEA_VENV:-/workspace/venv}/bin/python" "$ROOT/backend/.venv/bin/python"; do [ -x "$c" ] && { RPY="$c"; break; }; done
  [ -n "$RPY" ] || RPY="$(command -v python)"   # the image venv, the Route B venv, the Mac venv, else PATH
  [ -f "${REPAIROS_DATA_DIR:-$ROOT/data}/state.sqlite3" ] || die "--requeue reads STATE locally: no ${REPAIROS_DATA_DIR:-$ROOT/data}/state.sqlite3 here (run it inside the pod)"
  echo "replaying $REQUEUE with tools/requeue_scan.py (scan package, stills, complete: the at-Done order)"
  token="$(sed -n 's/^Authorization: Bearer //p' "$AUTH")"
  REPAIROS_DEVICE_TOKEN="$token" "$RPY" "$ROOT/tools/requeue_scan.py" --scan "$REQUEUE" --gateway "$BASE" \
    --timeout "$TIMEOUT" > "$TMP/requeue.json" || FAILED=1
  unset token
  [ -s "$TMP/requeue.json" ] && "$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); print('  requeue', d['scan_id'], d['status'], 'last upload -> report', d['seconds_last_upload_to_report'], 's,', d['claude_crop_calls'], 'Claude crop calls, verdict', d.get('verdict'))" "$TMP/requeue.json" || { echo '  requeue failed (its error is above)'; FAILED=1; }
fi

[ "$FAILED" -eq 0 ] && echo "PASS" || { echo "FAIL"; exit 1; }
