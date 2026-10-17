#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for resolve_frames.py: time parsing, frame and timecode math, file
names, and marker moments. Exporting stills runs against a live Resolve
Studio, not here."""
import importlib.util
import sys
import unittest
from fractions import Fraction
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "resolve_frames.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("resolve_frames", SCRIPT)
rf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rf)


class TestTimes(unittest.TestCase):
    def test_parse_at(self):
        self.assertEqual(rf.parse_at("95"), 95)
        self.assertEqual(rf.parse_at("1:35.5"), 95.5)
        for bad in ("1:75", "abc", "-3"):
            with self.assertRaises(ValueError):
                rf.parse_at(bad)

    def test_frames_and_timecode(self):
        self.assertEqual(rf.to_frame(95.5, Fraction(30)), 2865)
        self.assertEqual(rf.timecode(108000 + 2865, Fraction(30)),
                         "01:01:35:15")
        self.assertEqual(rf.timecode(0, Fraction(30000, 1001)),
                         "00:00:00:00")

    def test_frame_name(self):
        self.assertEqual(rf.frame_name(2865, Fraction(30)),
                         "frame-01m35s15f.png")


class TestMarkers(unittest.TestCase):
    def test_color_filter_and_offset(self):
        markers = {"markers": [{"seconds": 0.0, "color": "Blue"},
                               {"seconds": 30.0, "color": "Red"},
                               {"seconds": 60.0, "color": "Blue"}]}
        self.assertEqual(rf.moments_from_markers(markers, "Blue", 0.5),
                         [0.5, 60.5])
        self.assertEqual(len(rf.moments_from_markers(markers)), 3)


if __name__ == "__main__":
    unittest.main()
