"""Spoken-line cache and WAV wrapping. Audio is kept as raw 16-bit mono PCM; WAV is built on the way out."""
from __future__ import annotations

import hashlib
import os
import struct
import threading
from collections import OrderedDict
from pathlib import Path


def wav(pcm: bytes, sample_rate: int) -> bytes:
    """A complete RIFF/WAVE file (PCM, 16-bit, mono) around the samples: plays in AVAudioPlayer as is."""
    pcm = pcm[: len(pcm) - len(pcm) % 2]
    return b"".join([
        b"RIFF", struct.pack("<I", 36 + len(pcm)), b"WAVE",
        b"fmt ", struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16),
        b"data", struct.pack("<I", len(pcm)), pcm])


def line_key(text: str, model: str, voice: str, sample_rate: int) -> str:
    return hashlib.sha256(f"{model}\n{voice}\n{sample_rate}\n{text}".encode()).hexdigest()


class TTSCache:
    """Raw PCM per spoken line (sha256 of model, voice, rate and text): a small in-memory LRU over files in
    `<data dir>/voice_cache/`. The fixed scan cues are asked for at every app start, so they come from here."""

    def __init__(self, root: Path | None = None, max_items: int = 256):
        if root is None:
            from repairos.config import ROOT
            root = Path(os.environ.get("REPAIROS_DATA_DIR") or ROOT / "data") / "voice_cache"
        self.root = root
        self.max_items = max_items
        self._mem: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._lock:
            if key in self._mem:
                self._mem.move_to_end(key)
                return self._mem[key]
        p = self.root / f"{key}.pcm"
        try:
            data = p.read_bytes()
        except OSError:
            return None
        self._remember(key, data)
        return data

    def put(self, key: str, pcm: bytes) -> None:
        if not pcm:
            return
        self._remember(key, pcm)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.root / f".{key}.tmp"
            tmp.write_bytes(pcm)
            tmp.replace(self.root / f"{key}.pcm")
        except OSError:
            pass   # the memory copy still serves this process

    def _remember(self, key: str, pcm: bytes) -> None:
        with self._lock:
            self._mem[key] = pcm
            self._mem.move_to_end(key)
            while len(self._mem) > self.max_items:
                self._mem.popitem(last=False)
