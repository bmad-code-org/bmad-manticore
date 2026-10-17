#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for verify_transcript.py, the transcript completeness gate.

The headline cases are the two from the real failure (2026-07-24): the broken
transcript must FAIL and the good one must PASS. Everything else here defends
the property that makes that work, namely that the check is computed from the
AUDIO side and so does not inherit the pause-absorption bug it exists to
catch."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "verify_transcript.py"
spec = importlib.util.spec_from_file_location("verify_transcript", SCRIPT)
vt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vt)


def words_from(spans):
    """Word dicts from (text, start, end) triples."""
    return [{"word": t, "start": s, "end": e, "confidence": 1.0, "i": i,
             "gap_before": 0.0, "gap_after": 0.0}
            for i, (t, s, e) in enumerate(spans)]


def audio_map(duration, silence, media="m.mp4"):
    pairs = [(s, e) for s, e in silence]
    speech = []
    cursor = 0.0
    for s, e in pairs:
        if s > cursor:
            speech.append((cursor, s))
        cursor = max(cursor, e)
    if duration > cursor:
        speech.append((cursor, duration))
    return {
        "media": media,
        "duration": duration,
        "noise_db": -30.0,
        "min_silence": 0.3,
        "silent_seconds": sum(e - s for s, e in pairs),
        "speech_seconds": sum(e - s for s, e in speech),
        "counts": {"silence": len(pairs), "speech": len(speech)},
        "silence": [{"start": s, "end": e, "dur": e - s} for s, e in pairs],
        "speech": [{"start": s, "end": e, "dur": e - s} for s, e in speech],
    }


def run_cli(transcript, amap, extra=None, expect=None):
    with tempfile.TemporaryDirectory() as tmp:
        w = Path(tmp) / "words.json"
        a = Path(tmp) / "audio-map.json"
        o = Path(tmp) / "report.json"
        w.write_text(json.dumps(transcript))
        a.write_text(json.dumps(amap))
        r = subprocess.run(
            [sys.executable, str(SCRIPT), str(w), "--audio-map", str(a),
             "-o", str(o), *(extra or [])],
            capture_output=True, text=True)
        if expect is not None:
            assert r.returncode == expect, f"rc={r.returncode} {r.stderr}"
        report = json.loads(o.read_text()) if o.is_file() else None
        return report, r


class TestUncoveredRegions(unittest.TestCase):
    """Audible with no words. The complement of (silence + word spans)."""

    def test_speech_fully_covered_by_words_leaves_nothing(self):
        words = words_from([("a", 1.0, 2.0), ("b", 2.0, 3.0)])
        silence = [(0.0, 1.0), (3.0, 5.0)]
        self.assertEqual(vt.uncovered_regions(silence, words, 5.0), [])

    def test_audible_span_with_no_words_is_uncovered(self):
        words = words_from([("a", 1.0, 2.0)])
        silence = [(0.0, 1.0), (2.0, 3.0)]
        # 3.0 to 20.0 is neither silent nor transcribed: dropped speech.
        self.assertEqual(vt.uncovered_regions(silence, words, 20.0),
                         [(3.0, 20.0)])

    def test_silence_with_no_words_is_not_uncovered(self):
        words = words_from([("a", 1.0, 2.0)])
        silence = [(0.0, 1.0), (2.0, 20.0)]
        self.assertEqual(vt.uncovered_regions(silence, words, 20.0), [])

    def test_pause_absorbed_word_end_cannot_fake_coverage(self):
        # The failure mode this design defends against: parakeet extends a
        # word's end across the pause. Even if the last kept word's end
        # reaches 2s into an 18s dropped region, 16s stay uncovered.
        words = words_from([("about.", 30.0, 34.0)])
        silence = [(0.0, 30.0)]
        regions = vt.uncovered_regions(silence, words, 52.0)
        self.assertEqual(regions, [(34.0, 52.0)])

    def test_words_outside_any_speech_still_count_as_coverage(self):
        words = words_from([("a", 0.5, 1.5)])
        silence = [(0.0, 5.0)]
        self.assertEqual(vt.uncovered_regions(silence, words, 5.0), [])


