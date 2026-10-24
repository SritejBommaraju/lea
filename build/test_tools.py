#!/usr/bin/env python3
"""Self-test for the build tooling. Stdlib unittest only.

Run:  python3 -I build/test_tools.py        (or: python3 -m unittest build/test_tools.py)

Covers the four review bugs and the 24-hour rules:
  1. prerequisites of UNSELECTED implementations never block (card_is_selected / required_human_steps);
  2. B0.5's prerequisite check does not demand licenses/manifest.json, which B0.5 itself creates;
  3. next.py keeps `blocked` cards out of the ready list until they are cleared;
  4. optional (and stretch) cards never keep a slot or the build "unfinished" (status.py);
plus profile resolution, the demo-track validation rules and the real registry.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402
import next as next_tool  # noqa: E402
import status as status_tool  # noqa: E402

NEW_CARDS = {"B0.10", "B0.11", "S2.7", "S2.8", "S2.9", "S2.10", "S2.11", "C1.9", "M.STATE.11", "M.DET.5", "M.SEG.4", "M.OCRB.3", "K6.8", "E8.6", "O9.5"}


def registry(cards, **extra):
    """A minimal registry dict in the shape load_tasks returns."""
    data = {
        "phases": [{"id": "P0", "name": "p0"}],
        "lanes": [{"id": "L1", "name": "lane", "people": "x"}],
        "slots": [{"id": "STATE", "name": "state", "required": True},
                  {"id": "FUSION", "name": "fusion", "required": True}],
        "human_steps": [
            {"id": "spacetime_cli", "what": "w", "why": "w", "how": "h", "check": "spacetime_cli"},
            {"id": "jev_key", "what": "w", "why": "w", "how": "h", "check": "env_jev"},
            {"id": "dev_tools", "what": "w", "why": "w", "how": "h", "check": "tools_core"},
        ],
        "decisions": [], "done_checklist": [],
        "cards": cards,
    }
    data.update(extra)
    return _lib.prepare_tasks(data)


def card(cid, **kw):
    c = {"id": cid, "title": cid, "phase": "P0", "track": "demo", "estimate_hours": 1, "owner_lane": "L1"}
    c.update(kw)
    return c


def providers(**slots):
    return {"slots": slots, "profile": "test"}


SLOT_CARDS = [
    card("M.STATE.0", kind="module", slot="STATE", impl="interface"),
    card("M.STATE.1", kind="module", slot="STATE", impl="spacetimedb", human=["spacetime_cli"], depends_on=["M.STATE.0"]),
    card("M.STATE.9", kind="module", slot="STATE", impl="spacetimedb", optional=True, depends_on=["M.STATE.1"]),
    card("M.STATE.11", kind="module", slot="STATE", impl="sqlite", depends_on=["M.STATE.0"]),
    card("M.FUSION.0", kind="module", slot="FUSION", impl="interface"),
    card("M.FUSION.1", kind="module", slot="FUSION", impl="jev", human=["jev_key"]),
    card("M.FUSION.2", kind="module", slot="FUSION", impl="rules", always=True),
    card("B0.1", human=["dev_tools"]),
]


class Bug1UnselectedPrerequisites(unittest.TestCase):
    def test_unselected_impls_do_not_contribute_prerequisites(self):
        data = registry([dict(c) for c in SLOT_CARDS])
        need = _lib.required_human_steps(data, providers(STATE="sqlite", FUSION="rules"), track="demo")
        self.assertNotIn("spacetime_cli", need)
        self.assertNotIn("jev_key", need)
        self.assertIn("dev_tools", need)  # shared / core prerequisites still count

    def test_selected_impl_prerequisites_do_count(self):
        data = registry([dict(c) for c in SLOT_CARDS])
        need = _lib.required_human_steps(data, providers(STATE="spacetimedb", FUSION="jev"), track="demo")
        self.assertEqual(need["spacetime_cli"], {"M.STATE.1"})
        self.assertEqual(need["jev_key"], {"M.FUSION.1"})

    def test_always_cards_stay_in_play(self):
        data = registry([dict(c) for c in SLOT_CARDS])
        always = data["by_id"]["M.FUSION.2"]  # marked `always` in this synthetic registry only
        self.assertTrue(_lib.card_is_selected(always, providers(FUSION="jev")))
        self.assertFalse(_lib.card_is_selected(data["by_id"]["M.FUSION.1"], providers(FUSION="rules")))

    def test_real_registry_hackathon_profile(self):
        data = _lib.load_tasks()
        prov = _lib.load_providers(profile="hackathon", environ={})
        need = _lib.required_human_steps(data, prov, track="demo")
        for h in ("spacetime_cli", "spacetime_server", "gpu_box", "runpod", "s3_credentials"):
            self.assertNotIn(h, need, h)
        self.assertIn("jev_key", need)  # owner: FUSION=jev on the demo path, needed at H0 (B0.11)
        self.assertNotIn("M.FUSION.2", {c for cs in need.values() for c in cs})
        rules = data["by_id"]["M.FUSION.2"]
        self.assertEqual(rules["track"], "later")
        self.assertFalse(_lib.card_is_selected(rules, prov))


class Bug2LicencePrerequisite(unittest.TestCase):
    def test_base_check_ignores_the_manifest_b05_creates(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "LICENSE").write_text("x")
            (root / "NOTICE").write_text("x")
            res = _lib.run_checks({"license_base", "license_manifest"}, offline=True, env={}, root=root)
            self.assertIs(res["license_base"][0], True)
            self.assertIs(res["license_manifest"][0], False)  # the release check still wants the manifest

    def test_no_card_requires_a_file_it_creates(self):
        data = _lib.load_tasks()
        steps = {h["id"]: h for h in data["human_steps"]}
        b05 = data["by_id"]["B0.5"]
        self.assertNotIn("license_manifest", {steps[h]["check"] for h in b05["human"]})
        self.assertIn("release_licences", data["by_id"]["O9.4"]["human"])


class Bug3BlockedNotReady(unittest.TestCase):
    def test_blocked_card_stays_out_of_ready(self):
        data = registry([card("A1", status="done"), card("B1", depends_on=["A1"], status="blocked"),
                         card("C1", depends_on=["A1"])])
        ready, waiting = next_tool.plan(data, providers(), track="demo", checks={})
        self.assertEqual([r["id"] for r in ready], ["C1"])
        self.assertEqual([r["id"] for r in waiting], ["B1"])
        data["by_id"]["B1"]["status"] = "todo"  # cleared by a human
        ready, _ = next_tool.plan(data, providers(), track="demo", checks={})
        self.assertIn("B1", [r["id"] for r in ready])


class Bug4OptionalWorkSeparate(unittest.TestCase):
    def test_optional_card_does_not_keep_slot_unfinished(self):
        cards = [dict(c) for c in SLOT_CARDS]
        for c in cards:
            if c["id"] != "M.STATE.9":
                c["status"] = "done"
        data = registry(cards)
        ok, iface, counted, extra = status_tool.slot_state(data, providers(STATE="spacetimedb"), "STATE", "demo")
        self.assertTrue(ok)
        self.assertEqual([c["id"] for c in extra], ["M.STATE.9"])

    def test_slot_with_no_counted_card_is_not_done(self):
        data = registry([card("M.STATE.0", kind="module", slot="STATE", impl="interface", status="done")])
        self.assertFalse(status_tool.slot_state(data, providers(STATE="sqlite"), "STATE", "demo")[0])

    def test_build_complete_with_only_optional_and_stretch_left(self):
        data = registry([card("A1", status="done"), card("X1", optional=True), card("S1", stretch=True)])
        s = status_tool.summarize(data, providers(), track="demo", hours_left=24, checks={})
        self.assertTrue(s["complete"])
        self.assertEqual(s["cp_total"], 0.0)
        self.assertEqual({c["id"] for c in s["extras"]}, {"X1", "S1"})


class TwentyFourHourRules(unittest.TestCase):
    def test_demo_card_may_not_depend_on_later_card(self):
        data = registry([card("L1x", track="later", estimate_hours=None, owner_lane=None),
                         card("D1", depends_on=["L1x"])])
        self.assertTrue(any("depends on later card" in e for e in _lib.validate_tasks(data)))

    def test_demo_card_needs_hours_and_lane(self):
        data = registry([card("D1", estimate_hours=None), card("D2", owner_lane="L9"), card("D3", estimate_hours=6)])
        errs = _lib.validate_tasks(data)
        self.assertEqual(sum("demo card needs" in e for e in errs), 3)

    def test_critical_path_hours(self):
        data = registry([card("A", estimate_hours=2), card("B", estimate_hours=3, depends_on=["A"]),
                         card("C", estimate_hours=1, depends_on=["A"]), card("S", estimate_hours=4, stretch=True,
                                                                             depends_on=["B"])])
        self.assertEqual(_lib.critical_path(data, providers(), "demo"), (5.0, ["A", "B"]))

    def test_profile_resolution(self):
        data = {"profile": "hackathon", "profiles": {"hackathon": {"STATE": "sqlite"}, "full": {"STATE": "spacetimedb"}}}
        self.assertEqual(_lib.resolve_profile(dict(data), None, {})["slots"]["STATE"], "sqlite")
        self.assertEqual(_lib.resolve_profile(dict(data), None, {"REPAIROS_PROFILE": "full"})["slots"]["STATE"], "spacetimedb")
        env = {"REPAIROS_PROFILE": "full", "REPAIROS_SLOT_STATE": "postgres_queue"}
        self.assertEqual(_lib.resolve_profile(dict(data), None, env)["slots"]["STATE"], "postgres_queue")
        with self.assertRaises(ValueError):
            _lib.resolve_profile(dict(data), "nope", {})


class RealRegistry(unittest.TestCase):
    def test_validates_and_keeps_every_card(self):
        data = _lib.load_tasks()
        self.assertEqual(_lib.validate_tasks(data), [])
        ids = set(data["by_id"])
        self.assertTrue(NEW_CARDS <= ids)
        self.assertEqual(len(ids - NEW_CARDS), 127)
        self.assertEqual(data["by_id"]["M.FUSION.1"]["track"], "demo")
        self.assertEqual(data["by_id"]["M.DET.1"]["track"], "demo")  # owner 2026-10-24: Phase 1 zero-shot Grounding DINO
        for later in ("M.STATE.1", "M.FUSION.2", "F4.3", "M.DET.5", "M.SEG.1", "M.DEPTH.2", "T7.1", "E8.3", "M.HOST.1"):
            self.assertEqual(data["by_id"][later]["track"], "later", later)

    def test_demo_fits_in_24_hours(self):
        data = _lib.load_tasks()
        prov = _lib.load_providers(profile="hackathon", environ={})
        total, chain = _lib.critical_path(data, prov, "demo")
        self.assertLessEqual(total, 18, chain)  # leaves the H18-H24 dry run, video and final run


class Streams(unittest.TestCase):
    STREAMS = [{"id": "app", "name": "app", "lanes": ["L1"], "cards": ["C1.*", "I5.*"]},
               {"id": "grain", "name": "grain", "lanes": [], "cards": ["S2.7"]},
               {"id": "build", "name": "build", "lanes": [], "cards": ["*"]}]

    def test_first_matching_stream_wins(self):
        data = registry([card("C1.1"), card("S2.7"), card("S2.4"), card("I5.2")], streams=self.STREAMS)
        self.assertEqual({c["id"]: c["stream"] for c in data["cards"]},
                         {"C1.1": "app", "S2.7": "grain", "S2.4": "build", "I5.2": "app"})
        self.assertEqual(_lib.validate_tasks(data), [])

    def test_next_filters_by_stream(self):
        data = registry([card("C1.1"), card("S2.4")], streams=self.STREAMS)
        ready, _ = next_tool.plan(data, providers(), track="demo", checks={}, stream="app")
        self.assertEqual([r["id"] for r in ready], ["C1.1"])

    def test_card_outside_every_stream_is_an_error(self):
        data = registry([card("X9")], streams=self.STREAMS[:1])
        self.assertTrue(any("no stream" in e for e in _lib.validate_tasks(data)))

    def test_merged_card_ids_from_commit_subjects(self):
        subjects = ["S2.4 Alignment: robust ICP (#12)", "[M.STATE.11] SQLite state", "B0.1: scaffold",
                    "fix typo in README", "B0.10 hour 0 (#3)", "Merge pull request #7 from x/y"]
        self.assertEqual(_lib.card_ids_in_subjects(subjects, {"S2.4", "M.STATE.11", "B0.1", "B0.10", "B0.2"}),
                         {"S2.4", "M.STATE.11", "B0.1", "B0.10"})

    def test_merged_cards_count_as_done(self):
        data = registry([card("A1"), card("B1", depends_on=["A1"])])
        _lib.apply_merged(data, {"A1"})
        self.assertEqual(data["by_id"]["A1"]["status"], "done")
        ready, _ = next_tool.plan(data, providers(), track="demo", checks={})
        self.assertEqual([r["id"] for r in ready], ["B1"])

    def test_phase1_card_may_not_depend_on_phase2(self):
        data = registry([card("A1"), card("B1", phase1=True, depends_on=["A1"]), card("C1", phase1=True)])
        errs = _lib.validate_tasks(data)
        self.assertEqual([e for e in errs if "phase1" in e], ["B1: phase1 card depends on Phase 2 card A1"])

    def test_next_filters_phase1(self):
        data = registry([card("A1", phase1=True), card("B1")])
        ready, _ = next_tool.plan(data, providers(), track="demo", checks={}, phase1=True)
        self.assertEqual([r["id"] for r in ready], ["A1"])

    def test_real_registry_phases(self):
        by = _lib.load_tasks(merged=False)["by_id"]
        for cid in ("C1.3", "C1.4", "M.DET.0", "M.DET.1", "M.VLM.1", "M.FUSION.1", "F4.4", "E8.1"):
            self.assertTrue(by[cid]["phase1"], cid)
        for cid in ("S2.7", "S2.11", "M.GRAIN.1", "S2.3", "F4.7", "K6.4", "M.DET.5"):
            self.assertFalse(by[cid]["phase1"], cid)

    def test_real_registry_streams(self):
        data = _lib.load_tasks()
        got = {c["id"]: c["stream"] for c in data["cards"]}
        self.assertEqual({s["id"] for s in data["streams"]}, {"app", "modeling", "grain", "build"})
        for cid, want in (("C1.4", "app"), ("I5.4", "app"), ("M.VLM.1", "modeling"), ("E8.6", "modeling"),
                          ("S2.7", "grain"), ("S2.11", "grain"), ("S2.4", "build"), ("F4.8", "build")):
            self.assertEqual(got[cid], want, cid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
