#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for check_resolve.py: preset-name extraction and validation. The
Resolve calls themselves run against a live Studio instance, not here."""
import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "check_resolve.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("check_resolve", SCRIPT)
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)

LISTS = {
    "render-presets": ["H.264 Master", "YouTube - 2160p"],
    "project-presets": ["Current Settings", "Studio 4K30"],
    "fairlight-presets": [],
    "normalize-modes": ["Sample Peak Program", "YouTube", "EBU R128"],
}


class TestNames(unittest.TestCase):
    def test_every_shape_resolve_returns(self):
        self.assertEqual(cr.names(["a", "b"]), ["a", "b"])
        self.assertEqual(cr.names([{"Name": "a"}, {"Name": "b"}]), ["a", "b"])
        self.assertEqual(cr.names({1: "a", 2: "b"}), ["a", "b"])
        self.assertEqual(cr.names({}), [])
        self.assertEqual(cr.names(None), [])


class TestValidate(unittest.TestCase):
    def test_configured_names_that_exist_pass(self):
        checks = cr.validate({"project-preset": "Studio 4K30",
                              "render-preset": "YouTube - 2160p",
                              "loudness": "YouTube"}, LISTS)
        self.assertTrue(all(c["ok"] for c in checks.values()))

    def test_missing_name_fails_and_is_never_replaced(self):
        checks = cr.validate({"render-preset": "YouTube 2160p"}, LISTS)
        c = checks["resolve-render-preset"]
        self.assertFalse(c["ok"])
        self.assertEqual(c["value"], "YouTube 2160p")

    def test_empty_values_pass_with_their_fallback_said(self):
        checks = cr.validate({}, LISTS)
        self.assertTrue(all(c["ok"] for c in checks.values()))
        self.assertIn("H.264 Master",
                      checks["resolve-render-preset"]["note"])
        self.assertIn("skips", checks["resolve-loudness"]["note"])

    def test_missing_default_render_preset_is_said(self):
        lists = dict(LISTS, **{"render-presets": ["ProRes 422 HQ"]})
        note = cr.validate({}, lists)["resolve-render-preset"]["note"]
        self.assertIn("does not list", note)

    def test_unlistable_values_are_not_failed(self):
        lists = dict(LISTS, **{"normalize-modes": None})
        c = cr.validate({"loudness": "YouTube"}, lists)["resolve-loudness"]
        self.assertTrue(c["ok"])
        self.assertIn("not checked", c["note"])


if __name__ == "__main__":
    unittest.main()
