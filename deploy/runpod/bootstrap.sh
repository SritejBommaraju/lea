#!/usr/bin/env bash
# Route B (no image build): run the LEA backend on an OFFICIAL Runpod PyTorch pod. Everything lives on the pod's
# volume (/workspace), so the first boot installs and later boots only check and start.
#
# The pod's start command (README, "Route B") fetches this file from GitHub with the read-only token and runs it:
#   1. repo     clone $LEA_REPO at $LEA_REF into $LEA_CHECKOUT, or update the checkout already there
#   2. venv     create or reuse $LEA_VENV and install deploy/runpod/requirements.linux.lock (hash-checked); skipped
#               when the lock's sha256 matches the one recorded after the last successful install
#   3. weights  cache the detector weights (the revision pinned in repairos.slots.det_owlv2) in $HF_HOME, once
#   4. splat    LEA_SPLAT=1 only, IN THE BACKGROUND: install gsplat (requirements.splat.lock, hash-checked) into the
#               venv and compile its CUDA kernels once with the image's nvcc into $TORCH_EXTENSIONS_DIR on the volume;
#               the worker trains splats only after the ready marker ($LEA_GSPLAT_READY) exists, the mesh until then
#   5. start    exec deploy/runpod/start.sh from the checkout (gateway + worker, and the live bridge when configured)
#
# Secrets: GITHUB_TOKEN_READONLY (a Runpod secret) is used only for git and is never printed, never written to
# .git/config and never put in a URL: git gets it as an http.extraheader through GIT_CONFIG_* environment variables,
# and the variable is unset before anything else starts. The app's keys pass through untouched to start.sh.
#
# Knobs (all optional): LEA_REPO (SritejBommaraju/lea), LEA_REF (main; a branch, tag or commit sha), LEA_HOME
# (/workspace), LEA_UPDATE=0 (keep the checkout as it is), LEA_RUNPOD_SERVICES=0 (skip the image's own /start.sh:
# nginx, SSH with your account key, Jupyter only with JUPYTER_PASSWORD), LEA_PREPARE_ONLY=1 (stop after step 3),
# LEA_LOCK (lock path inside the checkout), TORCH_INDEX (PyTorch wheel index for the lock's +cu126 torch),
# LEA_SPLAT=1 (the photo-real splat tier: step 5 below, in the background; also sets LEA_RECREATION_ENGINE=splat
# unless that is set), LEA_SPLAT_JOBS (compile jobs, default 4).
set -euo pipefail

: "${LEA_REPO:=SritejBommaraju/lea}"
: "${LEA_REF:=main}"
: "${LEA_HOME:=/workspace}"
: "${LEA_CHECKOUT:=$LEA_HOME/lea}"
: "${LEA_VENV:=$LEA_HOME/venv}"
: "${LEA_LOCK:=deploy/runpod/requirements.linux.lock}"
: "${TORCH_INDEX:=https://download.pytorch.org/whl/cu126}"
: "${LEA_UPDATE:=1}"
: "${LEA_RUNPOD_SERVICES:=1}"
: "${LEA_PREPARE_ONLY:=0}"
: "${HF_HOME:=$LEA_HOME/hf}"
# The uv cache lives on the container disk, not the volume: on Runpod's /workspace, uv's rename from its cache temp
# dir into archive-v0 failed with "Cross-device link (os error 18)" (pod u8bcbzd1b1e2o0, 2026-10-24) even though both
# paths were under /workspace/.cache/uv. /tmp is one plain filesystem. Cache and venv are then on different
# filesystems, so packages are copied into the venv (UV_LINK_MODE=copy), and the cache is deleted after a good
# install (the container disk is wiped on every stop anyway; the venv on the volume is what persists).
: "${UV_CACHE_DIR:=/tmp/lea-uv-cache}"
: "${UV_LINK_MODE:=copy}"
: "${REPAIROS_DATA_DIR:=$LEA_HOME/lea-data}"
export HF_HOME UV_CACHE_DIR UV_LINK_MODE REPAIROS_DATA_DIR
export GIT_TERMINAL_PROMPT=0 PIP_DISABLE_PIP_VERSION_CHECK=1

