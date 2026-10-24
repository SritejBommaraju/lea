"""python -m repairos.recon build --segment top=<images dir> [--segment bottom=<images dir>] --out <dir>
[--no-apple] [--detail reduced] [--max-memory 6] [--min-free 25]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from repairos.recon.build import SegmentInput, build
from repairos.recon.cli import MemoryGuardError, ReconCliError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m repairos.recon")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="merged mesh + per-still poses in the first segment's ARKit frame")
    b.add_argument("--segment", action="append", required=True, metavar="NAME=DIR",
                   help="an Object Capture image folder; the first is the reference (top)")
    b.add_argument("--out", required=True, type=Path)
    b.add_argument("--no-apple", action="store_true", help="skip PhotogrammetrySession (LiDAR TSDF + features only)")
    b.add_argument("--detail", default="reduced", choices=["none", "preview", "reduced", "medium", "full"])
    b.add_argument("--max-memory", type=float, default=6.0, help="GB; abort when a step's RSS exceeds it")
    b.add_argument("--min-free", type=float, default=25.0, help="%% free memory needed before starting")
    a = ap.parse_args(argv)
    segs = []
    for s in a.segment:
        name, _, d = s.partition("=")
        if not d:
            name, d = f"seg{len(segs)}", s
        segs.append(SegmentInput(Path(d).expanduser(), name))
    try:
        res = build(segs, a.out, apple=not a.no_apple, detail=a.detail, max_memory_gb=a.max_memory,
                    min_free_pct=a.min_free)
    except (MemoryGuardError, ReconCliError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps({"ok": True, "out": str(res.out_dir), "package": res.package, "timings_s": res.timings,
                      "merges": [{k: m[k] for k in ("segment", "merged", "source", "agreement", "reasons")}
                                 for m in res.report.get("merges", [])]}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
