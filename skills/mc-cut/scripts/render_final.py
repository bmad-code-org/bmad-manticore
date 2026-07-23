#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Final-quality render of cut/edl.json with the beat table's graphics
composited: the offered gate-4 deliverable.

Usage:
    uv run {skill-root}/scripts/render_final.py <edl.json> -o renders/final.mp4 \
        [--project-dir <dir>] [--beats beats/beats.md --graphics-dir graphics/] \
        [--codec auto] [--crf 18] [--height <H>] [--parallel 2] \
        [--loudness-target -14] [--no-loudnorm] \
        [--boundary-frames <dir>] [--skip-disk-check] [--keep-temp]

Purpose:
    Renders the same EDL the creator approved at gate 2, with graphics
    composited from the approved beat table, at delivery resolution and codec.
    It shares its compositing core (composite_core.py, this folder) with
    render_preview.py, so the composited preview the creator iterated on is
    exactly what the final bakes.

Contract:
    input    edl.json {source, fade_ms, pad_ms, segments[]} (seconds against
             each segment's source). Optional --beats beats/beats.md (the
             engine-neutral beat table; anchors measured against the EDITED
             timeline) plus --graphics-dir holding one rendered overlay per
             beat id (<id>.mov ProRes 4444 alpha, or .webm/.mp4/.mkv/.png).
             Beats without a matching file are listed in the summary as
             overlays_missing, never fatal.
    output   an H.264 (or HEVC) mp4 at -o. The timeline is split into up to
             --parallel chunks at internal cut boundaries that avoid every
             overlay window; each chunk renders in its own ffmpeg process to
             an MPEG-TS intermediate beside the output, then the chunks are
             losslessly concatenated (concat demuxer, -c copy,
             aac_adtstoasc, +faststart). Intermediates are removed unless
             --keep-temp.
    encode   --codec auto picks the platform's hardware ladder: on macOS
             h264_videotoolbox when this ffmpeg lists it (bitrate ladder by
             output height); on Windows the first of h264_nvenc, h264_qsv,
             h264_amf that is listed AND passes a one-frame test encode
             (lavfi color source to the null muxer, probed once per run); on
             Linux h264_nvenc then h264_vaapi the same way (vaapi gets
             device init and an hwupload filtergraph tail). libx264 -crf
             --crf (default 18, preset medium) is the fallback everywhere;
             an explicit --codec not offered by ffmpeg falls back to
             libx264. hevc_videotoolbox gets -tag:v hvc1. Audio aac 192k.
             --height scales (aspect kept, even width); default keeps the
             source resolution.
    loudnorm two-pass ffmpeg loudnorm on the finished file, final render
             only (the fast preview never normalizes): pass 1 measures
             (loudnorm print_format=json over the whole timeline, which is
             why it runs after chunk concat, never per chunk), pass 2
             re-encodes the audio with the measured values (linear mode,
             TP -1.5, LRA 11, aac 192k 48kHz) while the video stream is
             copied, then atomically replaces the output. Target is
             --loudness-target in LUFS (default -14, the YouTube reference);
             --no-loudnorm skips both passes. Silent/unmeasurable audio
             (non-finite measurements) skips pass 2 with a warning instead
             of failing.
    safety   disk preflight before any render: estimated output bytes (from
             the bitrate ladder) times 2 must fit on the output volume, else
             the render refuses with a clear message (--skip-disk-check
             overrides). Every looped image overlay input carries an explicit
             -t duration cap so looped/synthetic sources can never run away.
             Progress lines print to stderr (aggregated across chunks, from
             ffmpeg -progress). Expected vs actual duration is checked to
             0.5s, and --boundary-frames extracts before/after stills at
             every internal cut for the boundary-frame inspection.
    summary  json.dumps on stdout: segments, chunks, encoder, overlays,
             overlays_missing, expected/actual duration, loudnorm (null when
             --no-loudnorm; else {target, applied, input_i, output_i,
             output_tp}), output path.

Exit codes: 0 ok, 1 failure, 2 usage.