T_ALL=$SECONDS
log() { echo "[bootstrap] $*"; }
took() { log "$1 done in $(( SECONDS - $2 )) s"; }
mkdir -p "$LEA_HOME" "$HF_HOME" "$UV_CACHE_DIR" "$REPAIROS_DATA_DIR"

# ------------------------------------------------------------------ 1. repo
git_auth_on() {   # the token as an Authorization header for github.com only, through the environment
  [ -n "${GITHUB_TOKEN_READONLY:-}" ] || return 0
  local basic
  basic="$(printf 'x-access-token:%s' "$GITHUB_TOKEN_READONLY" | base64 | tr -d '\n')"
  export GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0="http.https://github.com/.extraheader"
  export GIT_CONFIG_VALUE_0="Authorization: Basic $basic"
  export GIT_CONFIG_KEY_1="credential.helper" GIT_CONFIG_VALUE_1=""   # no helper may store or replace it
}
git_auth_off() { unset GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0 GIT_CONFIG_KEY_1 GIT_CONFIG_VALUE_1; }

T=$SECONDS
URL="https://github.com/$LEA_REPO.git"
if [ -d "$LEA_CHECKOUT/.git" ]; then
  git -C "$LEA_CHECKOUT" remote set-url origin "$URL"   # a plain URL, whatever was there before
  if [ "$LEA_UPDATE" = 1 ]; then
    git_auth_on
    if git -C "$LEA_CHECKOUT" fetch --quiet --depth 1 origin "$LEA_REF" 2>"$LEA_HOME/.git-fetch.err"; then
      git -C "$LEA_CHECKOUT" checkout --quiet --force --detach FETCH_HEAD
      log "repo: $LEA_REPO@$LEA_REF updated to $(git -C "$LEA_CHECKOUT" rev-parse --short HEAD)"
    else
      # an outage or an expired token must not keep the demo down: the checkout on the volume still runs
      log "WARNING: fetching $LEA_REF failed ($(head -c 200 "$LEA_HOME/.git-fetch.err" | tr '\n' ' ')); running the existing checkout $(git -C "$LEA_CHECKOUT" rev-parse --short HEAD)"
    fi
    git_auth_off
  else
    log "repo: LEA_UPDATE=0, keeping $(git -C "$LEA_CHECKOUT" rev-parse --short HEAD)"
  fi
else
  [ -n "${GITHUB_TOKEN_READONLY:-}" ] || log "WARNING: GITHUB_TOKEN_READONLY is not set; cloning without a token (works only if the repo is public)"
  rm -rf "$LEA_CHECKOUT.partial"
  git init --quiet "$LEA_CHECKOUT.partial"
  git -C "$LEA_CHECKOUT.partial" remote add origin "$URL"
  git_auth_on
  git -C "$LEA_CHECKOUT.partial" fetch --quiet --depth 1 origin "$LEA_REF" \
    || { git_auth_off; log "ERROR: cannot fetch $LEA_REPO@$LEA_REF (token scope, repo name or ref)"; exit 1; }
  git_auth_off
  git -C "$LEA_CHECKOUT.partial" checkout --quiet --detach FETCH_HEAD
  mv "$LEA_CHECKOUT.partial" "$LEA_CHECKOUT"
  log "repo: cloned $LEA_REPO@$LEA_REF at $(git -C "$LEA_CHECKOUT" rev-parse --short HEAD)"
fi
rm -f "$LEA_HOME/.git-fetch.err"
if git -C "$LEA_CHECKOUT" config --get-regexp '^(remote\..*\.url|http\..*extraheader)' | grep -qiE '@github|authorization'; then
  log "ERROR: a credential is stored in $LEA_CHECKOUT/.git/config; refusing to start"; exit 1
