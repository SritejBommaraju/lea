"""CLS=dinov2 (task M7): frozen DINOv2 features + a small trained head, calibrated.

Backbone: facebook/dinov2-base on Hugging Face, revision pinned below, never fine-tuned. Licence: Apache-2.0 for
the weights (model card `license: apache-2.0`, checked 2026-10-24, https://huggingface.co/facebook/dinov2-base) and
the code (`transformers`; facebookresearch/dinov2 is Apache-2.0). Download: model.safetensors 346 MB.

Features: the [CLS] token and the mean patch token of the last layer, concatenated, from the crop resized to a
square of `resolution` px with ImageNet normalisation (Pillow + numpy, no scipy). The head (training/models/
cls_dinov2/: head.npz + head.json, made by training/defects/train.py) has two parts:
- defect head: p(defect visible) on every crop, Platt-scaled on the validation split;
- kind head: p(kind | defect) over DEFECT_CLASSES, trained on defect crops of one known kind, temperature-scaled.
- unfamiliar check: the distance of the crop's features from the training features (PCA Mahalanobis plus
  residual); past the cutoff chosen on val the classifier abstains ("unfamiliar part or image type") and the crop
  goes to the VLM. The training data is mostly PCB imagery, so most other hardware should abstain.
head.json also carries the routing thresholds (`ok_below`, `flag_at`), the training data manifest hash and the
backbone revision; a head made for another backbone revision is refused.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from repairos.registry import register
from repairos.slots.classifier import UNFAMILIAR, DefectScore
from repairos.vision.generic_classes import DEFECT_CLASSES

MODEL_REPO = "facebook/dinov2-base"
MODEL_REVISION = "f9e44c814b77203eaa57a6bdbbd535f21ede1415"
MODEL_LICENSE = "Apache-2.0"
IMAGE_MEAN = (0.485, 0.456, 0.406)
IMAGE_STD = (0.229, 0.224, 0.225)
DEFAULT_RESOLUTION = 224
POOLS = ("cls", "mean")

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_HEAD_DIR = ROOT / "training" / "models" / "cls_dinov2"


def _softmax(z):
    import numpy as np

    z = z - z.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=-1, keepdims=True)


class Head:
    """The trained head in numpy: standardise, then linear (or one hidden ReLU layer), then temperature."""

    def __init__(self, head_dir: Path):
        import numpy as np

        self.dir = Path(head_dir)
        meta_path, w_path = self.dir / "head.json", self.dir / "head.npz"
        if not meta_path.exists() or not w_path.exists():
            raise RuntimeError(f"CLS=dinov2 has no trained head in {self.dir} (head.json + head.npz). Train it: "
                               "backend/.venv/bin/python training/defects/train.py all")
        self.meta = json.loads(meta_path.read_text())
        if self.meta["backbone"]["revision"] != MODEL_REVISION:
            raise RuntimeError(f"head in {self.dir} was trained on backbone revision "
                               f"{self.meta['backbone']['revision']}, slot pins {MODEL_REVISION}: retrain")
        if tuple(self.meta["classes"]) != DEFECT_CLASSES:
            raise RuntimeError(f"head classes {self.meta['classes']} differ from DEFECT_CLASSES: retrain")
        with np.load(w_path, allow_pickle=False) as z:
            self.w = {k: z[k].astype(np.float32) for k in z.files}
        self.resolution = int(self.meta["resolution"])
        self.pools = tuple(self.meta.get("pools", POOLS))
        self.ok_below = float(self.meta["thresholds"]["ok_below"])
        self.flag_at = float(self.meta["thresholds"]["flag_at"])

    def _logits(self, x, part: str):
        import numpy as np

        w = self.w
        h = (x - w["mean"]) / w["std"]
        if f"{part}_w1" in w:
            h = np.maximum(h @ w[f"{part}_w1"] + w[f"{part}_b1"], 0.0)
        return h @ w[f"{part}_w"] + w[f"{part}_b"]

    def ood_distance(self, feats):
        """Distance of each crop's features from the training distribution (training/defects/train.py ood_fit):
        Mahalanobis in the top principal components plus the residual outside them."""
        w = self.w
        c = (feats - w["mean"]) / w["std"] - w["ood_centre"]
        z = c @ w["ood_components"]
        resid = (c * c).sum(1) - (z * z).sum(1)
        k = w["ood_var"].shape[0]
        return ((z * z) / w["ood_var"]).sum(1) / k + resid / float(self.meta["ood"]["residual_var"]) / (c.shape[1] - k)

    def unfamiliar(self, feats):
        """True per crop when it is out of the training distribution (cutoff chosen on val, head.json `ood`)."""
        return self.ood_distance(feats) > float(self.meta["ood"]["cutoff"])

    def predict(self, feats) -> tuple[Any, Any]:
        """(p_defect (N,), p_kind (N, K)) for features (N, D)."""
        import numpy as np

        a, b = self.meta["calibration"]["defect_platt"]
        z = a * self._logits(feats, "defect")[:, 0] + b
        p_def = 1.0 / (1.0 + np.exp(-z))
        return p_def, _softmax(self._logits(feats, "kind") / float(self.meta["calibration"]["kind_temperature"]))


class Backbone:
    """Frozen DINOv2: crops -> features. Training and runtime share this code, so features match exactly."""

    def __init__(self, device: str | None = None, batch_size: int = 32):
        self._device = device
        self.batch_size = batch_size
        self._model = None
        self._lock = threading.Lock()
        self.load_s = 0.0

    def _load(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
            try:
                from transformers import AutoModel

                from repairos.slots.det_gdino import pick_device
            except ImportError as e:
                raise RuntimeError("CLS=dinov2 needs the vision extra: uv pip install -e 'backend[vision]'") from e
            t = time.perf_counter()
            self._device = self._device or pick_device()
            model = AutoModel.from_pretrained(MODEL_REPO, revision=MODEL_REVISION)
            self._model = model.to(self._device).eval()
            self.load_s = time.perf_counter() - t

    def pixels(self, crops: Sequence[Any], resolution: int):
        import numpy as np
        import torch
        from PIL import Image

        from repairos.vision.crops import as_pil

        mean = np.asarray(IMAGE_MEAN, dtype=np.float32)
        std = np.asarray(IMAGE_STD, dtype=np.float32)
        arr = [(np.asarray(as_pil(c).resize((resolution, resolution), Image.Resampling.BICUBIC),
                           dtype=np.float32) / 255.0 - mean) / std for c in crops]
        return torch.from_numpy(np.stack(arr)).permute(0, 3, 1, 2).contiguous()

    def embed(self, crops: Sequence[Any], resolution: int = DEFAULT_RESOLUTION, pools: Sequence[str] = POOLS):
        """(N, len(pools) * hidden) float32 numpy per crop: `cls` = the [CLS] token, `mean` / `max` = the mean /
        elementwise max of the patch tokens (max keeps a small defect that the mean dilutes)."""
        import numpy as np
        import torch

        self._load()
        out = []
        for i in range(0, len(crops), self.batch_size):
            px = self.pixels(crops[i:i + self.batch_size], resolution).to(self._device)
            with torch.inference_mode():
                h = self._model(pixel_values=px).last_hidden_state
            parts = {"cls": h[:, 0], "mean": h[:, 1:].mean(dim=1), "max": h[:, 1:].amax(dim=1)}
            out.append(torch.cat([parts[p] for p in pools], dim=1).float().cpu().numpy())
        return np.concatenate(out) if out else np.zeros((0, len(pools) * 768), dtype=np.float32)


@register("CLS", "dinov2")
class Dinov2DefectClassifier:
    def __init__(self, *, head_dir: str | Path | None = None, device: str | None = None, batch_size: int = 32):
        self.head = Head(Path(head_dir or os.environ.get("REPAIROS_CLS_HEAD_DIR") or DEFAULT_HEAD_DIR))
        self.backbone = Backbone(device=device, batch_size=batch_size)
        self.ok_below = self.head.ok_below
        self.flag_at = self.head.flag_at
        self.model_id = f"{MODEL_REPO}@{MODEL_REVISION[:7]}+head:{self.head.meta['head_id']}"
        self.last_timing: dict[str, float] = {}

    def classify(self, crops: Sequence[Any]) -> list[DefectScore]:
        if not crops:
            return []
        t = time.perf_counter()
        feats = self.backbone.embed(list(crops), self.head.resolution, self.head.pools)
        p_def, p_kind = self.head.predict(feats)
        odd = self.head.unfamiliar(feats)
        self.last_timing.update(classify_s=time.perf_counter() - t, crops=len(crops))
        out = []
        for pd, pk, o in zip(p_def.tolist(), p_kind, odd.tolist()):
            k = int(pk.argmax())
            out.append(DefectScore(p_defect=min(1.0, max(0.0, float(pd))), defect_class=DEFECT_CLASSES[k],
                                   p_class=float(pk[k]), model=self.model_id,
                                   abstain_reason=UNFAMILIAR if o else None))
        return out
