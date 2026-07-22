#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Emit SRT/VTT captions and a readable transcript for the EDITED timeline
from transcript/words.json and cut/edl.json.

Usage:
    uv run {skill-root}/scripts/captions.py <edl.json> \
        --words transcript/words.json --out-dir packaging/captions/
    uv run {skill-root}/scripts/captions.py <edl.json> \
        --words raw/a.mp4=transcript/a.words.json \
        --words raw/b.mp4=transcript/b.words.json \
        --out-dir packaging/captions/ [--no-clean] [--basename final]

Contract:
    edl.json is the cut stage's neutral edit description ({source,
    source_duration, segments: [{source, start, end, ...}]}); segment start
    and end are seconds against that segment's own source. Each --words file
    is a transcribe.py words.json (verbatim word timestamps against ONE
    source). --words may repeat; the form <source>=<path> binds a file to an
    EDL segment source explicitly, a bare <path> binds via the file's own
    "media" field (exact match, then basename match, then the trivial match
    when the EDL uses a single source and a single file was given). Every
    source named by a segment must resolve to a words file.

    Each segment's words (word midpoint inside [start, end)) are remapped to
    output-timeline times (segment offset + word time - segment start, span
    clamped to the segment), so reordered and multi-source edits caption
    correctly. Words are grouped into cues: new cue at segment boundaries,
    after sentence-ending punctuation, at speech pauses (--pause-split, 0.6 s
    default), and whenever text would no longer wrap into --max-lines (2)
    lines of --max-line-chars (42), or the cue would exceed --max-cue-seconds
    (7.0). Cues shorter than --min-cue-seconds (1.0) are extended toward the
    next cue.

    A light cleanup pass runs by default on this derived rendition only
    (words.json is never modified): standalone filler tokens (um, uh, ...)
    are dropped and stutter repeats (repeated word, or a "th-" fragment
    completed by the next word) are collapsed. --no-clean keeps the captions
    verbatim.

Output:
    Writes <out-dir>/<basename>.srt, <out-dir>/<basename>.vtt, and
    <out-dir>/transcript.md (timecoded paragraphs of the same rendition),
    creating <out-dir> if needed, then prints a JSON summary to stdout.

Exit codes: 0 ok, 1 failure (unreadable input, unmatched source, no words
on the timeline), 2 usage.

