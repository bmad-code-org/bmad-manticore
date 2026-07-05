#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Render the draft watch-copy of cut/edl.json and self-verify its cut boundaries.

Usage:
    uv run {skill-root}/scripts/render_preview.py <edl.json> -o <preview.mp4> \
        [--project-dir <dir>] [--boundary-frames <dir>] [--height 720]

Purpose:
    The gate-2 deliverable pair: a low-bitrate preview.mp4 the creator watches,
    plus the boundary-frame stills the skill inspects to confirm no cut lands
    inside a word (the cutting rules' self-verify step). Unlike the FCPXML
    export, the preview uses the EDL's raw segment times (not frame-snapped
    boundaries) and bakes the fades in, so it is the ground truth for what the
    audience hears.

Contract:
    input   edl.json: {source, fade_ms, pad_ms, segments[]}; each segment is
            {source, start, end, ...} with start/end in seconds against its
            source. N distinct sources become N ffmpeg inputs.
    output  a draft H.264/AAC mp4 at -o. One ffmpeg invocation builds the whole
            timeline via filter_complex: per segment a trim/atrim from its
            source, an afade in and out of fade_ms (edl.json, default 30ms) at
            every boundary, scaled to --height (default 720, aspect kept, even
            width), then concat; encoded libx264 crf 28 preset veryfast + aac.
            The exact ffmpeg command is printed to stderr on failure.
    boundary-frames
            optional dir. After the render, one frame just before and one just
            after each internal cut boundary of the OUTPUT is extracted to
            <dir>/boundary-<n>-a.jpg (before) and boundary-<n>-b.jpg (after),
            n starting at 1, so the skill can inspect each cut.
    summary json.dumps on stdout: segments, expected_duration (sum of raw
            segment durations), actual_duration (ffprobe of the output),
            boundary_frames, output path.

Exit codes: 0 ok (and expected vs actual duration within 0.5s), 1 failure,
2 usage.

STATUS: implemented (build-order item 1).
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


# --- pure timeline math (unit-tested) -------------------------------------

def segment_durations(edl):
    """Raw per-segment durations in seconds, in spine order."""
    return [seg["end"] - seg["start"] for seg in edl["segments"]]


def boundary_times(edl):
    """Output-timeline times (seconds) of each internal cut boundary."""
    durs = segment_durations(edl)
    times, running = [], 0.0
    for d in durs[:-1]:
        running += d
        times.append(running)
    return times


def _fmt(x):
    """Trim trailing zeros so ffmpeg filter args stay readable and exact."""
    return f"{x:.6f}".rstrip("0").rstrip(".")


def build_filter_complex(edl, source_index, height):
    """Build the filter_complex string for the whole preview timeline.

    source_index maps each source path to its ffmpeg -i input index. Each
    segment is trimmed from its source, PTS-reset, scaled to height (even width,
    square pixels), and given an in/out afade of fade_ms at its boundaries; all
    segments then concat to [outv]/[outa]."""
    fade = edl.get("fade_ms", 30) / 1000.0
    parts, vlabels, alabels = [], [], []
    for i, seg in enumerate(edl["segments"]):
        idx = source_index[seg["source"]]
        start, end = seg["start"], seg["end"]
        dur = end - start
        # Never let the two fades overlap on a very short segment.
        f = min(fade, dur / 2) if dur > 0 else 0.0
        vlab, alab = f"v{i}", f"a{i}"
        parts.append(
            f"[{idx}:v]trim=start={_fmt(start)}:end={_fmt(end)},"
            f"setpts=PTS-STARTPTS,scale=-2:{height},setsar=1[{vlab}]"
        )
        afade = (
            f"[{idx}:a]atrim=start={_fmt(start)}:end={_fmt(end)},"
            f"asetpts=PTS-STARTPTS"
        )
        if f > 0:
            afade += (
                f",afade=t=in:st=0:d={_fmt(f)}"
                f",afade=t=out:st={_fmt(dur - f)}:d={_fmt(f)}"
            )
        afade += f"[{alab}]"
        parts.append(afade)
        vlabels.append(f"[{vlab}]")
        alabels.append(f"[{alab}]")
    n = len(edl["segments"])
    concat_inputs = "".join(v + a for v, a in zip(vlabels, alabels))
    parts.append(f"{concat_inputs}concat=n={n}:v=1:a=1[outv][outa]")
    return ";".join(parts)


