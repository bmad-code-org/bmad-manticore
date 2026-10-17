#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for resolve_session.py: frame-rate parsing and the empty-project
rule. Opening projects runs against a live Resolve Studio, not here."""
import importlib.util
import sys
import unittest
from fractions import Fraction
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "resolve_session.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("resolve_session", SCRIPT)
rs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rs)


class TestFps(unittest.TestCase):
    def test_integer_and_ntsc_rates(self):
        self.assertEqual(rs.timeline_fps("30"), 30)
        self.assertEqual(rs.timeline_fps(25.0), 25)
        self.assertEqual(rs.timeline_fps("29.97"), Fraction(30000, 1001))
        self.assertEqual(rs.timeline_fps("23.976"), Fraction(24000, 1001))

    def test_rejects_nonsense(self):
        with self.assertRaises(ValueError):
            rs.timeline_fps("0")


class TestEmptyProject(unittest.TestCase):
    def test_only_an_empty_project_is_safe_to_leave(self):
        self.assertTrue(rs.is_empty_project(0, 0))
        self.assertFalse(rs.is_empty_project(1, 0))
        self.assertFalse(rs.is_empty_project(0, 2))


if __name__ == "__main__":
    unittest.main()
