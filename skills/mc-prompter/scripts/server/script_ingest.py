#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Script ingestion for mc-prompter (Phase A).

Usage:
    uv run {skill-root}/scripts/server/script_ingest.py <file> \
        [--fmt markdown|plain]

Contract:
    input     a script file: markdown (default) or plain text. The pipeline's
              script.md is directly consumable; any standalone file works too.
    output    the script model as JSON on stdout:
                  {
                    "title": "first h1 text or null",
                    "word-count": 1234,
                    "sections": [
                      {"id": "s0", "heading": "Intro or null", "level": 2,
                       "blocks": [
                         {"type": "para", "runs": [
                           {"text": "Speakable text.", "flags": []},
                           {"text": "Flagged sentence.", "flags": ["invented"]}
                         ]},
                         {"type": "note", "text": "pause here"},
                         {"type": "take", "source": "int1", "start": 42.0,
                          "end": 51.5, "runs": [...]}
                       ]}
                    ]
                  }
    markers   three inline marker types are recognized:
                [TAKE <source-id> <start>s-<end>s]  the whole paragraph becomes
                    a take block; the marker is stripped from the display text
                    and source/start/end are parsed (float seconds). A
                    malformed TAKE marker degrades to note handling and never
                    crashes ingestion.
                [INVENTED]  flags the sentence it immediately follows; the
                    marker is stripped and that sentence's run gets the flag
                    "invented".
                [any other bracketed text]  becomes a note block: in place if
                    the bracket is a whole line on its own, otherwise the note
                    text is extracted from the paragraph and emitted as note
                    blocks immediately after it.
    sections  markdown sections split on headings at any level; text before
              the first heading is a section with heading null (level 0). The
              first h1 becomes the document title and is not a section by
              itself; its content flows into the current heading-null section.
              Section ids are "s0", "s1", ... in document order.
    fmt       "markdown" (default) or "plain". Plain skips heading parsing:
              the whole document is one heading-null section of blank-line
              separated paragraphs, with the same marker rules.
    counting  word-count counts only speakable words (whitespace-split words
              of para and take runs); note text is never counted.

Sentence boundary rule (for [INVENTED]): the flagged sentence ends at the
marker; a sentence terminator (".", "!", "?") immediately before the marker
(ignoring whitespace) belongs to the flagged sentence, since the marker flags
the sentence it follows. The sentence starts just after the previous
terminator, or at the paragraph start when none exists. This is a
deliberately simple, deterministic rule; abbreviations and decimal points are
treated as sentence boundaries like any other period.

