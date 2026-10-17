#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Caption the cut timeline in DaVinci Resolve Studio and export SRT/VTT.

Usage:
    uv run {skill-root}/scripts/resolve_captions.py --project <slug> \
        --timeline "<cut timeline>" --out-dir packaging/captions/ \
        [--basename final] [--lines 1|2] [--chars-per-line 42] \
        [--reuse] [--wait 900]

Contract:
    input   the Resolve project and cut timeline named in
            cut/resolve-import.json (mc-cut writes it).
    does    runs Timeline.CreateSubtitlesFromAudio on the cut timeline, which
            adds a subtitle track, then reads that track's items and writes
            the SRT itself from their record frames, so cue times are exact
            timeline frames. --reuse skips generation and reads the highest
            subtitle track that already has items (a track the creator has
            corrected by hand in Resolve). Resolve's transcription drops
            fillers and false starts, which suits captions.
    output  <out-dir>/<basename>.srt, <basename>.vtt and transcript.md, each
            written to a temp file and moved into place. json.dumps summary
            on stdout.
    verify  this script checks only that captions came back. Run
            verify_captions.py on the SRT afterwards; it is the gate.

Exit codes: 0 ok, 1 no captions or Resolve refused, 2 usage, 4 Resolve
unavailable (fall back to captions.py and say so).

STATUS: implemented (pure logic covered by
scripts/tests/test-resolve_captions.py).
"""

import argparse
import json
import os
import sys
import tempfile
import time
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve_api
import resolve_session as rs


def cues_from_items(items, timeline_start, fps):
    """Cues in timeline seconds from subtitle items (pure).

    `items` are {start, end, text} with start/end as absolute record frames.
    Empty texts are dropped; cues come back sorted by start.
    """
    out = []
    for it in items:
        text = " ".join(str(it.get("text") or "").split())
        if not text:
            continue
        start = Fraction(int(round(it["start"])) - timeline_start) / fps
        end = Fraction(int(round(it["end"])) - timeline_start) / fps
        out.append({"start": float(start), "end": float(end), "text": text})
    return sorted(out, key=lambda c: c["start"])


def fmt_time(seconds, sep=","):
    """H:MM:SS,mmm for SRT (sep ",") or VTT (sep ".") (pure)."""
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def render_srt(cues):
    blocks = [f"{i}\n{fmt_time(c['start'])} --> {fmt_time(c['end'])}\n"
              f"{c['text']}\n" for i, c in enumerate(cues, 1)]
    return "\n".join(blocks)


def render_vtt(cues):
    blocks = [f"{fmt_time(c['start'], '.')} --> {fmt_time(c['end'], '.')}\n"
              f"{c['text']}\n" for c in cues]
    return "WEBVTT\n\n" + "\n".join(blocks)


def render_transcript(cues, pause=1.5):
    """Timecoded paragraphs, split where cues are `pause` seconds apart."""
    paras, cur, prev_end = [], [], None
    for c in cues:
        if cur and prev_end is not None and c["start"] - prev_end >= pause:
            paras.append(cur)
            cur = []
        cur.append(c)
        prev_end = c["end"]
    if cur:
        paras.append(cur)
    lines = ["# Transcript (Resolve captions, edited timeline)", ""]
    for p in paras:
        t = int(p[0]["start"])
        lines.append(f"**{t // 60}:{t % 60:02d}** "
                     + " ".join(c["text"] for c in p))
        lines.append("")
    return "\n".join(lines)


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _track_items(timeline, index):
    return [{"start": it.GetStart(), "end": it.GetEnd(), "text": it.GetName()}
            for it in (timeline.GetItemListInTrack("subtitle", index) or [])]


def _settings(resolve, lines, chars):
    s = {"captionPreset": resolve.AUTO_CAPTION_SUBTITLE_DEFAULT,
         "lineBreak": (resolve.AUTO_CAPTION_LINE_DOUBLE if lines == 2
                       else resolve.AUTO_CAPTION_LINE_SINGLE),
         "language": resolve.AUTO_CAPTION_AUTO}
    if chars:
        s["charsPerLine"] = chars
    return s


def _generate(resolve, timeline, lines, chars, wait):
    before = timeline.GetTrackCount("subtitle")
    if not timeline.CreateSubtitlesFromAudio(_settings(resolve, lines,
                                                       chars)):
        return None, "Resolve refused CreateSubtitlesFromAudio"
    deadline = time.monotonic() + wait
    last, stable = None, 0
    while time.monotonic() < deadline:
        count = timeline.GetTrackCount("subtitle")
        items = _track_items(timeline, count) if count > before else []
        if items and len(items) == last:
            stable += 1
            if stable >= 2:
                return items, None
        else:
            stable = 0
        last = len(items) if items else None
        time.sleep(2.0)
    return None, f"no subtitle track appeared within {wait}s"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True)
    p.add_argument("--timeline", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--basename", default="final")
    p.add_argument("--lines", type=int, choices=(1, 2), default=2)
    p.add_argument("--chars-per-line", type=int, default=42)
    p.add_argument("--reuse", action="store_true",
                   help="read the existing subtitle track instead of "
                        "generating a new one")
    p.add_argument("--wait", type=float, default=900.0,
                   help="seconds to wait for Resolve to finish captioning")
    args = p.parse_args(argv)
    if not 1 <= args.chars_per_line <= 60:
        print("resolve_captions: --chars-per-line must be 1 to 60",
              file=sys.stderr)
        return 2

    try:
        resolve, _, timeline = rs.open_timeline(args.project, args.timeline)
    except resolve_api.ResolveUnavailable as e:
        print(f"resolve_captions: {e}", file=sys.stderr)
        return resolve_api.UNAVAILABLE_EXIT
    except rs.SessionError as e:
        print(f"resolve_captions: {e}", file=sys.stderr)
        return 1

    if args.reuse:
        items = []
        for idx in range(timeline.GetTrackCount("subtitle"), 0, -1):
            items = _track_items(timeline, idx)
            if items:
                break
        problem = None if items else "no subtitle track with items to reuse"
    else:
        items, problem = _generate(resolve, timeline, args.lines,
                                   args.chars_per_line, args.wait)
    if problem:
        print(f"resolve_captions: {problem}", file=sys.stderr)
        return 1

    fps = rs.fps_of(timeline)
    start = timeline.GetStartFrame()
    cues = cues_from_items(items, start, fps)
    if not cues:
        print("resolve_captions: the subtitle track holds no text",
              file=sys.stderr)
        return 1
    out = Path(args.out_dir)
    write_atomic(out / f"{args.basename}.srt", render_srt(cues))
    write_atomic(out / f"{args.basename}.vtt", render_vtt(cues))
    write_atomic(out / "transcript.md", render_transcript(cues))
    duration = float(Fraction(timeline.GetEndFrame() - start) / fps)
    print(json.dumps({"ok": True, "cues": len(cues),
                      "timeline_seconds": round(duration, 3),
                      "fps": float(fps), "reused": args.reuse,
                      "srt": str(out / f"{args.basename}.srt")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
