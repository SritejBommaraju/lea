"""Decode a capture video frame by frame with the `ffmpeg` / `ffprobe` command-line tools.

ffmpeg runs as a separate program on the laptop (a dev-side tool, never linked or shipped; its licence depends on
the build, e.g. LGPL or GPL for Homebrew's). Missing -> HUMAN ACTION NEEDED: install ffmpeg (`brew install ffmpeg`).

Every decoded frame is returned exactly once, in decode order (`-fps_mode passthrough`), with the MP4 edit list
ignored (`-ignore_editlist 1`) so a sample written before the writer's start time is still returned; `probe` uses
the same options, so frame k of `frames()` is packet k (by presentation time) of `probe()`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from fractions import Fraction
from pathlib import Path

import numpy as np

from repairos.importers.images import ImportFormatError


class ToolMissing(RuntimeError):
    pass


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise ToolMissing(f"HUMAN ACTION NEEDED: install {name} (e.g. `brew install ffmpeg`); the importer decodes "
                          "the capture video with it")
    return path


def probe(path: Path) -> dict:
    """{width, height, time_base (Fraction), pts (sorted presentation times in time_base units)} of the first video
    stream."""
    out = subprocess.run([_tool("ffprobe"), "-v", "error", "-ignore_editlist", "1", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height,time_base:packet=pts", "-of", "json", str(path)],
                         capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise ImportFormatError(f"{path.name}: ffprobe failed: {out.stderr.strip()[:300]}")
    info = json.loads(out.stdout)
    if not info.get("streams"):
        raise ImportFormatError(f"{path.name}: no video stream")
    st = info["streams"][0]
    pts = sorted(int(p["pts"]) for p in info.get("packets", []) if p.get("pts") not in (None, "N/A"))
    return {"width": int(st["width"]), "height": int(st["height"]), "time_base": Fraction(st["time_base"]),
            "pts": pts}


def frames(path: Path, size: tuple[int, int] | None = None,
           select: Sequence[int] | None = None) -> Iterator[tuple[int, np.ndarray]]:
    """(frame index, H x W x 3 uint8 RGB) for every frame, or only the indices in `select`, optionally scaled to
    `size` = (width, height) with area averaging."""
    info_w, info_h = None, None
    filters = []
    wanted = sorted(set(select)) if select is not None else None
    if wanted is not None:
        if not wanted:
            return
        filters.append("select='" + "+".join(f"eq(n\\,{i})" for i in wanted) + "'")
    if size is not None:
        filters.append(f"scale={size[0]}:{size[1]}:flags=area")
    if size is None:
        meta = probe(path)
        info_w, info_h = meta["width"], meta["height"]
    w, h = size if size is not None else (info_w, info_h)
    cmd = [_tool("ffmpeg"), "-v", "error", "-nostdin", "-ignore_editlist", "1", "-i", str(path), "-map", "0:v:0"]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += ["-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    n_bytes = w * h * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        k = 0
        while True:
            buf = proc.stdout.read(n_bytes)
            if not buf:
                break
            if len(buf) != n_bytes:
                raise ImportFormatError(f"{path.name}: truncated frame from ffmpeg")
            idx = wanted[k] if wanted is not None else k
            yield idx, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            k += 1
        err = proc.stderr.read().decode("utf-8", "replace")
        if proc.wait() != 0:
            raise ImportFormatError(f"{path.name}: ffmpeg failed: {err.strip()[:300]}")
        if wanted is not None and k != len(wanted):
            raise ImportFormatError(f"{path.name}: asked ffmpeg for {len(wanted)} frames, got {k}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