class TestCluster(unittest.TestCase):
    def test_near_pieces_merge(self):
        self.assertEqual(vt.cluster([(1.0, 2.0), (2.5, 3.0)], 1.0),
                         [(1.0, 3.0)])

    def test_far_pieces_stay_apart(self):
        self.assertEqual(vt.cluster([(1.0, 2.0), (9.0, 10.0)], 1.0),
                         [(1.0, 2.0), (9.0, 10.0)])

    def test_empty_is_empty(self):
        self.assertEqual(vt.cluster([], 1.0), [])


class TestFindDropped(unittest.TestCase):
    def test_long_uncovered_region_is_reported(self):
        words = words_from([("a", 1.0, 2.0)])
        silence = [(0.0, 1.0), (2.0, 3.0)]
        dropped = vt.find_dropped(silence, words, 20.0)
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["start"], 3.0)
        self.assertEqual(dropped[0]["audible"], 17.0)

    def test_short_uncovered_region_is_below_threshold(self):
        words = words_from([("a", 1.0, 2.0)])
        silence = [(0.0, 1.0), (2.5, 20.0)]
        # Only 0.5s uncovered (2.0 to 2.5), under the 1.0s default.
        self.assertEqual(vt.find_dropped(silence, words, 20.0), [])

    def test_threshold_applies_to_audible_not_the_cluster_span(self):
        # Two 1.0s uncovered pieces separated by a 0.5s covered stretch
        # cluster into a 2.5s span, but only 2.0s of it is audible, so
        # clustering must not push it over a 2.5s threshold.
        words = words_from([("a", 0.0, 1.0), ("b", 2.0, 2.5)])
        silence = []
        dropped = vt.find_dropped(silence, words, 3.5, min_drop=2.5,
                                  cluster_gap=1.0)
        self.assertEqual(dropped, [])

    def test_reported_region_carries_a_readable_timecode(self):
        words = words_from([("a", 1.0, 2.0)])
        silence = [(0.0, 1.0), (2.0, 89.0)]
        dropped = vt.find_dropped(silence, words, 120.0)
        self.assertEqual(dropped[0]["at"], "1:29.0")

    def test_min_drop_is_configurable(self):
        words = words_from([("a", 1.0, 2.0)])
        silence = [(0.0, 1.0), (4.0, 20.0)]  # 2.0s uncovered
        self.assertEqual(vt.find_dropped(silence, words, 20.0, min_drop=2.5),
                         [])
        self.assertEqual(
            len(vt.find_dropped(silence, words, 20.0, min_drop=1.5)), 1)


class TestWordRate(unittest.TestCase):
    def test_speech_rate_excludes_dead_air(self):
        rate = vt.word_rate([0] * 100, duration=120.0, speech_seconds=60.0)
        self.assertEqual(rate["wall_wpm"], 50.0)
        self.assertEqual(rate["speech_wpm"], 100.0)

    def test_below_floor_fails(self):
        rate = vt.word_rate([0] * 50, duration=120.0, speech_seconds=60.0,
                            wpm=200.0, floor_ratio=0.6)
        self.assertFalse(rate["ok"])
        self.assertEqual(rate["floor_wpm"], 120.0)

    def test_above_floor_passes(self):
        rate = vt.word_rate([0] * 200, duration=120.0, speech_seconds=60.0,
                            wpm=200.0, floor_ratio=0.6)
        self.assertTrue(rate["ok"])

    def test_no_configured_wpm_skips_the_check(self):
        rate = vt.word_rate([0] * 5, duration=120.0, speech_seconds=60.0)
        self.assertTrue(rate["ok"])
        self.assertIn("skipped", rate["note"])

    def test_zero_duration_does_not_divide_by_zero(self):
        rate = vt.word_rate([], duration=0.0, speech_seconds=0.0, wpm=200.0)
        self.assertEqual(rate["wall_wpm"], 0.0)
        self.assertEqual(rate["speech_wpm"], 0.0)


