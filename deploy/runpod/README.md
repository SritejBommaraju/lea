# LEA backend on a Runpod GPU Pod

This runs the gateway, the pipeline worker and (when `SPACETIME_DB` is set) the SpacetimeDB live mirror in one
container on Runpod, so the iPhone app and the big screen work without the laptop. It is one pod: one GPU, one volume
for `REPAIROS_DATA_DIR` (SQLite plus files), and ports 8000 (gateway) and 8790 (live-mirror media) behind Runpod's
HTTPS proxy. What runs where, end to end: [`deploy/MANIFEST.md`](../MANIFEST.md).

Two routes: **Route A** builds our own image (Steps 0-2 below). **Route B** needs no image build: an official Runpod
PyTorch pod runs `deploy/runpod/bootstrap.sh` from the repo (next section).

## Ports

| Port | Proxy URL | What | When |
|---|---|---|---|
| `8000/http` | `https://<pod_id>-8000.proxy.runpod.net` | the gateway (phone API, SSE live events, `/healthz`) | always |
| `8790/http` | `https://<pod_id>-8790.proxy.runpod.net` | the bridge's media: still thumbnails and the scan mesh at signed, expiring URLs (HMAC); a URL without a valid signature gets 403 | `SPACETIME_DB` set |

`create_pod.sh` exposes both. In the console form, put `8000,8790` in **Expose HTTP ports**. start.sh sets
`SPACETIME_MEDIA_BASE` from `RUNPOD_POD_ID` (Runpod sets it in every pod), so the viewer gets the proxy URLs.

| File | What |
|---|---|
| `deploy/runpod/Dockerfile` | the image. It holds pinned deps, the OWLv2 weights baked in, and the gateway plus the worker |
| `deploy/runpod/start.sh` | a tiny supervisor. It starts the gateway, the worker and (with `SPACETIME_DB`) the live-mirror bridge, restarts one that crashes, prefixes its logs and stops cleanly |
| `deploy/runpod/requirements.linux.lock` | hash-pinned Linux x86_64 deps (torch cu126 plus CUDA wheels, open3d). Regenerate it with `lock.sh` |
| `deploy/runpod/create_pod.sh` | creates the pod through the Runpod REST API (the API-key route) and waits for `/healthz` |
| `.dockerignore` (repo root) | an allowlist. Only the files the image copies are sent to the builder, so `.env`, `private/` and `data/` never are |
| `.github/workflows/build-runpod-image.yml` | a manual build and push of the image to GHCR. It never deploys |
| `deploy/smoke.sh` | the end-to-end smoke test with step latencies |
| `deploy/runpod/bootstrap.sh` | Route B (no image build): on an official Runpod PyTorch pod, clones the repo, installs the lock into a venv on the volume, caches the weights, then runs `start.sh` |

## Route B (no image build): official Runpod PyTorch pod plus `bootstrap.sh`

Use this route when no image can be built. That was the case on 2026-10-24: GitHub Actions refused the job over the
owner account's billing, and the Mac has about 10 GB of free disk. The pod runs Runpod's own PyTorch image. The pod
start command fetches `deploy/runpod/bootstrap.sh` from the repo, and that script puts everything on the volume:

1. the checkout in `/workspace/lea`;
2. the venv in `/workspace/venv`, installed from `requirements.linux.lock`;
3. the OWLv2 weights in `/workspace/hf`.

Then it execs `deploy/runpod/start.sh` from that checkout. A restart only checks those three things and starts.

**Image.** Official template **Runpod Pytorch 2.8.0**, id `runpod-torch-v280`.
- Pin the image tag **`runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404`**: Ubuntu 24.04, Python 3.12.3, CUDA 12.8.1,
  host CUDA 12.8 to 13.4. This is from the live official catalog (`GET /v2/catalog/templates?source=official`), read
  2026-10-24.
- The bootstrap does not use the image's torch 2.8. The venv gets the lock's torch `2.14.1+cu126`, which runs on
  those hosts, so the cloud runs the same versions as the Mac.
- No older official template has Python 3.12. The 2.4 template is 3.11 and the 2.2 template is 3.10.

