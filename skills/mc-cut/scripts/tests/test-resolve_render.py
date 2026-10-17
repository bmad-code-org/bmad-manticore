#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for resolve_render.py: preset fallback, timeline length, render
identity, supersede detection, stream checks and the loudness gate. The
Resolve render itself runs against a live Studio instance, not here."""
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "resolve_render.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("resolve_render", SCRIPT)
rr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rr)


class TestPreset(unittest.TestCase):
    def test_configured_preset_is_used_silently(self):
        self.assertEqual(rr.resolve_preset(" YouTube - 2160p "),
                         ("YouTube - 2160p", None))

    def test_empty_preset_falls_back_out_loud(self):
        preset, notice = rr.resolve_preset("")
        self.assertEqual(preset, "H.264 Master")
        self.assertIn("H.264 Master", notice)


class TestLength(unittest.TestCase):
    def test_end_frame_is_exclusive(self):
        self.assertAlmostEqual(rr.expected_seconds(0, 5949, 30), 198.3)
        self.assertAlmostEqual(rr.expected_seconds(86400, 86430, 30), 1.0)


class TestFingerprint(unittest.TestCase):
    ROWS = [["video", 1, 0, 60, 300, "take.mp4", None],
            ["audio", 1, 0, 60, 300, "take.mp4", 0.0]]
    PARAMS = {"preset": "H.264 Master", "loudness": "YouTube"}

    def test_stable_for_the_same_timeline(self):
        self.assertEqual(rr.fingerprint(self.ROWS, self.PARAMS),
                         rr.fingerprint([list(r) for r in self.ROWS],
                                        dict(self.PARAMS)))

    def test_any_edit_changes_it(self):
        base = rr.fingerprint(self.ROWS, self.PARAMS)
        moved = [list(r) for r in self.ROWS]
        moved[0][4] += 1
        regained = [list(r) for r in self.ROWS]
        regained[1][6] = 6.0
        self.assertNotEqual(base, rr.fingerprint(moved, self.PARAMS))
        self.assertNotEqual(base, rr.fingerprint(regained, self.PARAMS))
        self.assertNotEqual(base, rr.fingerprint(
            self.ROWS, {**self.PARAMS, "preset": "ProRes 422 HQ"}))


class TestSupersede(unittest.TestCase):
    def test_newer_different_sidecar_supersedes(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "final.mp4.key"
            started = time.time() - 10
            key.write_text("other\n")
            self.assertTrue(rr.superseded_by_sidecar(key, started, "mine"))
            self.assertFalse(rr.superseded_by_sidecar(key, started, "other"))
            os.utime(key, (started - 5, started - 5))
            self.assertFalse(rr.superseded_by_sidecar(key, started, "mine"))
            self.assertFalse(rr.superseded_by_sidecar(
                Path(tmp) / "none.key", started, "mine"))


class TestPublishPath(unittest.TestCase):
    def test_takes_the_rendered_extension(self):
        self.assertEqual(rr.publish_path("r/final.mp4", "t/final.mov"),
                         Path("r/final.mov"))
        self.assertEqual(rr.publish_path("r/final.mp4", "t/final.mp4"),
                         Path("r/final.mp4"))


class TestStreams(unittest.TestCase):
    def streams(self, frames=5949, audio=198.378667):
        return [{"codec_type": "video", "nb_frames": str(frames),
                 "duration": str(frames / 30)},
                {"codec_type": "audio", "duration": str(audio)},
                {"codec_type": "data"}]

    def test_measured_resolve_render_passes(self):
        self.assertEqual(rr.check_streams(self.streams(), 5949, 30), [])

    def test_one_frame_short_fails(self):
        self.assertTrue(rr.check_streams(self.streams(frames=5948), 5949, 30))

    def test_audio_overrun_past_padding_fails(self):
        self.assertTrue(rr.check_streams(self.streams(audio=198.6), 5949, 30))
        self.assertTrue(rr.check_streams(self.streams(audio=197.9), 5949, 30))

    def test_missing_audio_fails(self):
        p = rr.check_streams(self.streams()[:1], 5949, 30)
        self.assertEqual(p, ["no audio stream"])


class TestLoudness(unittest.TestCase):
    SUMMARY = """
  Integrated loudness:
    I:         -21.6 LUFS
    Threshold: -31.8 LUFS
  True peak:
    Peak:       -1.1 dBFS"""

    def test_parses_ffmpeg_summary(self):
        self.assertEqual(rr.parse_ebur128(self.SUMMARY), (-21.6, -1.1))

    def test_peak_limited_render_misses_youtube(self):
        self.assertIn("-21.6", rr.loudness_problem("YouTube", -21.6))
        self.assertIsNone(rr.loudness_problem("YouTube", -14.6))

    def test_modes_without_a_known_target_are_not_judged(self):
        self.assertIsNone(rr.loudness_problem("True Peak", -30.0))
        self.assertIsNone(rr.loudness_problem("", -30.0))


if __name__ == "__main__":
    unittest.main()