STATUS: implemented (pure logic covered by scripts/tests; render path covered
by the synthesized-fixture integration test there).
"""

import argparse
import json
import math
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import composite_core as core

# loudnorm companions to the integrated target: true peak ceiling and
# loudness range, the common VOD-delivery pairing for a -14 LUFS target.
LOUDNORM_TP = -1.5
LOUDNORM_LRA = 11.0

# The measurement keys pass 2 feeds back to loudnorm; all must be finite.
MEASURED_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh",
                 "target_offset")


def parse_loudnorm_json(stderr_text):
    """Extract the JSON stats block loudnorm prints at the end of stderr.
    Numeric-looking values (including '-inf') become floats. Returns None
    when no parseable block is present."""
    start = stderr_text.rfind("{")
    if start == -1:
        return None
    end = stderr_text.find("}", start)
    if end == -1:
        return None
    try:
        raw = json.loads(stderr_text[start:end + 1])
    except json.JSONDecodeError:
        return None
    stats = {}
    for k, v in raw.items():
        try:
            stats[k] = float(v)
        except (TypeError, ValueError):
            stats[k] = v
    return stats


def loudnorm_spec(target, measured=None):
    """The loudnorm filter spec: measurement form (pass 1) without
    `measured`, application form (pass 2, linear) with it."""
    spec = f"loudnorm=I={target:g}:TP={LOUDNORM_TP:g}:LRA={LOUDNORM_LRA:g}"
    if measured is not None:
        spec += (f":measured_I={measured['input_i']:.2f}"
                 f":measured_TP={measured['input_tp']:.2f}"
                 f":measured_LRA={measured['input_lra']:.2f}"
                 f":measured_thresh={measured['input_thresh']:.2f}"
                 f":offset={measured['target_offset']:.2f}"
                 ":linear=true")
    return spec + ":print_format=json"


def measure_loudness(path, target):
    """Loudnorm pass 1: decode the audio once, print the measurement JSON.
    Returns the stats dict or None on failure (command echoed to stderr)."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
           "-vn", "-af", loudnorm_spec(target), "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print("loudnorm measurement failed:", file=sys.stderr)
        print(" ".join(cmd), file=sys.stderr)
        print(proc.stderr.strip()[-2000:], file=sys.stderr)
        return None
    stats = parse_loudnorm_json(proc.stderr)
    if stats is None:
        print("loudnorm measurement produced no stats block", file=sys.stderr)
    return stats