class TestTheRealFailure(unittest.TestCase):
    """The 2026-07-24 take: 20.5 minutes, 3 dropped paragraphs.

    Reduced to the essential shape. The good transcript covers all the
    speech; the broken one is missing three audible regions."""

    DURATION = 1230.0
    # Three dropped regions at the reported timecodes: 0:49, 1:27, 9:37.
    DROPS = [(49.0, 58.0), (87.0, 105.0), (577.0, 585.0)]

    def _silence(self):
        # Dead air spread through the take, none of it overlapping a drop.
        sil = [(0.0, 1.0)]
        t = 200.0
        while t < 560.0:
            sil.append((t, t + 2.0))
            t += 20.0
        t = 600.0
        while t < 1200.0:
            sil.append((t, t + 2.0))
            t += 20.0
        sil.append((1225.0, 1230.0))
        return sil

    WORD_S = 0.33  # about 180 wpm over speech time, a plausible read

    def _speech(self):
        silence = self._silence()
        out = []
        cursor = 0.0
        for s, e in silence:
            if s > cursor:
                out.append((cursor, s))
            cursor = max(cursor, e)
        if self.DURATION > cursor:
            out.append((cursor, self.DURATION))
        return out

    def _words(self, include_drops):
        """Words tiling every speech interval end to end.

        Contiguous by construction, so the only uncovered audio in the good
        case is nothing at all: any region this gate reports is a region the
        fixture deliberately dropped, never a tiling artifact."""
        spans = []
        idx = 0
        for s, e in self._speech():
            t = s
            while t < e - 1e-9:
                end = min(t + self.WORD_S, e)
                # Absorb a final sliver into the previous word so speech is
                # covered exactly.
                if e - end < self.WORD_S / 2:
                    end = e
                if include_drops or not any(ds <= t < de
                                            for ds, de in self.DROPS):
                    # A real read carries fillers; the gate checks for them.
                    text = "um" if idx % 50 == 0 else f"w{idx}"
                    spans.append((text, round(t, 3), round(end, 3)))
                idx += 1
                t = end
        return words_from(spans)

    def _transcript(self, words):
        return {"provider": "parakeet-mlx", "media": "m.mp4",
                "duration": self.DURATION, "text": "", "words": words}

    def test_good_transcript_passes(self):
        amap = audio_map(self.DURATION, self._silence())
        report = vt.build_report(self._transcript(self._words(True)), amap,
                                 wpm=198)
        self.assertTrue(report["ok"], report["dropped_regions"])
        self.assertEqual(report["dropped_regions"], [])

    def test_broken_transcript_fails(self):
        amap = audio_map(self.DURATION, self._silence())
        report = vt.build_report(self._transcript(self._words(False)), amap,
                                 wpm=198)
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["dropped_regions"]), 3)

    def test_broken_transcript_names_the_right_regions(self):
        amap = audio_map(self.DURATION, self._silence())
        report = vt.build_report(self._transcript(self._words(False)), amap,
                                 wpm=198)
        starts = sorted(d["start"] for d in report["dropped_regions"])
        for expected, got in zip([49.0, 87.0, 577.0], starts):
            self.assertAlmostEqual(got, expected, delta=1.5)

    def test_word_rate_alone_would_NOT_have_caught_it(self):
        """The documented caveat, pinned as a test.

        Three missing paragraphs are a ~3 percent word deficit in a 20 minute
        take, so the rate check passes on the BROKEN transcript. If this test
        ever starts failing, the rate check has become sensitive enough to
        matter and the docstring's caveat needs revisiting; until then, the
        coverage scan is the only thing standing between a Swiss-cheese
        transcript and a shipped cut."""
        amap = audio_map(self.DURATION, self._silence())
        broken = vt.build_report(self._transcript(self._words(False)), amap,
                                 wpm=198)
        self.assertTrue(broken["checks"]["word_rate"]["ok"])
        self.assertFalse(broken["checks"]["coverage"]["ok"])

    def test_catastrophic_truncation_does_trip_the_rate_check(self):
        amap = audio_map(self.DURATION, self._silence())
        half = self._words(True)[:len(self._words(True)) // 4]
        report = vt.build_report(self._transcript(half), amap, wpm=198)
        self.assertFalse(report["checks"]["word_rate"]["ok"])


class TestLowConfidence(unittest.TestCase):
    def test_run_of_low_confidence_words_is_reported(self):
        words = words_from([(f"w{i}", i * 1.0, i * 1.0 + 0.5)
                            for i in range(8)])
        for w in words[2:7]:
            w["confidence"] = 0.1
        runs = vt.low_confidence_runs(words)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["words"], 5)

    def test_short_run_is_ignored(self):
        words = words_from([(f"w{i}", i * 1.0, i * 1.0 + 0.5)
                            for i in range(8)])
        words[3]["confidence"] = 0.1
        self.assertEqual(vt.low_confidence_runs(words), [])

    def test_confidence_never_fails_the_gate(self):
        words = words_from([(f"w{i}", i * 1.0, i * 1.0 + 0.9)
                            for i in range(20)])
        for w in words:
            w["confidence"] = 0.0
        # The map must carry the 0.1s inter-word gaps, as a real one built at
        # the 0.1s default does; omitting them would make ordinary pauses
        # read as untranscribed audio.
        amap = audio_map(20.0, [(i * 1.0 + 0.9, (i + 1) * 1.0)
                                for i in range(19)] + [(19.9, 20.0)])
        report = vt.build_report({"words": words}, amap)
        self.assertTrue(report["ok"], report["dropped_regions"])
        self.assertTrue(report["checks"]["confidence"]["ok"])
        self.assertEqual(len(report["low_confidence"]), 1)


