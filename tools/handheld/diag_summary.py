#!/usr/bin/env python3
"""Summary of a hand-held ("Turn it") scan from the phone's records.

    python3 -I tools/handheld/diag_summary.py <session folder | diag.jsonl>

Pull a session first (the newest one is listed by `ls` of the Documents/sessions folder):
    xcrun devicectl device copy from --device <udid> --domain-type appDataContainer \
      --domain-identifier com.rohankilaru.repairos --source Documents/sessions/<id> --destination <folder>

Prints: the hand-held start (segmentation or hand pose, seed size), pose quality over time, lost and recovered
events, the tracker's and the display's timings (fps), stills with their pose source and quality, keyframes, the
coverage (underside cells) and whether raw frames were recorded for the Mac replay (tools/handheld/run.sh replay).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def load(path: Path) -> tuple[list[dict], Path | None]:
    diag = path / "diag.jsonl" if path.is_dir() else path
    rows = []
    for line in diag.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows, (path if path.is_dir() else None)


def ev(r: dict) -> str:
    return r.get("event") or ""


def ts(r: dict) -> float | None:
    """Seconds since the session's diag started (Diag writes `ms`)."""
    return r["ms"] / 1000 if isinstance(r.get("ms"), (int, float)) else None


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    rows, folder = load(Path(argv[1]))
    if not rows:
        print("no diag lines")
        return 1
    starts = [r for r in rows if ev(r) == "handheld_start"]
    if not starts:
        print("no handheld_start: this scan did not use Turn it")
    t0 = ts(starts[0]) if starts else None
    rel = (lambda r: f"{ts(r) - t0:6.1f} s" if t0 is not None and ts(r) is not None else "      ")
    for s in starts:
        print("start:", {k: s.get(k) for k in ("segmentation", "segmentation_supported", "hand_pose", "seed_points",
                                                "model_points", "removed_points", "box_cm", "record_raw")})
    for r in rows:
        if ev(r) in ("handheld_suggested", "handheld_suggestion_taken"):
            print(rel(r), ev(r), {k: r.get(k) for k in ("path_m", "covered")})
    print("\npose quality and timing (every 5 s):")
    print("   time  frames  q_p50  q_p10  lost  recov  fused  track_ms p50/p95  copy_ms p95  turned  underside  state")
    for r in rows:
        if ev(r) == "handheld_stats":
            print(f"{rel(r)}  {r.get('frames', 0):6}  {r.get('quality_p50', '-'):>5}  {r.get('quality_p10', '-'):>5}  "
                  f"{r.get('lost', 0):4}  {r.get('recovered', 0):5}  {r.get('fused', 0):5}  "
                  f"{r.get('track_ms_p50', '-'):>6}/{r.get('track_ms_p95', '-'):<6}     {r.get('copy_ms_p95', '-'):>6}  "
                  f"{r.get('turned_deg', 0):5}°  {r.get('underside_cells', 0):9}  {r.get('state', '')}"
                  f"{'  (final)' if r.get('final') else ''}")
    lost = [r for r in rows if ev(r) in ("handheld_lost", "handheld_recovered")]
    print(f"\nlost / recovered events: {sum(ev(r) == 'handheld_lost' for r in lost)} lost, "
          f"{sum(ev(r) == 'handheld_recovered' for r in lost)} recovered")
    for r in lost:
        print(" ", rel(r), ev(r), {k: r.get(k) for k in ("quality", "after_s", "start", "points", "features")})
    beats = [r for r in rows if ev(r) == "heartbeat" and isinstance(r.get("frame_ms"), dict) and r["frame_ms"].get("p95")]
    after = [b for b in beats if t0 is None or (ts(b) or 0) >= t0]
    if after:
        p95 = [b["frame_ms"]["p95"] for b in after]
        mean = [b["frame_ms"]["mean"] for b in after]
        print(f"\ndisplay frame time while turning: mean {sum(mean) / len(mean):.1f} ms (~{1000 / (sum(mean) / len(mean)):.0f} fps), "
              f"p95 median {sorted(p95)[len(p95) // 2]:.1f} ms, worst p95 {max(p95):.1f} ms ({len(after)} heartbeats)")
    stills = [r for r in rows if ev(r) == "still"]
    rejected = [r for r in rows if ev(r) == "still_rejected"]
    print(f"\nstills: {len(stills)} taken, {len(rejected)} rejected (blurry)")
    if folder is not None:
        sd = folder / "stills"
        q, under, src = [], 0, {}
        for p in sorted(sd.glob("*.json")) if sd.exists() else []:
            s = json.loads(p.read_text())
            src[s.get("pose_source", "arkit")] = src.get(s.get("pose_source", "arkit"), 0) + 1
            if s.get("pose_quality") is not None:
                q.append(s["pose_quality"])
            if str(s.get("view_cell", "")).startswith("b"):
                under += 1
        if q:
            q.sort()
            print(f"  pose source {src}; pose quality min {q[0]:.2f} median {q[len(q) // 2]:.2f}; "
                  f"{sum(x < 0.55 for x in q)} marked unreliable; {under} of the underside")
        pkgs = list(folder.glob("scans/*/scan_package.json"))
        for p in pkgs:
            pkg = json.loads(p.read_text())
            kfs = pkg.get("keyframes") or []
            tracked = [k for k in kfs if k.get("pose_source") == "object_tracked"]
            print(f"  scan package: pose_source {pkg.get('pose_source')}, {len(kfs)} keyframes ({len(tracked)} object-tracked), "
                  f"{pkg.get('point_count')} points")
        raw = folder / "raw"
        if raw.exists():
            n = len(list(raw.glob("*.json"))) - 1
            print(f"  raw frames recorded: {n} (replay: tools/handheld/run.sh replay {raw})")
    fin = [r for r in rows if ev(r) == "finish"]
    if fin:
        f = fin[-1]
        print("\nfinish:", {k: f.get(k) for k in ("stills", "keyframes", "points_in_box", "total_s", "frame_ms")})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
