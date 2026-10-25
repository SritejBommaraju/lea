#!/usr/bin/env python3
"""build/next.py: the tasks you can start now, per stream.

Run:  python3 -I build/next.py [--stream app|modeling|build] [--offline] [--json]

READY = status `todo`, every `depends_on` task done, and no human prerequisite known to be missing (steps a tool
cannot check are shown as "confirm"). WAITING = deps done but a human step is missing, or status `blocked`.
Tasks with status `doing` are in progress on their `branch` and never listed as ready.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402


def branch(task: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task["title"].lower()).strip("-")[:40].rstrip("-")
    return f"<you>/{task['id'].lower()}-{slug}"


def plan(data, stream=None, results=None, offline=False, env=None):
    """{stream id: {"ready": [rows], "waiting": [rows]}}. `results` (step id -> (ok, detail)) skips real checks."""
    by_id = data["by_id"]
    cands = [t for t in _lib.open_tasks(data, stream) if t["status"] != "doing" and _lib.deps_done(t, by_id)]
    if results is None:
        results = _lib.check_steps(data, {h for t in cands for h in t["human"]}, offline=offline, env=env)
    out = {s["id"]: {"ready": [], "waiting": []} for s in data["streams"] if stream in (None, s["id"])}
    for t in cands:
        missing = [h for h in t["human"] if results.get(h, (False,))[0] is False]
        confirm = [h for h in t["human"] if results.get(h, (False,))[0] is None]
        row = {"id": t["id"], "title": t["title"], "stream": t["stream"], "done_when": t["done_when"],
               "owns": t["owns"], "missing": missing, "confirm": confirm, "status": t["status"],
               "branch": branch(t)}
        out[t["stream"]]["waiting" if missing or t["status"] == "blocked" else "ready"].append(row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", choices=["app", "modeling", "build"])
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    data = _lib.load_tasks()
    errs = _lib.validate_tasks(data)
    if errs:
        print("build/tasks.yaml has errors; fix them first:", *errs, sep="\n  - ", file=sys.stderr)
        return 2
    result = plan(data, args.stream, offline=args.offline)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    names = {s["id"]: s["name"] for s in data["streams"]}
    for sid, groups in result.items():
        print(f"{names[sid]}: {len(groups['ready'])} ready")
        for r in groups["ready"]:
            note = f"  (confirm: {', '.join(r['confirm'])})" if r["confirm"] else ""
            print(f"  {r['id']:<5} {r['title'][:90]}{note}")
        for r in groups["waiting"]:
            why = "blocked" if r["status"] == "blocked" else "HUMAN ACTION NEEDED: " + ", ".join(r["missing"])
            print(f"  {r['id']:<5} waiting ({why}): {r['title'][:60]}")
        print()
    print("Start: git fetch origin && git switch -c <you>/<id>-<slug> origin/main; the PR title starts with the id.")
    print("Prerequisite detail: python3 -I build/doctor.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
