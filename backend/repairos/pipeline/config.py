"""config/pipeline.yaml: the pipeline's speed knobs (how much is inspected, how much runs at once). Every key is
required; a missing or wrong value is an error, never a default in code."""
from __future__ import annotations

import os
from pathlib import Path

from repairos.config import ROOT

PIPELINE_CONFIG = ROOT / "config" / "pipeline.yaml"
INT_KEYS = ("worker_jobs", "stills_at_once", "vlm_calls_at_once", "max_stills", "max_crops_per_still",
            "detector_tile_px", "continuation_timeout_s")
FLOAT_KEYS = ("same_region_m",)


def load_pipeline_config(path: Path | None = None) -> dict:
    import yaml

    path = Path(os.environ.get("REPAIROS_PIPELINE_CONFIG") or path or PIPELINE_CONFIG)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict = {}
    for k in INT_KEYS:
        v = data.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ValueError(f"{path}: {k} must be a whole number of at least 1")
        out[k] = v
    for k in FLOAT_KEYS:
        v = data.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            raise ValueError(f"{path}: {k} must be a positive number")
        out[k] = float(v)
    return out
