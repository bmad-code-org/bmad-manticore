#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for resolve_place.py: beat parsing, the frame plan (overlay lanes,
titles, plan errors), naming, timecode, and the placement comparisons. The
Resolve calls themselves run against a live Studio instance, not here."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "resolve_place.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("resolve_place", SCRIPT)
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)

TABLE = """
| id | start | dur | end | anchor word | type | engine | asset | composition |
|---|---|---|---|---|---|---|---|---|
| b01 | 5.0 | 3.0 | 8.0 | a | diagram | hyperframes | null | box |
| b02 | 6.0 | 2.0 | 8.0 | b | diagram | hyperframes | null | box |
| b03 | 12.0 | 3.0 | 15.0 | c | lower-third | hyperframes | null | name key |
| b04 | 1:20 | | 1:22.5 | d | diagram | hyperframes | photo-1 | photo |
"""


def beats():
    rows, skipped = rp.parse_beats(TABLE)
    assert not skipped, skipped
    return rows


class TestParse(unittest.TestCase):
    def test_rows_and_end_column(self):
        rows = beats()
        self.assertEqual([r["id"] for r in rows], ["b01", "b02", "b03", "b04"])
        self.assertEqual((rows[3]["start"], rows[3]["dur"]), (80.0, 2.5))
        self.assertEqual(rows[3]["asset"], "photo-1")
        self.assertIsNone(rows[0]["asset"])

    def test_bad_rows_are_reported(self):
        rows, skipped = rp.parse_beats(
            "| id | start | dur |\n|---|---|---|\n| x | soon | 2 |\n"
            "| y | 3 | 0 |\n")
        self.assertEqual(rows, [])
        self.assertEqual(len(skipped), 2)


class TestPlan(unittest.TestCase):
    FILES = {"b01": "/g/b01.mov", "b02": "/g/b02.mov", "b04": "/g/b04.mov"}
    TITLES = [{"id": "b03", "text": "Name"}]

    def test_overlaps_pack_into_lanes_and_titles_split_out(self):
        plan, errors = rp.build_plan(beats(), self.TITLES, self.FILES, 30)
        self.assertEqual(errors, [])
        self.assertEqual([[e["id"] for e in lane] for lane in plan["lanes"]],
                         [["b01", "b04"], ["b02"]])
        self.assertEqual((plan["lanes"][0][0]["start"],
                          plan["lanes"][0][0]["dur"]), (150, 90))
        self.assertEqual([(t["id"], t["start"], t["dur"])
                          for t in plan["titles"]], [("b03", 360, 90)])

    def test_missing_beat_is_a_plan_error(self):
        files = dict(self.FILES)
        del files["b04"]
        _, errors = rp.build_plan(beats(), self.TITLES, files, 30)
        self.assertTrue(any("b04" in e for e in errors))

    def test_title_with_a_rendered_file_is_refused(self):
        files = dict(self.FILES, b03="/g/b03.mov")
        _, errors = rp.build_plan(beats(), self.TITLES, files, 30)
        self.assertTrue(any("both a title" in e for e in errors))

    def test_overlapping_titles_are_refused(self):
        titles = [{"id": "b01", "text": "a"}, {"id": "b02", "text": "b"}]
        files = {"b03": "/g/b03.mov", "b04": "/g/b04.mov"}
        _, errors = rp.build_plan(beats(), titles, files, 30)
        self.assertTrue(any("overlap" in e for e in errors))

    def test_png_overlay_is_refused(self):
        files = dict(self.FILES, b04="/g/b04.png")
        _, errors = rp.build_plan(beats(), self.TITLES, files, 30)
        self.assertTrue(any("PNG" in e for e in errors))


class TestTitlesFile(unittest.TestCase):
    def test_validation(self):
        by_id = {b["id"]: b for b in beats()}
        self.assertEqual(rp.validate_titles(
            [{"id": "b03", "text": "x", "color": "#FFD400",
              "center": [0.2, 0.1]}], by_id), [])
        problems = rp.validate_titles(
            [{"id": "zz", "text": "x"}, {"id": "b03", "text": " "},
             {"id": "b01", "text": "x", "color": "red"},
             {"id": "b02", "text": "x", "center": [1]},
             {"id": "b04", "text": "x", "style": "Bold"}], by_id)
        self.assertEqual(len(problems), 5)
        self.assertTrue(rp.validate_titles({"id": "b03"}, by_id))

    def test_hex_color(self):
        self.assertEqual(rp.hex_rgb("#FF0000"), (1.0, 0.0, 0.0))


class TestHelpers(unittest.TestCase):
    def test_unique_name(self):
        self.assertEqual(rp.unique_name("s graphics", set()), "s graphics")
        self.assertEqual(rp.unique_name("s graphics", {"s graphics"}),
                         "s graphics v2")

    def test_timecode(self):
        self.assertEqual(rp.frames_to_tc(108000 + 95, 30), "01:00:03:05")
        self.assertEqual(rp.frames_to_tc(95, 29.97), "00:00:03:05")

    def test_find_overlay_prefers_mov(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "b01.webm").write_bytes(b"")
            Path(tmp, "b01.mov").write_bytes(b"")
            self.assertEqual(rp.find_overlay(tmp, "b01").suffix, ".mov")
            self.assertIsNone(rp.find_overlay(tmp, "b02"))


class TestCompare(unittest.TestCase):
    def setUp(self):
        files = {"b01": "/g/b01.mov", "b02": "/g/b02.mov", "b04": "/g/b04.mov"}
        self.plan, _ = rp.build_plan(beats(), [{"id": "b03", "text": "Name"}],
                                     files, 30)
        self.placed = {
            2 + k: [{"start": 1000 + e["start"], "duration": e["dur"],
                     "name": Path(e["path"]).name} for e in lane]
            for k, lane in enumerate(self.plan["lanes"])}

    def test_in_place_overlays_pass(self):
        self.assertEqual(rp.compare_overlays(self.plan["lanes"], self.placed,
                                             2, 1000), [])

    def test_one_frame_late_overlay_fails(self):
        self.placed[2][1]["start"] += 1
        whats = [p["what"] for p in rp.compare_overlays(
            self.plan["lanes"], self.placed, 2, 1000)]
        self.assertIn("overlay missing", whats)

    def test_short_overlay_and_wrong_media_fail(self):
        self.placed[3][0]["duration"] -= 1
        self.placed[2][0]["name"] = "other.mov"
        whats = [p["what"] for p in rp.compare_overlays(
            self.plan["lanes"], self.placed, 2, 1000)]
        self.assertEqual(sorted(whats), ["overlay duration", "overlay media"])

    def test_titles(self):
        t = self.plan["titles"]
        good = [{"start": 50 + t[0]["start"], "duration": t[0]["dur"],
                 "text": "Name"}]
        self.assertEqual(rp.compare_titles(t, good, 50), [])
        bad = [dict(good[0], text="Custom Title")]
        self.assertEqual([p["what"] for p in rp.compare_titles(t, bad, 50)],
                         ["title text"])
        self.assertEqual(rp.compare_titles(t, [], 50)[0]["what"],
                         "title count")


if __name__ == "__main__":
    unittest.main()
