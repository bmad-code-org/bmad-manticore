#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for captions.py: word-to-output-timeline mapping over reordered and
multi-source EDLs, the filler/stutter cleanup pass, cue grouping and wrapping
rules, SRT/VTT/transcript rendering, and the CLI end to end over synthetic
words.json + edl.json fixtures. Pure logic, no media, no network."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "captions.py"

spec = importlib.util.spec_from_file_location("captions", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def word(text, start, end, confidence=0.9):
    return {"word": text, "start": start, "end": end,
            "confidence": confidence}


def words_payload(media, words):
    return {"provider": "parakeet-mlx", "model": "m", "media": media,
            "duration": 60.0, "text": " ".join(w["word"] for w in words),
            "words": words}


def canned_words_a():
    # Source raw/a.mp4: intro sentence at 1-3 s, filler + point at 10-13 s.
    return [
        word("Alright,", 1.0, 1.4),
        word("welcome", 1.5, 1.9),
        word("back.", 2.0, 2.4),
        word("Um,", 10.0, 10.2),
        word("the", 10.3, 10.5),
        word("the", 10.6, 10.8),
        word("point", 10.9, 11.3),
        word("is", 11.4, 11.6),
        word("simple.", 11.7, 12.3),
    ]


def canned_words_b():
    return [
        word("Demo", 5.0, 5.4),
        word("time.", 5.5, 5.9),
    ]


def multi_source_edl():
    # Timeline: B[4.5,6.5) at 0-2, then A[9.5,13) at 2-5.5 (reordered join).
    return {
        "source": "raw/a.mp4",
        "source_duration": 60.0,
        "fade_ms": 30,
        "pad_ms": 60,
        "segments": [
            {"source": "raw/b.mp4", "start": 4.5, "end": 6.5},
            {"source": "raw/a.mp4", "start": 9.5, "end": 13.0},
        ],
    }


def single_source_edl():
    # Reorder within one source: A[9.5,13) first, then A[0.5,3) after it.
    return {
        "source": "raw/a.mp4",
        "source_duration": 60.0,
        "segments": [
            {"source": "raw/a.mp4", "start": 9.5, "end": 13.0},
            {"source": "raw/a.mp4", "start": 0.5, "end": 3.0},
        ],
    }


class TestCleanWords(unittest.TestCase):
    def test_fillers_dropped_and_repeats_collapsed(self):
        words = [
            {"word": "Um,", "start": 0.0, "end": 0.2},
            {"word": "the", "start": 0.3, "end": 0.5},
            {"word": "the", "start": 0.6, "end": 0.8},
            {"word": "point", "start": 0.9, "end": 1.2},
            {"word": "uh", "start": 1.3, "end": 1.4},
            {"word": "stands.", "start": 1.5, "end": 2.0},
        ]
        out, stats = mod.clean_words(words)
        self.assertEqual([w["word"] for w in out],
                         ["the", "point", "stands."])
        self.assertEqual(stats, {"fillers_dropped": 2,
                                 "repeats_collapsed": 1})
        # Collapsed word keeps the first start and the last end.
        self.assertEqual(out[0]["start"], 0.3)
        self.assertEqual(out[0]["end"], 0.8)
        # Input untouched.
        self.assertEqual(words[1]["start"], 0.3)
        self.assertEqual(len(words), 6)

    def test_hyphen_fragment_collapses_into_completion(self):
        words = [
            {"word": "th-", "start": 0.0, "end": 0.2},
            {"word": "the", "start": 0.3, "end": 0.5},
            {"word": "plan", "start": 0.6, "end": 1.0},
        ]
        out, stats = mod.clean_words(words)
        self.assertEqual([w["word"] for w in out], ["the", "plan"])
        self.assertEqual(out[0]["start"], 0.0)
        self.assertEqual(stats["repeats_collapsed"], 1)

    def test_disabled_keeps_everything(self):
        words = [
            {"word": "um", "start": 0.0, "end": 0.2},
            {"word": "the", "start": 0.3, "end": 0.5},
            {"word": "the", "start": 0.6, "end": 0.8},
        ]
        out, stats = mod.clean_words(words, enabled=False)
        self.assertEqual([w["word"] for w in out], ["um", "the", "the"])
        self.assertEqual(stats, {"fillers_dropped": 0,
                                 "repeats_collapsed": 0})

    def test_no_collapse_across_segments(self):
        words = [
            {"word": "go", "start": 0.9, "end": 1.0, "seg": 0},
            {"word": "Go", "start": 1.0, "end": 1.2, "seg": 1},
        ]
        out, stats = mod.clean_words(words)
        self.assertEqual(len(out), 2)
        self.assertEqual(stats["repeats_collapsed"], 0)


class TestAssignOutputTimes(unittest.TestCase):
    def test_multi_source_join_maps_by_segment_source(self):
        timed = mod.assign_output_times(multi_source_edl(), {
            "raw/a.mp4": canned_words_a(),
            "raw/b.mp4": canned_words_b(),
        })
        texts = [w["word"] for w in timed]
        self.assertEqual(texts, ["Demo", "time.", "Um,", "the", "the",
                                 "point", "is", "simple."])
        # B[4.5,6.5) at offset 0: "Demo" 5.0-5.4 -> 0.5-0.9.
        self.assertEqual((timed[0]["start"], timed[0]["end"]), (0.5, 0.9))
        self.assertEqual(timed[0]["_source"], "raw/b.mp4")
        self.assertEqual(timed[0]["seg"], 0)
        # A[9.5,13) at offset 2.0: "point" 10.9-11.3 -> 3.4-3.8.
        self.assertEqual((timed[5]["start"], timed[5]["end"]), (3.4, 3.8))
        self.assertEqual(timed[5]["seg"], 1)

    def test_reorder_within_one_source(self):
        timed = mod.assign_output_times(single_source_edl(),
                                        {"raw/a.mp4": canned_words_a()})
        texts = [w["word"] for w in timed]
        # Later material plays first, intro follows.
        self.assertEqual(texts, ["Um,", "the", "the", "point", "is",
                                 "simple.", "Alright,", "welcome", "back."])
        # "Alright," 1.0-1.4 in A[0.5,3) at offset 3.5 -> 4.0-4.4.
        self.assertEqual((timed[6]["start"], timed[6]["end"]), (4.0, 4.4))

    def test_midpoint_selection_and_span_clamp(self):
        edl = {"segments": [{"source": "s", "start": 1.0, "end": 2.0}]}
        timed = mod.assign_output_times(edl, {"s": [
            word("out", 0.0, 0.9),      # midpoint 0.45, excluded
            word("edge", 0.8, 1.4),     # midpoint 1.1, kept, start clamps
            word("gone", 1.9, 2.5),     # midpoint 2.2, excluded
        ]})
        self.assertEqual([w["word"] for w in timed], ["edge"])
        self.assertEqual((timed[0]["start"], timed[0]["end"]), (0.0, 0.4))


class TestMatchWordsFiles(unittest.TestCase):
    def test_explicit_media_and_basename_matching(self):
        edl = multi_source_edl()
        a = words_payload("raw/a.mp4", canned_words_a())
        b = words_payload("/abs/elsewhere/b.mp4", canned_words_b())
        resolved = mod.match_words_files(edl, [(None, a), (None, b)])
        self.assertEqual(resolved["raw/a.mp4"], a["words"])   # exact media
        self.assertEqual(resolved["raw/b.mp4"], b["words"])   # basename
        explicit = mod.match_words_files(edl, [("raw/a.mp4", b),
                                               ("raw/b.mp4", a)])
        self.assertEqual(explicit["raw/a.mp4"], b["words"])   # explicit wins

    def test_single_file_single_source_fallback(self):
        edl = {"segments": [{"source": "raw/take.mov",
                             "start": 0.0, "end": 5.0}]}
        payload = words_payload("something-else.wav", canned_words_a())
        resolved = mod.match_words_files(edl, [(None, payload)])
        self.assertEqual(resolved["raw/take.mov"], payload["words"])

    def test_unmatched_source_raises(self):
        with self.assertRaises(ValueError):
            mod.match_words_files(
                multi_source_edl(),
                [(None, words_payload("raw/a.mp4", canned_words_a()))])


class TestGroupCues(unittest.TestCase):
    def tw(self, text, start, end, seg=0):
        return {"word": text, "start": start, "end": end, "seg": seg}

    def test_sentence_end_and_pause_split(self):
        words = [
            self.tw("Hello.", 0.0, 0.5),
            self.tw("Next", 0.6, 0.9),
            self.tw("bit", 2.0, 2.4),  # 1.1 s pause before this word
        ]
        cues = mod.group_cues(words, pause_split=0.6)
        self.assertEqual([c["text"] for c in cues],
                         ["Hello.", "Next", "bit"])

    def test_segment_boundary_splits(self):
        words = [self.tw("one", 0.0, 0.4, seg=0),
                 self.tw("two", 0.4, 0.8, seg=1)]
        cues = mod.group_cues(words)
        self.assertEqual(len(cues), 2)

    def test_line_capacity_split_and_wrap(self):
        words = [self.tw("x" * 20, i * 0.4, i * 0.4 + 0.3)
                 for i in range(5)]
        cues = mod.group_cues(words, max_line_chars=42, max_lines=2)
        # 5 x 20 chars cannot wrap into 2 lines of 42; first cue holds 4.
        self.assertEqual(len(cues), 2)
        self.assertEqual(len(cues[0]["words"]), 4)
        self.assertEqual(cues[0]["lines"], ["x" * 20 + " " + "x" * 20] * 2)
        self.assertTrue(all(len(line) <= 42 for line in cues[0]["lines"]))

    def test_max_duration_split(self):
        words = [self.tw("a", 0.0, 0.2), self.tw("b", 7.5, 7.8)]
        cues = mod.group_cues(words, max_cue_seconds=7.0, pause_split=99)
        self.assertEqual(len(cues), 2)

    def test_min_duration_extends_but_not_past_next_cue(self):
        words = [self.tw("Hi.", 0.0, 0.3), self.tw("There.", 0.8, 1.1)]
        cues = mod.group_cues(words, min_cue_seconds=1.0)
        self.assertEqual(cues[0]["end"], 0.8)   # clamped to next cue start
        self.assertEqual(cues[1]["end"], 1.8)   # free to extend

    def test_wrap_lines_keeps_long_word_whole(self):
        self.assertEqual(mod.wrap_lines("a bb ccc", 4), ["a bb", "ccc"])
        self.assertEqual(mod.wrap_lines("supercalifragilistic", 5),
                         ["supercalifragilistic"])


class TestRendering(unittest.TestCase):
    def test_time_formats(self):
        self.assertEqual(mod.format_srt_time(1.28), "00:00:01,280")
        self.assertEqual(mod.format_srt_time(3661.5), "01:01:01,500")
        self.assertEqual(mod.format_vtt_time(1.28), "00:00:01.280")
        self.assertEqual(mod.format_srt_time(-0.5), "00:00:00,000")

    def test_srt_and_vtt_blocks(self):
        cues = [{"start": 0.0, "end": 1.5, "lines": ["Hello there."],
                 "text": "Hello there.", "words": []},
                {"start": 2.0, "end": 4.0, "lines": ["Line one", "line two"],
                 "text": "Line one line two", "words": []}]
        srt = mod.render_srt(cues)
        self.assertIn("1\n00:00:00,000 --> 00:00:01,500\nHello there.", srt)
        self.assertIn("2\n00:00:02,000 --> 00:00:04,000\n"
                      "Line one\nline two", srt)
        vtt = mod.render_vtt(cues)
        self.assertTrue(vtt.startswith("WEBVTT\n\n"))
        self.assertIn("00:00:02.000 --> 00:00:04.000\nLine one\nline two",
                      vtt)
        self.assertNotIn(",", vtt.splitlines()[2])

    def test_transcript_paragraphs(self):
        words = [
            {"word": "First", "start": 0.0, "end": 0.4, "seg": 0},
            {"word": "part.", "start": 0.5, "end": 0.9, "seg": 0},
            {"word": "Second", "start": 65.0, "end": 65.4, "seg": 1},
            {"word": "part.", "start": 65.5, "end": 65.9, "seg": 1},
        ]
        md = mod.render_transcript(words)
        self.assertIn("# Transcript", md)
        self.assertIn("[0:00] First part.", md)
        self.assertIn("[1:05] Second part.", md)


def run_cli(args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True)


class TestCli(unittest.TestCase):
    def write_fixtures(self, tmp):
        tmp = Path(tmp)
        edl = tmp / "edl.json"
        edl.write_text(json.dumps(multi_source_edl()))
        wa = tmp / "a.words.json"
        wa.write_text(json.dumps(words_payload("raw/a.mp4",
                                               canned_words_a())))
        wb = tmp / "b.words.json"
        wb.write_text(json.dumps(words_payload("raw/b.mp4",
                                               canned_words_b())))
        return edl, wa, wb

    def test_end_to_end_multi_source_with_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            edl, wa, wb = self.write_fixtures(tmp)
            out_dir = Path(tmp) / "captions"
            r = run_cli([str(edl), "--words", str(wa), "--words", str(wb),
                         "--out-dir", str(out_dir)])
            self.assertEqual(r.returncode, 0, r.stderr)
            summary = json.loads(r.stdout)
            self.assertTrue(summary["ok"])
            self.assertTrue(summary["clean"])
            self.assertEqual(summary["fillers_dropped"], 1)
            self.assertEqual(summary["repeats_collapsed"], 1)
            srt = (out_dir / "final.srt").read_text()
            vtt = (out_dir / "final.vtt").read_text()
            md = (out_dir / "transcript.md").read_text()
            # B material captions first, cleaned A material follows.
            self.assertIn("Demo time.", srt)
            self.assertIn("the point is simple.", srt)
            self.assertNotIn("Um,", srt)
            self.assertNotIn("the the", srt)
            self.assertLess(srt.index("Demo time."),
                            srt.index("the point is simple."))
            # "Demo" starts at 0.5 s on the edited timeline.
            self.assertIn("00:00:00,500 -->", srt)
            self.assertTrue(vtt.startswith("WEBVTT"))
            self.assertIn("00:00:00.500 -->", vtt)
            self.assertIn("[0:00] Demo time.", md)

    def test_no_clean_keeps_fillers(self):
        with tempfile.TemporaryDirectory() as tmp:
            edl, wa, wb = self.write_fixtures(tmp)
            out_dir = Path(tmp) / "captions"
            r = run_cli([str(edl), "--words", str(wa), "--words", str(wb),
                         "--out-dir", str(out_dir), "--no-clean"])
            self.assertEqual(r.returncode, 0, r.stderr)
            summary = json.loads(r.stdout)
            self.assertFalse(summary["clean"])
            self.assertEqual(summary["fillers_dropped"], 0)
            srt = (out_dir / "final.srt").read_text()
            self.assertIn("Um,", srt)
            self.assertIn("the the", srt)

    def test_explicit_source_binding_and_basename(self):
        with tempfile.TemporaryDirectory() as tmp:
            edl, wa, wb = self.write_fixtures(tmp)
            out_dir = Path(tmp) / "captions"
            r = run_cli([str(edl), "--words", f"raw/a.mp4={wa}",
                         "--words", f"raw/b.mp4={wb}",
                         "--out-dir", str(out_dir), "--basename", "edited"])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((out_dir / "edited.srt").exists())
            self.assertTrue((out_dir / "edited.vtt").exists())
            self.assertTrue((out_dir / "transcript.md").exists())

    def test_unmatched_source_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            edl, wa, _wb = self.write_fixtures(tmp)
            r = run_cli([str(edl), "--words", str(wa),
                         "--out-dir", str(Path(tmp) / "captions")])
            self.assertEqual(r.returncode, 1)
            self.assertIn("raw/b.mp4", r.stderr)

    def test_missing_required_args_exit_2(self):
        r = run_cli(["edl.json"])
        self.assertEqual(r.returncode, 2)
        r = run_cli(["edl.json", "--words", "w.json"])
        self.assertEqual(r.returncode, 2)

    def test_missing_edl_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_cli(["/nonexistent/edl.json", "--words",
                         str(Path(tmp) / "w.json"), "--out-dir", tmp])
            self.assertEqual(r.returncode, 1)

    def test_no_words_on_timeline_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            edl = Path(tmp) / "edl.json"
            edl.write_text(json.dumps({"segments": [
                {"source": "raw/a.mp4", "start": 50.0, "end": 55.0}]}))
            wa = Path(tmp) / "a.words.json"
            wa.write_text(json.dumps(words_payload("raw/a.mp4",
                                                   canned_words_a())))
            r = run_cli([str(edl), "--words", str(wa), "--out-dir", tmp])
            self.assertEqual(r.returncode, 1)
            self.assertIn("no words", r.stderr)


if __name__ == "__main__":
    unittest.main()
