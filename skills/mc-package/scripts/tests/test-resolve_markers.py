#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for resolve_markers.py: GetMarkers output to the sorted marker
list. Reading markers runs against a live Resolve Studio, not here."""
import importlib.util
import sys
import unittest
from fractions import Fraction
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "resolve_markers.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("resolve_markers", SCRIPT)
rm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rm)


class TestMarkers(unittest.TestCase):
    def test_sorted_with_seconds_and_fields(self):
        raw = {900.0: {"color": "Red", "name": "cut 3.0s", "note": "n",
                       "customData": "manticore-cut", "duration": 1},
               0.0: {"color": "Blue", "name": "hook", "note": "q",
                     "customData": "manticore-beat", "duration": 1}}
        m = rm.marker_list(raw, Fraction(30))
        self.assertEqual([(x["frame"], x["seconds"], x["color"], x["custom"])
                          for x in m],
                         [(0, 0.0, "Blue", "manticore-beat"),
                          (900, 30.0, "Red", "manticore-cut")])

    def test_empty(self):
        self.assertEqual(rm.marker_list(None, Fraction(30)), [])


if __name__ == "__main__":
    unittest.main()
