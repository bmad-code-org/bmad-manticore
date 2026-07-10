#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for server/producer.py (mc-prompter Phase C producer mode).

Run directly:
    uv run skills/mc-prompter/scripts/tests/test-producer.py

Pure stdlib unittest; no network, no models, no downloads. Every scenario
runs against a fake clock injected via now_fn: running long (proportional
replan + DROP candidate), running short (STRETCH), out-of-order coverage,
go-live/hold/resume/end clock math, wrap reserve protection, coverage
stickiness, and cue-candidate dedup keys.
"""

import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent / "server"))

from producer import Producer  # noqa: E402
from rundown import parse_rundown  # noqa: E402


class FakeClock:

    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def seg(seg_id, title, planned_s, kind="bullets", points=()):
    return {"id": seg_id, "title": title, "kind": kind,
            "planned-s": planned_s, "body": "",
            "points": [{"text": t, "covered": False} for t in points]}


def show_1800():
    """30 min show, 3 min protected wrap, budgets fill exactly."""
    return {
        "show": "Test show", "duration-s": 1800, "cue-density": None,
        "wrap-s": 180, "warnings": [],
        "segments": [
            seg("g0", "Intro", 180, kind="scripted"),
            seg("g1", "P1", 480, points=("point a", "point b")),
            seg("g2", "P2", 480, points=("point c", "point d")),
            seg("g3", "P3", 480, points=("point e",)),
            seg("g4", "Wrap", 180, kind="scripted"),
        ],
    }


def show_600():
    """10 min show, two equal segments, no wrap."""
    return {
        "show": "", "duration-s": 600, "cue-density": None,
        "wrap-s": None, "warnings": [],
        "segments": [seg("g0", "A", 300), seg("g1", "B", 300)],
    }


def live(rundown, clock=None, **kwargs):
    clock = clock or FakeClock()
    p = Producer(rundown, now_fn=clock, **kwargs)
    p.go_live()
    return p, clock


class TestClock(unittest.TestCase):

    def test_pre_show(self):
        p = Producer(show_1800(), now_fn=FakeClock())
        state = p.state
        self.assertFalse(state["live"])
        self.assertFalse(state["hold"])
        self.assertEqual(state["elapsed-s"], 0)
        self.assertEqual(state["remaining-s"], 1800)
        self.assertEqual(state["show-state"], "green")
        self.assertEqual(state["current"], "g0")
        self.assertIsNone(state["drop"])

    def test_pre_show_clock_does_not_run(self):
        clock = FakeClock()
        p = Producer(show_1800(), now_fn=clock)
        clock.advance(500)
        self.assertEqual(p.tick()["elapsed-s"], 0)

    def test_go_live_hold_resume_end(self):
        p, clock = live(show_1800())
        clock.advance(100)
        state = p.tick()
        self.assertTrue(state["live"])
        self.assertEqual(state["elapsed-s"], 100)
        p.hold()
        clock.advance(50)
        state = p.tick()
        self.assertTrue(state["hold"])
        self.assertEqual(state["elapsed-s"], 100)
        p.resume()
        clock.advance(25)
        state = p.tick()
        self.assertFalse(state["hold"])
        self.assertEqual(state["elapsed-s"], 125)
        p.end_show()
        clock.advance(500)
        state = p.tick()
        self.assertFalse(state["live"])
        self.assertEqual(state["elapsed-s"], 125)

    def test_end_show_spent_sums_to_elapsed_from_running(self):
        p, clock = live(show_1800())
        clock.advance(100)
        p.end_show()
        state = p.state
        self.assertEqual(state["elapsed-s"], 100)
        self.assertEqual(sum(s["spent-s"] for s in state["segments"]), 100)
        self.assertEqual(state["segments"][0]["spent-s"], 100)

    def test_end_show_spent_sums_to_elapsed_from_hold(self):
        p, clock = live(show_1800())
        clock.advance(100)
        p.hold()
        clock.advance(50)
        p.end_show()
        state = p.state
        self.assertEqual(state["elapsed-s"], 100)
        self.assertEqual(sum(s["spent-s"] for s in state["segments"]), 100)

    def test_end_show_spent_sums_to_elapsed_after_hold_resume(self):
        p, clock = live(show_1800())
        clock.advance(100)
        p.hold()
        clock.advance(50)
        p.resume()
        clock.advance(25)
        p.end_show()
        state = p.state
        self.assertEqual(state["elapsed-s"], 125)
        self.assertEqual(sum(s["spent-s"] for s in state["segments"]), 125)

    def test_non_monotonic_clock_never_double_counts(self):
        # now_fn must be non-decreasing; if it ever regresses anyway,
        # _synced never rewinds so the recovered span is not re-counted.
        p, clock = live(show_1800())
        clock.advance(100)
        p.tick()
        clock.advance(-10)
        p.tick()
        clock.advance(10)
        state = p.tick()
        self.assertEqual(state["elapsed-s"], 100)
        self.assertEqual(state["segments"][0]["spent-s"], 100)

    def test_double_go_live_is_noop(self):
        p, clock = live(show_1800())
        clock.advance(10)
        p.go_live()
        clock.advance(10)
        self.assertEqual(p.tick()["elapsed-s"], 20)

    def test_hold_does_not_attribute_spent(self):
        p, clock = live(show_1800())
        clock.advance(10)
        p.hold()
        clock.advance(100)
        state = p.tick()
        self.assertEqual(state["segments"][0]["spent-s"], 10)

    def test_spent_attribution_follows_current(self):
        p, clock = live(show_1800())
        clock.advance(60)
        p.advance_segment()
        clock.advance(30)
        state = p.tick()
        self.assertEqual(state["segments"][0]["state"], "done")
        self.assertEqual(state["segments"][0]["spent-s"], 60)
        self.assertEqual(state["segments"][1]["state"], "current")
        self.assertEqual(state["segments"][1]["spent-s"], 30)
        self.assertEqual(state["current"], "g1")


class TestReplan(unittest.TestCase):

    def test_pre_show_replan_equals_plan(self):
        p = Producer(show_1800(), now_fn=FakeClock())
        for s in p.state["segments"]:
            self.assertEqual(s["replanned-s"], s["planned-s"])
            self.assertEqual(s["timing"], "green")

    def test_running_long_replans_proportionally_with_drop(self):
        # Intro done on time, then segment 1 (P1) runs 12 minutes over
        # its 8 minute budget.
        p, clock = live(show_1800())
        clock.advance(180)
        p.advance_segment()
        clock.advance(1200)
        state = p.tick()
        by_id = {s["id"]: s for s in state["segments"]}
        # remaining 420, wrap reserve 180, avail 240 across equal
        # originals (480 each) -> 80 each
        self.assertEqual(by_id["g1"]["replanned-s"], 80)
        self.assertEqual(by_id["g2"]["replanned-s"], 80)
        self.assertEqual(by_id["g3"]["replanned-s"], 80)
        self.assertEqual(by_id["g1"]["timing"], "red")
        self.assertEqual(by_id["g2"]["timing"], "green")
        # pending floor is max(45, 120) = 120 > 80: DROP the last
        # non-wrap pending segment, even split 240 // 2
        self.assertEqual(state["drop"],
                         {"segment": "g3", "text": "DROP: P3, or 120s each"})
        keys = [c["key"] for c in p.cue_candidates()]
        self.assertIn("drop:g3", keys)

    def test_replan_proportional_to_unequal_originals(self):
        rundown = {
            "show": "", "duration-s": 600, "cue-density": None,
            "wrap-s": None, "warnings": [],
            "segments": [seg("g0", "A", 100), seg("g1", "B", 200),
                         seg("g2", "C", 300)],
        }
        p, clock = live(rundown)
        clock.advance(300)
        state = p.tick()
        self.assertEqual([s["replanned-s"] for s in state["segments"]],
                         [50, 100, 150])
        self.assertIsNone(state["drop"])
        clock.advance(160)
        state = p.tick()
        # avail 140: B share 47 < floor 50 -> DROP names C (last pending)
        self.assertEqual(state["drop"],
                         {"segment": "g2", "text": "DROP: C, or 70s each"})

    def test_wrap_reserve_protected(self):
        p, clock = live(show_1800())
        clock.advance(1500)
        state = p.tick()
        wrap = state["segments"][-1]
        self.assertEqual(wrap["replanned-s"], 180)
        self.assertEqual(wrap["planned-s"], 180)

    def test_done_segments_report_plan(self):
        p, clock = live(show_1800())
        clock.advance(60)
        p.advance_segment()
        state = p.tick()
        intro = state["segments"][0]
        self.assertEqual(intro["replanned-s"], 180)
        self.assertEqual(intro["spent-s"], 60)
        self.assertEqual(intro["timing"], "green")

    def test_segment_timing_thresholds(self):
        rundown = {
            "show": "", "duration-s": 600, "cue-density": None,
            "wrap-s": None, "warnings": [],
            "segments": [seg("g0", "A", 600)],
        }
        p, clock = live(rundown)
        clock.advance(100)
        self.assertEqual(p.tick()["segments"][0]["timing"], "green")
        clock.advance(200)   # spent 300 vs replanned 300
        self.assertEqual(p.tick()["segments"][0]["timing"], "yellow")
        clock.advance(100)   # spent 400 vs replanned 200
        self.assertEqual(p.tick()["segments"][0]["timing"], "red")
        clock.advance(200)   # spent 600 vs replanned 0
        self.assertEqual(p.tick()["segments"][0]["timing"], "red")

    def test_show_state_thresholds(self):
        p, clock = live(show_1800())
        clock.advance(1439)
        self.assertEqual(p.tick()["show-state"], "green")
        clock.advance(1)     # exactly 80 percent
        self.assertEqual(p.tick()["show-state"], "yellow")
        clock.advance(361)   # 1801 of 1800
        state = p.tick()
        self.assertEqual(state["show-state"], "red")
        self.assertEqual(state["remaining-s"], -1)


class TestCoverage(unittest.TestCase):

    def test_next_is_first_uncovered_unskipped_in_order(self):
        p, _ = live(show_1800())
        self.assertEqual(p.state["next-point"],
                         {"segment": "g1", "idx": 0, "text": "point a"})

    def test_out_of_order_coverage_keeps_next_defined(self):
        p, _ = live(show_1800())
        p.mark_covered("g2", 0)
        self.assertEqual(p.state["next-point"]["segment"], "g1")
        p.mark_covered("g1", 0)
        self.assertEqual(p.state["next-point"],
                         {"segment": "g1", "idx": 1, "text": "point b"})
        p.mark_covered("g1", 1)
        self.assertEqual(p.state["next-point"],
                         {"segment": "g2", "idx": 1, "text": "point d"})

    def test_next_null_when_everything_covered_or_skipped(self):
        p, _ = live(show_1800())
        p.mark_covered("g1", 0)
        p.mark_covered("g1", 1)
        p.skip_point("g2", 0)
        p.mark_covered("g2", 1)
        p.mark_covered("g3", 0)
        self.assertIsNone(p.state["next-point"])

    def test_skip_excluded_from_next(self):
        p, _ = live(show_1800())
        p.skip_point("g1", 0)
        self.assertEqual(p.state["next-point"],
                         {"segment": "g1", "idx": 1, "text": "point b"})

    def test_propose_flips_uncovered_only_once(self):
        p, _ = live(show_1800())
        self.assertTrue(p.propose_coverage("g1", 0))
        self.assertFalse(p.propose_coverage("g1", 0))
        point = p.state["segments"][1]["points"][0]
        self.assertTrue(point["covered"])

    def test_propose_after_human_skip_does_nothing(self):
        p, _ = live(show_1800())
        p.skip_point("g1", 0)
        self.assertFalse(p.propose_coverage("g1", 0))
        point = p.state["segments"][1]["points"][0]
        self.assertFalse(point["covered"])
        self.assertTrue(point["skipped"])

    def test_human_can_cover_a_skipped_point(self):
        p, _ = live(show_1800())
        p.skip_point("g1", 0)
        p.mark_covered("g1", 0)
        self.assertTrue(p.state["segments"][1]["points"][0]["covered"])

    def test_nothing_ever_uncovers(self):
        p, _ = live(show_1800())
        p.mark_covered("g1", 0)
        p.skip_point("g1", 0)
        self.assertTrue(p.state["segments"][1]["points"][0]["covered"])

    def test_propose_ignores_unknown_ids(self):
        p, _ = live(show_1800())
        self.assertFalse(p.propose_coverage("g9", 0))
        self.assertFalse(p.propose_coverage("g1", 99))

    def test_human_calls_raise_on_unknown_ids(self):
        p, _ = live(show_1800())
        with self.assertRaises(ValueError):
            p.mark_covered("g9", 0)
        with self.assertRaises(ValueError):
            p.skip_point("g1", 99)
        with self.assertRaises(ValueError):
            p.make_current("g9")


class TestSegmentControl(unittest.TestCase):

    def test_make_current_forward_marks_old_done(self):
        p, _ = live(show_1800())
        p.make_current("g2")
        states = {s["id"]: s["state"] for s in p.state["segments"]}
        self.assertEqual(states["g0"], "done")
        self.assertEqual(states["g1"], "pending")
        self.assertEqual(states["g2"], "current")
        self.assertEqual(p.state["current"], "g2")

    def test_make_current_backward_reopens(self):
        p, _ = live(show_1800())
        p.make_current("g2")
        p.make_current("g0")
        states = {s["id"]: s["state"] for s in p.state["segments"]}
        self.assertEqual(states["g0"], "current")
        self.assertEqual(states["g2"], "pending")

    def test_advance_through_the_end(self):
        p, _ = live(show_1800())
        for _ in range(5):
            p.advance_segment()
        state = p.state
        self.assertEqual(state["current"], "g4")
        self.assertTrue(all(s["state"] == "done"
                            for s in state["segments"]))

    def test_backward_jump_after_advance_past_end_keeps_last_done(self):
        p, _ = live(show_600())
        p.advance_segment()
        p.advance_segment()  # past the end: pointer rests on done g1
        p.make_current("g0")
        states = {s["id"]: s["state"] for s in p.state["segments"]}
        self.assertEqual(states["g0"], "current")
        self.assertEqual(states["g1"], "done")
        self.assertEqual(p.state["current"], "g0")

    def test_done_pointer_does_not_accrue_spent(self):
        p, clock = live(show_600())
        clock.advance(60)
        p.advance_segment()
        clock.advance(60)
        p.advance_segment()  # past the end at t=120
        clock.advance(300)
        state = p.tick()
        self.assertEqual(state["elapsed-s"], 420)
        self.assertEqual(state["current"], "g1")
        self.assertEqual(state["segments"][1]["state"], "done")
        self.assertEqual(state["segments"][1]["spent-s"], 60)
        self.assertEqual(sum(s["spent-s"] for s in state["segments"]), 120)


class TestCueCandidates(unittest.TestCase):

    def test_quiet_pre_show_hold_and_ended(self):
        clock = FakeClock()
        p = Producer(show_1800(), now_fn=clock)
        self.assertEqual(p.cue_candidates(), [])
        p.go_live()
        clock.advance(1900)
        self.assertTrue(p.cue_candidates())
        p.hold()
        self.assertEqual(p.cue_candidates(), [])
        p.resume()
        self.assertTrue(p.cue_candidates())
        p.end_show()
        self.assertEqual(p.cue_candidates(), [])

    def test_wrap_cue_at_reserve(self):
        p, clock = live(show_1800())
        clock.advance(1619)
        self.assertNotIn("wrap", [c["key"] for c in p.cue_candidates()])
        clock.advance(1)
        cues = {c["key"]: c for c in p.cue_candidates()}
        self.assertIn("wrap", cues)
        self.assertEqual(cues["wrap"]["text"], "WRAP")
        self.assertEqual(cues["wrap"]["tier"], "attention")

    def test_over_cue_text_and_minute_key(self):
        p, clock = live(show_1800())
        clock.advance(1830)
        cues = {c["key"]: c for c in p.cue_candidates()}
        self.assertEqual(cues["over:0"]["text"], "0:30 OVER")
        self.assertEqual(cues["over:0"]["tier"], "attention")
        clock.advance(60)
        cues = {c["key"]: c for c in p.cue_candidates()}
        self.assertNotIn("over:0", cues)
        self.assertEqual(cues["over:1"]["text"], "1:30 OVER")

    def test_segment_30s_cue(self):
        p, clock = live(show_600())
        clock.advance(179)
        self.assertNotIn("seg-30:g0",
                         [c["key"] for c in p.cue_candidates()])
        clock.advance(1)  # replanned share 210, spent 180 -> 30 left
        cues = {c["key"]: c for c in p.cue_candidates()}
        self.assertIn("seg-30:g0", cues)
        self.assertEqual(cues["seg-30:g0"]["text"], "30 seconds")
        self.assertEqual(cues["seg-30:g0"]["tier"], "card")

    def test_candidates_are_stateless_with_stable_dedup_keys(self):
        p, clock = live(show_600())
        clock.advance(200)
        first = p.cue_candidates()
        clock.advance(1)
        second = p.cue_candidates()
        self.assertEqual([c["key"] for c in first],
                         [c["key"] for c in second])
        self.assertIn("seg-30:g0", [c["key"] for c in first])

    def test_stretch_when_ahead_of_plan(self):
        p, clock = live(show_1800())
        clock.advance(60)
        p.advance_segment()  # 180s of plan credit in 60s
        keys = [c["key"] for c in p.cue_candidates()]
        self.assertIn("stretch", keys)
        cue = next(c for c in p.cue_candidates() if c["key"] == "stretch")
        self.assertEqual(cue["text"], "STRETCH")
        self.assertEqual(cue["tier"], "card")

    def test_no_stretch_on_pace(self):
        p, clock = live(show_1800())
        clock.advance(60)
        self.assertNotIn("stretch",
                         [c["key"] for c in p.cue_candidates()])

    def test_candidate_shape(self):
        p, clock = live(show_1800())
        clock.advance(1900)
        for cue in p.cue_candidates():
            self.assertEqual(set(cue), {"tier", "text", "key"})
            self.assertIn(cue["tier"], ("card", "attention"))


class TestRailState(unittest.TestCase):

    TOP_KEYS = {"live", "hold", "elapsed-s", "remaining-s", "show-state",
                "current", "next-point", "segments", "drop"}
    SEG_KEYS = {"id", "title", "kind", "planned-s", "replanned-s",
                "spent-s", "state", "timing", "points"}
    POINT_KEYS = {"text", "covered", "skipped"}

    def test_wire_shape_is_verbatim(self):
        p, clock = live(show_1800())
        clock.advance(200)
        state = p.tick()
        self.assertEqual(set(state), self.TOP_KEYS)
        for s in state["segments"]:
            self.assertEqual(set(s), self.SEG_KEYS)
            for point in s["points"]:
                self.assertEqual(set(point), self.POINT_KEYS)
        self.assertEqual(set(state["next-point"]),
                         {"segment", "idx", "text"})

    def test_tick_matches_state_property(self):
        p, clock = live(show_1800())
        clock.advance(100)
        self.assertEqual(p.tick(), p.state)

    def test_returned_state_is_a_deep_copy(self):
        p, _ = live(show_1800())
        state = p.state
        state["segments"][1]["points"][0]["covered"] = True
        state["live"] = False
        fresh = p.state
        self.assertFalse(fresh["segments"][1]["points"][0]["covered"])
        self.assertTrue(fresh["live"])


class TestRundownIntegration(unittest.TestCase):

    TEXT = """---
show: "Integration"
duration-minutes: 10
cue-density: chatty
wrap-minutes: 2
---

## Intro (2 min)

Scripted intro.

## Ideas

- first idea
- second idea

## Wrap (2 min)

Scripted wrap.
"""

    def test_producer_accepts_parse_rundown_output(self):
        p, clock = live(parse_rundown(self.TEXT))
        clock.advance(60)
        state = p.tick()
        self.assertEqual(state["elapsed-s"], 60)
        self.assertEqual([s["id"] for s in state["segments"]],
                         ["g0", "g1", "g2"])
        self.assertEqual(state["next-point"]["text"], "first idea")
        self.assertEqual(state["segments"][1]["planned-s"], 360)

    def test_frontmatter_cue_density_overrides_arg(self):
        p = Producer(parse_rundown(self.TEXT), cue_density="normal",
                     now_fn=FakeClock())
        self.assertEqual(p.cue_density, "chatty")

    def test_arg_used_when_frontmatter_silent(self):
        p = Producer(show_1800(), cue_density="minimal",
                     now_fn=FakeClock())
        self.assertEqual(p.cue_density, "minimal")


if __name__ == "__main__":
    unittest.main()
