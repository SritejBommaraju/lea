#!/usr/bin/env bash
# Create the LEA pod through the Runpod REST API (the API-key route; the console route is in README.md, step 3), then
# wait until https://<pod_id>-8000.proxy.runpod.net/healthz answers.
#
#   RUNPOD_API_KEY=... deploy/runpod/create_pod.sh --image ghcr.io/<owner>/lea-backend:demo [options]
#
#   --image IMG              the pushed image (required)
#   --registry-auth ID       Runpod container registry credential id, for a private image (README step 2)
#   --gpus "A,B,..."         GPU type ids in priority order (default: the cheapest adequate first, see README)
#   --community              Community Cloud (cheaper, less reliable); default Secure Cloud
#   --cpu                    CPU pod instead (fallback: no GPU free; detector on the CPU, a still takes seconds longer)
#   --brightdata             also pass BRIGHTDATA_API_KEY / BRIGHTDATA_SERP_ZONE from Runpod secrets
#   --name NAME              pod name (default lea-demo)
#   --dry-run                print the request body and stop (it holds only secret REFERENCES, never values)
#
# App secrets never pass through this script: the pod env holds {{ RUNPOD_SECRET_<name> }} references that Runpod
# fills in at start (README step 1 creates the secrets). RUNPOD_API_KEY is read from the environment, never printed.
set -euo pipefail

IMAGE=""; AUTH_ID=""; CLOUD=SECURE; NAME=lea-demo; DRY=0; BRIGHT=0; CPU=0
GPUS="NVIDIA RTX A5000,NVIDIA GeForce RTX 3090,NVIDIA RTX A6000,NVIDIA A40,NVIDIA L4,NVIDIA GeForce RTX 4090"
while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE="$2"; shift 2;;
    --registry-auth) AUTH_ID="$2"; shift 2;;
    --gpus) GPUS="$2"; shift 2;;
    --community) CLOUD=COMMUNITY; shift;;
    --cpu) CPU=1; shift;;
    --brightdata) BRIGHT=1; shift;;
    --name) NAME="$2"; shift 2;;
    --dry-run) DRY=1; shift;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "unknown argument $1"; exit 2;;
  esac
done
[ -n "$IMAGE" ] || { echo "--image is required"; exit 2; }

BODY="$(IMAGE="$IMAGE" AUTH_ID="$AUTH_ID" CLOUD="$CLOUD" NAME="$NAME" GPUS="$GPUS" BRIGHT="$BRIGHT" CPU="$CPU" python3 - <<'EOF'
import json, os
ref = lambda name: "{{ RUNPOD_SECRET_%s }}" % name
env = {
    "ANTHROPIC_API_KEY": ref("anthropic_api_key"),
    "TYPESAFE_KEY": ref("typesafe_key"),
    "REPAIROS_PAIRING_CODE": ref("repairos_pairing_code"),
    "REPAIROS_DATA_DIR": "/workspace/lea-data",
}
if os.environ["BRIGHT"] == "1":
    env["BRIGHTDATA_API_KEY"] = ref("brightdata_api_key")
    env["BRIGHTDATA_SERP_ZONE"] = ref("brightdata_serp_zone")
body = {
    "name": os.environ["NAME"],
    "imageName": os.environ["IMAGE"],
    "computeType": "GPU",
    "gpuTypeIds": [g.strip() for g in os.environ["GPUS"].split(",") if g.strip()],
    "gpuTypePriority": "custom",
    "gpuCount": 1,
    "cloudType": os.environ["CLOUD"],
    "containerDiskInGb": 30,
    "volumeInGb": 20,
    "volumeMountPath": "/workspace",
    "ports": ["8000/http"],
    "env": env,
    # torch cu126 runs on any 12.x+ driver; hosts that report 12.6 or newer avoid old-driver surprises
    "allowedCudaVersions": ["12.6", "12.8", "12.9", "13.0"],
    "minRAMPerGPU": 16,
}
if os.environ["CPU"] == "1":   # same image; torch finds no GPU and the detector runs on the CPU
    for k in ("gpuTypeIds", "gpuTypePriority", "gpuCount", "allowedCudaVersions", "minRAMPerGPU"):
        body.pop(k)
    body.update(computeType="CPU", cpuFlavorIds=["cpu5c", "cpu3c"], cpuFlavorPriority="custom", vcpuCount=8)
    env["REPAIROS_TORCH_DEVICE"] = "cpu"
if os.environ["AUTH_ID"]:
    body["containerRegistryAuthId"] = os.environ["AUTH_ID"]
print(json.dumps(body, indent=1))
EOF
)"

if [ "$DRY" = 1 ]; then echo "$BODY"; exit 0; fi
[ -n "${RUNPOD_API_KEY:-}" ] || { echo "RUNPOD_API_KEY is not set"; exit 2; }
TMP="$(mktemp -d)"; chmod 700 "$TMP"; trap 'rm -rf "$TMP"' EXIT
( umask 077; printf 'Authorization: Bearer %s\n' "$RUNPOD_API_KEY" > "$TMP/auth.h" )

code="$(curl -sS -o "$TMP/pod.json" -w '%{http_code}' -X POST https://rest.runpod.io/v1/pods \
  -H "@$TMP/auth.h" -H 'Content-Type: application/json' --data-binary "$BODY")"
if [ "$code" != 201 ] && [ "$code" != 200 ]; then
  echo "create failed: HTTP $code"; head -c 1000 "$TMP/pod.json"; echo
  echo "(400 with 'no instances available': retry with --community or more --gpus; see README)"; exit 1
fi
POD="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['id'])" "$TMP/pod.json")"
URL="https://${POD}-8000.proxy.runpod.net"
echo "pod $POD created; gateway will be at $URL"
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); g=d.get('gpu') or {}; print('  hardware:', g.get('displayName') or (d.get('machine') or {}).get('gpuTypeId') or 'CPU', ' cost/hr (credits = USD):', d.get('costPerHr', '?'))" "$TMP/pod.json" || true

echo "waiting for the image pull and /healthz (first start pulls ~4.5 GB: a few minutes)"
for i in $(seq 1 120); do
  if curl -fsS -m 10 "$URL/healthz" >/dev/null 2>&1; then echo "UP after ~$((i * 10)) s: $URL"; exit 0; fi
  sleep 10
done
echo "not up after 20 min: check the pod's logs in the console (Pods > $NAME > Logs)"; exit 1