class TestMapGranularityGuard(unittest.TestCase):
    """A coarse audio map omits natural inter-word gaps, which then read as
    untranscribed audio. Explain that rather than failing a good transcript
    with no reason given."""

    def test_fine_map_produces_no_warning(self):
        self.assertIsNone(vt.map_too_coarse({"min_silence": 0.1}))

    def test_boundary_granularity_is_acceptable(self):
        self.assertIsNone(vt.map_too_coarse({"min_silence": 0.2}))

    def test_coarse_map_warns(self):
        warning = vt.map_too_coarse({"min_silence": 0.5})
        self.assertIsNotNone(warning)
        self.assertIn("--map-granularity", warning)

    def test_warning_names_the_remedy(self):
        self.assertIn("Regenerate the map",
                      vt.map_too_coarse({"min_silence": 0.5}))

    def test_missing_granularity_does_not_warn(self):
        self.assertIsNone(vt.map_too_coarse({}))

    def test_warning_rides_on_the_report(self):
        words = words_from([("a", 1.0, 2.0)])
        amap = audio_map(5.0, [(0.0, 1.0), (2.0, 5.0)])
        amap["min_silence"] = 0.9
        self.assertIsNotNone(vt.build_report({"words": words},
                                             amap)["map_warning"])


class TestCli(unittest.TestCase):
    def _good(self):
        words = words_from([(f"w{i}", i * 1.0, i * 1.0 + 0.9)
                            for i in range(10)])
        return {"duration": 11.0, "words": words}, audio_map(11.0,
                                                             [(10.0, 11.0)])

    def test_passing_transcript_exits_zero(self):
        t, a = self._good()
        report, r = run_cli(t, a, expect=0)
        self.assertTrue(report["ok"])

    def test_failing_transcript_exits_one_and_names_regions(self):
        t, a = self._good()
        a = audio_map(60.0, [(50.0, 60.0)])  # 40s of speech with no words
        _, r = run_cli(t, a, expect=1)
        self.assertIn("TRANSCRIPT INCOMPLETE", r.stderr)
        self.assertIn("dropped speech at", r.stderr)

    def test_failure_message_points_at_the_remediation(self):
        t, _ = self._good()
        a = audio_map(60.0, [(50.0, 60.0)])
        _, r = run_cli(t, a, expect=1)
        self.assertIn("--window", r.stderr)

    def test_missing_audio_map_is_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = Path(tmp) / "words.json"
            w.write_text(json.dumps({"duration": 1.0, "words": []}))
            r = subprocess.run(
                [sys.executable, str(SCRIPT), str(w), "--audio-map",
                 str(Path(tmp) / "nope.json")],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)

    def test_audio_map_missing_keys_is_usage_error(self):
        t, _ = self._good()
        with tempfile.TemporaryDirectory() as tmp:
            w = Path(tmp) / "words.json"
            a = Path(tmp) / "audio-map.json"
            w.write_text(json.dumps(t))
            a.write_text(json.dumps({"duration": 1.0}))
            r = subprocess.run(
                [sys.executable, str(SCRIPT), str(w), "--audio-map", str(a)],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)
            self.assertIn("analyze_audio.py", r.stderr)

    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--wpm", r.stdout)


