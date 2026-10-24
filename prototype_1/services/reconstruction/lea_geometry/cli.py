"""lea-qa: run geometry QA and write report.json, aligned_mm.ply (mesh, reference frame) and grain_points.ply."""
import argparse
import json
import sys
from pathlib import Path

import open3d as o3d

from .qa import BOX_AXES, run_qa, write_grain_ply


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lea-qa")
    ap.add_argument("--scan", required=True)
    ap.add_argument("--reference")
    ap.add_argument("--reference-units-per-mm", type=float, default=1.0, help="1 for a mm reference, 0.001 for metres")
    ap.add_argument("--anchor", choices=BOX_AXES, required=True)
    ap.add_argument("--anchor-mm", type=float, required=True)
    ap.add_argument("--holdout", choices=BOX_AXES, required=True)
    ap.add_argument("--max-fit-rms-mm", type=float, required=True)
    ap.add_argument("--engine", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        result, mesh = run_qa(a.scan, a.reference, a.reference_units_per_mm, a.anchor, a.anchor_mm, a.holdout,
                              a.max_fit_rms_mm, a.engine)
    except (ValueError, IOError) as e:
        print(json.dumps({"error": str(e)}))
        return 2
    o3d.io.write_triangle_mesh(str(out / "aligned_mm.ply"), mesh, write_ascii=False)
    points = write_grain_ply(mesh, out / "grain_points.ply")
    (out / "report.json").write_text(result.to_json())
    print(json.dumps({"report": str(out / "report.json"), "aligned_mesh": str(out / "aligned_mm.ply"),
                      "grain_ply": str(out / "grain_points.ply"), "grain_points": points}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
