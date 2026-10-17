#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for resolve_import.py: spine parsing, review markers, timeline naming
and the sync comparison. The Resolve calls themselves run against a live
Studio instance, not here."""
import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "resolve_import.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("resolve_import", SCRIPT)
ri = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ri)

FCPXML = """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.9"><resources>
<format id="r1" frameDuration="1/30s"/>
<asset id="a1" hasVideo="1" hasAudio="1"/>
</resources><library><event><project><sequence><spine>
<asset-clip ref="a1" offset="0s" name="hook" start="300/30s" duration="60/30s"/>
<asset-clip ref="a1" offset="60/30s" name="hook" start="450/30s" duration="30/30s"/>
<asset-clip ref="a1" offset="90/30s" name="body" start="600/30s" duration="90/30s"/>
</spine></sequence></project></event></library></fcpxml>"""

SEGMENTS = [
    {"source": "s", "start": 10.0, "end": 12.0, "beat": "hook", "quote": "a"},
    {"source": "s", "start": 15.0, "end": 16.0, "beat": "hook", "quote": "b",
     "reason": "dead air"},
    {"source": "s", "start": 20.0, "end": 23.0, "beat": "body", "quote": "c",
     "reason": "false start"},
]


class TestSpine(unittest.TestCase):
    def test_reads_frames(self):
        fps, has_audio, clips = ri.read_spine(FCPXML)
        self.assertEqual(fps, 30)
        self.assertTrue(has_audio)
        self.assertEqual([(c["offset"], c["start"], c["duration"])
                          for c in clips],
                         [(0, 300, 60), (60, 450, 30), (90, 600, 90)])


class TestMarkers(unittest.TestCase):
    def setUp(self):
        _, _, self.clips = ri.read_spine(FCPXML)

    def test_beat_changes_and_large_cuts(self):
        m = ri.build_markers(SEGMENTS, self.clips, cut_marker_min=2.0)
        self.assertEqual([(x["frame"], x["color"], x["name"]) for x in m],
                         [(0, "Blue", "hook"), (60, "Red", "cut 3.0s"),
                          (90, "Blue", "body / cut 4.0s")])
        self.assertIn("false start", m[2]["note"])

    def test_small_cuts_get_no_marker(self):
        m = ri.build_markers(SEGMENTS, self.clips, cut_marker_min=5.0)
        self.assertEqual([x["color"] for x in m], ["Blue", "Blue"])

    def test_one_marker_per_frame(self):
        m = ri.build_markers(SEGMENTS, self.clips, cut_marker_min=0.5)
        frames = [x["frame"] for x in m]
        self.assertEqual(len(frames), len(set(frames)))


class TestNaming(unittest.TestCase):
    def test_never_reuses_a_name(self):
        self.assertEqual(ri.unique_name("x rough", set()), "x rough")
        self.assertEqual(ri.unique_name("x rough", {"x rough"}), "x rough v2")
        self.assertEqual(ri.unique_name("x rough",
                                        {"x rough", "x rough v2"}),
                         "x rough v3")


class TestCompare(unittest.TestCase):
    def setUp(self):
        _, _, self.clips = ri.read_spine(FCPXML)
        self.items = [{"start": 86400 + c["offset"],
                       "duration": c["duration"],
                       "left_offset": c["start"]} for c in self.clips]

    def test_in_sync_timeline_passes(self):
        self.assertEqual(ri.compare(self.clips, self.items, 86400), [])

    def test_one_frame_source_slip_fails(self):
        self.items[1]["left_offset"] += 1
        p = ri.compare(self.clips, self.items, 86400)
        self.assertEqual([(x["what"], x["clip"]) for x in p], [("start", 1)])

    def test_missing_item_fails(self):
        p = ri.compare(self.clips, self.items[:2], 86400)
        self.assertEqual(p[0]["what"], "item count")


class TestEmptyProject(unittest.TestCase):
    def test_only_an_empty_project_is_safe_to_leave(self):
        self.assertTrue(ri.is_empty_project(0, 0))
        self.assertFalse(ri.is_empty_project(1, 0))
        self.assertFalse(ri.is_empty_project(0, 3))


if __name__ == "__main__":
    unittest.main()