STATUS: implemented (covered by scripts/tests/test-captions.py).
"""

import argparse
import json
import re
import sys
from pathlib import Path

FILLERS = frozenset({
    "um", "umm", "uh", "uhh", "uhm", "erm", "er", "hmm", "hm", "mm", "mhm",
})
PUNCT_STRIP = re.compile(r"[^\w']+", re.UNICODE)
SENTENCE_END = re.compile(r"[.!?][\"')\]]*$")


def normalize_token(word):
    """Lowercased word with punctuation stripped ('Alright,' -> 'alright')."""
    return PUNCT_STRIP.sub("", str(word)).lower()


def clean_words(words, enabled=True):
    """Filler drop + stutter collapse over word dicts (needs 'word' key).

    Returns (new_words, stats) with stats {'fillers_dropped',
    'repeats_collapsed'}. Collapsing merges the repeat into one word: text of
    the later word, start of the earlier, end of the later. Never mutates
    the input dicts."""
    stats = {"fillers_dropped": 0, "repeats_collapsed": 0}
    if not enabled:
        return list(words), stats
    out = []
    for w in words:
        norm = normalize_token(w["word"])
        if norm in FILLERS:
            stats["fillers_dropped"] += 1
            continue
        if out:
            prev = out[-1]
            prev_norm = normalize_token(prev["word"])
            same_source = (prev.get("_source") == w.get("_source")
                           and prev.get("seg") == w.get("seg"))
            repeat = norm != "" and norm == prev_norm
            fragment = (str(prev["word"]).rstrip().endswith(("-", "—"))
                        and prev_norm != "" and norm.startswith(prev_norm)
                        and norm != prev_norm)
            if same_source and (repeat or fragment):
                merged = dict(w)
                merged["start"] = prev["start"]
                out[-1] = merged
                stats["repeats_collapsed"] += 1
                continue
        out.append(dict(w))
    return out, stats


def match_words_files(edl, entries):
    """Resolve each EDL segment source to a parsed words payload.

    entries: [(explicit_source_or_None, payload_dict)]. Match order per
    source: explicit binding, exact media string, media basename, then the
    single-file single-source fallback. Returns {source: [word, ...]};
    raises ValueError when a source stays unmatched."""
    sources = []
    for seg in edl.get("segments", []):
        if seg["source"] not in sources:
            sources.append(seg["source"])
    resolved = {}
    for src in sources:
        payload = None
        for explicit, data in entries:
            if explicit == src:
                payload = data
                break
        if payload is None:
            for explicit, data in entries:
                if explicit is None and str(data.get("media", "")) == src:
                    payload = data
                    break
        if payload is None:
            for explicit, data in entries:
                if explicit is None and Path(
                        str(data.get("media", ""))).name == Path(src).name:
                    payload = data
                    break
        if payload is None and len(sources) == 1 and len(entries) == 1:
            payload = entries[0][1]
        if payload is None:
            raise ValueError(
                f"no words file matches EDL source {src!r}; pass "
                f"--words {src}=<path>")
        resolved[src] = payload.get("words", [])
    return resolved


def assign_output_times(edl, words_by_source):
    """Project each kept word onto the edited timeline.

    A word belongs to a segment when its midpoint falls in [start, end);
    its span is clamped to the segment and offset onto the output timeline.
    Returns timeline-ordered dicts {word, start, end, confidence, seg,
    _source}."""
    out = []
    offset = 0.0
    for seg_index, seg in enumerate(edl["segments"]):
        s, e = float(seg["start"]), float(seg["end"])
        for w in words_by_source.get(seg["source"], []):
            mid = (float(w["start"]) + float(w["end"])) / 2.0
            if not (s <= mid < e):
                continue
            out.append({
                "word": str(w["word"]),
                "start": round(offset + max(float(w["start"]), s) - s, 3),
                "end": round(offset + min(float(w["end"]), e) - s, 3),
                "confidence": w.get("confidence"),
                "seg": seg_index,
                "_source": seg["source"],
            })
        offset += e - s
    return out


def wrap_lines(text, max_chars):
    """Greedy word wrap; a single word longer than max_chars overflows its
    own line rather than being split."""
    lines = []
    current = ""
    for token in text.split():
        candidate = token if not current else current + " " + token
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = token
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def group_cues(timed_words, max_line_chars=42, max_lines=2,
               min_cue_seconds=1.0, max_cue_seconds=7.0, pause_split=0.6):
    """Group timeline-ordered words into caption cues.

    Cue boundaries: segment change, sentence-ending punctuation, a pause of
    pause_split seconds or more before the next word, text overflowing
    max_lines lines of max_line_chars, or duration passing max_cue_seconds.
    Short cues are stretched toward the next cue up to min_cue_seconds.
    Returns [{start, end, lines, text, words}]."""
    cues = []
    bucket = []

    def flush():
        if not bucket:
            return
        text = " ".join(w["word"] for w in bucket).strip()
        if text:
            cues.append({
                "start": bucket[0]["start"],
                "end": bucket[-1]["end"],
                "lines": wrap_lines(text, max_line_chars),
                "text": text,
                "words": list(bucket),
            })
        bucket.clear()

    for w in timed_words:
        if bucket:
            candidate = " ".join(x["word"] for x in bucket) + " " + w["word"]
            overflow = len(wrap_lines(candidate, max_line_chars)) > max_lines
            too_long = w["end"] - bucket[0]["start"] > max_cue_seconds
            new_seg = w["seg"] != bucket[-1]["seg"]
            paused = w["start"] - bucket[-1]["end"] >= pause_split
            if overflow or too_long or new_seg or paused:
                flush()
        bucket.append(w)
        if SENTENCE_END.search(w["word"].strip()):
            flush()
    flush()

    for i, cue in enumerate(cues):
        if cue["end"] - cue["start"] < min_cue_seconds:
            target = cue["start"] + min_cue_seconds
            if i + 1 < len(cues):
                target = min(target, cues[i + 1]["start"])
            cue["end"] = round(max(cue["end"], target), 3)
    return cues


def format_srt_time(seconds):
    """Seconds -> '00:00:01,280' (SRT comma milliseconds)."""
    ms = max(0, round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_vtt_time(seconds):
    """Seconds -> '00:00:01.280' (WebVTT dot milliseconds)."""
    return format_srt_time(seconds).replace(",", ".")


def render_srt(cues):
    blocks = []
    for i, cue in enumerate(cues, 1):
        blocks.append(f"{i}\n{format_srt_time(cue['start'])} --> "
                      f"{format_srt_time(cue['end'])}\n"
                      + "\n".join(cue["lines"]))
    return "\n\n".join(blocks) + "\n"


def render_vtt(cues):
    blocks = ["WEBVTT"]
    for cue in cues:
        blocks.append(f"{format_vtt_time(cue['start'])} --> "
                      f"{format_vtt_time(cue['end'])}\n"
                      + "\n".join(cue["lines"]))
    return "\n\n".join(blocks) + "\n"


def transcript_timecode(seconds):
    """Seconds -> '[m:ss]' or '[h:mm:ss]' paragraph marker."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"[{h}:{m:02d}:{s:02d}]"
    return f"[{m}:{s:02d}]"