fi
took "repo" "$T"

# ------------------------------------------------------------------ 2. system libraries open3d loads (container disk)
# open3d (the scan mesh, the textured recreation) loads libGL, libEGL (its headless rendering module; the pod's
# recreation failed with "libEGL.so.1: cannot open shared object file", 2026-10-24) and libgomp at import. Each library
# missing from the loader cache is installed once per container start (the container disk is fresh on every start);
# nothing runs when they are all there. After the venv, any library open3d still cannot resolve is logged (step 3).
T=$SECONDS
want_libs=("libGL.so.1:libgl1" "libEGL.so.1:libegl1" "libgomp.so.1:libgomp1")
missing_pkgs=()
if command -v ldconfig >/dev/null; then
  cache="$(ldconfig -p)"
  for pair in "${want_libs[@]}"; do
    grep -q "${pair%%:*}" <<<"$cache" || missing_pkgs+=("${pair##*:}")
  done
fi
if [ "${#missing_pkgs[@]}" -gt 0 ]; then
  if [ "$(id -u)" = 0 ] && command -v apt-get >/dev/null; then
    log "installing ${missing_pkgs[*]} for open3d"
    DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null \
      && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing_pkgs[@]}" >/dev/null \
      || log "WARNING: apt-get failed; the 3D scan mesh and the textured recreation will report 'failed' until ${missing_pkgs[*]} are present"
  else
    log "WARNING: ${missing_pkgs[*]} missing and no root apt-get; the 3D scan mesh and the textured recreation will be unavailable"
  fi
fi
took "system libraries" "$T"

# ------------------------------------------------------------------ 3. venv + locked deps
T=$SECONDS
LOCK="$LEA_CHECKOUT/$LEA_LOCK"
[ -f "$LOCK" ] || { log "ERROR: no lock file at $LOCK"; exit 1; }
sha() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
WANT="$(sha "$LOCK")"
HAVE="$(cat "$LEA_VENV/.lea-lock-sha256" 2>/dev/null || true)"
# Python 3.12, as the lock was compiled for: the image's /usr/bin/python3.12, else uv's own 3.12, else python3
PY_BASE="${LEA_PYTHON:-$(command -v python3.12 || { command -v uv >/dev/null && echo 3.12; } || command -v python3)}"
if [ "$WANT" = "$HAVE" ] && [ -x "$LEA_VENV/bin/python" ]; then
  log "venv: lock unchanged (${WANT:0:12}); skipping install"
