#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for server/rundown.py (mc-prompter Phase C producer mode).

Run directly:
    uv run skills/mc-prompter/scripts/tests/test-rundown.py

Pure stdlib unittest; no network, no models, no downloads. Covers the
binding format in references/rundown-spec.md: frontmatter, time-suffix
accept/reject with line numbers, budget reconciliation warnings, even
split, kind detection, points, and the CLI.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent / "server"))

from rundown import RundownError, main, parse_rundown  # noqa: E402

FULL = """---
show: "Why local models win"
duration-minutes: 30
cue-density: normal        # hands-off | minimal | normal | chatty
wrap-minutes: 3
---

## Intro (3 min)

Full scripted intro text, prompted normally.

## Point 1: The cost argument (5 min)

- cloud bills compound, local is capex
- the 4090 anecdote

## Point 2: Latency (19 min)

- round trips add up
- the demo

## Wrap (3 min)

Scripted wrap text.
"""


def make(front, body):
    return f"---\n{front}\n---\n\n{body}"


class TestFrontmatter(unittest.TestCase):

    def test_full_example_fields(self):
        plan = parse_rundown(FULL)
        self.assertEqual(plan["show"], "Why local models win")
        self.assertEqual(plan["duration-s"], 1800)
        self.assertEqual(plan["cue-density"], "normal")
        self.assertEqual(plan["wrap-s"], 180)
        self.assertEqual(plan["warnings"], [])

    def test_show_optional_defaults_empty(self):
        plan = parse_rundown(make("duration-minutes: 10", "## A\n"))
        self.assertEqual(plan["show"], "")

    def test_cue_density_optional_defaults_null(self):
        plan = parse_rundown(make("duration-minutes: 10", "## A\n"))
        self.assertIsNone(plan["cue-density"])
        self.assertIsNone(plan["wrap-s"])

    def test_inline_comment_stripped(self):
        plan = parse_rundown(make(
            "duration-minutes: 10\ncue-density: chatty   # dense", "## A\n"))
        self.assertEqual(plan["cue-density"], "chatty")

    def test_missing_frontmatter_is_error(self):
        with self.assertRaises(RundownError) as ctx:
            parse_rundown("## A (3 min)\n\nText.\n")
        self.assertEqual(ctx.exception.line, 1)

    def test_unterminated_frontmatter_is_error(self):
        with self.assertRaises(RundownError):
            parse_rundown("---\nduration-minutes: 10\n\n## A\n")

    def test_missing_duration_is_error(self):
        with self.assertRaises(RundownError) as ctx:
            parse_rundown(make('show: "X"', "## A\n"))
        self.assertIn("duration-minutes", ctx.exception.message)

    def test_non_integer_duration_is_error(self):
        with self.assertRaises(RundownError) as ctx:
            parse_rundown(make("duration-minutes: thirty", "## A\n"))
        self.assertEqual(ctx.exception.line, 2)

    def test_negative_duration_is_error(self):
        with self.assertRaises(RundownError):
            parse_rundown(make("duration-minutes: -5", "## A\n"))

    def test_invalid_cue_density_is_error(self):
        with self.assertRaises(RundownError) as ctx:
            parse_rundown(make(
                "duration-minutes: 10\ncue-density: loud", "## A\n"))
        self.assertEqual(ctx.exception.line, 3)
        self.assertIn("cue-density", ctx.exception.message)

    def test_wrap_exceeding_duration_is_error(self):
        with self.assertRaises(RundownError):
            parse_rundown(make(
                "duration-minutes: 5\nwrap-minutes: 6", "## A\n"))

    def test_unknown_key_warns(self):
        plan = parse_rundown(make(
            "duration-minutes: 10\nduration_mins: 5", "## A\n"))
        self.assertTrue(any("duration_mins" in w for w in plan["warnings"]))


class TestTimeSuffix(unittest.TestCase):

    def _one(self, heading, minutes=10):
        return parse_rundown(make(f"duration-minutes: {minutes}",
                                  f"{heading}\n"))

    def test_n_min_accepted(self):
        plan = self._one("## Intro (3 min)")
        self.assertEqual(plan["segments"][0]["planned-s"], 180)
        self.assertEqual(plan["segments"][0]["title"], "Intro")

    def test_nm_accepted(self):
        plan = self._one("## Intro (10m)")
        self.assertEqual(plan["segments"][0]["planned-s"], 600)

    def test_minutes_word_rejected_with_line_number(self):
        with self.assertRaises(RundownError) as ctx:
            parse_rundown(make("duration-minutes: 10",
                               "## A (2 min)\n\n## B (3 minutes)\n"))
        self.assertEqual(ctx.exception.line, 7)
        self.assertIn("(3 minutes)", ctx.exception.message)

    def test_clock_form_rejected(self):
        with self.assertRaises(RundownError):
            self._one("## Intro (3:00)")

    def test_glued_seconds_rejected(self):
        with self.assertRaises(RundownError):
            self._one("## Intro (90s)")

    def test_bare_number_rejected(self):
        with self.assertRaises(RundownError):
            self._one("## Intro (5)")

    def test_zero_budget_rejected(self):
        with self.assertRaises(RundownError):
            self._one("## Intro (0 min)")

    def test_non_time_parens_stay_in_title(self):
        plan = self._one("## The setup (demo)")
        self.assertEqual(plan["segments"][0]["title"], "The setup (demo)")
        self.assertEqual(plan["segments"][0]["planned-s"], 600)

    def test_digit_in_non_time_parens_allowed(self):
        plan = self._one("## Q and A (part 2)")
        self.assertEqual(plan["segments"][0]["title"], "Q and A (part 2)")


