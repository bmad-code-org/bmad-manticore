#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for verify_captions.py, the captions gate: SRT parsing, the
structure checks, and speech coverage against edited-timeline words,
including the CLI exit codes."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "verify_captions.py"
spec = importlib.util.spec_from_file_location("verify_captions", SCRIPT)
vc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vc)

SRT = """1
00:00:00,000 --> 00:00:02,500
Hello there.

2
00:00:03,000 --> 00:00:05,000
Second line
wraps here.
"""


def w(text, a, b):
    return {"word": text, "clean_start": a, "clean_end": b}


WORDS = [w("Hello", 0.1, 0.5), w("there.", 0.6, 1.0), w("um", 2.6, 2.8),
         w("Second", 3.1, 3.5), w("line", 3.6, 3.9), w("wraps", 4.0, 4.3),
         w("here.", 4.4, 4.8)]


def run_cli(srt, words, extra=()):
    with tempfile.TemporaryDirectory() as tmp:
        s, j = Path(tmp) / "c.srt", Path(tmp) / "w.json"
        s.write_text(srt)
        j.write_text(json.dumps({"clean_duration": 6.0, "words": words}))
        return subprocess.run([sys.executable, str(SCRIPT), str(s),
                               "--words", str(j), *extra],
                              capture_output=True, text=True)


class TestParse(unittest.TestCase):
    def test_parses_multiline_text(self):
        cues = vc.parse_srt(SRT)
        self.assertEqual(cues[1]["text"], "Second line wraps here.")
        self.assertEqual((cues[1]["start"], cues[1]["end"]), (3.0, 5.0))

    def test_rejects_missing_arrow(self):
        with self.assertRaises(ValueError):
            vc.parse_srt("1\n00:00:00,000 00:00:01,000\nx\n")


class TestStructure(unittest.TestCase):
    def test_clean_cues_pass(self):
        self.assertEqual(vc.structure_problems(vc.parse_srt(SRT), 6.0), [])

    def test_overlap_reversed_and_overrun_fail(self):
        cues = [{"index": 1, "start": 0, "end": 2, "text": "a"},
                {"index": 2, "start": 1.5, "end": 1.0, "text": "b"},
                {"index": 3, "start": 5, "end": 9, "text": "c"}]
        probs = vc.structure_problems(cues, 6.0)
        self.assertEqual(len(probs), 3)


class TestCoverage(unittest.TestCase):
    def test_full_coverage_and_fillers_skipped(self):
        r = vc.build_report(vc.parse_srt(SRT), WORDS, 6.0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["coverage"]["words_checked"], 6)

    def test_skipped_paragraph_fails(self):
        words = WORDS + [w(f"lost{i}", 20 + i * 0.4, 20.3 + i * 0.4)
                         for i in range(8)]
        r = vc.build_report(vc.parse_srt(SRT), words, 30.0)
        self.assertFalse(r["coverage"]["ok"])
        self.assertEqual(len(r["coverage"]["long_gaps"]), 1)


class TestCli(unittest.TestCase):
    def test_pass_exit_0(self):
        self.assertEqual(run_cli(SRT, WORDS).returncode, 0)

    def test_malformed_exit_1(self):
        r = run_cli("garbage\n", WORDS)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CAPTIONS FAIL", r.stderr)

    def test_wrong_words_file_exit_2(self):
        r = run_cli(SRT, [{"word": "x", "start": 0, "end": 1}])
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
