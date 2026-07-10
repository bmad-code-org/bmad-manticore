#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for script_ingest.py (mc-prompter Phase A).

Run directly:
    uv run skills/mc-prompter/scripts/tests/test-script_ingest.py

Pure stdlib unittest; no network, no models, no downloads. Uses the committed
fixture at fixtures/sample-script.md plus small inline documents.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent / "server"))

from script_ingest import ingest  # noqa: E402

FIXTURE = TESTS_DIR / "fixtures" / "sample-script.md"


def blocks_of(doc, section_index):
    return doc["sections"][section_index]["blocks"]


def speakable_text(doc):
    parts = []
    for section in doc["sections"]:
        for block in section["blocks"]:
            if block["type"] in ("para", "take"):
                for run in block["runs"]:
                    parts.append(run["text"])
    return " ".join(parts)


class TestSectioning(unittest.TestCase):

    def test_title_from_first_h1(self):
        doc = ingest("# My Title\n\nHello there.\n")
        self.assertEqual(doc["title"], "My Title")

    def test_first_h1_is_not_a_section(self):
        doc = ingest("# My Title\n\nHello there.\n\n## One\n\nBody.\n")
        headings = [s["heading"] for s in doc["sections"]]
        self.assertNotIn("My Title", headings)

    def test_h1_content_lands_in_null_heading_section(self):
        doc = ingest("# My Title\n\nHello there.\n\n## One\n\nBody.\n")
        first = doc["sections"][0]
        self.assertIsNone(first["heading"])
        self.assertEqual(first["level"], 0)
        self.assertEqual(first["blocks"][0]["runs"][0]["text"],
                         "Hello there.")

    def test_preamble_before_any_heading(self):
        doc = ingest("Lead-in text.\n\n## One\n\nBody.\n")
        self.assertIsNone(doc["title"])
        self.assertIsNone(doc["sections"][0]["heading"])
        self.assertEqual(doc["sections"][1]["heading"], "One")

    def test_section_ids_are_sequential(self):
        doc = ingest("Pre.\n\n## A\n\nx.\n\n## B\n\ny.\n\n### C\n\nz.\n")
        self.assertEqual([s["id"] for s in doc["sections"]],
                         ["s0", "s1", "s2", "s3"])
        self.assertEqual(doc["sections"][3]["level"], 3)

    def test_heading_levels_recorded(self):
        doc = ingest("## Two\n\na.\n\n### Three\n\nb.\n")
        self.assertEqual(doc["sections"][0]["level"], 2)
        self.assertEqual(doc["sections"][1]["level"], 3)

    def test_second_h1_is_a_normal_section(self):
        doc = ingest("# Title\n\n# Another\n\nBody.\n")
        self.assertEqual(doc["title"], "Title")
        self.assertEqual(doc["sections"][0]["heading"], "Another")
        self.assertEqual(doc["sections"][0]["level"], 1)


class TestTakeParsing(unittest.TestCase):

    def test_take_paragraph_parsed(self):
        doc = ingest("[TAKE int1 12.0s-19.5s]\nAlready recorded line.\n")
        block = blocks_of(doc, 0)[0]
        self.assertEqual(block["type"], "take")
        self.assertEqual(block["source"], "int1")
        self.assertEqual(block["start"], 12.0)
        self.assertEqual(block["end"], 19.5)
        self.assertEqual(block["runs"][0]["text"], "Already recorded line.")

    def test_take_marker_inline_mid_paragraph(self):
        doc = ingest("Some text [TAKE cam2 5s-9s] more text.\n")
        block = blocks_of(doc, 0)[0]
        self.assertEqual(block["type"], "take")
        self.assertEqual(block["source"], "cam2")
        self.assertEqual(block["start"], 5.0)
        self.assertEqual(block["end"], 9.0)
        self.assertEqual(block["runs"][0]["text"], "Some text more text.")

    def test_malformed_take_degrades_to_note(self):
        doc = ingest("[TAKE int1]\nStill speakable text.\n")
        blocks = blocks_of(doc, 0)
        types = [b["type"] for b in blocks]
        self.assertIn("para", types)
        self.assertIn("note", types)
        note = next(b for b in blocks if b["type"] == "note")
        self.assertEqual(note["text"], "TAKE int1")

    def test_malformed_take_missing_s_suffix(self):
        doc = ingest("Text here. [TAKE int1 12.0-19.5]\n")
        blocks = blocks_of(doc, 0)
        self.assertEqual(blocks[0]["type"], "para")
        self.assertEqual(blocks[1]["type"], "note")


