#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for resolve_beat_markers.py: table parsing, marker planning, collision
handling and the read-back check. The Resolve calls run against a live Studio
instance, not here."""
import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "resolve_beat_markers.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("resolve_beat_markers", SCRIPT)
rb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rb)

TABLE = """| id | start | dur | end | anchor word | anchor ts | spoken phrase | type | engine | asset | composition |
|---|---|---|---|---|---|---|---|---|---|---|
| B1 | 2.0 | 3.0 | 5.0 | lost | 2.0 | lost in the middle | stat | hyperframes | null | big number card |
| B2 | 2.0 | 1.0 | 3.0 | middle | 2.0 | the middle | diagram | hyperframes | null | staged diagram |
| B3 | 10.5 | 2.0 | 12.5 | subscribe | 10.5 | hit subscribe | cta | hyperframes | null | subscribe pill |
| B4 | soon | 2.0 | 12.5 | x | | x | stat | hyperframes | null | x |
"""


class TestParse(unittest.TestCase):
    def test_rows_and_bad_start(self):
        rows, skipped = rb.parse_table(TABLE)
        self.assertEqual([r["id"] for r in rows], ["B1", "B2", "B3"])
        self.assertEqual(rows[0]["composition"], "big number card")
        self.assertEqual(len(skipped), 1)


class TestColors(unittest.TestCase):
    def test_cta_is_yellow_and_types_are_stable(self):
        self.assertEqual(rb.type_color("CTA"), "Yellow")
        self.assertEqual(rb.type_color("stat"), rb.type_color("Stat"))

    def test_never_mc_cut_colors(self):
        for t in ["stat", "diagram", "quote", "b-roll", "overlay", ""]:
            self.assertNotIn(rb.type_color(t), ("Blue", "Red"))


class TestPlan(unittest.TestCase):
    def setUp(self):
        self.rows, _ = rb.parse_table(TABLE)

    def test_frames_are_timeline_relative(self):
        m = rb.plan_markers(self.rows, 30.0)
        self.assertEqual([x["frame"] for x in m], [60, 315])

    def test_same_frame_beats_merge(self):
        m = rb.plan_markers(self.rows, 30.0)[0]
        self.assertEqual(m["ids"], ["B1", "B2"])
        self.assertEqual(m["name"], "B1 stat / B2 diagram")
        self.assertEqual(m["duration"], 90)
        self.assertIn("lost in the middle", m["note"])


class TestPlace(unittest.TestCase):
    def test_nudges_off_foreign_markers(self):
        planned = [{"frame": 60, "ids": ["B1"]}, {"frame": 61, "ids": ["B2"]}]
        placed, nudged, blocked = rb.place(planned, {60})
        self.assertEqual([p["frame"] for p in placed], [61, 62])
        self.assertEqual(len(nudged), 2)
        self.assertEqual(blocked, [])

    def test_blocked_when_no_free_frame(self):
        placed, _, blocked = rb.place([{"frame": 0, "ids": ["B1"]}],
                                      set(range(0, 10)), max_nudge=5)
        self.assertEqual(placed, [])
        self.assertEqual(len(blocked), 1)


class TestCheck(unittest.TestCase):
    PLACED = [{"frame": 60, "name": "B1 stat", "ids": ["B1"]}]

    def test_clean_read_back(self):
        markers = {60: {"name": "B1 stat", "customData": rb.CUSTOM},
                   0: {"name": "hook", "customData": "manticore-beat"}}
        self.assertEqual(rb.check(self.PLACED, markers), [])

    def test_missing_and_stale_fail(self):
        markers = {90: {"name": "old", "customData": rb.CUSTOM}}
        what = [p["what"] for p in rb.check(self.PLACED, markers)]
        self.assertEqual(what, ["missing", "stale"])

    def test_foreign_marker_on_the_frame_is_not_ours(self):
        markers = {60: {"name": "creator note", "customData": ""}}
        self.assertEqual(rb.check(self.PLACED, markers)[0]["what"], "missing")


if __name__ == "__main__":
    unittest.main()
