#!/usr/bin/env python3
"""build/doctor.py: check the prerequisites the active profile + track need, and print what a human must do.

Run:  python3 -I build/doctor.py [--offline] [--json] [--cards ID,ID] [--track demo|later|all] [--profile NAME]

- Profile: config/providers.yaml `profiles:` (default `hackathon`; env REPAIROS_PROFILE or --profile picks another;
  REPAIROS_SLOT_* overrides single slots). Only the SELECTED implementation's cards contribute prerequisites,
  plus core cards, slot interfaces and `always` cards (replay double, rules fallback).
- Track: `demo` (default) checks only cards on the 24-hour path (BUILD.md §0); `all` checks every card.
- Reads backend/.env for keys. Never prints values.
- --offline skips network calls (Grain /health, Jev, SpacetimeDB /v1/ping).
- --cards limits the human-step list to the cards you intend to claim.
- Exit code 0 when nothing blocks the selected configuration, 1 otherwise.

Agents: run this before claiming a card. If it prints HUMAN ACTION NEEDED, stop and ask the human.
Never fake, stub or mock around a missing prerequisite (SPEC GEN3).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402

OK, BAD, UNK = "ok  ", "FAIL", "?   "
CONFIG_HINT = {
    "anthropic_key": "LLM / VLM", "openai_key": "LLM / VLM", "openai_compatible_url": "LLM / VLM",
    "runpod": "TRAIN", "modal_account": "TRAIN / HOST", "lambda_key": "TRAIN", "spacetime_cli": "STATE",
    "spacetime_server": "STATE", "spacetime_login": "STATE_TARGET", "s3_credentials": "BLOB",
    "nexar_key": "SUPPLIER", "digikey_key": "SUPPLIER", "gpu_box": "HOST",
}


def collect(data, providers, track, only=None, offline=False, env=None, root=None):
    """(rows, actions): rows = [(step id, ok, detail, cards)], actions = the rows that are not ok."""
    need = _lib.required_human_steps(data, providers, only_card_ids=only, track=track)
    steps = {h["id"]: h for h in data["human_steps"]}
    checks = _lib.run_checks({steps[h]["check"] for h in need if h in steps}, offline=offline, env=env, root=root)
    rows, actions = [], []
    first = ("dev_tools", "xcode", "repo_access")
    for hid, cards in sorted(need.items(), key=lambda kv: (kv[0] not in first, kv[0])):
        ok, detail = _lib.human_step_satisfied(steps[hid], checks)
        rows.append((hid, ok, detail, sorted(cards)))
        if ok is not True:
            actions.append((hid, steps[hid], ok, detail, sorted(cards)))
    return rows, actions


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="skip network reachability checks")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--cards", default="", help="comma-separated card ids to scope the human steps")
    ap.add_argument("--track", choices=["demo", "later", "all"], default=_lib.DEFAULT_TRACK)
    ap.add_argument("--profile", default=None, help="provider profile (default: REPAIROS_PROFILE or providers.yaml)")
    args = ap.parse_args()

    data = _lib.load_tasks()
    errs = _lib.validate_tasks(data)
    providers = _lib.load_providers(profile=args.profile)
    slots = providers["slots"]
    only = set(filter(None, args.cards.split(","))) or None
    rows, actions = collect(data, providers, args.track, only=only, offline=args.offline)

    if args.json:
        print(json.dumps({
            "registry_errors": errs, "profile": providers["profile"], "track": args.track, "selected": slots,
            "steps": [{"id": h, "ok": ok, "detail": d, "blocks": c} for h, ok, d, c in rows],
            "human_action_needed": [h for h, *_ in actions],
        }, indent=2))
        return 0 if not actions and not errs else 1

    print(f"RepairOS doctor  (profile: {providers['profile']}, track: {args.track})")
    print("=" * 72)
    if errs:
        print("REGISTRY ERRORS in build/tasks.yaml (fix these first):")
        for e in errs:
            print("  -", e)
        print()
    print("Selected providers (config/providers.yaml profile, env REPAIROS_SLOT_* overrides):")
    for k in sorted(slots):
        print(f"  {k:<20} {slots[k]}")
    print()
    print(f"Prerequisite checks for the {args.track} track" + (" (offline)" if args.offline else "") + ":")
    for hid, ok, detail, cards in rows:
        mark = OK if ok is True else BAD if ok is False else UNK
        print(f"  [{mark}] {hid:<24} {detail}")
    print()
    if not actions:
        print("All prerequisites for this profile and track are met. Run: python3 -I build/next.py")
        return 0 if not errs else 1
    print("HUMAN ACTION NEEDED")
    print("-" * 72)
    for i, (hid, step, ok, detail, cards) in enumerate(actions, 1):
        state = "not met" if ok is False else "could not verify; confirm with the human"
        hint = CONFIG_HINT.get(hid)
        hint = f" (to avoid this, change `{hint}` in the active profile of config/providers.yaml)" if hint else ""
        shown = ", ".join(cards[:12]) + (f", +{len(cards) - 12} more" if len(cards) > 12 else "")
        print(f"{i}. {step['what']}")
        print(f"   status: {state} ({detail})")
        print(f"   why:    {step['why']}")
        print(f"   blocks: {shown}{hint}")
        print(f"   how:    {step['how']}")
        print()
    print("Agents: do the part of your card that does not need these, or pick another card (build/next.py).")
    print("Do not stub, mock or fake a missing service or device.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
