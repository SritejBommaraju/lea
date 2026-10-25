"""Assembly-check eval (backend/repairos/vision/assembly.py): does the check catch logical faults (unplugged, empty or
reversed battery, ribbon out, missing screw, cover not clipped) and stay quiet on scans that are fine?

    cd backend && $PY ../eval/assembly/run.py --labels L.json --images DIR [--scans scan_a,scan_b --db state.sqlite3
        --blobs data/blobs] --out ../eval/assembly/results/<name>.json [--max-calls 60]

Positives: public photos (licence-clean, used locally, never committed; `labels.json` holds the source, licence and
the fault's box), each run as a one-photo scan with no product name. Negatives: public photos of correctly assembled
parts, and real scans read only from a copy of the state database (the stills the pipeline would pick, with the
object words the scan's report had). The real Claude slot is used through its normal code path, so the spend guards
charge every call (point REPAIROS_DATA_DIR at a scratch folder to count this run's calls on their own).

Scoring, per photo:
  caught     an issue finding whose box meets the labelled fault box (padded by 5 % of the photo's long side)
  retake     no issue there, but a "take a closer photo" row with its box on the fault
  missed     neither
  false alarm  an issue finding anywhere on a negative, or away from the fault on a positive
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ISSUE = {"missing", "unseated", "loose", "reversed", "damaged", "broken", "shifted"}


def meets(a, b, pad: float) -> bool:
    return not (a[2] < b[0] - pad or a[0] > b[2] + pad or a[3] < b[1] - pad or a[1] > b[3] + pad)


def score(findings: list[dict], label_box, size) -> dict:
    pad = 0.05 * max(size)
    issues = [f for f in findings if f["state"] in ISSUE]
    at = [f for f in issues if label_box and meets(f["evidence_refs"]["crop_box_px"], label_box, pad)]
    retake = [f for f in findings if f["state"] == "insufficient_evidence" and label_box
              and (f.get("evidence_refs") or {}).get("crop_box_px")
              and meets(f["evidence_refs"]["crop_box_px"], label_box, pad)]
    return {"caught": bool(at), "retake": bool(retake) and not at, "false_alarms": len(issues) - len(at),
            "gaps": sum(1 for f in findings if f["state"] == "insufficient_evidence"
                        and not (f.get("evidence_refs") or {}).get("crop_box_px"))}


def brief(findings: list[dict]) -> list[dict]:
    return [{"state": f["state"], "lead": f["evidence"][0], "severity": f.get("severity"),
             "box": (f.get("evidence_refs") or {}).get("crop_box_px"),
             "close_look": next((e for e in f["evidence"] if e.startswith("vision model")), None)}
            for f in findings]


def calls_so_far() -> tuple[int, float]:
    from repairos import spend

    u = spend.usage()["services"]["anthropic"]
    return int(u.get("last_day") or 0), float(u.get("usd_day") or 0.0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--labels", required=True)
    ap.add_argument("--images", required=True)
    ap.add_argument("--scans", default="")
    ap.add_argument("--db", default=None)
    ap.add_argument("--blobs", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-calls", type=int, default=60)
    ap.add_argument("--only", default="", help="comma-separated label files or scan ids to run")
    a = ap.parse_args(argv)

    from repairos.registry import default_registry
    from repairos.vision.assembly import check_assembly, pick_stills
    from repairos.vision.crops import load_still

    judge = default_registry().slot("VLM")
    only = {x for x in a.only.split(",") if x}
    out_path = Path(a.out)
    rows = json.loads(out_path.read_text())["rows"] if out_path.exists() else []
    done = {r["case"] for r in rows}
    cases = []
    for lab in json.loads(Path(a.labels).read_text()):
        cases.append(("photo", lab["file"], lab))
    for sid in [x for x in a.scans.split(",") if x]:
        cases.append(("scan", sid, None))
    db = sqlite3.connect(a.db) if a.db else None
    for kind, case, lab in cases:
        if case in done or (only and case not in only):
            continue
        n0, usd0 = calls_so_far()
        if n0 + 4 > a.max_calls:
            print(f"stop: {n0} calls so far, the next case could pass --max-calls {a.max_calls}", file=sys.stderr)
            break
        t0 = time.monotonic()
        if kind == "photo":
            im = load_still(Path(a.images) / case)
            views = [({"still_id": Path(case).stem, "kind": "unguided"}, im)]
            words, size, label_box = None, im.size, lab.get("box")
        else:
            stills = [json.loads(r[0]) for r in db.execute("select json from rows where kind='still' and scan_id=?",
                                                           (case,))]
            rep = db.execute("select json from rows where kind='report' and scan_id=?", (case,)).fetchone()
            obj = (json.loads(rep[0]).get("object") or {}) if rep else {}
            words = " ".join(str(obj[k]) for k in ("brand", "model", "category") if obj.get(k)) or None
            views = []
            for s in pick_stills(stills, 4):
                sha = s["image_file"].split(":")[-1]
                views.append((s, load_still(Path(a.blobs) / sha[:2] / sha)))
            size, label_box = views[0][1].size, None
        res = check_assembly(views, scan_id=f"eval_{Path(case).stem}", judge=judge, object_words=words)
        n1, usd1 = calls_so_far()
        sc = score(res.findings, label_box, size)
        rows.append({"case": case, "kind": kind, "fault": lab["fault"] if lab else "none (real scan)",
                     "object_words": words, **sc, "calls": n1 - n0, "usd": round(usd1 - usd0, 4),
                     "seconds": round(time.monotonic() - t0, 1), "record": res.record, "findings": brief(res.findings)})
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({"rows": rows}, indent=1))
        print(f"{case}: {rows[-1]['fault']} caught={sc['caught']} retake={sc['retake']} "
              f"false_alarms={sc['false_alarms']} gaps={sc['gaps']} calls={n1 - n0}", flush=True)
    n, usd = calls_so_far()
    print(f"Claude calls charged this run (spend DB): {n}, about {usd:.3f} USD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