def apply_loudnorm(path, measured, target):
    """Loudnorm pass 2: re-encode the audio with the measured values (video
    copied) to a dotfile beside the output, then atomically replace it.
    Returns the pass-2 stats dict (carries output_i/output_tp) or None on
    failure; the original file is left untouched on failure."""
    tmp = path.with_name(f".{path.stem}-loudnorm{path.suffix}")
    cmd = ["ffmpeg", "-y", "-i", str(path), "-c:v", "copy",
           "-af", loudnorm_spec(target, measured),
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    if path.suffix.lower() in (".mp4", ".mov"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(tmp))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print("loudnorm apply failed:", file=sys.stderr)
        print(" ".join(cmd), file=sys.stderr)
        print(proc.stderr.strip()[-2000:], file=sys.stderr)
        tmp.unlink(missing_ok=True)
        return None
    stats = parse_loudnorm_json(proc.stderr) or {}
    tmp.replace(path)
    return stats


def run_loudnorm(output, target):
    """Both loudnorm passes over the finished render. Returns the summary
    dict, or None on a hard failure (caller exits 1)."""
    print(f"render_final: loudnorm pass 1 of 2 (measuring, target "
          f"{target:g} LUFS)", file=sys.stderr)
    measured = measure_loudness(output, target)
    if measured is None:
        return None
    finite = all(isinstance(measured.get(k), float)
                 and math.isfinite(measured[k]) for k in MEASURED_KEYS)
    if not finite:
        print("render_final: audio is silent or unmeasurable; skipping "
              "loudness normalization", file=sys.stderr)
        return {"target": target, "applied": False,
                "input_i": measured.get("input_i"),
                "output_i": None, "output_tp": None}
    print("render_final: loudnorm pass 2 of 2 (applying)", file=sys.stderr)
    stats = apply_loudnorm(output, measured, target)
    if stats is None:
        return None
    return {"target": target, "applied": True,
            "input_i": measured["input_i"],
            "output_i": stats.get("output_i"),
            "output_tp": stats.get("output_tp")}


def run_chunks(cmds, durations, total):
    """Run the chunk ffmpeg commands in parallel, aggregating -progress output
    into percent lines on stderr. Returns (return_codes, stderr_tails)."""
    procs, readers = [], []
    progress = [0.0] * len(cmds)
    tails = [""] * len(cmds)
    lock = threading.Lock()

    def read_out(i, pipe):
        for line in pipe:
            info = core.parse_progress(line)
            if "seconds" in info:
                with lock:
                    progress[i] = min(info["seconds"], durations[i])
        pipe.close()

    def read_err(i, pipe):
        tail = deque(maxlen=60)
        for line in pipe:
            tail.append(line)
        tails[i] = "".join(tail)
        pipe.close()

    for i, cmd in enumerate(cmds):
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        procs.append(p)
        for target, pipe in ((read_out, p.stdout), (read_err, p.stderr)):
            t = threading.Thread(target=target, args=(i, pipe), daemon=True)
            t.start()
            readers.append(t)

    print(f"render_final: rendering {len(cmds)} chunk(s), {total:.1f}s of "
          "timeline", file=sys.stderr)
    last = -1
    while any(p.poll() is None for p in procs):
        time.sleep(0.5)
        with lock:
            done = sum(progress)
        pct = int(done / total * 100) if total else 0
        if pct != last:
            print(f"render_final: {pct}% ({done:.1f}/{total:.1f}s, "
                  f"{len(cmds)} chunk(s))", file=sys.stderr)
            last = pct
    for t in readers:
        t.join(timeout=5)
    return [p.wait() for p in procs], tails


def concat_chunks(chunk_files, output):
    """Losslessly concatenate the MPEG-TS chunks into the final mp4."""
    list_file = output.parent / f".{output.stem}-concat.txt"
    lines = []
    for p in chunk_files:
        quoted = str(p.resolve()).replace("'", "'\\''")
        lines.append(f"file '{quoted}'\n")
    list_file.write_text("".join(lines), encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
           "-c", "copy", "-bsf:a", "aac_adtstoasc",
           "-movflags", "+faststart", str(output)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    list_file.unlink(missing_ok=True)
    if proc.returncode != 0:
        print("ffmpeg concat failed:", file=sys.stderr)
        print(" ".join(cmd), file=sys.stderr)
        print(proc.stderr.strip()[-2000:], file=sys.stderr)
        return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("edl", help="path to cut/edl.json")
    parser.add_argument("-o", "--output", required=True,
                        help="output final.mp4")
    parser.add_argument("--project-dir", default=None,
                        help="base for source paths (default: edl parent's parent)")
    parser.add_argument("--beats", default=None,
                        help="beats/beats.md to composite graphics from")
    parser.add_argument("--graphics-dir", default=None,
                        help="dir holding one rendered overlay per beat id")
    parser.add_argument("--codec", default="auto",
                        help="auto | libx264 | h264_videotoolbox | hevc_videotoolbox")
    parser.add_argument("--crf", type=int, default=18,
                        help="libx264 quality (ignored by videotoolbox)")
    parser.add_argument("--height", type=int, default=None,
                        help="scale output to this height (default: source native)")
    parser.add_argument("--parallel", type=int, default=2,
                        help="max parallel render chunks (default 2)")
    parser.add_argument("--loudness-target", type=float, default=-14.0,
                        help="two-pass loudnorm integrated target in LUFS "
                             "(default -14)")
    parser.add_argument("--no-loudnorm", action="store_true",
                        help="skip loudness normalization entirely")
    parser.add_argument("--boundary-frames", default=None,
                        help="dir to write per-cut boundary stills into")
    parser.add_argument("--skip-disk-check", action="store_true")
    parser.add_argument("--keep-temp", action="store_true",
                        help="keep chunk intermediates beside the output")
    args = parser.parse_args(argv)

    edl_path = Path(args.edl).resolve()
    if not edl_path.is_file():
        print(f"edl not found: {edl_path}", file=sys.stderr)
        return 2
    if bool(args.beats) != bool(args.graphics_dir):
        print("--beats and --graphics-dir must be given together", file=sys.stderr)
        return 2
    project_dir = (
        Path(args.project_dir).resolve() if args.project_dir
        else edl_path.parent.parent
    )
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    if not edl.get("segments"):
        print("edl has no segments", file=sys.stderr)
        return 2

    overlays, missing = [], []
    if args.beats:
        try:
            text = Path(args.beats).read_text(encoding="utf-8")
        except OSError as e:
            print(f"cannot read beat table: {e}", file=sys.stderr)
            return 1
        beats, skipped = core.parse_beats_table(text)
        for reason in skipped:
            print(f"beat row skipped: {reason}", file=sys.stderr)
        overlays, missing = core.resolve_overlays(beats, args.graphics_dir)

    distinct = []
    for seg in edl["segments"]:
        if seg["source"] not in distinct:
            distinct.append(seg["source"])
    multi = len(distinct) > 1

    # Output dimensions from the first source (needed for overlay scaling,
    # the bitrate ladder, and the disk estimate).
    dims = core.probe_dims(project_dir / edl["segments"][0]["source"])
    if dims is None:
        print("cannot probe source dimensions "
              f"({project_dir / edl['segments'][0]['source']})", file=sys.stderr)
        return 1
    if args.height:
        out_h = args.height
        out_w = core.even(dims[0] * args.height / dims[1])
        scale_height = args.height
    else:
        out_w, out_h = core.even(dims[0]), core.even(dims[1])
        scale_height = None
    overlay_size = (out_w, out_h) if overlays else None
    # Multi-source timelines normalize every segment (and every chunk) to this
    # one frame so the mixed-size concat matches; single-source is unchanged.
    target = (out_w, out_h) if multi else None
    # Audio-less sources get synthesized silence; probe each distinct source
    # once and pass the map to every chunk.
    audio_map = {src: core.probe_has_audio(project_dir / src)
                 for src in distinct}

    total = sum(core.segment_durations(edl))
    encoder = core.pick_encoder(args.codec)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    needed = core.estimate_output_bytes(total, out_h, encoder)
    ok, free = core.check_disk(output.parent, needed)
    if not ok and not args.skip_disk_check:
        print(f"render_final: insufficient disk space: needs about "
              f"{2 * needed / 1e9:.1f} GB headroom (2x the estimated "
              f"{needed / 1e9:.1f} GB output), {free / 1e9:.1f} GB free on "
              f"{output.parent}. Free space or pass --skip-disk-check.",
              file=sys.stderr)
        return 1

    enc = core.encode_args(encoder, crf=args.crf, height=out_h)
    progress_flags = ("-progress", "pipe:1", "-nostats")
    chunks = core.plan_chunks(edl, overlays, args.parallel)

    if len(chunks) == 1:
        cmd, _ = core.build_command(edl, project_dir, output, scale_height,
                                    overlays=overlays, overlay_size=overlay_size,
                                    encode=enc, extra_output_flags=progress_flags,
                                    encoder=encoder, target=target,
                                    audio_map=audio_map)
        rcs, tails = run_chunks([cmd], [total], total)
        if rcs[0] != 0:
            print("ffmpeg render failed:", file=sys.stderr)
            print(" ".join(cmd), file=sys.stderr)
            print(tails[0].strip()[-2000:], file=sys.stderr)
            return 1
    else:
        cmds, files, durs = [], [], []
        for i, ch in enumerate(chunks):
            sub = {
                "source": edl.get("source"),
                "fade_ms": edl.get("fade_ms", 30),
                "segments": edl["segments"][ch["seg_start"]:ch["seg_end"]],
            }
            f = output.parent / f".{output.stem}-chunk{i}.ts"
            cmd, _ = core.build_command(sub, project_dir, f, scale_height,
                                        overlays=ch["overlays"],
                                        overlay_size=overlay_size,
                                        encode=enc,
                                        extra_output_flags=progress_flags,
                                        encoder=encoder, target=target,
                                        audio_map=audio_map)
            cmds.append(cmd)
            files.append(f)
            durs.append(ch["duration"])
        rcs, tails = run_chunks(cmds, durs, total)
        if any(rcs):
            for i, rc in enumerate(rcs):
                if rc:
                    print(f"ffmpeg chunk {i} failed:", file=sys.stderr)
                    print(" ".join(cmds[i]), file=sys.stderr)
                    print(tails[i].strip()[-2000:], file=sys.stderr)
            if not args.keep_temp:
                for f in files:
                    f.unlink(missing_ok=True)
            return 1
        ok = concat_chunks(files, output)
        if not args.keep_temp:
            for f in files:
                f.unlink(missing_ok=True)
        if not ok:
            return 1

    loudnorm = None
    if not args.no_loudnorm:
        loudnorm = run_loudnorm(output, args.loudness_target)
        if loudnorm is None:
            return 1

    actual = core.probe_duration(output)

    boundary_count = 0
    if args.boundary_frames:
        boundary_count = core.extract_boundary_frames(
            output, edl, Path(args.boundary_frames))

    summary = {
        "segments": len(edl["segments"]),
        "chunks": len(chunks),
        "encoder": encoder,
        "overlays": len(overlays),
        "overlays_missing": missing,
        "expected_duration_seconds": round(total, 3),
        "actual_duration_seconds": round(actual, 3) if actual is not None else None,
        "loudnorm": loudnorm,
        "boundary_frames": boundary_count,
        "output": str(output.resolve()),
    }
    print(json.dumps(summary, indent=2))

    if actual is None or abs(actual - total) > 0.5:
        print(f"duration mismatch: expected {total:.3f}s, got {actual}s",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