def build_command(edl, project_dir, output, height):
    """Assemble (ffmpeg_argv, source_index) for the single render invocation."""
    distinct = []
    for seg in edl["segments"]:
        if seg["source"] not in distinct:
            distinct.append(seg["source"])
    source_index = {src: i for i, src in enumerate(distinct)}
    argv = ["ffmpeg", "-y"]
    for src in distinct:
        argv += ["-i", str((project_dir / src).resolve())]
    argv += [
        "-filter_complex", build_filter_complex(edl, source_index, height),
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
        "-c:a", "aac", "-movflags", "+faststart",
        str(output),
    ]
    return argv, source_index


# --- ffprobe / frame extraction (thin subprocess wrappers) ----------------

def probe_duration(path):
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-print_format", "json", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    dur = json.loads(proc.stdout).get("format", {}).get("duration")
    return float(dur) if dur is not None else None


def extract_boundary_frames(output, edl, out_dir):
    """One still just before and just after each internal cut of the output."""
    out_dir.mkdir(parents=True, exist_ok=True)
    durs = segment_durations(edl)
    times = boundary_times(edl)
    count = 0
    for n, t in enumerate(times, start=1):
        # Stay inside the neighbouring segments even when they are short.
        before = max(0.0, t - min(0.05, durs[n - 1] / 2))
        after = t + min(0.05, durs[n] / 2)
        for suffix, ts in (("a", before), ("b", after)):
            dest = out_dir / f"boundary-{n}-{suffix}.jpg"
            proc = subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{ts:.6f}", "-i", str(output),
                 "-frames:v", "1", "-q:v", "3", str(dest)],
                capture_output=True, text=True,
            )
            if proc.returncode == 0 and dest.is_file():
                count += 1
    return count


# --- cli ------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("edl", help="path to cut/edl.json")
    parser.add_argument("-o", "--output", required=True, help="output preview.mp4")
    parser.add_argument("--project-dir", default=None,
                        help="base for source paths (default: edl parent's parent)")
    parser.add_argument("--boundary-frames", default=None,
                        help="dir to write per-cut boundary stills into")
    parser.add_argument("--height", type=int, default=720)
    args = parser.parse_args(argv)

    edl_path = Path(args.edl).resolve()
    if not edl_path.is_file():
        print(f"edl not found: {edl_path}", file=sys.stderr)
        return 2
    project_dir = (
        Path(args.project_dir).resolve() if args.project_dir
        else edl_path.parent.parent
    )
    edl = json.loads(edl_path.read_text())
    if not edl.get("segments"):
        print("edl has no segments", file=sys.stderr)
        return 2

    output = Path(args.output)
    cmd, _ = build_command(edl, project_dir, output, args.height)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print("ffmpeg render failed:", file=sys.stderr)
        print(" ".join(cmd), file=sys.stderr)
        print(proc.stderr.strip()[-2000:], file=sys.stderr)
        return 1

    expected = sum(segment_durations(edl))
    actual = probe_duration(output)

    boundary_count = 0
    if args.boundary_frames:
        boundary_count = extract_boundary_frames(
            output, edl, Path(args.boundary_frames))

    summary = {
        "segments": len(edl["segments"]),
        "expected_duration_seconds": round(expected, 3),
        "actual_duration_seconds": round(actual, 3) if actual is not None else None,
        "boundary_frames": boundary_count,
        "output": str(output.resolve()),
    }
    print(json.dumps(summary, indent=2))

    if actual is None or abs(actual - expected) > 0.5:
        print(
            f"duration mismatch: expected {expected:.3f}s, got {actual}s",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
