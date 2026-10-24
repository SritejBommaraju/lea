# syntax=docker/dockerfile:1.7
# LEA backend (gateway + pipeline worker) for a Runpod GPU Pod. Build from the REPO ROOT (the context needs
# backend/, config/, spec/contracts/, samples/session.schema.json, tools/requeue_scan.py; .dockerignore lets in only
# those and this folder):
#
#   docker buildx build --platform linux/amd64 -f deploy/runpod/Dockerfile -t <registry>/lea-backend:<tag> --push .
#
# Image size: about 8 GB unpacked (the locked wheels download as 3.8 GB: torch 0.9, cuDNN 0.7, open3d 0.4, cuBLAS 0.4;
# plus OWLv2 weights 0.6 GB), about 4.5 GB compressed in the registry. Set the pod's container disk to 30 GB.
#
# CUDA: torch 2.14.1+cu126 and its pinned NVIDIA runtime wheels (deploy/runpod/requirements.linux.lock) carry the
# whole CUDA 12.6 user space, so the base stays a plain Python image; the host driver comes from Runpod's NVIDIA
# container runtime (any driver >= 525; Ampere, Ada and Hopper GPUs; not Blackwell, see README). Without a GPU the
# same image runs the detector on the CPU (REPAIROS_TORCH_DEVICE=cpu, or automatically when no GPU is visible).
# No secrets are baked in: keys arrive as environment variables at pod start (README: Runpod secrets).

ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

# ---------------------------------------------------------------- deps: one venv, hash-pinned, no caches
FROM ${PYTHON_IMAGE} AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
COPY deploy/runpod/requirements.linux.lock /tmp/requirements.linux.lock
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes --no-deps \
      --extra-index-url https://download.pytorch.org/whl/cu126 \
      -r /tmp/requirements.linux.lock

# ---------------------------------------------------------------- weights: fetched once, at build time
FROM deps AS weights
ENV HF_HOME=/opt/hf PYTHONPATH=/app/backend
COPY backend/repairos /app/backend/repairos
COPY config /app/config
# The revision comes from the slot itself (backend/repairos/slots/det_owlv2), so the image always caches exactly
# what the code will ask for; HF_HUB_OFFLINE=1 at run time then guarantees no fetch on a cold start.
RUN /opt/venv/bin/python -c "\
from repairos.slots.det_owlv2 import MODEL_REPO, MODEL_REVISION; \
from transformers import AutoProcessor, Owlv2ForObjectDetection; \
AutoProcessor.from_pretrained(MODEL_REPO, revision=MODEL_REVISION); \
Owlv2ForObjectDetection.from_pretrained(MODEL_REPO, revision=MODEL_REVISION); \
print('cached', MODEL_REPO, MODEL_REVISION)" \
 && rm -rf /opt/hf/xet /opt/hf/hub/.locks

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="lea-backend" \
      org.opencontainers.image.description="LEA (RepairOS) gateway + pipeline worker for Runpod" \
      org.opencontainers.image.licenses="Apache-2.0"
# No init or process manager: start.sh supervises the two processes and forwards signals. curl: the healthcheck.
# libgomp1 + libgl1: open3d (the scan mesh, repairos/pipeline/mesh.py) loads OpenMP and libGL even headless.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates libgomp1 libgl1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=deps /opt/venv /opt/venv
COPY --from=weights /opt/hf /opt/hf
WORKDIR /app
# Repo layout as the code expects it (repairos.config.ROOT = the repo root two levels above the package).
COPY LICENSE NOTICE /app/
COPY backend/repairos /app/backend/repairos
COPY config /app/config
COPY spec/contracts /app/spec/contracts
COPY samples/session.schema.json /app/samples/session.schema.json
COPY tools/requeue_scan.py /app/tools/requeue_scan.py
COPY deploy/runpod/start.sh /app/deploy/runpod/start.sh
COPY deploy/smoke.sh deploy/smoke-still.jpg /app/deploy/

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    REPAIROS_DATA_DIR=/workspace/lea-data \
    PORT=8000

EXPOSE 8000
# The Runpod volume disk mounts at /workspace (README); without one, the data dir lives in the container and is lost
# when the pod stops.
VOLUME ["/workspace"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1
CMD ["bash", "/app/deploy/runpod/start.sh"]