class TestParseRegion(unittest.TestCase):

    def test_plain_region(self):
        self.assertEqual(vt.parse_region("12.5-18.0"), (12.5, 18.0))

    def test_integers(self):
        self.assertEqual(vt.parse_region("3-9"), (3.0, 9.0))

    def test_whitespace_is_tolerated(self):
        self.assertEqual(vt.parse_region("  4.0-5.0 "), (4.0, 5.0))

    def test_backwards_region_is_rejected(self):
        with self.assertRaises(ValueError):
            vt.parse_region("9-3")

    def test_zero_length_region_is_rejected(self):
        with self.assertRaises(ValueError):
            vt.parse_region("5-5")

    def test_garbage_is_rejected(self):
        with self.assertRaises(ValueError):
            vt.parse_region("sometime")


class TestAcceptances(unittest.TestCase):
    """The escape valve: blocking by default, past it only with a reason."""

    def setUp(self):
        self.dropped = [{"start": 10.0, "end": 14.0, "audible": 4.0,
                         "at": "0:10.0"}]

    def test_no_acceptances_leaves_everything_failing(self):
        remaining, cleared = vt.apply_acceptances(self.dropped, [])
        self.assertEqual(len(remaining), 1)
        self.assertEqual(cleared, [])

    def test_a_fully_covering_acceptance_clears_the_region(self):
        accepted = [{"start": 9.0, "end": 15.0, "reason": "audience laugh"}]
        remaining, cleared = vt.apply_acceptances(self.dropped, accepted)
        self.assertEqual(remaining, [])
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0]["accepted_by"]["reason"], "audience laugh")

    def test_an_exactly_covering_acceptance_clears_the_region(self):
        accepted = [{"start": 10.0, "end": 14.0, "reason": "music bed"}]
        remaining, _ = vt.apply_acceptances(self.dropped, accepted)
        self.assertEqual(remaining, [])

    def test_partial_coverage_does_not_clear(self):
        # The creator listened to 10-12. The region runs to 14. What is in
        # 12-14 is still unaccounted for, so it keeps failing.
        accepted = [{"start": 10.0, "end": 12.0, "reason": "laugh"}]
        remaining, cleared = vt.apply_acceptances(self.dropped, accepted)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(cleared, [])

    def test_an_unrelated_acceptance_does_not_clear(self):
        accepted = [{"start": 100.0, "end": 200.0, "reason": "elsewhere"}]
        remaining, _ = vt.apply_acceptances(self.dropped, accepted)
        self.assertEqual(len(remaining), 1)

    def test_only_the_covered_region_clears(self):
        dropped = self.dropped + [{"start": 40.0, "end": 44.0,
                                   "audible": 4.0, "at": "0:40.0"}]
        accepted = [{"start": 9.0, "end": 15.0, "reason": "laugh"}]
        remaining, cleared = vt.apply_acceptances(dropped, accepted)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["start"], 40.0)
        self.assertEqual(len(cleared), 1)