else
  log "venv: installing $LEA_LOCK (${WANT:0:12}) into $LEA_VENV with Python $PY_BASE; first boot downloads ~3.8 GB"
  rm -f "$LEA_VENV/.lea-lock-sha256"
  hashes=(); grep -q -- '--hash=sha256:' "$LOCK" && hashes=(--require-hashes)
  index=(); [ -n "$TORCH_INDEX" ] && index=(--extra-index-url "$TORCH_INDEX")
  pip_install() { mkdir -p "$UV_CACHE_DIR"; "$LEA_VENV/bin/python" -m pip install --quiet --no-deps \
                    --cache-dir "$UV_CACHE_DIR/pip" ${hashes[@]+"${hashes[@]}"} ${index[@]+"${index[@]}"} -r "$LOCK"; }
  # The pip route always starts from an EMPTY venv: pip install never removes a package that an older lock had and
  # this one dropped (uv pip sync does), and a venv made before --seed has no pip at all. A fresh venv from this
  # lock alone is exactly the lock, so the marker below stays true. Tries python -m venv, then uv venv --seed, then
  # ensurepip, and checks that pip runs.
  fresh_pip_venv() {
    local py="$PY_BASE"
    [ -x "$py" ] || py="$(uv python find "$PY_BASE" 2>/dev/null || command -v python3)"
    log "venv: rebuilding $LEA_VENV empty for the pip install (with $py)"
    rm -rf "$LEA_VENV"
    if "$py" -m venv "$LEA_VENV" >/dev/null 2>&1 && "$LEA_VENV/bin/python" -m pip --version >/dev/null 2>&1; then return 0; fi
    rm -rf "$LEA_VENV"
    if command -v uv >/dev/null && uv venv --quiet --seed --python "$py" "$LEA_VENV" \
       && "$LEA_VENV/bin/python" -m pip --version >/dev/null 2>&1; then return 0; fi
    if [ -x "$LEA_VENV/bin/python" ] && "$LEA_VENV/bin/python" -m ensurepip >/dev/null 2>&1 \
       && "$LEA_VENV/bin/python" -m pip --version >/dev/null 2>&1; then return 0; fi
    log "WARNING: could not make a venv with pip from $py"; return 1
  }
  ok=0
  if command -v uv >/dev/null; then
    [ -x "$LEA_VENV/bin/python" ] || uv venv --quiet --seed --python "$PY_BASE" "$LEA_VENV"
    log "venv: $("$LEA_VENV/bin/python" -V), uv cache $UV_CACHE_DIR (link mode $UV_LINK_MODE)"
    # sync = exactly the lock: packages a previous lock had and this one does not are removed. Two tries, the second
    # with an empty cache (a half-written cache entry from a failed try is not reused), then pip.
    for attempt in 1 2; do
      mkdir -p "$UV_CACHE_DIR"
      if uv pip sync --quiet --python "$LEA_VENV/bin/python" ${hashes[@]+"${hashes[@]}"} ${index[@]+"${index[@]}"} \
           --index-strategy unsafe-best-match "$LOCK"; then ok=1; break; fi
      log "WARNING: uv install attempt $attempt failed; clearing $UV_CACHE_DIR and retrying"
      rm -rf "$UV_CACHE_DIR"
    done
    if [ "$ok" = 0 ]; then
      log "WARNING: uv failed twice; installing with pip into a fresh venv instead"
      fresh_pip_venv && pip_install && ok=1
    fi
  else
    # no uv: pip into a fresh venv (exactly the lock), one retry with an emptied cache; a retry into the same venv
    # cannot leave extras, it only holds packages of this lock
    if fresh_pip_venv; then
      pip_install || { log "WARNING: pip install failed; retrying once"; rm -rf "$UV_CACHE_DIR"; pip_install; } && ok=1
    fi
  fi
  rm -rf "$UV_CACHE_DIR"   # ~4-11 GB of wheels on the container disk; the venv on the volume is what persists
  [ "$ok" = 1 ] || { log "ERROR: installing $LEA_LOCK failed (see above); the pod will restart the container"; exit 1; }
  echo "$WANT" > "$LEA_VENV/.lea-lock-sha256"   # written last: an interrupted install is redone on the next boot
fi
took "venv" "$T"
# open3d's native modules: name any shared library the loader still cannot find (the mesh would fail on it)
O3D_MISSING="$("$LEA_VENV/bin/python" -c '
import glob, importlib.util, os, subprocess
spec = importlib.util.find_spec("open3d")
libs = set()
if spec and spec.origin:
    for so in glob.glob(os.path.join(os.path.dirname(spec.origin), "**", "*.so*"), recursive=True):
        out = subprocess.run(["ldd", so], capture_output=True, text=True).stdout
        libs |= {line.split()[0] for line in out.splitlines() if "not found" in line}
print(" ".join(sorted(libs)))
' 2>/dev/null || true)"
[ -z "$O3D_MISSING" ] || log "WARNING: open3d cannot find $O3D_MISSING; the 3D scan mesh and the textured recreation will fail (add its package to step 2)"

export PATH="$LEA_VENV/bin:$PATH" PYTHONPATH="$LEA_CHECKOUT/backend" PYTHONUNBUFFERED=1 HF_HUB_DISABLE_TELEMETRY=1

