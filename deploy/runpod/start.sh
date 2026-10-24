#!/usr/bin/env bash
# Container entry point: the gateway (uvicorn, port $PORT) and one pipeline worker, sharing $REPAIROS_DATA_DIR.
# A tiny supervisor, no process manager: each process is restarted after a crash (with a short backoff), every line
# of output is prefixed with its name, and SIGTERM/SIGINT (pod stop) stops both cleanly.
set -uo pipefail

: "${REPAIROS_DATA_DIR:=/workspace/lea-data}"
: "${PORT:=8000}"
: "${APP_DIR:=/app}"   # the repo layout inside the image (Dockerfile)
export REPAIROS_DATA_DIR PORT
cd "$APP_DIR"

mkdir -p "$REPAIROS_DATA_DIR"
# Code that writes under the repo's data/ instead of REPAIROS_DATA_DIR (the Jev exchange records,
# repairos/slots/fusion_jev RECORD_DIR) lands on the volume too.
if [ ! -e "$APP_DIR/data" ]; then ln -s "$REPAIROS_DATA_DIR" "$APP_DIR/data"; fi

# Names only, never values: say which settings are missing before anything starts.
for key in ANTHROPIC_API_KEY TYPESAFE_KEY REPAIROS_PAIRING_CODE; do
  if [ -z "${!key:-}" ]; then echo "[start] WARNING: $key is not set (Runpod pod env / secret); see deploy/runpod/README.md"; fi
done
# A Runpod secret reference that did not resolve arrives as the literal template text.
for key in ANTHROPIC_API_KEY TYPESAFE_KEY REPAIROS_PAIRING_CODE BRIGHTDATA_API_KEY; do
  case "${!key:-}" in *RUNPOD_SECRET*) echo "[start] WARNING: $key holds an unresolved {{ RUNPOD_SECRET_... }} reference";; esac
done
python - <<'EOF' 2>/dev/null || true
import torch
gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "no CUDA GPU visible (the detector runs on the CPU)"
print(f"[start] torch {torch.__version__}, CUDA {torch.version.cuda}, GPU: {gpu}")
EOF
echo "[start] data dir $REPAIROS_DATA_DIR, gateway on :$PORT"

supervise() {  # supervise <name> <command...>: run it, prefix its output, restart it on exit; TERM stops it
  local name=$1; shift
  local backoff=2 child=0 stopping=0
  trap 'stopping=1; [ "$child" -gt 0 ] && kill -TERM "$child" 2>/dev/null' TERM INT
  while [ "$stopping" -eq 0 ]; do
    local started=$SECONDS
    "$@" > >(sed -u "s/^/[$name] /") 2>&1 &
    child=$!
    wait "$child"; local code=$?
    # a trapped TERM interrupts wait; wait again so the child finishes its own shutdown (bounded by stop below)
    if [ "$stopping" -ne 0 ]; then wait "$child" 2>/dev/null; break; fi
    # a process that ran for a minute was healthy: restart fast; a crash loop backs off up to 30 s
    if [ $((SECONDS - started)) -gt 60 ]; then backoff=2; else backoff=$(( backoff < 30 ? backoff * 2 : 30 )); fi
    echo "[start] $name exited ($code); restarting in ${backoff}s"
    sleep "$backoff" & wait $!
  done
}

pids=()
alive() { local p; for p in "${pids[@]}"; do kill -0 "$p" 2>/dev/null && return 0; done; return 1; }
stop() {
  echo "[start] stopping"
  kill -TERM "${pids[@]}" 2>/dev/null
  # uvicorn and the worker exit within a few seconds of TERM. If not, exiting PID 1 ends the container anyway.
  for _ in $(seq 20); do alive || exit 0; sleep 0.5; done
  echo "[start] still running after 10 s; exiting"
  exit 0
}
trap stop TERM INT

# One uvicorn process: the gateway and the worker share one SQLite file, and the gateway is async.
supervise gateway python -m uvicorn repairos.main:app --host 0.0.0.0 --port "$PORT" \
  --proxy-headers --forwarded-allow-ips '*' --timeout-keep-alive 75 &
pids+=($!)
supervise worker python -m repairos.pipeline.worker --name "runpod-${RUNPOD_POD_ID:-local}" &
pids+=($!)

# wait returns when a trapped signal arrives (stop exits) or the supervisors end
while alive; do wait; done
