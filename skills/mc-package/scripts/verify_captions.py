#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Captions gate: prove an SRT is well formed and covers the edited speech.

Usage:
    uv run {skill-root}/scripts/verify_captions.py packaging/captions/final.srt \
        --words cut/edited-words.json \
        [--duration <seconds>] [--max-gap 2.0] [--max-uncovered 0.05] \
        [--tolerance 0.5] [-o packaging/work/captions-check.json]

Contract:
    input   an SRT from either caption lane (captions.py or
            resolve_captions.py) and cut/edited-words.json from mc-cut's
            edited_transcript.py ({clean_duration, words: [{word,
            clean_start, clean_end, ...}]}), which places every kept word on
            the edited timeline.
    checks  1. STRUCTURE: every block parses (index, "a --> b" times, text),
               each cue ends after it starts, cues run in order without
               overlapping, and none starts before 0 or ends past the
               timeline (duration from --duration, else clean_duration,
               with one frame of slack).
            2. COVERAGE: every non-filler word's midpoint must sit inside a
               cue, widened by --tolerance seconds each side. Fillers are
               skipped because one caption lane drops them on purpose. It
               fails when uncovered words exceed --max-uncovered of the
               total, or when a run of uncovered words spans --max-gap
               seconds or more: that is captioning that skipped speech.
    output  json.dumps summary on stdout; -o writes the full report.

Exit codes: 0 pass, 1 captions malformed or missing speech, 2 usage.

