#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Chapters gate: check a chapter list against YouTube's chapter rules.

Usage:
    uv run {skill-root}/scripts/verify_chapters.py packaging/work/chapters-edited.md \
        [--duration <edited timeline seconds>] [-o packaging/work/chapters-check.json]

Contract:
    input   a chapters file in edited (published) timecodes: one chapter per
            line, the first timecode token (M:SS, MM:SS or H:MM:SS) followed
            by its label. Lines with no timecode are ignored.
    checks  the first chapter is at 0:00; there are at least 3; times rise
            strictly; every chapter runs at least 10 seconds, the last one
            measured to --duration when given; every label is non-empty.
            YouTube drops all chapters when any rule fails, so each is fatal.
    output  json.dumps summary on stdout; -o writes it as a report too.

Exit codes: 0 pass, 1 rule violated, 2 usage.

STATUS: implemented (covered by scripts/tests/test-verify_chapters.py).
"""

import argparse
import json
import re
import sys
from pathlib import Path

TC = re.compile(r"(?<![\d:])(?:(\d+):)?(\d{1,2}):(\d{2})(?![\d:])")
MIN_CHAPTERS = 3
MIN_SECONDS = 10


def parse_chapters(text):
    """[(seconds, label)] from the first timecode on each line (pure)."""
    out = []
    for line in text.splitlines():
        m = TC.search(line)
        if not m:
            continue
        h, mi, s = m.groups()
        seconds = int(h or 0) * 3600 + int(mi) * 60 + int(s)
        label = (line[:m.start()] + line[m.end():])
        label = re.sub(r"^[\s|*\-–—:>#]+|[\s|*]+$", "", label).strip()
        out.append((seconds, label))
    return out


def problems(chapters, duration=None):
    out = []
    if len(chapters) < MIN_CHAPTERS:
        out.append(f"{len(chapters)} chapters; YouTube needs at least "
                   f"{MIN_CHAPTERS}")
    if chapters and chapters[0][0] != 0:
        out.append(f"first chapter is at {chapters[0][0]}s, not 0:00")
    for i, (t, label) in enumerate(chapters):
        if not label:
            out.append(f"chapter at {t}s has no label")
        nxt = chapters[i + 1][0] if i + 1 < len(chapters) else duration
        if nxt is None:
            continue
        if nxt <= t and i + 1 < len(chapters):
            out.append(f"chapter at {nxt}s does not come after {t}s")
        elif nxt - t < MIN_SECONDS:
            out.append(f"chapter at {t}s ({label!r}) runs {nxt - t:g}s; "
                       f"each needs at least {MIN_SECONDS}s")
    if duration is not None and chapters and chapters[-1][0] >= duration:
        out.append(f"last chapter at {chapters[-1][0]}s is past the "
                   f"{duration:g}s video")
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("chapters")
    p.add_argument("--duration", type=float, default=None)
    p.add_argument("-o", "--output", default=None)
    args = p.parse_args(argv)
    try:
        text = Path(args.chapters).read_text(encoding="utf-8")
    except OSError as e:
        print(f"verify_chapters: {e}", file=sys.stderr)
        return 2
    chapters = parse_chapters(text)
    found = problems(chapters, args.duration)
    report = {"ok": not found, "chapters": len(chapters),
              "problems": found}
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if found:
        print("\nCHAPTERS FAIL: YouTube ignores every chapter when one rule "
              "fails. Fix the list and re-run.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