class TestInventedFlag(unittest.TestCase):

    def test_flags_the_sentence_it_follows(self):
        doc = ingest("First sentence. Second sentence. [INVENTED] Third.\n")
        runs = blocks_of(doc, 0)[0]["runs"]
        self.assertEqual(runs[0]["text"], "First sentence.")
        self.assertEqual(runs[0]["flags"], [])
        self.assertEqual(runs[1]["text"], "Second sentence.")
        self.assertEqual(runs[1]["flags"], ["invented"])
        self.assertEqual(runs[2]["text"], "Third.")
        self.assertEqual(runs[2]["flags"], [])

    def test_flags_from_paragraph_start(self):
        doc = ingest("Only sentence with no terminator [INVENTED] tail.\n")
        runs = blocks_of(doc, 0)[0]["runs"]
        self.assertEqual(runs[0]["flags"], ["invented"])
        self.assertEqual(runs[0]["text"],
                         "Only sentence with no terminator")
        self.assertEqual(runs[1]["text"], "tail.")

    def test_marker_mid_sentence_flags_partial_span(self):
        doc = ingest("Solid claim. Shaky number [INVENTED] and onward.\n")
        runs = blocks_of(doc, 0)[0]["runs"]
        self.assertEqual(runs[1]["text"], "Shaky number")
        self.assertEqual(runs[1]["flags"], ["invented"])

    def test_marker_not_leaked_into_text(self):
        doc = ingest("A claim. [INVENTED] More text.\n")
        self.assertNotIn("INVENTED", speakable_text(doc))


class TestNotes(unittest.TestCase):

    def test_whole_line_note_becomes_note_block(self):
        doc = ingest("[pause here]\n")
        block = blocks_of(doc, 0)[0]
        self.assertEqual(block["type"], "note")
        self.assertEqual(block["text"], "pause here")

    def test_inline_note_extracted_after_paragraph(self):
        doc = ingest("Keep talking [look at camera] without stopping.\n")
        blocks = blocks_of(doc, 0)
        self.assertEqual(blocks[0]["type"], "para")
        self.assertEqual(blocks[0]["runs"][0]["text"],
                         "Keep talking without stopping.")
        self.assertEqual(blocks[1]["type"], "note")
        self.assertEqual(blocks[1]["text"], "look at camera")

    def test_note_text_excluded_from_word_count(self):
        with_note = ingest("One two three. [a very long stage direction]\n")
        without = ingest("One two three.\n")
        self.assertEqual(with_note["word-count"], 3)
        self.assertEqual(with_note["word-count"], without["word-count"])


class TestWordCount(unittest.TestCase):

    def test_counts_para_and_take_runs_only(self):
        doc = ingest(
            "## S\n\nFour words right here.\n\n"
            "[TAKE int1 1.0s-2.0s]\nThree more words.\n\n"
            "[note words never counted at all]\n"
        )
        self.assertEqual(doc["word-count"], 7)

    def test_invented_runs_are_counted(self):
        doc = ingest("Two words. [INVENTED] Two more.\n")
        self.assertEqual(doc["word-count"], 4)


class TestPlainFormat(unittest.TestCase):

    def test_no_heading_parsing(self):
        doc = ingest("# not a title\n\nBody text.\n", fmt="plain")
        self.assertIsNone(doc["title"])
        self.assertEqual(len(doc["sections"]), 1)
        self.assertIsNone(doc["sections"][0]["heading"])
        self.assertIn("# not a title", speakable_text(doc))

    def test_marker_rules_still_apply(self):
        doc = ingest("Spoken bit [aside] here.\n\n[TAKE t1 0.5s-2.5s]\nDone.\n",
                     fmt="plain")
        blocks = blocks_of(doc, 0)
        types = [b["type"] for b in blocks]
        self.assertEqual(types, ["para", "note", "take"])

    def test_unknown_fmt_raises(self):
        with self.assertRaises(ValueError):
            ingest("text", fmt="html")


