#!/usr/bin/env python3
"""build/doctor.py: check the prerequisites the open tasks need and print what a human must do.

Run:  python3 -I build/doctor.py [--offline] [--stream app|modeling|build] [--tasks A2,M7] [--json]

Reads backend/.env for keys and never prints values. --offline skips network calls. Exit 0 when nothing blocks,
1 when a HUMAN ACTION NEEDED item or a registry error is left.

Agents: run this before starting a task. If it prints HUMAN ACTION NEEDED, stop and ask your human.
Never fake, stub or mock around a missing prerequisite (PLAN.md invariant GEN3).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402

MARK = {True: "ok  ", False: "FAIL", None: "?   "}


def collect(data, stream=None, task_ids=None, offline=False, env=None):
    """[(step id, ok, detail, [task ids])] for every human step the open tasks need, and the subset not ok."""
    need = _lib.required_human_steps(data, task_ids=task_ids, stream=stream)
    results = _lib.check_steps(data, need, offline=offline, env=env)
    rows = [(h, *results[h], sorted(need[h])) for h in sorted(need)]
    return rows, [r for r in rows if r[1] is not True]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="skip network reachability checks")
    ap.add_argument("--stream", choices=["app", "modeling", "build"])
    ap.add_argument("--tasks", default="", help="comma-separated task ids to scope the check")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    data = _lib.load_tasks()
    errs = _lib.validate_tasks(data)
    only = set(filter(None, args.tasks.split(","))) or None
    rows, actions = collect(data, args.stream, only, args.offline)
    steps = {h["id"]: h for h in data["human_steps"]}
    code = 1 if actions or errs else 0

    if args.json:
        print(json.dumps({"registry_errors": errs, "steps": [{"id": h, "ok": ok, "detail": d, "blocks": t}
                                                             for h, ok, d, t in rows],
                          "human_action_needed": [r[0] for r in actions]}, indent=2))
        return code

    print("LEA doctor" + (" (offline)" if args.offline else ""))
    print("=" * 72)
    for e in errs:
        print("REGISTRY ERROR (build/tasks.yaml):", e)
    for h, ok, detail, _ in rows:
        print(f"  [{MARK[ok]}] {h:<16} {detail}")
    print()
    if not actions:
        print("All prerequisites met. Next: python3 -I build/next.py")
        return code
    print("HUMAN ACTION NEEDED")
    print("-" * 72)
    for i, (h, ok, detail, tasks) in enumerate(actions, 1):
        print(f"{i}. {steps[h]['what']}")
        print(f"   status: {'not met' if ok is False else 'cannot be checked by a tool; confirm with the human'}"
              f" ({detail})")
        print(f"   blocks: {', '.join(tasks)}")
        print(f"   how:    {steps[h]['how']}")
    print()
    print("Agents: work on a task these do not block (build/next.py). Never fake a missing service or device.")
    return code


if __name__ == "__main__":
    sys.exit(main())