Exit codes: 0 ok, 2 file missing or unreadable, 2 bad --fmt (argparse usage).
Pure stdlib; importable by server/main.py via `from . import script_ingest`
or as a plain module from the scripts/server directory.
"""

import argparse
import json
import re
import sys
from pathlib import Path

TAKE_RE = re.compile(r"\[TAKE\s+(\S+)\s+(\d+(?:\.\d+)?)s-(\d+(?:\.\d+)?)s\]")
INVENTED = "[INVENTED]"
INVENTED_RE = re.compile(r"\[INVENTED\]")
BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
WHOLE_LINE_NOTE_RE = re.compile(r"^\[([^\[\]]+)\]$")
EMPTY_BRACKET_RE = re.compile(r"\[\s*\]")
SENTENCE_ENDS = ".!?"


def _clean(text):
    """Collapse whitespace runs to single spaces and strip the ends."""
    return re.sub(r"\s+", " ", text).strip()


def _build_runs(text):
    """Split paragraph text into runs, attributing [INVENTED] flags.

    Each [INVENTED] marker flags the sentence it immediately follows. A
    sentence terminator (".", "!", "?") sitting right before the marker
    (ignoring whitespace) is part of the flagged sentence; the sentence
    starts after the previous terminator or at the start of the unconsumed
    text. Everything else becomes unflagged runs. Markers are stripped.
    Empty runs are dropped.
    """
    runs = []
    pos = 0
    for m in INVENTED_RE.finditer(text):
        segment = text[pos:m.start()]
        stripped = segment.rstrip()
        if stripped and stripped[-1] in SENTENCE_ENDS:
            search = stripped[:-1]
        else:
            search = stripped
        boundary = max(search.rfind(c) for c in SENTENCE_ENDS)
        if boundary >= 0:
            before = _clean(segment[:boundary + 1])
            flagged = _clean(segment[boundary + 1:])
        else:
            before = ""
            flagged = _clean(segment)
        if before:
            runs.append({"text": before, "flags": []})
        if flagged:
            runs.append({"text": flagged, "flags": ["invented"]})
        pos = m.end()
    tail = _clean(text[pos:])
    if tail:
        runs.append({"text": tail, "flags": []})
    return runs


def _paragraph_blocks(para_text):
    """Turn one paragraph's raw text into an ordered list of blocks.

    Order of operations: whole-line bracket notes are pulled out first (they
    become note blocks; a paragraph that is only note lines yields only
    notes), then a well-formed TAKE marker is stripped and remembered, then
    remaining non-INVENTED brackets are extracted as trailing note blocks,
    and finally [INVENTED] attribution builds the runs.
    """
    notes = []
    kept_lines = []
    for line in para_text.splitlines():
        m = WHOLE_LINE_NOTE_RE.match(line.strip())
        if m:
            content = m.group(1)
            if content == INVENTED[1:-1] or TAKE_RE.fullmatch(line.strip()):
                kept_lines.append(line)
            else:
                notes.append(content.strip())
        else:
            kept_lines.append(line)
    text = "\n".join(kept_lines)

    take = None
    tm = TAKE_RE.search(text)
    if tm:
        take = {
            "source": tm.group(1),
            "start": float(tm.group(2)),
            "end": float(tm.group(3)),
        }
        text = text[:tm.start()] + text[tm.end():]

    def _extract_note(m):
        content = m.group(1)
        if content == INVENTED[1:-1]:
            return m.group(0)
        notes.append(content.strip())
        return " "

    # Repeat until stable so nested brackets are fully extracted: each pass
    # handles the innermost pairs and exposes the next level. Empty bracket
    # pairs (literal or left over once inner content is consumed) are dropped
    # so no bracket residue reaches speakable text. Bounded: every pass that
    # changes the text removes characters.
    while True:
        new_text = BRACKET_RE.sub(_extract_note, text)
        new_text = EMPTY_BRACKET_RE.sub(" ", new_text)
        if new_text == text:
            break
        text = new_text

    blocks = []
    runs = _build_runs(text)
    if runs:
        if take:
            blocks.append({"type": "take", **take, "runs": runs})
        else:
            blocks.append({"type": "para", "runs": runs})
    for note in notes:
        blocks.append({"type": "note", "text": note})
    return blocks


def _split_paragraphs(lines):
    """Split a list of lines into blank-line separated paragraph strings."""
    paras = []
    buf = []
    for line in lines:
        if line.strip():
            buf.append(line)
        elif buf:
            paras.append("\n".join(buf))
            buf = []
    if buf:
        paras.append("\n".join(buf))
    return paras


def _word_count(sections):
    total = 0
    for section in sections:
        for block in section["blocks"]:
            if block["type"] in ("para", "take"):
                for run in block["runs"]:
                    total += len(run["text"].split())
    return total


def ingest(text, fmt="markdown"):
    """Ingest script text and return the script model dict.

    fmt "markdown" splits sections on headings; the first h1 becomes title.
    fmt "plain" produces a single heading-null section. Marker rules are
    identical in both formats. Raises ValueError on an unknown fmt.
    """
    if fmt not in ("markdown", "plain"):
        raise ValueError(f"unknown fmt: {fmt}")

    # Defensive BOM strip: pasted text (POST /api/source) may carry a leading
    # U+FEFF that would otherwise break heading detection on the first line.
    text = text.lstrip("﻿")

    title = None
    sections = []
    current = None

    def _ensure_section(heading=None, level=0):
        nonlocal current
        current = {"heading": heading, "level": level,
                   "lines": [], "blocks": []}
        sections.append(current)

    if fmt == "plain":
        _ensure_section()
        current["lines"] = text.splitlines()
    else:
        for line in text.splitlines():
            hm = HEADING_RE.match(line)
            if hm:
                level = len(hm.group(1))
                heading_text = hm.group(2).strip()
                if level == 1 and title is None:
                    title = heading_text
                    continue
                _ensure_section(heading_text, level)
            else:
                if current is None:
                    if not line.strip():
                        continue
                    _ensure_section()
                current["lines"].append(line)

    for section in sections:
        for para in _split_paragraphs(section.pop("lines")):
            section["blocks"].extend(_paragraph_blocks(para))

    out_sections = [
        {"id": f"s{i}", "heading": s["heading"], "level": s["level"],
         "blocks": s["blocks"]}
        for i, s in enumerate(sections)
    ]
    return {
        "title": title,
        "word-count": _word_count(out_sections),
        "sections": out_sections,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", help="path to the script file")
    parser.add_argument("--fmt", default="markdown",
                        choices=("markdown", "plain"),
                        help="input format (default: markdown)")
    args = parser.parse_args(argv)

    path = Path(args.file)
    if not path.is_file():
        print(f"error: script not found: {path}", file=sys.stderr)
        return 2
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return 2

    doc = ingest(text, fmt=args.fmt)
    print(json.dumps(doc, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)
