#!/usr/bin/env python3
"""One-screen summary of a scan from the phone's diag.jsonl: how long it took, what it kept, and whether it said
"that's enough", per capture segment (0 = the object as it stands, 1+ = the bottom), against the targets.

Usage: python3 -I tools/scan_summary.py <diag.jsonl>
Pull it read-only: xcrun devicectl device copy from --device <udid> --domain-type appDataContainer
  --domain-identifier com.rohankilaru.repairos --source Documents/sessions/<id>/diag.jsonl --destination diag.jsonl
"""
from __future__ import annotations

import json
import sys

# What a small-object scan should look like (main segment).
TARGETS = {"tap_to_enough_s": 45, "stills": (15, 25), "keyframes": (40, 60), "frame_p95_ms": 18.0}


def main(path: str) -> int:
    events = [json.loads(line) for line in open(path) if line.strip()]
    segments: dict[int, dict] = {}
    seg = 0
    for e in events:
        ev = e.get("event")
        if ev == "scan_start":
            seg = e.get("segment", 0)
            segments.setdefault(seg, {"start_ms": e["ms"], "stills": 0, "dropped": 0, "replaced": 0, "rejected": 0, "p95": []})
        s = segments.setdefault(seg, {"start_ms": e.get("ms", 0), "stills": 0, "dropped": 0, "replaced": 0, "rejected": 0, "p95": []})
        if ev == "scan_tapped":
            s["tap_ms"] = e["ms"]
        elif ev == "lock" and "lock_ms_at" not in s:
            s["lock_ms_at"] = e["ms"]
            s["size_cm"] = e.get("size_cm")
        elif ev == "still":
            s["stills"] += 1
        elif ev == "still_dropped":
            s["dropped"] += 1
        elif ev == "still_replaced":
            s["replaced"] += 1
        elif ev == "still_rejected":
            s["rejected"] += 1
        elif ev == "enough":
            s["enough_ms"] = e["ms"]
            s["enough"] = e
        elif ev == "heartbeat" and e.get("frame_ms", {}).get("p95"):
            s["p95"].append(e["frame_ms"]["p95"])
            if "selection" in e:
                s["last_selection"] = e["selection"]
        elif ev == "finish":
            s["finish_ms"] = e["ms"]
            s["finish"] = e
    for n, s in sorted(segments.items()):
        tap = s.get("tap_ms", s["start_ms"])
        fin = s.get("finish", {})
        sel = fin.get("selection") or s.get("last_selection") or {}
        print(f"\nsegment {n}  ({'bottom' if n else 'as it stands'})  box {s.get('size_cm')} cm")
        if "enough_ms" in s:
            print(f"  that's enough after   {(s['enough_ms'] - tap) / 1000:.1f} s   (target ≤ {TARGETS['tap_to_enough_s']} s for a small object)")
        else:
            print(f"  that's enough         never   (progress at Done {sel.get('progress_pct', '?')} %)")
        if "finish_ms" in s:
            print(f"  tap to Done           {(s['finish_ms'] - tap) / 1000:.1f} s")
        print(f"  stills kept           {fin.get('stills', s['stills'])}   (target {TARGETS['stills'][0]}–{TARGETS['stills'][1]}); "
              f"dropped as not better {s['dropped']}, replaced {s['replaced']}, blurry {s['rejected']}")
        kf = sel.get("keyframes_kept", fin.get("keyframes"))
        print(f"  keyframes kept        {kf}   (target {TARGETS['keyframes'][0]}–{TARGETS['keyframes'][1]}); dropped {sel.get('keyframes_dropped', {})}")
        if sel:
            print(f"  views {sel.get('views')}  faces {sel.get('faces')}  mesh {sel.get('mesh')}  side sectors {sel.get('side_sectors')}"
                  f"  top {sel.get('top_sectors')}  keyframe sectors {sel.get('keyframe_sectors')}  lined up {sel.get('lined_up')}")
        p95 = max(s["p95"]) if s["p95"] else None
        print(f"  frame p95 (worst)     {p95} ms   (60 fps ≈ 16.7; target ≤ {TARGETS['frame_p95_ms']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