class TestAcceptanceCLI(unittest.TestCase):
    """A run that fails must pass once the region is signed off, and only then."""

    def broken(self):
        # 3 seconds of audible speech with no words over it.
        words = words_from([("hello", 0.0, 0.33), ("there", 0.33, 0.66)])
        amap = audio_map(10.0, [(0.66, 3.0), (6.0, 10.0)])
        return words, amap

    def test_the_region_fails_without_an_acceptance(self):
        words, amap = self.broken()
        report, r = run_cli({"words": words}, amap, expect=1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"]["coverage"]["dropped_regions"], 1)

    def test_accepting_the_region_passes_the_gate(self):
        words, amap = self.broken()
        report, r = run_cli(
            {"words": words}, amap,
            extra=["--accept-region", "2.9-6.1", "--reason", "music bed"],
            expect=0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["checks"]["coverage"]["accepted_regions"], 1)

    def test_the_acceptance_and_its_reason_are_recorded(self):
        words, amap = self.broken()
        report, _ = run_cli(
            {"words": words}, amap,
            extra=["--accept-region", "2.9-6.1", "--reason", "music bed"],
            expect=0)
        self.assertEqual(report["accepted"][0]["accepted_by"]["reason"],
                         "music bed")

    def test_acceptance_without_a_reason_is_a_usage_error(self):
        words, amap = self.broken()
        _, r = run_cli({"words": words}, amap,
                       extra=["--accept-region", "2.9-6.1"], expect=2)
        self.assertIn("requires --reason", r.stderr)

    def test_an_empty_reason_is_a_usage_error(self):
        words, amap = self.broken()
        run_cli({"words": words}, amap,
                extra=["--accept-region", "2.9-6.1", "--reason", "  "],
                expect=2)

    def test_a_reason_with_no_region_is_a_usage_error(self):
        words, amap = self.broken()
        run_cli({"words": words}, amap, extra=["--reason", "why"], expect=2)

    def test_a_malformed_region_is_a_usage_error(self):
        words, amap = self.broken()
        _, r = run_cli({"words": words}, amap,
                       extra=["--accept-region", "nope", "--reason", "x"],
                       expect=2)
        self.assertIn("bad --accept-region", r.stderr)

    def test_a_partial_acceptance_still_fails_the_gate(self):
        words, amap = self.broken()
        report, _ = run_cli(
            {"words": words}, amap,
            extra=["--accept-region", "3.0-4.0", "--reason", "partial"],
            expect=1)
        self.assertFalse(report["ok"])


class TestVerbatimFillers(unittest.TestCase):
    """A transcriber that normalizes fillers away loses no speech, so only
    this check catches it (Resolve 21.1 transcription, 2026-10-16)."""

    def words(self, n, filler_every=None):
        return words_from([
            ("uh," if filler_every and i % filler_every == 0 else f"w{i}",
             i * 0.3, i * 0.3 + 0.3) for i in range(n)])

    def test_zero_fillers_over_enough_speech_fails(self):
        self.assertFalse(vt.filler_check(self.words(600), 180.0)["ok"])

    def test_fillers_pass_and_punctuation_is_ignored(self):
        r = vt.filler_check(self.words(600, filler_every=100), 180.0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["fillers"], 6)

    def test_short_speech_is_not_judged(self):
        self.assertTrue(vt.filler_check(self.words(200), 60.0)["ok"])

    def test_creator_acceptance_passes_and_is_recorded(self):
        r = vt.filler_check(self.words(600), 180.0,
                            accepted_reason="scripted read")
        self.assertTrue(r["ok"])
        self.assertEqual(r["accepted_reason"], "scripted read")

    def test_cli_fails_a_normalized_transcript(self):
        words = self.words(600)
        amap = audio_map(180.0, [])
        report, r = run_cli({"words": words}, amap, expect=1)
        self.assertFalse(report["checks"]["fillers"]["ok"])
        self.assertIn("NOT VERBATIM", r.stderr)

    def test_cli_acceptance_needs_a_reason(self):
        run_cli({"words": self.words(600)}, audio_map(180.0, []),
                extra=["--accept-no-fillers", " "], expect=2)


if __name__ == "__main__":
    unittest.main()
