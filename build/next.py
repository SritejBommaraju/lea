#!/usr/bin/env python3
"""build/next.py: list the cards an agent can safely claim right now.

Run:  python3 -I build/next.py [--track demo|later|all] [--stream app] [--phase1] [--lane L2] [--profile NAME] [--all] [--gh] [--offline]
                               [--phase P2] [--kind core|module] [--json]

A card is READY when:
  1. status is `todo` (a `blocked` card stays out until someone clears it back to `todo`);
  2. every `depends_on` card is `done`;
  3. it is on the track (default `demo`, the 24-hour path) and in play for the active profile: a core card, a slot
     interface card, the selected implementation or an `always` card (unselected implementations are hidden
     unless --all);
  4. its human prerequisites pass build/doctor.py's checks (steps that cannot be checked are shown as a warning);
  5. none of its `owns` globs overlap the `owns` of a card that is `claimed` or `in_review`;
  6. with --gh: no open GitHub issue labelled `card` with the same id already has an assignee.

Output: a table sorted by critical-path weight (hours on the demo track, agent-days otherwise), with the estimate
and the owner lane. The footer says how to claim.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402


def slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:40].rstrip("-")


def plan(data, providers, track="demo", show_all=False, lane=None, phase=None, kind=None, checks=None,
         offline=False, claims=None, stream=None, phase1=False):
    """Return (ready, waiting) rows. `checks` (check id -> (ok, detail)) skips running the real checks (tests)."""
    by_id = data["by_id"]
    steps = {h["id"]: h for h in data["human_steps"]}
    claims = claims or {}
    weight = _lib.downstream_weight(data, providers, track)
    decided = _lib.decided_questions(data)
    active = [c for c in data["cards"] if c["status"] in ("claimed", "in_review")]

    candidates = []
    for c in data["cards"]:
        if c["status"] not in ("todo", "blocked"):
            continue
        if not _lib.in_track(c, track):
            continue
        if phase and c["phase"] != phase:
            continue
        if kind and c["kind"] != kind:
            continue
        if lane and c["owner_lane"] != lane:
            continue
        if stream and c.get("stream") != stream:
            continue
        if phase1 and not c["phase1"]:
            continue
        in_play = _lib.card_is_selected(c, providers)
        if not in_play and not show_all:
            continue
        if any(by_id[d]["status"] != "done" for d in c["depends_on"]):
            continue
        candidates.append((c, in_play))

    if checks is None:
        need = _lib.required_human_steps(data, providers, only_card_ids={c["id"] for c, _ in candidates}, track=track)
        checks = _lib.run_checks({steps[h]["check"] for h in need if h in steps}, offline=offline)

    ready, waiting = [], []
    for c, in_play in candidates:
        missing, unverified = [], []
        for h in c["human"]:
            ok, _ = _lib.human_step_satisfied(steps[h], checks)
            if ok is False:
                missing.append(h)
            elif ok is None:
                unverified.append(h)
        overlaps = [f"{o['id']}: {g1} ~ {g2}" for o in active for g1, g2 in _lib.owns_overlap(c, o)]
        gh = claims.get(c["id"])
        row = {
            "id": c["id"], "phase": c["phase"], "kind": c["kind"], "slot": c["slot"], "impl": c["impl"],
            "title": c["title"], "track": c["track"], "lane": c["owner_lane"], "stream": c.get("stream"), "estimate_hours": c["estimate_hours"],
            "weight": round(weight(c["id"]), 2), "unit": "h" if track == "demo" else "d", "in_play": in_play,
            "stretch": bool(c["stretch"]), "optional": bool(c["optional"]), "status": c["status"],
            "human_missing": missing, "human_unverified": unverified, "file_overlap": overlaps,
            "gh_issue": gh["number"] if gh else None, "gh_assignees": gh["assignees"] if gh else [],
            "questions_open": [q for q in c["questions"] if q not in decided],
            "branch": f"<you>/{c['id'].lower().replace('.', '-')}-{slug(c['title'])}",
        }
        if c["status"] == "blocked" or missing or overlaps or (gh and gh["assignees"]):
            waiting.append(row)
        else:
            ready.append(row)
    key = lambda r: (r["stretch"] or r["optional"], -r["weight"], r["phase"], r["id"])  # noqa: E731
    ready.sort(key=key)
    waiting.sort(key=key)
    return ready, waiting


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", choices=["demo", "later", "all"], default=_lib.DEFAULT_TRACK)
    ap.add_argument("--profile", default=None)
    ap.add_argument("--stream", default=None, help="only this work stream (app, modeling, build, grain; areas/README.md)")
    ap.add_argument("--phase1", action="store_true", help="only Phase 1 cards (no Grain; BUILD.md Phases)")
    ap.add_argument("--lane", default=None, help="only this owner lane (L1..L6, aliases inside the streams)")
    ap.add_argument("--all", action="store_true", help="include unselected module implementations")
    ap.add_argument("--gh", action="store_true", help="also consult open GitHub issues labelled `card`")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--phase", default=None)
    ap.add_argument("--kind", choices=["core", "module"], default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    data = _lib.load_tasks()
    errs = _lib.validate_tasks(data)
    if errs:
        print("build/tasks.yaml has errors; fix them first:", file=sys.stderr)
        for e in errs:
            print("  -", e, file=sys.stderr)
        return 2
    providers = _lib.load_providers(profile=args.profile)
    claims = _lib.gh_issue_claims() if args.gh else {}
    ready, waiting = plan(data, providers, track=args.track, show_all=args.all, lane=args.lane, phase=args.phase,
                          kind=args.kind, offline=args.offline, claims=claims, stream=args.stream,
                          phase1=args.phase1)

    if args.json:
        print(json.dumps({"profile": providers["profile"], "track": args.track, "ready": ready, "waiting": waiting},
                         indent=2))
        return 0

    unit = "hours" if args.track == "demo" else "agent-days"
    print(f"Ready to claim now ({len(ready)}); profile {providers['profile']}, track {args.track}; cp = critical-path "
          f"weight in {unit}")
    print("=" * 110)
    print(f"{'card':<12}{'lane':<5}{'est h':>6}{'cp':>7}  title")
    for r in ready:
        flags = []
        if r["human_unverified"]:
            flags.append("confirm: " + ",".join(r["human_unverified"]))
        if r["questions_open"]:
            flags.append("open: " + ",".join(r["questions_open"]))
        if r["stretch"]:
            flags.append("stretch: only if the core path is done by ~H16")
        if not r["in_play"]:
            flags.append("optional implementation, not selected")
        est = "" if r["estimate_hours"] is None else f"{r['estimate_hours']:g}"
        print(f"{r['id']:<12}{(r['lane'] or '-'):<5}{est:>6}{r['weight']:>7g}  {r['title'][:78]}")
        if flags:
            print(f"{'':<30}({'; '.join(flags)})")
    if waiting:
        print()
        print(f"Deps done but NOT claimable yet ({len(waiting)}):")
        for r in waiting:
            why = []
            if r["status"] == "blocked":
                why.append("status: blocked (set it back to todo in tasks.yaml when cleared)")
            if r["human_missing"]:
                why.append("human: " + ",".join(r["human_missing"]))
            if r["file_overlap"]:
                why.append("files overlap " + "; ".join(r["file_overlap"])[:60])
            if r["gh_assignees"]:
                why.append(f"issue #{r['gh_issue']} assigned to {','.join(a for a in r['gh_assignees'] if a)}")
            print(f"  {r['id']:<12} {(r['lane'] or '-'):<4} {r['title'][:56]:<56}  <- {'; '.join(why)}")
    print()
    print("To claim: say the card id and your branch in your stream's channel, then")
    print("          git fetch origin && git switch -c <you>/<id>-<slug> origin/main   (loop: AGENTS.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
