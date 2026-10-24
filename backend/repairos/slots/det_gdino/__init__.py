"""DET=gdino: Grounding DINO tiny, zero-shot open-vocabulary detection (BUILD M.DET.1). No training.

Model: IDEA-Research/grounding-dino-tiny on Hugging Face, revision pinned below. Licence: Apache-2.0 for the code
(`transformers`) and the weights (model card `license: apache-2.0`, checked 2026-10-24,
https://huggingface.co/IDEA-Research/grounding-dino-tiny). Download: model.safetensors 689 MB + tokenizer/config
files (~1 MB), cached by huggingface_hub under ~/.cache/huggingface.

Runs on MPS on Apple silicon when available, else CPU (override: REPAIROS_TORCH_DEVICE=cpu|mps|cuda). torch and
transformers are imported on first use only. Full-resolution stills are tiled (tiling.py) so small parts survive;
boxes come back in the input image's pixels.

Measured on this Mac (M4, MPS) on a real 4008 x 3008 photo: ~1.1 s per view per call; one call with all ~70 generic
classes mislabels most parts (the classes compete inside one text), so prompts go in calls of `prompts_per_call`.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from repairos.registry import register
from repairos.slots.detector import Detection
from repairos.vision.tiling import prompt_spans, tiled_detect
from repairos.vision.prompts import build_prompts

MODEL_REPO = "IDEA-Research/grounding-dino-tiny"
MODEL_REVISION = "a2bb814dd30d776dcf7e30523b00659f4f141c71"
MODEL_LICENSE = "Apache-2.0"

DEFAULT_MIN_SCORE = 0.30   # BUILD M.DET.1 box threshold
MAX_TEXT_TOKENS = 250      # the model's text limit is 256 tokens, incl. [CLS]/[SEP]


def pick_device() -> str:
    import torch

    forced = os.environ.get("REPAIROS_TORCH_DEVICE")
    if forced:
        return forced
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


@register("DET", "gdino")
class GroundingDinoDetector:
    """Detector Protocol (slots/detector.py) on Grounding DINO tiny."""

    model_id = f"{MODEL_REPO}@{MODEL_REVISION[:7]}"

    def __init__(self, *, min_score: float = DEFAULT_MIN_SCORE, tile_px: int = 1600, tile_overlap: float = 0.2,
                 use_tiles: bool = True, batch_size: int = 4, nms_iou: float = 0.5, cross_label_iou: float = 0.6,
                 max_detections: int = 150, prompts_per_call: int = 24, device: str | None = None):
        self.min_score = min_score
        self.tile_px = tile_px
        self.tile_overlap = tile_overlap
        self.use_tiles = use_tiles
        self.batch_size = batch_size
        self.nms_iou = nms_iou
        self.cross_label_iou = cross_label_iou
        self.max_detections = max_detections
        self.prompts_per_call = prompts_per_call
        self._device = device
        self._model = None
        self._processor = None
        self._lock = threading.Lock()
        self.last_timing: dict[str, float] = {}

    # -- loading --------------------------------------------------------------------------------------------------
    def _load(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
            try:
                import torch  # noqa: F401
                from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor
            except ImportError as e:  # the optional extra is not installed
                raise RuntimeError("DET=gdino needs the vision extra: uv pip install -e 'backend[vision]'") from e
            t = time.perf_counter()
            self._device = self._device or pick_device()
            self._processor = AutoProcessor.from_pretrained(MODEL_REPO, revision=MODEL_REVISION)
            model = AutoModelForZeroShotObjectDetection.from_pretrained(MODEL_REPO, revision=MODEL_REVISION)
            self._model = model.to(self._device).eval()
            self.last_timing["load_s"] = time.perf_counter() - t

    @property
    def device(self) -> str:
        self._load()
        return self._device

    # -- prompts --------------------------------------------------------------------------------------------------
    def _chunks(self, prompts: list[str]) -> list[list[str]]:
        """Split prompts into calls of at most `prompts_per_call` prompts and the model's token limit. Long prompt
        lists dilute Grounding DINO's text matching (measured: 70 classes in one call mislabel most parts)."""
        tok = self._processor.tokenizer
        chunks: list[list[str]] = []
        cur: list[str] = []
        used = 2  # [CLS] [SEP]
        for p in prompts:
            if "." in p:
                raise ValueError(f"prompt may not contain '.': {p!r}")
            n = len(tok(p, add_special_tokens=False)["input_ids"]) + 1  # + separator
            if cur and (used + n > MAX_TEXT_TOKENS or len(cur) >= self.prompts_per_call):
                chunks.append(cur)
                cur, used = [], 2
            cur.append(p)
            used += n
        if cur:
            chunks.append(cur)
        return chunks

    # -- inference ------------------------------------------------------------------------------------------------
    def _run(self, images: list[Any], prompts: list[str], min_score: float) -> list[list[tuple[tuple, str, float]]]:
        """Per image: (box in that image's pixels, prompt, score) for every query above min_score."""
        import torch

        text = " . ".join(prompts) + " ."
        inputs = self._processor(images=images, text=[text] * len(images), return_tensors="pt")
        ids = inputs["input_ids"][0].tolist()
        sep_id = self._processor.tokenizer.convert_tokens_to_ids(".")
        spans = prompt_spans(ids, sep_id, len(prompts))
        inputs = {k: v.to(self._device) for k, v in inputs.items()}
        with torch.inference_mode():
            out = self._model(**inputs)
        probs = out.logits.sigmoid().float().cpu()          # (B, Q, T)
        boxes = out.pred_boxes.float().cpu()                # (B, Q, 4) cx cy w h, normalised to the image
        span_scores = torch.stack([probs[:, :, s].max(dim=-1).values for s in spans], dim=-1)  # (B, Q, P)
        best, which = span_scores.max(dim=-1)
        results = []
        for b, im in enumerate(images):
            w, h = im.size
            keep = (best[b] >= min_score).nonzero().flatten().tolist()
            res = []
            for q in keep:
                cx, cy, bw, bh = boxes[b, q].tolist()
                res.append((((cx - bw / 2) * w, (cy - bh / 2) * h, (cx + bw / 2) * w, (cy + bh / 2) * h),
                            prompts[which[b, q].item()], float(best[b, q])))
            results.append(res)
        return results

    def detect(self, image: Any, prompts: list[str] | None = None, min_score: float | None = None) -> list[Detection]:
        from repairos.vision.crops import as_pil

        self._load()
        prompts = list(dict.fromkeys(prompts or build_prompts()))
        min_score = self.min_score if min_score is None else min_score
        im = as_pil(image)
        chunks = self._chunks(prompts)
        t0 = time.perf_counter()

        def run(images):
            per_image = [[] for _ in images]
            for chunk in chunks:
                for acc, dets in zip(per_image, self._run(images, chunk, min_score)):
                    acc.extend(dets)
            return per_image

        merged, views = tiled_detect(
            im.size, run, tile_px=self.tile_px, overlap=self.tile_overlap, use_tiles=self.use_tiles,
            batch_size=self.batch_size, nms_iou=self.nms_iou, cross_label_iou=self.cross_label_iou,
            max_detections=self.max_detections,
            crop=lambda t: im if t == (0, 0, im.width, im.height) else im.crop(t))
        self.last_timing.update(detect_s=time.perf_counter() - t0, views=views, calls_per_view=len(chunks))
        return [Detection(box=tuple(round(v, 1) for v in b), label=lab, score=round(s, 4)) for b, lab, s in merged]