# ------------------------------------------------------------------ 4. detector weights
T=$SECONDS
WEIGHTS="$(python -c 'from repairos.slots.det_owlv2 import MODEL_REPO as r, MODEL_REVISION as v; print(f"{r}@{v}")')"
MARK="$HF_HOME/.lea-weights-$(printf '%s' "$WEIGHTS" | tr '/@' '__')"
if [ -f "$MARK" ]; then
  log "weights: $WEIGHTS cached"
else
  log "weights: downloading $WEIGHTS (~620 MB) to $HF_HOME"
  HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 python - <<'EOF'
from repairos.slots.det_owlv2 import MODEL_REPO, MODEL_REVISION
from transformers import AutoProcessor, Owlv2ForObjectDetection
AutoProcessor.from_pretrained(MODEL_REPO, revision=MODEL_REVISION)
Owlv2ForObjectDetection.from_pretrained(MODEL_REPO, revision=MODEL_REVISION)
EOF
  touch "$MARK"
fi
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1   # from here on nothing fetches weights
took "weights" "$T"

# ------------------------------------------------------------------ 4b. splat tier (background, LEA_SPLAT=1)
# gsplat (nerfstudio-project/gsplat 1.5.3, Apache-2.0) has no prebuilt wheel for torch 2.14.1+cu126 / Python 3.12
# (https://docs.gsplat.studio/whl/gsplat/ stops at torch 2.4 + cu124, cp310; read 2026-10-24), so its plain wheel is
# installed and its kernels JIT-compiled once (gsplat/cuda/_backend.py, torch.utils.cpp_extension) with the image's
# nvcc (runpod/pytorch is built FROM nvidia/cuda:*-cudnn-devel, so /usr/local/cuda/bin/nvcc is there). The build
# lands on the volume; a restart only checks the marker. It never blocks the gateway: until the marker exists the
# worker keeps the textured mesh as the recreation.
: "${LEA_SPLAT:=0}"
: "${TORCH_EXTENSIONS_DIR:=$LEA_HOME/torch_extensions}"
: "${LEA_GSPLAT_READY:=$TORCH_EXTENSIONS_DIR/.lea-gsplat-ready}"
export TORCH_EXTENSIONS_DIR LEA_GSPLAT_READY
splat_setup() {
  set +e
  local t=$SECONDS lock="$LEA_CHECKOUT/deploy/runpod/requirements.splat.lock" py="$LEA_VENV/bin/python" want have
  local out="$LEA_HOME/.splat-setup.log"
  say() { echo "[splat-setup] $*"; }
  fail() { say "FAILED: $* (the recreation stays the textured mesh; full log $out)"; rm -f "$LEA_GSPLAT_READY"; exit 0; }
  [ -f "$lock" ] || fail "no $lock in this checkout"
  : "${CUDA_HOME:=/usr/local/cuda}"; export CUDA_HOME PATH="$CUDA_HOME/bin:$PATH"
  command -v nvcc >/dev/null || fail "no nvcc (CUDA_HOME=$CUDA_HOME); this image has no CUDA toolkit"
  "$py" -c 'import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)' || fail "torch sees no CUDA GPU"
  want="$(sha "$lock") $("$py" -c 'import torch;print(torch.__version__, "sm%d%d" % torch.cuda.get_device_capability())') $(nvcc --version | grep -o 'release [0-9.]*')"
  have="$(cat "$LEA_GSPLAT_READY" 2>/dev/null || true)"
  if [ "$want" = "$have" ] && "$py" -c 'import gsplat' 2>/dev/null; then say "READY (cached: $want)"; exit 0; fi
  rm -f "$LEA_GSPLAT_READY"   # from here until the end the worker treats the splat as not available
  say "installing gsplat into $LEA_VENV ($want)"
  if command -v uv >/dev/null; then
    uv pip install --quiet --python "$py" --no-deps --require-hashes -r "$lock" >>"$out" 2>&1 \
      || "$py" -m pip install --quiet --no-deps --require-hashes -r "$lock" >>"$out" 2>&1 || fail "installing $lock"
  else
    "$py" -m pip install --quiet --no-deps --require-hashes -r "$lock" >>"$out" 2>&1 || fail "installing $lock"
  fi
  rm -rf "$UV_CACHE_DIR"
  say "compiling gsplat's CUDA kernels with $(nvcc --version | grep -o 'release [0-9.]*') (MAX_JOBS=${LEA_SPLAT_JOBS:-4}, low priority; ~5-15 min the first time)"
  local tc=$SECONDS
  MAX_JOBS="${LEA_SPLAT_JOBS:-4}" nice -n 19 "$py" - >>"$out" 2>&1 <<'PY' || fail "compiling or running the kernels (tail: $(tail -c 400 "$out" | tr '\n' ' '))"
import torch
from gsplat.cuda._backend import _C
assert _C is not None, "gsplat could not load its CUDA extension"
from gsplat import rasterization
d = torch.device("cuda")
n = 2000
means = torch.randn(n, 3, device=d) * 0.1 + torch.tensor([0.0, 0.0, 1.0], device=d)
quats = torch.nn.functional.normalize(torch.randn(n, 4, device=d), dim=-1)
scales = torch.full((n, 3), 0.01, device=d)
opac = torch.full((n,), 0.5, device=d)
cols = torch.rand(n, 1, 3, device=d)
k = torch.tensor([[[300.0, 0, 160], [0, 300.0, 120], [0, 0, 1]]], device=d)
img, alpha, _ = rasterization(means, quats, scales, opac, cols, torch.eye(4, device=d)[None], k, 320, 240, sh_degree=0)
assert img.shape == (1, 240, 320, 3) and float(alpha.max()) > 0, "the smoke render drew nothing"
print("gsplat smoke render ok", float(alpha.mean()))
PY
  echo "$want" > "$LEA_GSPLAT_READY"
  say "READY: gsplat installed and compiled in $(( SECONDS - tc )) s (setup $(( SECONDS - t )) s); splats train from now on"
}
if [ "$LEA_SPLAT" = 1 ]; then
  : "${LEA_RECREATION_ENGINE:=splat}"; export LEA_RECREATION_ENGINE
  mkdir -p "$TORCH_EXTENSIONS_DIR"
  log "splat: LEA_SPLAT=1, engine $LEA_RECREATION_ENGINE; gsplat setup runs in the background ([splat-setup] lines)"
  ( splat_setup ) &
else
  log "splat: off (LEA_SPLAT=$LEA_SPLAT); the recreation is the textured mesh"
fi

log "ready in $(( SECONDS - T_ALL )) s (repo $(git -C "$LEA_CHECKOUT" rev-parse --short HEAD), venv ${WANT:0:12}, data $REPAIROS_DATA_DIR)"
unset GITHUB_TOKEN_READONLY
[ "$LEA_PREPARE_ONLY" = 1 ] && { log "LEA_PREPARE_ONLY=1: not starting"; exit 0; }

# ------------------------------------------------------------------ 5. start (after the background splat setup)
# The official image's own start script (nginx on its side ports, SSH with the account key, Jupyter only when
# JUPYTER_PASSWORD is set) in the background, so SSH and the web terminal keep working. It binds none of 8000/8790.
if [ "$LEA_RUNPOD_SERVICES" = 1 ] && [ -x /start.sh ]; then
  log "starting the image's /start.sh in the background (SSH, web terminal)"
  /start.sh > "$LEA_HOME/.runpod-start.log" 2>&1 &
fi
export APP_DIR="$LEA_CHECKOUT"
exec bash "$LEA_CHECKOUT/deploy/runpod/start.sh"
