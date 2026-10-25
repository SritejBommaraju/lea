#!/usr/bin/env python3
"""Self-test for the build tooling. Stdlib unittest only.

Run:  python3 -I build/test_tools.py
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402
import doctor  # noqa: E402
import next as next_tool  # noqa: E402
import status as status_tool  # noqa: E402

STEPS = [{"id": "jev_key", "what": "w", "how": "h", "check": "env_jev"},
         {"id": "test_products", "what": "w", "how": "h", "check": "none"}]


def task(tid, stream="build", **kw):
    return {"id": tid, "title": f"task {tid}", "stream": stream, "owns": [f"x/{tid}/**"], "done_when": "d", **kw}


def registry(*tasks, milestones=None):
    return _lib.prepare_tasks({
        "streams": [{"id": s, "name": s} for s in ("app", "modeling", "build")],
        "human_steps": STEPS, "milestones": milestones or [], "tasks": list(tasks)})


class Parser(unittest.TestCase):
    def test_subset(self):
        data = _lib.parse_yaml_subset('a: 1\nb: [x, "y: z"]\nc:\n  - id: t\n    d: true  # note\n')
        self.assertEqual(data, {"a": 1, "b": ["x", "y: z"], "c": [{"id": "t", "d": True}]})


class Validation(unittest.TestCase):
    def test_unknown_dependency_cycle_and_phase(self):
        errs = _lib.validate_tasks(registry(task("B1", depends_on=["B2"]), task("B2", depends_on=["B1"]),
                                            task("B3", depends_on=["B9"]), task("B4", phase=2),
                                            task("B5", depends_on=["B4"])))
        self.assertTrue(any("cycle" in e for e in errs))
        self.assertTrue(any("unknown dependency B9" in e for e in errs))
        self.assertTrue(any("B5: Phase 1 task depends on Phase 2 task B4" in e for e in errs))

    def test_fields_and_references(self):
        errs = _lib.validate_tasks(registry(task("b1"), task("B2", stream="nope"), task("B3", human=["nope"]),
                                            task("B4", status="claimed"), task("B5", slots=["det-owl"]),
                                            {"id": "B6", "stream": "build"},
                                            milestones=[{"id": "MS1", "requires": ["B99"]}]))
        for needle in ("b1: id", "B2: unknown stream", "B3: unknown human step", "B4: bad status", "B5: slot",
                       "B6: needs title", "MS1: unknown task B99"):
            self.assertTrue(any(needle in e for e in errs), needle)

    def test_done_task_needs_done_deps(self):
        errs = _lib.validate_tasks(registry(task("B1"), task("B2", status="done", depends_on=["B1"])))
        self.assertIn("B2: done but its dependency B1 is not", errs)


class Ready(unittest.TestCase):
    def setUp(self):
        self.data = registry(task("B1", status="done"), task("B2", depends_on=["B1"], human=["jev_key"]),
                             task("B3", depends_on=["B2"]), task("M1", stream="modeling", status="blocked"),
                             task("A1", stream="app", human=["test_products"]),
                             task("M2", stream="modeling", status="doing", branch="x/m2"))

    def plan(self, jev_ok):
        res = {"jev_key": (jev_ok, ""), "test_products": (None, "manual")}
        return next_tool.plan(self.data, results=res)

    def test_ready_per_stream(self):
        p = self.plan(True)
        self.assertEqual([r["id"] for r in p["build"]["ready"]], ["B2"])
        self.assertEqual([r["id"] for r in p["app"]["ready"]], ["A1"])
        self.assertEqual(p["app"]["ready"][0]["confirm"], ["test_products"])
        self.assertEqual([r["id"] for r in p["modeling"]["waiting"]], ["M1"])  # blocked stays out of ready

    def test_missing_human_step_waits(self):
        p = self.plan(False)
        self.assertEqual(p["build"]["ready"], [])
        self.assertEqual(p["build"]["waiting"][0]["missing"], ["jev_key"])

    def test_stream_filter(self):
        self.assertEqual(set(next_tool.plan(self.data, stream="app", results={})), {"app"})

    def test_human_steps_only_for_open_tasks(self):
        self.data["by_id"]["B2"]["status"] = "done"
        self.assertNotIn("jev_key", _lib.required_human_steps(self.data))

    def test_doctor_offline_reports_missing_keys(self):
        rows, actions = doctor.collect(self.data, offline=True, env={})
        self.assertEqual({r[0] for r in actions}, {"jev_key", "test_products"})
        rows, actions = doctor.collect(self.data, stream="build", offline=True, env={"TYPESAFE_KEY": "x"})
        self.assertEqual(actions, [])

    def test_status_renders_milestones(self):
        self.data["milestones"] = [{"id": "MS1", "title": "m", "requires": ["B1", "B2"]}]
        text = status_tool.render(self.data, {"jev_key": (True, ""), "test_products": (None, "")})
        self.assertIn("| MS1 | m | 1/2 |", text)
        self.assertIn("| B3 | after B2 |", text)
        self.assertIn("| M1 | blocked |", text)
        self.assertIn("| M2 | in progress (`x/m2`) |", text)

    def test_doing_is_never_ready_and_needs_a_branch(self):
        self.assertNotIn("M2", {r["id"] for g in self.plan(True).values() for k in g for r in g[k]})
        self.assertIn("M3: doing needs `branch` (where the work lives)",
                      _lib.validate_tasks(registry(task("M3", stream="modeling", status="doing"))))


class Git(unittest.TestCase):
    def test_ids_in_commit_subjects(self):
        subjects = ["B10 Pipeline orchestrator (#4)", "[A2] scan", "M7: classifier", "B1x nope", "fix B9"]
        self.assertEqual(_lib.ids_in_subjects(subjects, {"B10", "A2", "M7", "B1", "B9"}), {"B10", "A2", "M7"})


class RealFiles(unittest.TestCase):
    def test_registry_is_valid(self):
        data = _lib.load_tasks(merged=False)
        self.assertEqual(_lib.validate_tasks(data), [])
        self.assertTrue(all(len([t for t in data["tasks"] if t["stream"] == s["id"]]) <= 15
                            for s in data["streams"]))

    def test_plan_lists_the_same_tasks(self):
        plan = (_lib.ROOT / "PLAN.md").read_text(encoding="utf-8")
        in_plan = set(re.findall(r"^\| ([ABM]\d+) \|", plan, flags=re.M))
        self.assertEqual(in_plan, set(_lib.load_tasks(merged=False)["by_id"]))
        in_plan_ms = set(re.findall(r"^\| (MS\d+) \|", plan, flags=re.M))
        self.assertEqual(in_plan_ms, {m["id"] for m in _lib.load_tasks(merged=False)["milestones"]})

    def test_plan_marks_done_tasks(self):
        plan = (_lib.ROOT / "PLAN.md").read_text(encoding="utf-8")
        done_in_plan = set(re.findall(r"^\| ([ABM]\d+) \|.*\*\*Done\*\*", plan, flags=re.M))
        data = _lib.load_tasks(merged=False)
        self.assertEqual(done_in_plan, {t["id"] for t in data["tasks"] if t["status"] == "done"})

    def test_providers_resolve(self):
        p = _lib.load_providers(environ={})
        self.assertEqual(p["profile"], "phase1")
        self.assertEqual(p["slots"]["DET"], "owlv2")
        self.assertEqual(_lib.load_providers(environ={"REPAIROS_SLOT_DET": "gdino"})["slots"]["DET"], "gdino")

    def test_task_slots_exist_in_providers(self):
        selected = _lib.load_providers(environ={})["slots"]
        for t in _lib.load_tasks(merged=False)["tasks"]:
            for s in t["slots"]:
                self.assertIn(s.split("=")[0], selected, f"{t['id']}: {s}")


if __name__ == "__main__":
    unittest.main(verbosity=1)
