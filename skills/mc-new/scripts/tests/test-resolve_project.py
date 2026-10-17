#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for resolve_project.py: the project-switch plan and preset names. The
Resolve calls themselves run against a live Studio instance, not here."""
import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "resolve_project.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("resolve_project", SCRIPT)
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)


class TestPlan(unittest.TestCase):
    def test_target_already_open_is_reused(self):
        self.assertEqual(rp.plan("vid", True, "vid", ["vid"])[0],
                         "reuse-open")

    def test_never_leaves_a_project_with_work(self):
        action, note = rp.plan("client-edit", True, "vid", [])
        self.assertEqual(action, "refuse")
        self.assertIn("client-edit", note)

    def test_existing_project_is_loaded_not_recreated(self):
        self.assertEqual(rp.plan("Untitled Project", False, "vid",
                                 ["vid"])[0], "load")

    def test_new_project_is_created(self):
        self.assertEqual(rp.plan("Untitled Project", False, "vid", [])[0],
                         "create")
        self.assertEqual(rp.plan(None, False, "vid", [])[0], "create")


class TestHelpers(unittest.TestCase):
    def test_empty_project(self):
        self.assertTrue(rp.is_empty_project(0, 0))
        self.assertFalse(rp.is_empty_project(1, 0))
        self.assertFalse(rp.is_empty_project(0, 2))

    def test_preset_names(self):
        self.assertEqual(rp.preset_names([{"Name": "a"}, {"Name": ""}, "b"]),
                         ["a", "b"])
        self.assertEqual(rp.preset_names(None), [])


if __name__ == "__main__":
    unittest.main()