class TestFixture(unittest.TestCase):

    def setUp(self):
        self.doc = ingest(FIXTURE.read_text(encoding="utf-8"))

    def test_title_and_sections(self):
        self.assertEqual(self.doc["title"], "Why Local Tools Win")
        headings = [s["heading"] for s in self.doc["sections"]]
        self.assertEqual(headings, [None, "Cold open", "The cost argument",
                                    "Latency", "Wrap"])

    def test_two_takes(self):
        takes = [b for s in self.doc["sections"] for b in s["blocks"]
                 if b["type"] == "take"]
        self.assertEqual(len(takes), 2)
        self.assertEqual(takes[0]["source"], "int1")
        self.assertEqual(takes[0]["start"], 12.0)
        self.assertEqual(takes[0]["end"], 19.5)
        self.assertEqual(takes[1]["start"], 41.25)
        self.assertEqual(takes[1]["end"], 52.0)

    def test_three_invented_flags(self):
        flagged = [r for s in self.doc["sections"] for b in s["blocks"]
                   if b["type"] in ("para", "take") for r in b["runs"]
                   if "invented" in r["flags"]]
        self.assertEqual(len(flagged), 3)

    def test_invented_after_terminator_flags_previous_sentence(self):
        latency = next(s for s in self.doc["sections"]
                       if s["heading"] == "Latency")
        flagged = [r for b in latency["blocks"] if b["type"] == "para"
                   for r in b["runs"] if "invented" in r["flags"]]
        self.assertEqual(
            flagged[0]["text"],
            "Local inference latency has dropped forty percent this year.")

    def test_notes_present_and_not_counted(self):
        notes = [b for s in self.doc["sections"] for b in s["blocks"]
                 if b["type"] == "note"]
        self.assertGreaterEqual(len(notes), 2)
        self.assertNotIn("smile, beat", speakable_text(self.doc))
        self.assertGreater(self.doc["word-count"], 100)

    def test_no_markers_leak_into_display_text(self):
        text = speakable_text(self.doc)
        self.assertNotIn("[", text)
        self.assertNotIn("TAKE", text)
        self.assertNotIn("INVENTED", text)


class TestRobustness(unittest.TestCase):

    def test_bom_prefixed_title_parses(self):
        doc = ingest("﻿# My Title\n\nHello there.\n")
        self.assertEqual(doc["title"], "My Title")
        self.assertNotIn("#", speakable_text(doc))
        self.assertEqual(doc["word-count"], 2)

    def test_bom_file_read_via_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bom.md"
            path.write_bytes("# My Title\n\nHello there.\n".encode("utf-8-sig"))
            proc = subprocess.run(
                [sys.executable, str(TESTS_DIR.parent / "server"
                                     / "script_ingest.py"), str(path)],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            doc = json.loads(proc.stdout)
            self.assertEqual(doc["title"], "My Title")

    def test_non_utf8_file_exits_2_with_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cp1252.md"
            path.write_bytes("# Title\n\nCurly ’quote’.\n"
                             .encode("cp1252"))
            proc = subprocess.run(
                [sys.executable, str(TESTS_DIR.parent / "server"
                                     / "script_ingest.py"), str(path)],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("error: cannot read", proc.stderr)

    def test_nested_brackets_fully_extracted(self):
        doc = ingest("Talk here [note [nested] stuff] more.\n")
        blocks = blocks_of(doc, 0)
        notes = [b["text"] for b in blocks if b["type"] == "note"]
        self.assertIn("nested", notes)
        self.assertTrue(any("note" in n and "stuff" in n for n in notes))
        text = speakable_text(doc)
        self.assertNotIn("[", text)
        self.assertNotIn("]", text)
        self.assertEqual(text, "Talk here more.")

    def test_empty_brackets_never_leak(self):
        doc = ingest("Before [] after.\n\nAlso [[]] here.\n")
        text = speakable_text(doc)
        self.assertNotIn("[", text)
        self.assertNotIn("]", text)
        self.assertIn("Before after.", text)
        self.assertIn("Also here.", text)


if __name__ == "__main__":
    unittest.main()