**The read-only GitHub token** goes in a Runpod secret named `github_token_readonly`.
- Make it a **fine-grained PAT** with repository access only to `SritejBommaraju/lea` and **Contents: Read-only**.
- Only the repo owner (Sritej) can create one for this repo; a collaborator's fine-grained PAT cannot select another
  user's repo. A classic PAT with `repo` scope works too, but it is read-write on every repo of its owner, so avoid
  it.
- The token is used only for git and the loader's one API call. It is sent as a header, never put in a URL and
  never written to `.git/config`. A credential helper cannot store it, it is never printed, and it is unset before
  the app starts.

**Pod settings (console)**

1. Create the secrets as in Step 1, plus `github_token_readonly`.
2. Go to **Pods > Deploy**, choose **Secure Cloud** and **RTX A5000** ($0.27/hr), and pick the template **Runpod
   Pytorch 2.8.0**.
3. Click **Edit Template** and set the fields below.

| Field | Value |
|---|---|
| Container image | `runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404` |
| Container disk | **40 GB**: during an install, uv's cache (~8 GB) sits in `/tmp/lea-uv-cache` on this disk. It is deleted after the install |
| Volume disk | **50 GB** at **`/workspace`**: venv ~7 GB, weights 0.6 GB, data |
| Expose HTTP ports | **`8000,8790`**: 8000 = gateway; 8790 = live-mirror media, used when the checked-out `start.sh` runs the bridge (`SPACETIME_DB` set) |
| Expose TCP ports | `22` (optional: SSH with your account key, through the image's own start script) |
| Start Jupyter notebook | off (not needed) |
| Container Start Command | the loader below, on one line |

Environment variables:
```
GITHUB_TOKEN_READONLY  = {{ RUNPOD_SECRET_github_token_readonly }}
LEA_REF                = main            # or the branch / tag / commit sha to demo
ANTHROPIC_API_KEY      = {{ RUNPOD_SECRET_anthropic_api_key }}
TYPESAFE_KEY           = {{ RUNPOD_SECRET_typesafe_key }}
REPAIROS_PAIRING_CODE  = {{ RUNPOD_SECRET_repairos_pairing_code }}
REPAIROS_DATA_DIR      = /workspace/lea-data
# optional, as in Step 2: BRIGHTDATA_API_KEY, BRIGHTDATA_SERP_ZONE, and the live-mirror variables
# (SPACETIME_DB etc.) when the checked-out ref has the bridge
```

Container Start Command (the loader):
```
bash -c 'umask 077; printf "Authorization: Bearer %s\n" "$GITHUB_TOKEN_READONLY" > /tmp/.lea-gh.h; curl -fsS --retry 3 -H @/tmp/.lea-gh.h -H "Accept: application/vnd.github.raw+json" -o /workspace/.bootstrap.new "https://api.github.com/repos/SritejBommaraju/lea/contents/deploy/runpod/bootstrap.sh?ref=${LEA_REF:-main}" && mv /workspace/.bootstrap.new /workspace/bootstrap.sh || echo "[loader] fetch failed; using the copy on the volume"; rm -f /tmp/.lea-gh.h; exec bash /workspace/bootstrap.sh'
```
- It fetches `bootstrap.sh` at `LEA_REF` through the GitHub contents API. The token goes in a header file that only
  root can read and that is deleted right after the call.
- If GitHub is unreachable, it runs the copy saved on the volume from the last boot.
- `bootstrap.sh` must exist at `LEA_REF`. Merge this branch first, or point `LEA_REF` at a branch that has it.

**API route** (REST `POST https://rest.runpod.io/v1/pods`). Use the same fields as `create_pod.sh` builds, with
these changes:
- `"imageName": "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"`
- `"ports": ["8000/http", "8790/http", "22/tcp"]`
- `"volumeInGb": 50`
- `"allowedCudaVersions": ["12.8", "12.9", "13.0"]`
- in `env`: `"GITHUB_TOKEN_READONLY": "{{ RUNPOD_SECRET_github_token_readonly }}"` and `"LEA_REF": "main"`
- `"dockerStartCmd": ["bash", "-c", "<the loader's text between the single quotes>"]`

**Expected boot times**

| Boot | Time | What happens |
|---|---|---|
| First | **about 4-8 min** after the pod is scheduled | Runpod pulls its own PyTorch image, usually cached on its hosts. Then clone ~2 s; apt `libgl1 libgomp1` for open3d ~15 s; venv download ~3.8 GB plus install ~2-4 min; weights 620 MB ~0.5 min |
| Restart | **about 20-30 s** to a serving gateway | git fetch ~1 s, lock unchanged, weights cached, the apt step again (the container disk is wiped on every stop) |

The first job then loads the detector onto the GPU in a few seconds. Run `deploy/smoke.sh` once to do that.

Measured on the Mac on 2026-10-24, with no GPU and a temp dir as `/workspace`, using the Mac lock because the Linux
wheels cannot install there:

| Run | Time | Notes |
|---|---|---|
| First run | **54 s** | clone 1 s; venv 17 s from a warm uv cache (no torch download); OWLv2 download 36 s on home broadband |
| Second run | **1.3 s** | repo fetch, lock-hash skip, weights marker |
| Full boot | under 2 s to `exec start.sh`, then `/healthz` up | `deploy/smoke.sh` then **PASSED** end to end, 17.6 s from complete to report on the CPU |

Checked in the same tests:
- no secret appeared in the boot log;
- `GITHUB_TOKEN_READONLY` was absent from the gateway's environment;
- `.git/config` held only the plain `https://github.com/SritejBommaraju/lea.git`;
- with a bad token, the fetch failed and the boot continued on the existing checkout.

**Logs.** Pods > the pod > Logs. Lines are prefixed `[bootstrap]` (step timings), then `[start]`, `[gateway]`,
`[worker]` (and `[bridge]` when it runs). The image's own start script logs to `/workspace/.runpod-start.log`.

**Knobs** (pod env, all optional):
- `LEA_UPDATE=0` keeps the checkout instead of fetching `LEA_REF`. Use it to freeze the demo.
- `LEA_RUNPOD_SERVICES=0` skips the image's `/start.sh` (nginx on 8001/3001/…, SSH, Jupyter). It binds neither 8000
  nor 8790.
- `LEA_PREPARE_ONLY=1` installs everything and exits. Use it to warm a volume the night before.

**Why the uv cache is not on the volume.** The first real pod (`u8bcbzd1b1e2o0`, 2026-10-24) failed two boots. uv
could not rename a file from its cache temp dir into `archive-v0` on `/workspace`: `Cross-device link (os error
18)`, even though both paths were under `/workspace/.cache/uv`. The pod restarted the container itself and the
third boot worked.

`bootstrap.sh` now handles this itself:
- It keeps the cache on the container disk (`UV_CACHE_DIR=/tmp/lea-uv-cache`).
- It copies packages into the venv (`UV_LINK_MODE=copy`), because the cache and the venv are on different
  filesystems.
- If an install attempt fails, it retries once with an emptied cache, then falls back to pip. The pip fallback
  always rebuilds the venv empty first, for two reasons:
  - pip never removes packages that an older lock had and the new one dropped;
  - venvs made by the first version of this script have no pip.
- The lock marker is written only after an install succeeds. If every attempt fails, the script exits with an error
  and the next boot starts over.
- Both variables can be overridden in the pod env.
- To replay a scan inside the pod: `bash /workspace/lea/deploy/smoke.sh http://127.0.0.1:8000 --requeue <scan_id>`.
  It finds `/workspace/venv` by itself.

## Cost (runpod.io/pricing, read 2026-10-24)

| GPU (24 GB) | Secure Cloud | Community Cloud | Notes |
|---|---|---|---|
| **RTX A5000** | **$0.27/hr** | $0.16/hr | **Pick this.** It is the cheapest listed GPU pod. Ampere, 24 GB VRAM, 25 GB RAM, 9 vCPU |
| RTX 3090 | $0.50/hr | $0.22/hr | first fallback |
| RTX A6000 / A40 (48 GB) | $0.59/hr | $0.33 / $0.35/hr | fallback |
| L4 | $0.59/hr | $0.44/hr | fallback |
| RTX 4090 | $0.89/hr | $0.34/hr | fallback |

- OWLv2 base needs about 2 GB of VRAM. Any 16 GB+ card is plenty, so we pay only for availability and reliability.
- **Use Secure Cloud for the demo.** Community Cloud is cheaper, but hosts and IPs are less stable.
- Storage costs $0.10/GB-month for container and volume disk while the pod runs, and $0.20/GB-month for the volume
  while it is stopped. With 30 GB container plus 20 GB volume, that is about $0.007/hr.
- Billing is per second. Ingress and egress are free. On-demand deploy needs one hour of credit on the account.
- **Total: about $0.28/hr on an A5000.** A full day left running costs about $6.70. The Claude and Jev API calls are
  billed separately on those accounts.
- **Do not pick a Blackwell GPU** (RTX 5090, RTX Pro 6000 and its MIG slices, B200, B300). The image's torch is the
  CUDA 12.6 build, which has no kernels for them. If only those are free, see "Other GPUs" below.
- CPU pods (`cpu3c`, `cpu5c` flavours) are not priced on the public page; check the console. See "CPU fallback".

## Before you start: what we need from the owner

1. **Runpod access.** Either option works:
   - **An API key** (Runpod console > Settings > API Keys, read/write). It lets an agent run `create_pod.sh`. It
     cannot create the secrets (step 1), so someone still clicks those in, or the agent gets a teammate's login
     for 3 minutes.
   - **A teammate logged into the console.** They follow steps 1 to 3 by hand, about 10 minutes.
   - Either way, the account needs about $5 of credit.
2. **The secret values,** typed into Runpod by whoever holds them: `ANTHROPIC_API_KEY`, `TYPESAFE_KEY` and
   `REPAIROS_PAIRING_CODE`. Optionally also `BRIGHTDATA_API_KEY`, `BRIGHTDATA_SERP_ZONE`, `CARTESIA_API_KEY` and
   `SPACETIME_TOKEN` (the full list is in step 1).
   - Use a **new, long, random pairing code** for the cloud: `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`.
     The proxy URL is public, and the pairing code is the only gate on pairing a device.
3. **A built image in a registry** (step 0). This can happen tonight, before anyone has Runpod access.

## Step 0: build and push the image (no Runpod access needed)

The image is linux/amd64, about 4.5 GB compressed and about 8 GB unpacked. Pick one route.

**A. GitHub Actions (recommended).**
1. Merge `.github/workflows/build-runpod-image.yml` to `main`. GitHub only shows "Run workflow" for workflows that
   are on the default branch.
2. Run it:
   ```
   gh workflow run build-runpod-image.yml --repo SritejBommaraju/lea --ref rohan/demo-polish -f tag=demo
   gh run watch --repo SritejBommaraju/lea          # about 15-25 min
   ```
   You can also use the Actions tab > Runpod image > Run workflow, picking the branch to build.
3. The result is `ghcr.io/sritejbommaraju/lea-backend:demo`. It is a **private** package, because the repo is
   private.

**B. Any x86_64 machine with Docker and at least 25 GB of free disk, from the repo root:**
```
docker login ghcr.io -u <github-user>          # a PAT with write:packages (or docker login to Docker Hub)
docker buildx build --platform linux/amd64 -f deploy/runpod/Dockerfile -t ghcr.io/sritejbommaraju/lea-backend:demo --push .
```
An Apple Silicon Mac also builds it, under emulation, but it is slow and needs about 25 GB of free disk. The
owner's Mac had 12 GB free on 2026-10-24, so do not build there.

**Registry pull credential.** Runpod needs one for a private image:
- For GHCR, use a GitHub classic PAT with only `read:packages`, from someone who can read the package (Sritej as
  owner, or a collaborator the package is shared with).
- In the Runpod console, add it under **Settings > Container Registry Auth** (the docs call these "registry
  credentials"). Username = GitHub user, password = the PAT.
- Note its id for `create_pod.sh --registry-auth`, or pick it in the pod form.
- Making the package public would skip this step. Do that only if the owner OKs publishing the backend code.

## Step 1: Runpod secrets (console, about 3 minutes)

In the Runpod console, go to **Secrets > Create Secret**. Create one secret per name below. Runpod never shows a
value again after you save it.

| Runpod secret name | Becomes the pod env var | Required |
|---|---|---|
| `anthropic_api_key` | `ANTHROPIC_API_KEY` | yes. VLM and LLM slots (Claude) |
| `typesafe_key` | `TYPESAFE_KEY` | yes. FUSION=jev has no fallback, so Jev down is a blocker |
| `repairos_pairing_code` | `REPAIROS_PAIRING_CODE` | yes. The phone pairs with it. Use a new long random one |
| `brightdata_api_key` | `BRIGHTDATA_API_KEY` | optional. Reference hints and "Fix it" (parts and prices) stay off without it |
| `brightdata_serp_zone` | `BRIGHTDATA_SERP_ZONE` | optional. The SERP zone name (ours: `serp_api1`); needed together with the key |
| `cartesia_api_key` | `CARTESIA_API_KEY` | optional. Voice; without it the phone uses its own voice and speech recognition |
| `spacetime_token` | `SPACETIME_TOKEN` | with `SPACETIME_DB`. The bridge identity's token (the `token` field of the file `--mint-identity` wrote, e.g. `~/.config/lea/stdb-maincloud.json`); the module was published with that identity as `WRITER_IDENTITY` |

Plain env (not secrets), only for the live mirror:

| Env var | Value | Note |
|---|---|---|
| `SPACETIME_DB` | `lea-calhacks` | the Maincloud database; unset = no bridge process |
| `SPACETIME_HOST` | `https://maincloud.spacetimedb.com` | start.sh's default when `SPACETIME_DB` is set |
| `SPACETIME_MEDIA_PORT` | `8790` | default |
| `SPACETIME_MEDIA_BASE` | `https://<pod_id>-8790.proxy.runpod.net` | derived from `RUNPOD_POD_ID` when unset |

The bridge pairs itself with the gateway once (device `spacetime-bridge`, with `REPAIROS_PAIRING_CODE`) and keeps
the token in `/workspace/lea-data/live/`. start.sh warns by name about every unset optional key and every
unresolved `{{ RUNPOD_SECRET_... }}` reference, never a value.


The pod env then references each secret as `{{ RUNPOD_SECRET_<name> }}`. Example:
`ANTHROPIC_API_KEY={{ RUNPOD_SECRET_anthropic_api_key }}`. In the pod form, the key icon next to an env var picks a
secret.

Not needed on Runpod: `REPAIROS_RELEASE`. Leave `REPAIROS_RELEASE` unset, because the
phase1 profile uses `HOST=local_mac`, which a release build refuses.

### Why not GitHub repository secrets?

GitHub secrets exist only inside a GitHub Actions run. A Runpod pod cannot read them at runtime. There are two
ways to get secrets into the pod:

1. **Runpod secrets, set in the console (recommended).** It is three minutes of clicking. The values go straight
   from the key holder to Runpod, never through GitHub, CI logs or the image. Rotating a key means editing one
   secret and restarting the pod.
2. **A GitHub Actions workflow** that reads repo secrets and creates the pod through the Runpod API, passing them
   as plain pod env. This route has three problems:
   - It needs `RUNPOD_API_KEY` plus all the app keys as repo secrets. On a personal repo only the owner (Sritej)
     can add those.
   - The values then sit in the pod's env in clear text, where the console shows them.
   - The workflow becomes deploy code that must never echo them.

   It is more moving parts for no demo benefit.

So the workflow here only builds and pushes the image, which it does far better than a laptop. Secrets stay in
Runpod.

## Step 2: create the pod (about 5 minutes, most of it the image pull)

**Console route.**
1. Go to **Pods > Deploy**, choose **Secure Cloud**, and pick **RTX A5000** (1 GPU). If none is free, pick a
   fallback from the cost table. Optionally filter CUDA to 12.6 or newer.
2. Click **Edit Template** (or **Change Template > custom**) and set:
   - Container image: `ghcr.io/sritejbommaraju/lea-backend:demo`, plus the registry credential from step 0.
   - Container disk: **30 GB**.
   - Volume disk: **20 GB**, mount path **`/workspace`**.
   - Expose HTTP ports: **`8000,8790`**. Expose TCP ports: leave empty. No SSH or Jupyter is needed.
   - Environment variables, keys only, values from the secrets:
     ```
     ANTHROPIC_API_KEY      = {{ RUNPOD_SECRET_anthropic_api_key }}
     TYPESAFE_KEY           = {{ RUNPOD_SECRET_typesafe_key }}
     REPAIROS_PAIRING_CODE  = {{ RUNPOD_SECRET_repairos_pairing_code }}
     REPAIROS_DATA_DIR      = /workspace/lea-data
     # optional:
     BRIGHTDATA_API_KEY     = {{ RUNPOD_SECRET_brightdata_api_key }}
     BRIGHTDATA_SERP_ZONE   = {{ RUNPOD_SECRET_brightdata_serp_zone }}
     CARTESIA_API_KEY       = {{ RUNPOD_SECRET_cartesia_api_key }}
     # the big screen (live mirror):
     SPACETIME_DB           = lea-calhacks
     SPACETIME_HOST         = https://maincloud.spacetimedb.com
     SPACETIME_TOKEN        = {{ RUNPOD_SECRET_spacetime_token }}
     ```
   - Leave the start command empty. The image's CMD runs `start.sh`.
3. Click **Deploy On-Demand**, not spot or interruptible.
   - Optional: save the settings as a template (**Templates > New Template**, same fields) so a redeploy is one
     click.
4. Copy the **pod id** from the pod card. The gateway is at **`https://<pod_id>-8000.proxy.runpod.net`**.

**API route** (needs an API key and the secrets from step 1; no extra tools beyond curl and python3):
```
export RUNPOD_API_KEY=...            # never echoed by the script
deploy/runpod/create_pod.sh --image ghcr.io/sritejbommaraju/lea-backend:demo --registry-auth <registry-auth-id> \
  --all --spacetime lea-calhacks
# --all = --brightdata --cartesia; --spacetime DB adds the live mirror (SPACETIME_TOKEN from the secret)
# --dry-run prints the request body (secret references only); --community, --gpus "...", --cpu
```
The script prints the pod id and the proxy URL, then waits until `/healthz` answers. If the API rejects the secret
references (they are documented for templates and the pod form), save a template in the console with those env vars
once and use it from the console instead.

**Storage notes**
- Use the pod's **volume disk** (local to the host, at `/workspace`), not a network volume:
  - SQLite with gateway and worker writing at once wants a local disk.
  - A network volume can never be detached and is billed even with no pod.
- The volume survives **stop and restart**. It is **deleted on terminate**.
- A stopped pod can come back with no GPU, if its host's GPU was rented meanwhile. For the demo, **leave the pod
  running** ($0.28/hr) rather than stopping it overnight.
- The container disk (everything outside `/workspace`) is wiped on every stop.

## Step 3: smoke test (about 1 minute)

From the laptop:
```
REPAIROS_PAIRING_CODE=... deploy/smoke.sh https://<pod_id>-8000.proxy.runpod.net
#   or omit the variable and type the code at the hidden prompt
```
The script:
- calls `/healthz`;
- pairs a device named `smoke`;
- creates a session, uploads a small synthetic board still, posts a no-depth scan package and the still, and
  completes the scan;
- polls the pipeline until done, fetches the report, and prints each step's latency.

`PASS` means the gateway, the worker, the GPU detector, Claude and the report path all work.
- The first run includes loading the detector onto the GPU (seconds). Run it once before the demo to warm it up.
- Reference on the Mac (M-series, MPS): about 10 s from `complete` to the report.
- `--image real.jpg` sends a real photo. It exercises detector boxes and Claude crop calls more.
- To replay a real recorded scan through the live stack with `tools/requeue_scan.py`, run this **inside the pod**,
  because the tool reads STATE from the local data dir (Pod > Connect > Web Terminal):
  ```
  REPAIROS_PAIRING_CODE=... bash /app/deploy/smoke.sh http://127.0.0.1:8000 --requeue <scan_id>
  ```
  This needs a scan in the pod's STATE: any phone scan made against the pod, or a laptop `data/` copied to
  `/workspace/lea-data`.

Logs are under Pods > the pod > **Logs**. Lines are prefixed `[start]`, `[gateway]`, `[worker]` and `[bridge]`.
- `[start]` reports the GPU torch sees.
- It warns, by name only, about any key that is unset or an unresolved `RUNPOD_SECRET` reference.

## Step 4: point the phone at the pod

The app needs the full URL, **with `https://`**. The app adds `http://` to an address typed without a scheme, and
plain HTTP fails App Transport Security.

- **In the app:** the Settings pairing screen ("Connect to your Mac", `ios/RepairOS/App/SettingsView.swift`).
  1. Set **Address** to `https://<pod_id>-8000.proxy.runpod.net`.
  2. Set **Pairing code** to the Runpod one, then tap **Pair**.
  3. **Check connection** should say Online.
  4. Re-pair after switching backends: the cloud's STATE is new, so the Mac's token is unknown there (403).
- **From the Mac over USB** (`ios/README.md`):
  ```
  xcrun devicectl device process launch --device <udid> com.rohankilaru.repairos \
    --gateway https://<pod_id>-8000.proxy.runpod.net --pairing-code <code>
  ```
- **ATS:** no change is needed. The proxy serves TLS 1.3 with a valid Google Trust Services certificate.
  `nscurl --ats-diagnostics https://<id>-8000.proxy.runpod.net` passes the default ATS policy. `Info.plist` only adds
  `NSAllowsLocalNetworking` for the Mac LAN case, which stays as is.

## Time budget once access arrives (target under 15 minutes)

| Step | Time |
|---|---|
| 1. Secrets in the console | 3 min |
| 0b. Registry credential in the console (if not done) | 1 min |
| 2. Deploy pod (form or `create_pod.sh`) | 2 min, plus 3-5 min image pull |
| 3. Smoke test | 1 min |
| 4. Phone: address + pair + check | 1 min |
| **Total** | **about 11-13 min** |

This assumes step 0 (the image build) was done beforehand.

## Proxy limits that matter

- **Each request has 100 s to answer** (Cloudflare, otherwise HTTP 524). Every gateway call answers quickly:
  - `POST .../complete` returns 202 at once, and the pipeline runs in the worker.
  - Blob uploads are 4 MB chunks.
- **Live events (SSE)** are one long response. The gateway writes a keepalive every 15 s, and the app reconnects
  with `Last-Event-ID` (`ios/RepairOS/Upload/LiveEvents.swift`). If Cloudflare still cuts a stream, the app resumes
  without gaps.
- The URL is public. Anyone with the pairing code can pair, so keep the code long and stop the pod after the demo.

## Spend guards

Every paid call is checked by `backend/repairos/spend.py` before it is made: Claude (crop checks, identity, hints,
summaries, voice answers), Jev, Bright Data, Cartesia and the SpacetimeDB live mirror. Runpod pod hours are not in
code: stop the pod after the demo.
- **Caps** are in `config/pipeline.yaml` under `spend:`, per service: `per_minute` (last 60 s), `per_day` and
  `usd_per_day` (last 24 h), `repeat_per_minute` (the same request again and again) and `breaker_cooldown_s`. The
  defaults and the reasoning for each are in comments there (Claude: 400 a minute, 4,000 a day, about 25 USD a day).
- **Breaker.** A cap that trips pauses that service for its cooldown: every call is refused at once. The scan goes
  on: regions not yet checked are `not_inspected` with "paused: usage limit reached", the report still builds and
  says so, and hints, voice and the live mirror are skipped until the pause ends.
- **Alerts.** A trip, or a service passing 80 % of a daily cap, logs one `SPEND ALERT` line at ERROR, and the phone
  shows a banner (live event `spend_alert`, on the session streams and `GET /v1/events`). Watch for it with
  `grep "SPEND ALERT"` on the pod log.
- **Status.** `GET /v1/usage` (a paired device token) returns per-service counts, estimated USD and breakers; the
  app shows it in Settings > Usage. On the pod: `python -m repairos.spend status`, and
  `python -m repairos.spend reset <service>` reopens a paused service once the cause is fixed.
- **Counters** are one SQLite file shared by the gateway, the worker and the bridge, so a restart does not reset
  them: `$REPAIROS_DATA_DIR/spend.sqlite3` (`/workspace/lea-data` on the pod, on the volume).

| Variable | Default | Meaning |
|---|---|---|
| `REPAIROS_SPEND_DB` | `$REPAIROS_DATA_DIR/spend.sqlite3` | where the counters live (every process must use the same file) |
| `REPAIROS_PIPELINE_CONFIG` | `config/pipeline.yaml` | the file the caps are read from (`spend:` section) |

## CPU fallback

The same image runs on a CPU pod. torch sees no GPU and the detector runs on the CPU, so each still costs seconds
more and a 10-still scan is noticeably slower.
- API route: `create_pod.sh --cpu`. It uses flavours `cpu5c`/`cpu3c` with 8 vCPU, and sets
  `REPAIROS_TORCH_DEVICE=cpu`.
- Console route: pick **CPU** as the compute type. Runpod templates are tied to a compute type, so a saved GPU
  template does not apply; enter the same fields again.

## Other GPUs (Blackwell)

The image uses torch `2.14.1+cu126`. It works on any host driver from CUDA 12.x and on Ampere, Ada and Hopper
GPUs. For Blackwell cards:
1. Run `TORCH_CUDA=cu130 deploy/runpod/lock.sh`.
2. Change the `--extra-index-url` in the Dockerfile to `.../whl/cu130`.
3. Rebuild, and pick a host with CUDA 13.0 (driver 580 or newer).

## Validation done (2026-10-24)

- **Not built locally.** The Docker daemon was off, and the Mac has 12 GB of free disk against about 25 GB needed.
  hadolint is not installed. The Dockerfile was checked by a reasoning pass instead:
  - multi-stage;
  - hash-pinned pip install;
  - the base image pinned by digest;
  - weights cached from the slot's own `MODEL_REVISION`, with `HF_HUB_OFFLINE=1` at runtime;
  - repo layout matching `repairos.config.ROOT`.
- **Lock unchanged by the 2026-10-24 Fix it / bridge work:** both use only the standard library (and Pillow, already
  locked), so no dependency was added.
- **Lock resolved.** `requirements.linux.lock` resolves 114 packages for x86_64 manylinux_2_35, with the same
  versions as `backend/requirements.lock`.
- **Layout and supervisor tested on the Mac** with the image's file layout:
  - Only `backend/repairos`, `config`, `spec/contracts`, `samples/session.schema.json` and
    `tools/requeue_scan.py` were present, and there was no `.env`.
  - `start.sh` ran the gateway and worker; `smoke.sh` passed end to end, including `--requeue`; SIGTERM stopped
    both cleanly.
  - No secret value appeared in any log.
- **Not tested until the first real build:**
  - that `pip install` succeeds on Linux;
  - that open3d imports with only `libgomp1` and `libgl1` installed;
  - that Runpod resolves `{{ RUNPOD_SECRET_* }}` in API-created pods.

## Licences

Everything added is MIT, Apache or BSD:
- the base image (`python:3.12-slim`) is PSF plus Debian;
- the GitHub Actions are MIT and Apache-2.0;
- `cuda-bindings` and `cuda-pathfinder` are Apache-2.0;
- triton is MIT.

These need a decision:
- **The NVIDIA CUDA runtime wheels** (`nvidia-*-cu12`, `cuda-toolkit`) are under NVIDIA's proprietary,
  redistributable licence. Any GPU build of torch requires them, so they cannot be avoided for GPU inference.
  - They live only in the private image and are not added to the repo.
  - Invariant L2 needs an owner ruling on them (B15).
- **`certifi` (MPL-2.0)** comes in with open3d's dependency tree. It was already flagged in `pyproject.toml`.
