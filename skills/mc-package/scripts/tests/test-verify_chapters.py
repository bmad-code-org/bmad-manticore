#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for verify_chapters.py: timecode parsing and YouTube's chapter
rules, including the CLI exit codes."""
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "verify_chapters.py"
spec = importlib.util.spec_from_file_location("verify_chapters", SCRIPT)
vc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vc)

GOOD = "0:00 Intro\n0:45 The problem\n2:10 The fix\n1:02:03 Wrap\n"


class TestParse(unittest.TestCase):
    def test_formats_and_labels(self):
        self.assertEqual(vc.parse_chapters(GOOD),
                         [(0, "Intro"), (45, "The problem"), (130, "The fix"),
                          (3723, "Wrap")])

    def test_table_rows_and_bullets(self):
        text = "- 00:00 Intro\n| 1:00 | Middle |\nno time here\n"
        self.assertEqual(vc.parse_chapters(text),
                         [(0, "Intro"), (60, "Middle")])


class TestRules(unittest.TestCase):
    def test_good_list_passes(self):
        self.assertEqual(vc.problems(vc.parse_chapters(GOOD), 3800), [])

    def test_each_rule(self):
        self.assertTrue(any("at least 3" in p for p in
                            vc.problems([(0, "a"), (30, "b")])))
        self.assertTrue(any("not 0:00" in p for p in
                            vc.problems([(5, "a"), (30, "b"), (60, "c")])))
        self.assertTrue(any("runs 5s" in p for p in
                            vc.problems([(0, "a"), (5, "b"), (60, "c")])))
        self.assertTrue(any("does not come after" in p for p in
                            vc.problems([(0, "a"), (60, "b"), (30, "c")])))
        self.assertTrue(any("no label" in p for p in
                            vc.problems([(0, ""), (30, "b"), (60, "c")])))

    def test_last_chapter_measured_to_duration(self):
        self.assertTrue(any("runs 4s" in p for p in
                            vc.problems([(0, "a"), (30, "b"), (60, "c")],
                                        64)))


class TestCli(unittest.TestCase):
    def run_cli(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "c.md"
            f.write_text(text)
            return subprocess.run([sys.executable, str(SCRIPT), str(f)],
                                  capture_output=True, text=True)

    def test_exit_codes(self):
        self.assertEqual(self.run_cli(GOOD).returncode, 0)
        self.assertEqual(self.run_cli("0:00 a\n0:05 b\n").returncode, 1)


if __name__ == "__main__":
    unittest.main()
