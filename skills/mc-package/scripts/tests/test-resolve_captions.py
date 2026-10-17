#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for resolve_captions.py: subtitle items to cues, SRT/VTT/transcript
rendering, and atomic writes. Generating subtitles runs against a live
Resolve Studio, not here."""
import importlib.util
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "resolve_captions.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("resolve_captions", SCRIPT)
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)
spec2 = importlib.util.spec_from_file_location(
    "verify_captions", SCRIPT.parent / "verify_captions.py")
vc = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(vc)

ITEMS = [
    {"start": 86400 + 90, "end": 86400 + 150, "text": "second  cue"},
    {"start": 86400 + 0, "end": 86400 + 75, "text": "First cue."},
    {"start": 86400 + 160, "end": 86400 + 170, "text": "   "},
]


class TestCues(unittest.TestCase):
    def test_timeline_relative_sorted_and_cleaned(self):
        cues = rc.cues_from_items(ITEMS, 86400, Fraction(30))
        self.assertEqual([(c["start"], c["end"], c["text"]) for c in cues],
                         [(0.0, 2.5, "First cue."), (3.0, 5.0, "second cue")])

    def test_ntsc_frames(self):
        cues = rc.cues_from_items([{"start": 30, "end": 60, "text": "x"}], 0,
                                  Fraction(30000, 1001))
        self.assertAlmostEqual(cues[0]["start"], 1.001)


class TestRender(unittest.TestCase):
    def setUp(self):
        self.cues = rc.cues_from_items(ITEMS, 86400, Fraction(30))

    def test_time_format(self):
        self.assertEqual(rc.fmt_time(3723.4567), "01:02:03,457")
        self.assertEqual(rc.fmt_time(1.5, "."), "00:00:01.500")

    def test_srt_round_trips_through_the_gate_parser(self):
        parsed = vc.parse_srt(rc.render_srt(self.cues))
        self.assertEqual([(c["index"], c["start"], c["end"], c["text"])
                          for c in parsed],
                         [(1, 0.0, 2.5, "First cue."),
                          (2, 3.0, 5.0, "second cue")])

    def test_vtt_header_and_dot_times(self):
        vtt = rc.render_vtt(self.cues)
        self.assertTrue(vtt.startswith("WEBVTT\n\n"))
        self.assertIn("00:00:03.000 --> 00:00:05.000", vtt)

    def test_transcript_splits_at_pauses(self):
        cues = [{"start": 0, "end": 2, "text": "a"},
                {"start": 2.2, "end": 3, "text": "b"},
                {"start": 70, "end": 72, "text": "c"}]
        md = rc.render_transcript(cues)
        self.assertIn("**0:00** a b", md)
        self.assertIn("**1:10** c", md)

    def test_atomic_write_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "final.srt"
            rc.write_atomic(path, "x")
            self.assertEqual(path.read_text(), "x")
            self.assertEqual([p.name for p in path.parent.iterdir()],
                             ["final.srt"])


if __name__ == "__main__":
    unittest.main()