class TestSegments(unittest.TestCase):

    def test_ids_in_order(self):
        plan = parse_rundown(FULL)
        self.assertEqual([s["id"] for s in plan["segments"]],
                         ["g0", "g1", "g2", "g3"])

    def test_content_before_first_heading_is_error(self):
        with self.assertRaises(RundownError) as ctx:
            parse_rundown(make("duration-minutes: 10",
                               "stray prose\n\n## A\n"))
        self.assertEqual(ctx.exception.line, 5)

    def test_no_segments_is_error(self):
        with self.assertRaises(RundownError):
            parse_rundown("---\nduration-minutes: 10\n---\n\n")

    def test_deeper_headings_are_body(self):
        plan = parse_rundown(make("duration-minutes: 10",
                                  "## A\n\n### sub\n\nText.\n"))
        self.assertEqual(len(plan["segments"]), 1)
        self.assertEqual(plan["segments"][0]["kind"], "scripted")


class TestKindDetection(unittest.TestCase):

    def test_prose_is_scripted(self):
        plan = parse_rundown(FULL)
        self.assertEqual(plan["segments"][0]["kind"], "scripted")
        self.assertEqual(plan["segments"][0]["points"], [])
        self.assertIn("Full scripted intro", plan["segments"][0]["body"])

    def test_bullets_only_is_bullets_with_points(self):
        plan = parse_rundown(FULL)
        seg = plan["segments"][1]
        self.assertEqual(seg["kind"], "bullets")
        self.assertEqual(
            [p["text"] for p in seg["points"]],
            ["cloud bills compound, local is capex", "the 4090 anecdote"])
        self.assertTrue(all(p["covered"] is False for p in seg["points"]))

    def test_empty_body_is_bullets_with_no_points(self):
        plan = parse_rundown(make("duration-minutes: 10", "## A\n"))
        self.assertEqual(plan["segments"][0]["kind"], "bullets")
        self.assertEqual(plan["segments"][0]["points"], [])

    def test_mixed_body_is_scripted(self):
        plan = parse_rundown(make(
            "duration-minutes: 10",
            "## A\n\nIntro sentence.\n\n- a bullet\n"))
        self.assertEqual(plan["segments"][0]["kind"], "scripted")
        self.assertEqual(plan["segments"][0]["points"], [])

    def test_mixed_body_warns_points_not_tracked(self):
        plan = parse_rundown(make(
            "duration-minutes: 10",
            "## A\n\nIntro sentence.\n\n- a bullet\n"))
        self.assertTrue(any("points are not tracked" in w
                            for w in plan["warnings"]))
        self.assertTrue(any("'A'" in w for w in plan["warnings"]))

    def test_star_bullets_are_prose(self):
        plan = parse_rundown(make("duration-minutes: 10",
                                  "## A\n\n* not a point\n"))
        self.assertEqual(plan["segments"][0]["kind"], "scripted")
        # No "- " bullet lines, so no mixed-bullets warning either.
        self.assertEqual(plan["warnings"], [])

    def test_wrapped_bullet_forces_scripted_with_warning(self):
        plan = parse_rundown(make(
            "duration-minutes: 10",
            "## A\n\n- a long point that wraps\n  onto a second line\n"))
        self.assertEqual(plan["segments"][0]["kind"], "scripted")
        self.assertEqual(plan["segments"][0]["points"], [])
        self.assertTrue(any("points are not tracked" in w
                            for w in plan["warnings"]))

    def test_nested_sub_bullets_join_their_parent_point(self):
        plan = parse_rundown(make(
            "duration-minutes: 10",
            "## A\n\n- main\n  - detail\n- other\n"))
        seg = plan["segments"][0]
        self.assertEqual(seg["kind"], "bullets")
        self.assertEqual([p["text"] for p in seg["points"]],
                         ["main; detail", "other"])
        self.assertEqual(plan["warnings"], [])

    def test_indented_bullet_without_parent_is_scripted(self):
        plan = parse_rundown(make(
            "duration-minutes: 10",
            "## A\n\n  - orphan sub-bullet\n"))
        self.assertEqual(plan["segments"][0]["kind"], "scripted")
        self.assertEqual(plan["segments"][0]["points"], [])
        self.assertTrue(any("points are not tracked" in w
                            for w in plan["warnings"]))


