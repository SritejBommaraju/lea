#!/usr/bin/env python3
"""build/status.py: write build/STATUS.md: what is done, in progress, blocked (and on what), what is left, and
whether the demo still fits in the hours left.

Run:  python3 -I build/status.py [--track demo|later|all] [--hours-left 24] [--profile NAME] [--offline] [--stdout]

Default: the demo track (the 24-hour path, BUILD.md §0) under the active profile (hackathon unless
REPAIROS_PROFILE / --profile says otherwise). Sections: demo totals; critical path in HOURS and whether it fits in
--hours-left; load per lane; ready now; waiting and why; Grain targets with their fallbacks; human steps; module
slots for the profile; optional and stretch work (tracked separately, never "unfinished"); the later track; the
full-build critical path (agent-days); the global "fully works" checklist. Agents run it at the start and end of a
session; the file lands through the card's PR.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402

OUT = _lib.ROOT / "build" / "STATUS.md"


def count(cards):
    d = {s: 0 for s in _lib.STATUSES}
    for c in cards:
        d[c["status"]] += 1
    return d


def slot_state(data, providers, sid, track):
    """(ok, interface cards, counted chosen cards, optional/stretch chosen cards) for one slot on a track.
    ok = every interface card done + at least one chosen non-optional card + all of those done.
    Optional extras (e.g. M.STATE.9) never keep a slot unfinished."""
    sel = providers["slots"].get(sid)
    cards = [c for c in data["cards"] if c["kind"] == "module" and c["slot"] == sid and _lib.in_track(c, track)]
    iface = [c for c in cards if c["impl"] == "interface"]
    chosen = [c for c in cards if c["impl"] == sel]
    counted = [c for c in chosen if _lib.counts_toward_done(c)]
    extra = [c for c in chosen if not _lib.counts_toward_done(c)]
    ok = all(c["status"] == "done" for c in iface) and bool(counted) and all(c["status"] == "done" for c in counted)
    return ok, iface, counted, extra


def summarize(data, providers, track="demo", hours_left=24.0, checks=None, offline=False):
    """Everything status.md shows, as data (tests use this)."""
    by_id = data["by_id"]
    steps = {h["id"]: h for h in data["human_steps"]}
    decided = _lib.decided_questions(data)
    scope = [c for c in data["cards"] if _lib.in_scope(c, providers, track)]
    counted = [c for c in scope if _lib.counts_toward_done(c)]
    extras = [c for c in scope if not _lib.counts_toward_done(c)]
    need = _lib.required_human_steps(data, providers, track=track)
    if checks is None:
        checks = _lib.run_checks({steps[h]["check"] for h in need if h in steps}, offline=offline)

    def human_ok(h):
        return _lib.human_step_satisfied(steps[h], checks)

    cp_total, cp_chain = _lib.critical_path(data, providers, track)
    remaining = sum(_lib.card_cost(c, track) for c in counted)
    lanes = []
    for lane in data["lanes"]:
        lc = [c for c in counted if c["owner_lane"] == lane["id"]]
        left = sum(_lib.card_cost(c, track) for c in lc)
        lanes.append({"id": lane["id"], "people": lane.get("people", ""), "name": lane.get("name", ""),
                      "cards_left": sum(1 for c in lc if c["status"] != "done"), "hours_left": left,
                      "in_progress": [c["id"] for c in lc if c["status"] in ("claimed", "in_review")],
                      "fits": left <= hours_left})

    ready, waiting = [], []
    for c in counted + extras:
        if c["status"] in ("done", "claimed", "in_review"):
            continue
        deps = [d for d in c["depends_on"] if by_id[d]["status"] != "done"]
        hm = [h for h in c["human"] if human_ok(h)[0] is False]
        why = []
        if c["status"] == "blocked":
            why.append("status: blocked")
        if deps:
            why.append("deps: " + ", ".join(deps[:4]) + (f" +{len(deps) - 4}" if len(deps) > 4 else ""))
        if hm:
            why.append("human: " + ", ".join(hm))
        openq = [q for q in c["questions"] if q not in decided]
        if openq:
            why.append("open SPEC §11: " + ", ".join(openq))
        (waiting if why else ready).append((c, why))

    slots = []
    for s in data["slots"]:
        ok, iface, cnt, extra = slot_state(data, providers, s["id"], track)
        slots.append({"id": s["id"], "required": s["required"], "selected": providers["slots"].get(s["id"]),
                      "ok": ok, "iface": iface, "counted": cnt, "extra": extra})

    tot = count(counted)
    done_all = all(c["status"] == "done" for c in counted)
    return {
        "track": track, "profile": providers["profile"], "hours_left": hours_left, "scope": scope,
        "counted": counted, "extras": extras, "totals": tot, "remaining": remaining,
        "cp_total": cp_total, "cp_chain": cp_chain,
        "fits": cp_total <= hours_left if track == "demo" else None, "lanes": lanes,
        "ready": ready, "waiting": waiting, "slots": slots, "need": need, "human_ok": human_ok,
        "complete": done_all,
    }


def render(data, providers, s, offline=False):
    by_id = data["by_id"]
    steps = {h["id"]: h for h in data["human_steps"]}
    track = s["track"]
    unit = "h" if track == "demo" else "agent-days"
    L = []
    w = L.append
    w("# RepairOS build status\n")
    w(f"Generated {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by `python3 -I build/status.py`"
      f"{' (offline)' if offline else ''}: profile **{s['profile']}**, track **{track}**"
      + (f", **{s['hours_left']:g} h left**" if track == "demo" else "")
      + ". Do not edit by hand; regenerate and commit through your card's PR.\n")
    errs = _lib.validate_tasks(data)
    if errs:
        w("## Registry errors\n")
        for e in errs:
            w(f"- {e}")
        w("")

    t = s["totals"]
    title = "The 24-hour demo path" if track == "demo" else f"Track `{track}`"
    w(f"## {title}\n")
    w("| Cards (counted) | Done | In progress | Blocked | Todo | Remaining | Optional / stretch (separate) |")
    w("|---|---|---|---|---|---|---|")
    w(f"| {len(s['counted'])} | {t['done']} | {t['claimed'] + t['in_review']} | {t['blocked']} | {t['todo']} | "
      f"{s['remaining']:g} {unit} | {len(s['extras'])} |")
    w("")
    chain = " -> ".join(f"`{c}` ({_lib.card_cost(by_id[c], track):g})" for c in s["cp_chain"]) or "nothing left"
    w(f"**Critical path: {s['cp_total']:g} {unit}**: {chain}\n")
    if track == "demo":
        slack = s["hours_left"] - s["cp_total"]
        if s["fits"]:
            w(f"**Fits**: {s['cp_total']:g} h of chained work in {s['hours_left']:g} h left, {slack:g} h slack "
              "(the demo dry run, backup video and final run sit in that slack; BUILD.md §0.A).\n")
        else:
            w(f"**Does NOT fit**: {s['cp_total']:g} h of chained work but {s['hours_left']:g} h left "
              f"(over by {-slack:g} h). Apply the cut list in BUILD.md §0.F now.\n")
        w("Load per lane (hours of unfinished demo cards; a lane is one person plus agents):\n")
        w("| Lane | People | Cards left | Hours left | In progress | Fits |")
        w("|---|---|---|---|---|---|")
        for ln in s["lanes"]:
            w(f"| {ln['id']} | {ln['people']} | {ln['cards_left']} | {ln['hours_left']:g} | "
              f"{', '.join(ln['in_progress']) or '-'} | {'yes' if ln['fits'] else '**no**'} |")
        w("")
        w(f"**Demo path complete: {'YES' if s['complete'] else 'NOT YET'}.**\n")

    w("## Ready now (deps done, todo; human checks applied)\n")
    if s["ready"]:
        w("| Card | Lane | Est. h | Title |")
        w("|---|---|---|---|")
        for c, _ in sorted(s["ready"], key=lambda cw: (bool(cw[0]["stretch"]), cw[0]["owner_lane"] or "", cw[0]["id"])):
            est = "" if c["estimate_hours"] is None else f"{c['estimate_hours']:g}"
            w(f"| {c['id']}{' (stretch)' if c['stretch'] else ''} | {c['owner_lane'] or '-'} | {est} | {c['title']} |")
    else:
        w("- none")
    w("")

    w("## Waiting, and on what\n")
    if s["waiting"]:
        w("| Card | Lane | Waiting on |")
        w("|---|---|---|")
        for c, why in sorted(s["waiting"], key=lambda cw: (cw[0]["owner_lane"] or "", cw[0]["id"])):
            w(f"| {c['id']} | {c['owner_lane'] or '-'} | {' / '.join(why)} |")
    else:
        w("- none")
    w("")

    w("## Grain changes and targets the demo uses (S2.7-S2.11 must land; BUILD.md §0.D)\n")
    gmap = {}
    for c in s["counted"]:
        if c["status"] != "done":
            for g in c["grain_needs"]:
                gmap.setdefault(g, []).append(c["id"])
    if gmap:
        for g in sorted(gmap):
            w(f"- {g}: {', '.join(sorted(gmap[g]))}")
    else:
        w("- none")
    w("")

    w("## Human steps outstanding (this profile and track)\n")
    any_h = False
    for h, cards in sorted(s["need"].items()):
        ok, detail = s["human_ok"](h)
        if ok is True:
            continue
        any_h = True
        st = "NOT MET" if ok is False else "unverified"
        shown = sorted(cards)
        w(f"- **{h}** ({st}): {steps[h]['what']}. Blocks {', '.join(shown[:10])}"
          f"{f' +{len(shown) - 10} more' if len(shown) > 10 else ''}. How: {steps[h]['how']}")
    if not any_h:
        w("- none")
    w("")

    w(f"## Module slots (profile `{s['profile']}`)\n")
    w("| Slot | Required | Selected | Interface cards | Selected impl cards | Optional extras (not counted) | Slot done |")
    w("|---|---|---|---|---|---|---|")
    for sl in s["slots"]:
        f = lambda cs: ", ".join(f"{c['id']} ({c['status']})" for c in cs) or "-"  # noqa: E731
        w(f"| {sl['id']} | {'yes' if sl['required'] else 'no'} | `{sl['selected']}` | {f(sl['iface'])} | "
          f"{f(sl['counted'])} | {f(sl['extra'])} | {'yes' if sl['ok'] else 'no'} |")
    w("")

    w("## Optional and stretch work (tracked separately; never keeps the build unfinished)\n")
    if s["extras"]:
        for c in s["extras"]:
            kind = "stretch" if c["stretch"] else "optional"
            w(f"- {c['id']} ({kind}, {c['status']}): {c['title']}")
    else:
        w("- none")
    w("")

    if track == "demo":
        later = [c for c in data["cards"] if c["track"] == "later"]
        lc = count(later)
        w("## Later track (after the hackathon; all cards stay in the registry)\n")
        w(f"{len(later)} cards: {lc['done']} done, {lc['todo']} todo, {lc['blocked']} blocked. "
          "Run `python3 -I build/status.py --track all --profile full` for the full build.\n")
        cp_all, chain_all = _lib.critical_path(data, _lib.resolve_profile(dict(providers), "full", {})
                                               if providers.get("profiles") else providers, "all")
        w(f"Full build critical path (profile `full`, all tracks): about **{cp_all:g} agent-days**: "
          + " -> ".join(f"`{c}`" for c in chain_all) + "\n")

    w("## Fully works checklist (global definition of done, BUILD.md §9; after the hackathon)\n")
    w("| # | Item | Met | Missing |")
    w("|---|---|---|---|")
    all_met = True
    for item in data["done_checklist"]:
        missing = [r for r in item.get("requires", []) if by_id[r]["status"] != "done"]
        ev = item.get("evidence")
        ev_missing = ev if ev and not (_lib.ROOT / ev).exists() else None
        met = not missing and not ev_missing
        all_met &= met
        miss = ", ".join(missing) + (f"; evidence file {ev_missing}" if ev_missing else "")
        w(f"| {item['id']} | {item['text']} | {'yes' if met else 'no'} | {miss or '-'} |")
    w("")
    w(f"**RepairOS fully works: {'YES' if all_met else 'NOT YET'}.**\n")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--track", choices=["demo", "later", "all"], default=_lib.DEFAULT_TRACK)
    ap.add_argument("--hours-left", type=float, default=24.0, help="wall-clock hours until the demo (default 24)")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--stdout", action="store_true", help="print instead of writing build/STATUS.md")
    args = ap.parse_args()

    data = _lib.load_tasks()
    providers = _lib.load_providers(profile=args.profile)
    s = summarize(data, providers, track=args.track, hours_left=args.hours_left, offline=args.offline)
    text = render(data, providers, s, offline=args.offline)
    if args.stdout:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(text)
    else:
        OUT.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {OUT.relative_to(_lib.ROOT)}")
        if args.track == "demo":
            verdict = "fits" if s["fits"] else "DOES NOT FIT"
            print(f"demo critical path {s['cp_total']:g} h vs {args.hours_left:g} h left: {verdict}; "
                  f"{s['remaining']:g} h of demo work remaining across {len(s['lanes'])} lanes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
