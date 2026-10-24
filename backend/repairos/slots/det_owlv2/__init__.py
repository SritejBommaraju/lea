"""DET=owlv2: OWLv2 base, zero-shot open-vocabulary detection (BUILD M.DET.4). No training.

Model: google/owlv2-base-patch16-ensemble on Hugging Face, revision pinned below. Licence: Apache-2.0 for the code
(`transformers`) and the weights (model card `license: apache-2.0`, checked 2026-10-24,
https://huggingface.co/google/owlv2-base-patch16-ensemble). Download: model.safetensors 620 MB + ~1.6 MB tokenizer
and config files.

Unlike Grounding DINO, each class is scored independently against every box, so the whole generic vocabulary goes in
one call (measured on this Mac, M4/MPS: ~0.65 s per 960 px view for ~70 classes). Preprocessing (pad to square,
resize to 960, CLIP normalisation) is done here with Pillow + numpy, matching the model's image processor, to avoid
a scipy dependency. Full-resolution stills are tiled like every DET implementation (vision/tiling.py).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from repairos.registry import register
from repairos.slots.detector import Detection
from repairos.vision.prompts import build_prompts
from repairos.vision.tiling import tiled_detect

MODEL_REPO = "google/owlv2-base-patch16-ensemble"
MODEL_REVISION = "cfd3195ba4ea9592eec887ded089f4c08eff231d"
MODEL_LICENSE = "Apache-2.0"

DEFAULT_MIN_SCORE = 0.20
QUERY_TEMPLATE = "a photo of a {}"


@register("DET", "owlv2")
class Owlv2Detector:
    model_id = f"{MODEL_REPO}@{MODEL_REVISION[:7]}"

    def __init__(self, *, min_score: float = DEFAULT_MIN_SCORE, tile_px: int = 1600, tile_overlap: float = 0.2,
                 use_tiles: bool = True, batch_size: int = 4, nms_iou: float = 0.5, cross_label_iou: float = 0.6,
                 max_detections: int = 150, device: str | None = None):
        self.min_score = min_score
        self.tile_px = tile_px
        self.tile_overlap = tile_overlap
        self.use_tiles = use_tiles
        self.batch_size = batch_size
        self.nms_iou = nms_iou
        self.cross_label_iou = cross_label_iou
        self.max_detections = max_detections
        self._device = device
        self._model = None
        self._processor = None
        self._lock = threading.Lock()
        self.last_timing: dict[str, float] = {}

    def _load(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
            try:
                from transformers import AutoProcessor, Owlv2ForObjectDetection

                from repairos.slots.det_gdino import pick_device
            except ImportError as e:
                raise RuntimeError("DET=owlv2 needs the vision extra: uv pip install -e 'backend[vision]'") from e
            t = time.perf_counter()
            self._device = self._device or pick_device()
            self._processor = AutoProcessor.from_pretrained(MODEL_REPO, revision=MODEL_REVISION)
            model = Owlv2ForObjectDetection.from_pretrained(MODEL_REPO, revision=MODEL_REVISION)
            self._model = model.to(self._device).eval()
            self.last_timing["load_s"] = time.perf_counter() - t

    @property
    def device(self) -> str:
        self._load()
        return self._device

    def _pixels(self, images: list[Any]):
        import numpy as np
        import torch
        from PIL import Image

        ip = self._processor.image_processor
        side = ip.size["height"]
        mean = np.asarray(ip.image_mean, dtype=np.float32)
        std = np.asarray(ip.image_std, dtype=np.float32)
        arrays, squares = [], []
        for im in images:
            s = max(im.size)
            canvas = Image.new("RGB", (s, s), (128, 128, 128))  # OWLv2 pads bottom/right with grey
            canvas.paste(im, (0, 0))
            a = np.asarray(canvas.resize((side, side), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
            arrays.append((a - mean) / std)
            squares.append(s)
        return torch.from_numpy(np.stack(arrays)).permute(0, 3, 1, 2).contiguous(), squares

    def _run(self, images: list[Any], prompts: list[str], min_score: float):
        import torch

        tok = self._processor.tokenizer([QUERY_TEMPLATE.format(p) for p in prompts], padding="max_length",
                                        max_length=16, truncation=True, return_tensors="pt")
        pixels, squares = self._pixels(images)
        n = len(images)
        with torch.inference_mode():
            out = self._model(input_ids=tok["input_ids"].repeat(n, 1).to(self._device),
                              attention_mask=tok["attention_mask"].repeat(n, 1).to(self._device),
                              pixel_values=pixels.to(self._device))
        probs = out.logits.sigmoid().float().cpu()     # (B, Q, C)
        boxes = out.pred_boxes.float().cpu()           # (B, Q, 4) cx cy w h, normalised to the padded square
        best, which = probs.max(dim=-1)
        results = []
        for b in range(n):
            s = squares[b]
            res = []
            for q in (best[b] >= min_score).nonzero().flatten().tolist():
                cx, cy, w, h = (boxes[b, q] * s).tolist()
                res.append(((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), prompts[which[b, q].item()],
                            float(best[b, q])))
            results.append(res)
        return results

    def detect(self, image: Any, prompts: list[str] | None = None, min_score: float | None = None) -> list[Detection]:
        from repairos.vision.crops import as_pil

        self._load()
        prompts = list(dict.fromkeys(prompts or build_prompts()))
        min_score = self.min_score if min_score is None else min_score
        im = as_pil(image)
        t0 = time.perf_counter()
        merged, views = tiled_detect(
            im.size, lambda images: self._run(images, prompts, min_score), tile_px=self.tile_px,
            overlap=self.tile_overlap, use_tiles=self.use_tiles, batch_size=self.batch_size, nms_iou=self.nms_iou,
            cross_label_iou=self.cross_label_iou, max_detections=self.max_detections,
            crop=lambda t: im if t == (0, 0, im.width, im.height) else im.crop(t))
        self.last_timing.update(detect_s=time.perf_counter() - t0, views=views)
        return [Detection(box=tuple(round(v, 1) for v in b), label=lab, score=round(s, 4)) for b, lab, s in merged]