class TestTimeMath(unittest.TestCase):

    def test_even_split_of_unbudgeted(self):
        plan = parse_rundown(make(
            "duration-minutes: 10\nwrap-minutes: 2",
            "## A (3 min)\n\n## B\n\n## C\n\n## Wrap\n"))
        planned = {s["title"]: s["planned-s"] for s in plan["segments"]}
        self.assertEqual(planned["A"], 180)
        self.assertEqual(planned["Wrap"], 120)
        # 600 - 120 - 180 = 300 split across B and C
        self.assertEqual(planned["B"], 150)
        self.assertEqual(planned["C"], 150)
        self.assertEqual(plan["warnings"], [])

    def test_even_split_remainder_goes_to_earliest(self):
        plan = parse_rundown(make(
            "duration-minutes: 1",
            "## A\n\n## B\n\n## C\n"))
        self.assertEqual([s["planned-s"] for s in plan["segments"]],
                         [20, 20, 20])
        plan = parse_rundown(make(
            "duration-minutes: 1",
            "## A\n\n## B\n\n## C\n\n## D\n\n## E\n\n## F\n\n## G\n"))
        planned = [s["planned-s"] for s in plan["segments"]]
        self.assertEqual(sum(planned), 60)
        self.assertEqual(planned, [9, 9, 9, 9, 8, 8, 8])

    def test_overflow_reconciliation_scales_and_warns(self):
        plan = parse_rundown(make(
            "duration-minutes: 30",
            "## A (20 min)\n\n## B (20 min)\n"))
        self.assertTrue(any("duration-minutes wins" in w
                            for w in plan["warnings"]))
        planned = [s["planned-s"] for s in plan["segments"]]
        self.assertEqual(sum(planned), 1800)
        self.assertEqual(planned, [900, 900])

    def test_overflow_scaling_is_proportional(self):
        plan = parse_rundown(make(
            "duration-minutes: 30",
            "## A (30 min)\n\n## B (10 min)\n"))
        planned = [s["planned-s"] for s in plan["segments"]]
        self.assertEqual(sum(planned), 1800)
        self.assertEqual(planned, [1350, 450])

    def test_wrap_protected_from_scaling(self):
        plan = parse_rundown(make(
            "duration-minutes: 20\nwrap-minutes: 4",
            "## A (20 min)\n\n## B (12 min)\n\n## Wrap\n"))
        planned = {s["title"]: s["planned-s"] for s in plan["segments"]}
        self.assertEqual(planned["Wrap"], 240)
        self.assertEqual(planned["A"] + planned["B"], 960)
        self.assertEqual(planned["A"], 600)
        self.assertEqual(planned["B"], 360)

    def test_wrap_suffix_mismatch_warns_and_wrap_minutes_wins(self):
        plan = parse_rundown(make(
            "duration-minutes: 10\nwrap-minutes: 2",
            "## A\n\n## Wrap (3 min)\n"))
        self.assertEqual(plan["segments"][-1]["planned-s"], 120)
        self.assertTrue(any("wrap-minutes wins" in w
                            for w in plan["warnings"]))

    def test_slack_warns_when_all_budgeted(self):
        plan = parse_rundown(make(
            "duration-minutes: 30",
            "## A (10 min)\n\n## B (10 min)\n"))
        self.assertTrue(any("unallocated" in w for w in plan["warnings"]))
        self.assertEqual([s["planned-s"] for s in plan["segments"]],
                         [600, 600])

    def test_wrap_equal_to_duration_loads_with_zero_plan_warning(self):
        plan = parse_rundown(make(
            "duration-minutes: 5\nwrap-minutes: 5",
            "## A\n\n- a point\n\n## Wrap\n"))
        planned = [s["planned-s"] for s in plan["segments"]]
        self.assertEqual(planned, [0, 300])
        self.assertTrue(any("planned 0s" in w and "A" in w
                            for w in plan["warnings"]))

    def test_every_segment_gets_planned_s(self):
        plan = parse_rundown(FULL)
        for seg in plan["segments"]:
            self.assertIsInstance(seg["planned-s"], int)
        self.assertEqual(sum(s["planned-s"] for s in plan["segments"]), 1800)


class TestCli(unittest.TestCase):

    def test_prints_json_for_valid_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rundown.md"
            path.write_text(FULL, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main([str(path)])
            self.assertEqual(code, 0)
            plan = json.loads(out.getvalue())
            self.assertEqual(plan["duration-s"], 1800)
            self.assertEqual(len(plan["segments"]), 4)

    def test_parse_error_exits_1_with_line_number(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.md"
            path.write_text(make("duration-minutes: 10",
                                 "## A (5 minutes)\n"), encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = main([str(path)])
            self.assertEqual(code, 1)
            self.assertIn("line 5", err.getvalue())

    def test_missing_file_exits_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["/nonexistent/rundown.md"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