STATUS: implemented (covered by scripts/tests/test-verify_captions.py).
"""

import argparse
import json
import re
import sys
from pathlib import Path

FILLERS = frozenset({"um", "umm", "uh", "uhh", "uhm", "erm", "er", "ah",
                     "hmm", "hm", "mm", "mhm"})
TIME = re.compile(r"^(\d+):(\d{2}):(\d{2})[,.](\d{3})$")
FRAME_SLACK = 0.05
DEFAULT_MAX_GAP = 2.0
DEFAULT_MAX_UNCOVERED = 0.05
DEFAULT_TOLERANCE = 0.5


def parse_time(text):
    m = TIME.match(text.strip())
    if not m:
        raise ValueError(f"bad time {text!r}")
    h, mi, s, ms = map(int, m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000


def parse_srt(text):
    """Cues from SRT text; raises ValueError naming the bad block (pure)."""
    cues = []
    blocks = [b for b in re.split(r"\n\s*\n", text.replace("\r\n", "\n")
                                  .strip()) if b.strip()]
    for n, block in enumerate(blocks, 1):
        lines = block.strip().split("\n")
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise ValueError(f"block {n} is not index, times, text")
        parts = lines[1].split("-->")
        if len(parts) != 2:
            raise ValueError(f"block {n} has no 'start --> end' line")
        text_ = " ".join(" ".join(lines[2:]).split())
        if not text_:
            raise ValueError(f"block {n} has no text")
        cues.append({"index": int(lines[0]), "start": parse_time(parts[0]),
                     "end": parse_time(parts[1]), "text": text_})
    return cues


def structure_problems(cues, duration=None):
    out = []
    if not cues:
        return ["no cues"]
    for i, c in enumerate(cues):
        if c["end"] <= c["start"]:
            out.append(f"cue {c['index']} ends at or before it starts")
        if c["start"] < 0:
            out.append(f"cue {c['index']} starts before 0")
        if duration is not None and c["end"] > duration + FRAME_SLACK:
            out.append(f"cue {c['index']} ends at {c['end']:.3f}s, past the "
                       f"{duration:.3f}s timeline")
        if i and c["start"] < cues[i - 1]["end"] - 1e-6:
            out.append(f"cue {c['index']} starts before cue "
                       f"{cues[i - 1]['index']} ends (overlap or out of order)")
    return out


def norm(word):
    return re.sub(r"[^\w']+", "", str(word)).lower()


def coverage(cues, words, tolerance=DEFAULT_TOLERANCE):
    """Uncovered non-filler words and their runs (pure)."""
    spans = [(c["start"] - tolerance, c["end"] + tolerance) for c in cues]
    checked, runs, run = 0, [], []
    for w in words:
        if norm(w.get("word")) in FILLERS or not norm(w.get("word")):
            continue
        checked += 1
        mid = (float(w["clean_start"]) + float(w["clean_end"])) / 2
        if any(a <= mid <= b for a, b in spans):
            if run:
                runs.append(run)
                run = []
        else:
            run.append(w)
    if run:
        runs.append(run)
    return checked, [{"start": round(float(r[0]["clean_start"]), 2),
                      "end": round(float(r[-1]["clean_end"]), 2),
                      "words": len(r),
                      "text": " ".join(str(x["word"]) for x in r)[:120]}
                     for r in runs]


def build_report(cues, words, duration, max_gap=DEFAULT_MAX_GAP,
                 max_uncovered=DEFAULT_MAX_UNCOVERED,
                 tolerance=DEFAULT_TOLERANCE):
    problems = structure_problems(cues, duration)
    checked, runs = coverage(cues, words, tolerance)
    uncovered = sum(r["words"] for r in runs)
    share = uncovered / checked if checked else 0.0
    long_runs = [r for r in runs if r["end"] - r["start"] >= max_gap]
    cov_ok = share <= max_uncovered and not long_runs
    return {"ok": not problems and cov_ok,
            "cues": len(cues), "duration": duration,
            "structure": {"ok": not problems, "problems": problems},
            "coverage": {"ok": cov_ok, "words_checked": checked,
                         "uncovered": uncovered,
                         "uncovered_share": round(share, 4),
                         "max_uncovered": max_uncovered,
                         "long_gaps": long_runs},
            "uncovered_runs": runs}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("srt")
    p.add_argument("--words", required=True,
                   help="cut/edited-words.json (edited-timeline word times)")
    p.add_argument("--duration", type=float, default=None,
                   help="edited timeline seconds (default: clean_duration)")
    p.add_argument("--max-gap", type=float, default=DEFAULT_MAX_GAP)
    p.add_argument("--max-uncovered", type=float,
                   default=DEFAULT_MAX_UNCOVERED)
    p.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    p.add_argument("-o", "--output", default=None)
    args = p.parse_args(argv)

    try:
        text = Path(args.srt).read_text(encoding="utf-8-sig")
        edited = json.loads(Path(args.words).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"verify_captions: cannot read inputs: {e}", file=sys.stderr)
        return 2
    words = edited.get("words")
    if not isinstance(words, list) or (words and "clean_start" not in words[0]):
        print("verify_captions: --words must be cut/edited-words.json "
              "(words with clean_start/clean_end)", file=sys.stderr)
        return 2
    duration = args.duration
    if duration is None and edited.get("clean_duration") is not None:
        duration = float(edited["clean_duration"])
    try:
        cues = parse_srt(text)
    except ValueError as e:
        report = {"ok": False, "cues": 0, "duration": duration,
                  "structure": {"ok": False, "problems": [str(e)]},
                  "coverage": {"ok": False}, "uncovered_runs": []}
    else:
        report = build_report(cues, words, duration, args.max_gap,
                              args.max_uncovered, args.tolerance)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("ok", "cues", "duration",
                                             "structure", "coverage")},
                     indent=2))
    if report["ok"]:
        return 0
    print("\nCAPTIONS FAIL. Do not ship them.", file=sys.stderr)
    for prob in report["structure"]["problems"][:10]:
        print(f"  {prob}", file=sys.stderr)
    for r in report["coverage"].get("long_gaps", [])[:10]:
        print(f"  no caption over {r['start']}s to {r['end']}s: "
              f"{r['text']!r}", file=sys.stderr)
    if report["coverage"].get("uncovered_share", 0) > args.max_uncovered:
        print(f"  {report['coverage']['uncovered']} of "
              f"{report['coverage']['words_checked']} words have no caption",
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
