"""Run `tools/recon-cli` (Apple PhotogrammetrySession on the Mac) from Python, with a memory guard, and read what it
writes.

    res = run_cli(["reconstruct", "--input", images, "--out", out, "--detail", "reduced"], max_memory_gb=6)
    res.summary      # the JSON object recon-cli printed on stdout
    res.events       # its stderr JSON lines (progress, stages, errors)
    res.peak_rss_mb  # peak resident memory of the process tree, sampled every `poll_s`

The guard: before starting, `memory_pressure`'s free percentage must be at least `min_free_pct` (the Mac is shared
with other jobs); while running, the CLI's process tree is killed when its resident memory exceeds `max_memory_gb`.
Both raise `MemoryGuardError` with the numbers.

The binary is `$REPAIROS_RECON_CLI`, else `tools/recon-cli/.build/release/recon-cli` in the repo
(`swift build -c release --package-path tools/recon-cli`).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from repairos.locate.camera import FLIP_YZ, Pinhole, Pose

REPO = Path(__file__).resolve().parents[3]
DEFAULT_BINARY = REPO / "tools" / "recon-cli" / ".build" / "release" / "recon-cli"
DEFAULT_MAX_MEMORY_GB = 6.0
DEFAULT_MIN_FREE_PCT = 25.0


class ReconCliError(RuntimeError):
    """recon-cli failed: bad usage, no poses, or it could not be found."""


class MemoryGuardError(RuntimeError):
    """The Mac had too little free memory to start, or the job grew past its memory limit and was killed."""


@dataclass
class CliResult:
    summary: dict[str, Any]
    events: list[dict[str, Any]]
    returncode: int
    wall_s: float
    peak_rss_mb: float
    stderr_other: list[str] = field(default_factory=list)   # stderr lines that were not JSON (system logging)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and bool(self.summary.get("ok"))


def binary() -> Path:
    p = Path(os.environ.get("REPAIROS_RECON_CLI", DEFAULT_BINARY))
    if not p.exists():
        raise ReconCliError(f"{p} not found: swift build -c release --package-path tools/recon-cli")
    return p


def parse_events(stderr: str) -> tuple[list[dict[str, Any]], list[str]]:
    """recon-cli's stderr: (JSON event objects, every other non-empty line)."""
    events, other = [], []
    for line in stderr.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                other.append(line)
                continue
            if isinstance(obj, dict) and "event" in obj:
                events.append(obj)
                continue
        other.append(line)
    return events, other


def parse_summary(stdout: str) -> dict[str, Any]:
    """The last JSON object recon-cli printed on stdout ({} when there is none)."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    return {}


_FREE_RE = re.compile(r"System-wide memory free percentage:\s*(\d+(?:\.\d+)?)%")


def parse_free_percent(text: str) -> float | None:
    m = _FREE_RE.search(text)
    return float(m.group(1)) if m else None


def memory_free_percent() -> float | None:
    """macOS `memory_pressure`'s system-wide free percentage (None when the tool is missing)."""
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=30).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return parse_free_percent(out)


def check_free_memory(min_free_pct: float = DEFAULT_MIN_FREE_PCT) -> float | None:
    free = memory_free_percent()
    if free is not None and free < min_free_pct:
        raise MemoryGuardError(f"only {free:.0f}% of memory is free (at least {min_free_pct:.0f}% needed): not "
                               "starting a heavy job")
    return free