def render_transcript(timed_words, paragraph_pause=1.5):
    """Timecoded paragraphs of the edited timeline. New paragraph at a
    segment change or a pause of paragraph_pause seconds or more."""
    paragraphs = []
    bucket = []
    for w in timed_words:
        if bucket and (w["seg"] != bucket[-1]["seg"]
                       or w["start"] - bucket[-1]["end"] >= paragraph_pause):
            paragraphs.append(bucket)
            bucket = []
        bucket.append(w)
    if bucket:
        paragraphs.append(bucket)
    out = ["# Transcript", ""]
    for para in paragraphs:
        text = " ".join(w["word"] for w in para).strip()
        if not text:
            continue
        out.append(f"{transcript_timecode(para[0]['start'])} {text}")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def parse_words_arg(value):
    """'--words path' or '--words source=path' -> (source_or_None, path).

    Splits on the first '=' only when the left side names an EDL source
    (contains no path separator ambiguity worth guessing beyond '=')."""
    if "=" in value:
        source, path = value.split("=", 1)
        if source and path:
            return source, path
    return None, value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("edl", help="path to cut/edl.json")
    parser.add_argument("--words", action="append", required=True,
                        metavar="[SOURCE=]PATH",
                        help="words.json path, optionally bound to an EDL "
                             "segment source; repeatable")
    parser.add_argument("--out-dir", required=True,
                        help="directory for the caption deliverables")
    parser.add_argument("--basename", default="final",
                        help="basename for the .srt/.vtt pair (default final)")
    parser.add_argument("--no-clean", action="store_true",
                        help="skip the filler/stutter cleanup pass")
    parser.add_argument("--max-line-chars", type=int, default=42)
    parser.add_argument("--max-lines", type=int, default=2)
    parser.add_argument("--min-cue-seconds", type=float, default=1.0)
    parser.add_argument("--max-cue-seconds", type=float, default=7.0)
    parser.add_argument("--pause-split", type=float, default=0.6)
    args = parser.parse_args(argv)

    try:
        edl = json.loads(Path(args.edl).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"captions: cannot read {args.edl}: {e}", file=sys.stderr)
        return 1
    if not edl.get("segments"):
        print("captions: edl has no segments", file=sys.stderr)
        return 1

    entries = []
    for raw in args.words:
        source, path = parse_words_arg(raw)
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"captions: cannot read {path}: {e}", file=sys.stderr)
            return 1
        entries.append((source, data))

    try:
        words_by_source = match_words_files(edl, entries)
    except ValueError as e:
        print(f"captions: {e}", file=sys.stderr)
        return 1

    timed = assign_output_times(edl, words_by_source)
    if not timed:
        print("captions: no words fall inside any EDL segment",
              file=sys.stderr)
        return 1
    cleaned, clean_stats = clean_words(timed, enabled=not args.no_clean)
    cues = group_cues(cleaned,
                      max_line_chars=args.max_line_chars,
                      max_lines=args.max_lines,
                      min_cue_seconds=args.min_cue_seconds,
                      max_cue_seconds=args.max_cue_seconds,
                      pause_split=args.pause_split)
    if not cues:
        print("captions: cleanup removed every word; nothing to caption",
              file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        srt_path = out_dir / f"{args.basename}.srt"
        vtt_path = out_dir / f"{args.basename}.vtt"
        md_path = out_dir / "transcript.md"
        srt_path.write_text(render_srt(cues), encoding="utf-8")
        vtt_path.write_text(render_vtt(cues), encoding="utf-8")
        md_path.write_text(render_transcript(cleaned), encoding="utf-8")
    except OSError as e:
        print(f"captions: cannot write outputs: {e}", file=sys.stderr)
        return 1

    print(json.dumps({
        "ok": True,
        "cues": len(cues),
        "words": len(cleaned),
        "clean": not args.no_clean,
        **clean_stats,
        "duration": round(cues[-1]["end"], 3),
        "srt": str(srt_path),
        "vtt": str(vtt_path),
        "transcript": str(md_path),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
