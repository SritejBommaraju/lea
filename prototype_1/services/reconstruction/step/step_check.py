"""Re-import a STEP file with OpenCascade and report what is really in it.

A file named .step proves nothing; this reports solid count, B-rep validity, volume and the bounding
box in mm, and, when expected dimensions are given (--expect NAME=MM:TOL ...), whether the sorted box
extents match. Prints one JSON object.
"""
import argparse
import json
import sys

from build123d import import_step
from OCP.BRepCheck import BRepCheck_Analyzer


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step")
    ap.add_argument("--expect", action="append", default=[], help="longest|middle|shortest=MM:TOL")
    a = ap.parse_args(argv)
    try:
        shape = import_step(a.step)
    except Exception as e:  # unreadable file is a result, not a crash
        print(json.dumps({"ok": False, "error": f"STEP import failed: {e}"}))
        return 0
    solids = shape.solids()
    valid = all(BRepCheck_Analyzer(s.wrapped).IsValid() for s in solids) if solids else False
    bb = shape.bounding_box()
    size = sorted([bb.size.X, bb.size.Y, bb.size.Z], reverse=True)
    checks = []
    for spec in a.expect:
        name, rest = spec.split("=")
        mm, tol = (float(x) for x in rest.split(":"))
        got = size[("longest", "middle", "shortest").index(name)]
        checks.append({"dimension": name, "expected_mm": mm, "tolerance_mm": tol, "measured_mm": round(got, 3), "ok": abs(got - mm) <= tol})
    print(json.dumps({
        "ok": bool(solids) and valid and all(c["ok"] for c in checks),
        "solids": len(solids), "valid": valid, "volume_mm3": round(sum(s.volume for s in solids), 3),
        "bbox_mm": [round(v, 3) for v in size], "dimension_checks": checks,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