def tree_rss_mb(root_pid: int) -> float:
    """Resident memory (MB) of `root_pid` and all its descendants, from `ps`."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True, text=True,
                             timeout=10).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0.0
    children: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        pid, ppid, kb = (int(v) for v in parts)
        children.setdefault(ppid, []).append(pid)
        rss[pid] = kb
    total, stack = 0, [root_pid]
    while stack:
        p = stack.pop()
        total += rss.get(p, 0)
        stack.extend(children.get(p, []))
    return total / 1024.0


def run_cli(args: Sequence[str | os.PathLike], *, max_memory_gb: float = DEFAULT_MAX_MEMORY_GB,
            min_free_pct: float | None = DEFAULT_MIN_FREE_PCT, poll_s: float = 0.5, timeout_s: float = 3600.0,
            check: bool = True, on_event=None) -> CliResult:
    """Run recon-cli with the memory guard. `check`: raise ReconCliError when it fails."""
    if min_free_pct is not None:
        check_free_memory(min_free_pct)
    cmd = [str(binary()), *map(str, args)]
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    peak = 0.0
    limit_mb = max_memory_gb * 1024.0
    # Drain stderr on a thread so a chatty child never blocks on a full pipe.
    import threading

    err_lines: list[str] = []

    def pump():
        assert proc.stderr is not None
        for line in proc.stderr:
            err_lines.append(line)
            if on_event and line.startswith("{"):
                try:
                    on_event(json.loads(line))
                except (json.JSONDecodeError, Exception):  # noqa: BLE001 - a callback must not kill the run
                    pass

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    killed = None
    while proc.poll() is None:
        rss = tree_rss_mb(proc.pid)
        peak = max(peak, rss)
        if rss > limit_mb:
            killed = f"recon-cli used {rss / 1024:.2f} GB, over the {max_memory_gb:.1f} GB limit: killed"
            proc.kill()
            break
        if time.monotonic() - t0 > timeout_s:
            killed = f"recon-cli ran longer than {timeout_s:.0f} s: killed"
            proc.kill()
            break
        time.sleep(poll_s)
    stdout = proc.stdout.read() if proc.stdout else ""
    proc.wait()
    th.join(timeout=5)
    wall = time.monotonic() - t0
    events, other = parse_events("".join(err_lines))
    summary = parse_summary(stdout)
    # The CLI's own getrusage peak covers memory between our samples.
    peak = max(peak, float(summary.get("peak_rss_mb") or 0.0))
    res = CliResult(summary, events, proc.returncode, wall, peak, other)
    if killed:
        if "GB" in killed:
            raise MemoryGuardError(killed)
        raise ReconCliError(killed)
    if check and not res.ok:
        msg = summary.get("error") or "; ".join(summary.get("errors") or []) or f"exit {proc.returncode}"
        raise ReconCliError(f"recon-cli {args[0] if args else ''} failed: {msg}")
    return res


# --- reading what recon-cli writes -------------------------------------------------------------------------------

def _m(rows: Sequence[float], n: int) -> np.ndarray:
    a = np.asarray(rows, dtype=float)
    if a.size != n * n:
        raise ValueError(f"expected {n * n} values, got {a.size}")
    return a.reshape(n, n)


@dataclass(frozen=True)
class ApplePose:
    """One image's PhotogrammetrySession pose, camera-to-world in the session's own frame."""
    sample_id: int
    file: str | None
    matrix: np.ndarray                 # 4x4 camera-to-world as Apple returns it
    intrinsics: np.ndarray | None      # 3x3 K (macOS 26+), pixels of the stored image buffer

    @property
    def centre(self) -> np.ndarray:
        return self.matrix[:3, 3].copy()

    def pose(self, *, opencv_axes: bool = False) -> Pose:
        """As our Pose (ARKit camera axes). `opencv_axes`: Apple's camera looks down +Z (then flip Y and Z)."""
        r = self.matrix[:3, :3]
        u, _, vt = np.linalg.svd(r)
        r = u @ vt
        return Pose(r @ FLIP_YZ if opencv_axes else r, self.matrix[:3, 3].copy())


def parse_poses(doc: Mapping) -> list[ApplePose]:
    out = []
    for row in doc.get("poses", []):
        k = row.get("intrinsics_row_major")
        out.append(ApplePose(int(row["id"]), row.get("file"), _m(row["transform_row_major"], 4),
                             _m(k, 3) if k else None))
    return out


def load_poses(path: str | Path) -> list[ApplePose]:
    return parse_poses(json.loads(Path(path).read_text()))


@dataclass(frozen=True)
class SampleInfo:
    """What one Object Capture image carries (recon-cli samples)."""
    file: str
    width: int
    height: int
    arkit_pose: Pose | None            # camera-to-world, ARKit camera axes, the capture session's ARKit world
    cam: Pinhole | None                # intrinsics of the stored image buffer
    gravity: np.ndarray | None
    scan_pass_id: int | None
    session_uuid: str | None
    bounding_box: np.ndarray | None    # 4x4 (Object Capture's object box), ARKit world
    depth_file: Path | None            # float32 metres, depth_shape
    conf_file: Path | None             # float32 0-2
    depth_shape: tuple[int, int] | None   # (height, width)


def parse_samples(doc: Mapping, base_dir: str | Path) -> list[SampleInfo]:
    base = Path(base_dir)
    out = []
    for row in doc.get("images", []):
        if "error" in row:
            continue
        cam_row = row.get("camera")
        pose = cam = None
        if cam_row:
            m = _m(cam_row["transform_row_major"], 4)
            pose = Pose.from_matrix(m)
            k = _m(cam_row["intrinsics_row_major"], 3)
            cam = Pinhole(k[0, 0], k[1, 1], k[0, 2], k[1, 2], int(row["width_px"]), int(row["height_px"]))
        d, c = row.get("depth") or {}, row.get("confidence") or {}
        out.append(SampleInfo(
            file=row["file"], width=int(row["width_px"]), height=int(row["height_px"]), arkit_pose=pose, cam=cam,
            gravity=np.asarray(row["gravity"], float) if row.get("gravity") else None,
            scan_pass_id=row.get("scan_pass_id"), session_uuid=row.get("session_uuid"),
            bounding_box=_m(row["bounding_box_row_major"], 4) if row.get("bounding_box_row_major") else None,
            depth_file=base / d["file"] if d.get("file") else None,
            conf_file=base / c["file"] if c.get("file") else None,
            depth_shape=(int(d["height"]), int(d["width"])) if d else None))
    return out


def load_samples(path: str | Path) -> list[SampleInfo]:
    path = Path(path)
    return parse_samples(json.loads(path.read_text()), path.parent)


def read_f32(path: Path, shape: tuple[int, int]) -> np.ndarray:
    a = np.fromfile(path, dtype="<f4")
    if a.size != shape[0] * shape[1]:
        raise ValueError(f"{path}: {a.size} values, expected {shape[0]} x {shape[1]}")
    return a.reshape(shape)
