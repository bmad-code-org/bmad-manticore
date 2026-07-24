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
        [--segment-target-seconds 600] [--no-cache] \
        [--loudness-target -14] [--no-loudnorm] \
        [--boundary-frames <dir>] [--skip-disk-check] [--keep-temp]

Purpose:
    Renders the same EDL the creator approved at gate 2, with graphics
    composited from the approved beat table, at delivery resolution and codec.
    It shares its compositing core (composite_core.py, this folder) with
    render_preview.py, so the composited preview the creator iterated on is
    exactly what the final bakes.

    Incremental: the timeline is partitioned into persistent, content-addressed
    render segments under renders/segments/, so a re-render only re-encodes the
    segments whose inputs actually changed (a tweaked graphic, a re-cut region)
    and reuses the rest from cache. Changing one overlay on a two-hour video is
    a seconds-long re-render, not a full one.

Contract:
    input    edl.json {source, fade_ms, pad_ms, segments[]} (seconds against
             each segment's source). Optional --beats beats/beats.md (the
             engine-neutral beat table; anchors measured against the EDITED
             timeline) plus --graphics-dir holding one rendered overlay per
             beat id (<id>.mov ProRes 4444 alpha, or .webm/.mp4/.mkv/.png).
             Beats without a matching file are listed in the summary as
             overlays_missing, never fatal.
    output   an H.264 (or HEVC) mp4 at -o. The timeline is partitioned into
             render segments with STABLE, sticky boundaries: greedy left to
             right to --segment-target-seconds (default 600 = 10 min), each
             snapped to the next safe cut that no overlay spans, so an edit
             cannot move an earlier boundary. Each segment is content-addressed
             by a hash of its inputs (its EDL slice, the digest of every source
             and overlay file it consumes, and the resolved render identity)
             and persisted VIDEO-ONLY to renders/segments/<id>.ts. On each
             render only the segments whose hash changed (or all of them when
             the encoder/ffmpeg/dimensions change, since a mixed-encoder concat
             is invalid) are re-encoded, via a bounded pool of --parallel
             workers, each to a temp renamed into place only on success; the
             rest are reused. The video segments are then losslessly
             concatenated (concat demuxer, -c copy, +faststart) and the
             whole-program audio, rebuilt fresh every render (video-only
             segments never carry audio, so per-segment AAC seam drift cannot
             accumulate), is muxed in (-c copy). renders/segments/manifest.json
             records the render identity, each segment's hash, and the concat
             order; --no-cache re-renders every segment; --keep-temp keeps the
             concat/audio intermediates. Persisted segments survive across runs
             as the cache; segments orphaned by a boundary shift are removed.
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
             why it runs after the mux, never per segment), pass 2
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
             Progress lines print to stderr (aggregated across the segment
             pool, from ffmpeg -progress). Expected vs actual duration is
             checked to 0.5s, and --boundary-frames extracts before/after
             stills at every internal cut for the boundary-frame inspection.
    summary  json.dumps on stdout: segments (EDL segment count),
             render_segments, segments_rendered, segments_cached, encoder,
             overlays, overlays_missing, expected/actual duration, loudnorm
             (null when --no-loudnorm; else {target, applied, input_i,
             output_i, output_tp}), boundary_frames, output path.

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


def run_jobs(cmds, durations, total, workers, label="render_final",
             noun="segment"):
    """Run the ffmpeg commands with at most `workers` in flight at once,
    aggregating -progress output into percent lines on stderr. A bounded pool
    (not one process per job) so an incremental render of many dirty segments
    never spawns dozens of concurrent ffmpegs. Returns (return_codes,
    stderr_tails) aligned to cmds; an empty cmds list returns ([], [])."""
    n = len(cmds)
    if n == 0:
        return [], []
    rcs = [None] * n
    tails = [""] * n
    progress = [0.0] * n
    lock = threading.Lock()
    sem = threading.Semaphore(max(1, workers))

    def run_one(i):
        with sem:
            p = subprocess.Popen(cmds[i], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)

            def read_out(pipe):
                for line in pipe:
                    info = core.parse_progress(line)
                    if "seconds" in info:
                        with lock:
                            progress[i] = min(info["seconds"], durations[i])
                pipe.close()

            t = threading.Thread(target=read_out, args=(p.stdout,), daemon=True)
            t.start()
            tail = deque(maxlen=60)
            for line in p.stderr:
                tail.append(line)
            p.stderr.close()
            t.join(timeout=5)
            rcs[i] = p.wait()
            tails[i] = "".join(tail)
            with lock:
                progress[i] = durations[i]

    workers_threads = [threading.Thread(target=run_one, args=(i,), daemon=True)
                       for i in range(n)]
    for t in workers_threads:
        t.start()
    print(f"{label}: rendering {n} {noun}(s), {total:.1f}s "
          f"({min(max(1, workers), n)} at a time)", file=sys.stderr)
    last = -1
    while any(t.is_alive() for t in workers_threads):
        time.sleep(0.5)
        with lock:
            done = sum(progress)
        pct = int(done / total * 100) if total else 0
        if pct != last:
            print(f"{label}: {pct}% ({done:.1f}/{total:.1f}s, {n} {noun}(s))",
                  file=sys.stderr)
            last = pct
    for t in workers_threads:
        t.join()
    return rcs, tails


def concat_video(segment_files, output):
    """Losslessly concatenate the video-only segment files into `output`
    (concat demuxer, -c copy). No audio bitstream filter: the segments carry
    no audio, the whole-program audio is muxed in afterward."""
    list_file = output.parent / f".{output.stem}-concat.txt"
    lines = []
    for p in segment_files:
        quoted = str(Path(p).resolve()).replace("'", "'\\''")
        lines.append(f"file '{quoted}'\n")
    list_file.write_text("".join(lines), encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
           "-c", "copy", "-movflags", "+faststart", str(output)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    list_file.unlink(missing_ok=True)
    if proc.returncode != 0:
        print("ffmpeg video concat failed:", file=sys.stderr)
        print(" ".join(cmd), file=sys.stderr)
        print(proc.stderr.strip()[-2000:], file=sys.stderr)
        return False
    return True


def mux_av(video, audio, output):
    """Mux the concatenated video-only file and the whole-program audio into
    the final container, both stream-copied (no re-encode)."""
    cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(audio),
           "-c", "copy", "-map", "0:v:0", "-map", "1:a:0"]
    if str(output).endswith((".mp4", ".mov")):
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(output))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print("ffmpeg mux failed:", file=sys.stderr)
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
                        help="max segment renders in flight at once (worker "
                             "pool size, default 2)")
    parser.add_argument("--segment-target-seconds", type=float, default=600.0,
                        help="target duration per persisted render segment; "
                             "boundaries snap to the next safe cut (default "
                             "600 = 10 min)")
    parser.add_argument("--no-cache", action="store_true",
                        help="ignore any persisted segments and re-render every "
                             "one (the manifest is still rewritten)")
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

    progress_flags = ("-progress", "pipe:1", "-nostats")
    enc_video = core.encode_args(encoder, crf=args.crf, height=out_h,
                                 streams="video")
    enc_audio = core.encode_args(encoder, streams="audio")

    # The persistent incremental segment cache lives beside the output.
    segments_dir = output.parent / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = segments_dir / "manifest.json"

    segments = core.plan_segments(edl, overlays, args.segment_target_seconds)

    # Shared render identity: any change here (encoder swap, ffmpeg bump,
    # output dimensions, encode args) dirties every segment, since a
    # mixed-encoder -c copy concat would produce a broken file.
    render_key = {
        "encoder": encoder,
        "ffmpeg": core.ffmpeg_version(),
        "dims": [out_w, out_h],
        "encode": enc_video,
    }
    # Sources are fingerprinted cheaply (size+mtime); overlay files are hashed
    # by content so a re-rendered graphic dirties the segment that consumes it.
    source_digests = {src: core.content_digest(project_dir / src, cheap=True)
                      for src in distinct}
    overlay_digests = {ov["id"]: core.content_digest(ov["path"])
                       for ov in overlays}

    prior = None if args.no_cache else core.load_manifest(manifest_path)
    prior_by_id = {e["id"]: e for e in (prior or {}).get("segments", [])}
    key_changed = bool(prior) and prior.get("render_key") != render_key
    if key_changed:
        print("render_final: render settings changed (encoder/ffmpeg/dims/"
              "encode); re-rendering every segment", file=sys.stderr)

    # Resolve each render-segment's stable id, content hash, and cache status.
    plan = []
    for seg in segments:
        sid = core.segment_id(edl, seg)
        ih = core.segment_input_hash(edl, seg, render_key, source_digests,
                                     overlay_digests)
        seg_file = segments_dir / f"{sid}.ts"
        cached = (not key_changed
                  and prior_by_id.get(sid, {}).get("input_hash") == ih
                  and seg_file.is_file())
        plan.append({"seg": seg, "id": sid, "hash": ih,
                     "file": seg_file, "cached": cached})

    # Render the dirty segments video-only (deduped by id), via the bounded
    # worker pool, each to a temp renamed into place only on success.
    jobs = {}
    for item in plan:
        if not item["cached"]:
            jobs.setdefault(item["id"], item)
    jobs = list(jobs.values())
    if jobs:
        cmds, durs, temps = [], [], []
        for item in jobs:
            seg = item["seg"]
            sub = {
                "source": edl.get("source"),
                "fade_ms": edl.get("fade_ms", 30),
                "segments": edl["segments"][seg["seg_start"]:seg["seg_end"]],
            }
            tmp = segments_dir / f".{item['id']}.tmp.ts"
            cmd, _ = core.build_command(
                sub, project_dir, tmp, scale_height, overlays=seg["overlays"],
                overlay_size=overlay_size, encode=enc_video,
                extra_output_flags=progress_flags, encoder=encoder,
                target=target, streams="video")
            cmds.append(cmd)
            durs.append(seg["duration"])
            temps.append(tmp)
        rcs, tails = run_jobs(cmds, durs, sum(durs), args.parallel)
        if any(rcs):
            for i, rc in enumerate(rcs):
                if rc:
                    print(f"ffmpeg segment {jobs[i]['id']} failed:",
                          file=sys.stderr)
                    print(" ".join(cmds[i]), file=sys.stderr)
                    print(tails[i].strip()[-2000:], file=sys.stderr)
            for tmp in temps:
                tmp.unlink(missing_ok=True)
            return 1
        for item, tmp in zip(jobs, temps):
            tmp.replace(item["file"])
    else:
        print("render_final: all segments cached; skipping video re-render",
              file=sys.stderr)

    # Rebuild the whole-program audio every render: cheap, and it keeps the
    # timeline exact (per-segment AAC seam priming would accumulate drift).
    audio_tmp = output.parent / f".{output.stem}-audio.m4a"
    acmd, _ = core.build_command(
        edl, project_dir, audio_tmp, None, encode=enc_audio,
        extra_output_flags=progress_flags, audio_map=audio_map,
        streams="audio")
    arcs, atails = run_jobs([acmd], [total], total, 1, noun="audio pass")
    if arcs[0] != 0:
        print("ffmpeg audio render failed:", file=sys.stderr)
        print(" ".join(acmd), file=sys.stderr)
        print(atails[0].strip()[-2000:], file=sys.stderr)
        audio_tmp.unlink(missing_ok=True)
        return 1

    # Losslessly concat the (cached + fresh) video segments, then mux the
    # whole-program audio in. Both stream-copied, no re-encode.
    video_tmp = output.parent / f".{output.stem}-video.mp4"
    ok = concat_video([item["file"] for item in plan], video_tmp)
    if ok:
        ok = mux_av(video_tmp, audio_tmp, output)
    if not args.keep_temp:
        video_tmp.unlink(missing_ok=True)
        audio_tmp.unlink(missing_ok=True)
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

    # Record the manifest (only on a successful render) and GC segment files
    # left orphaned by boundary shifts. One manifest entry per unique id.
    seen_ids, manifest_segments = set(), []
    for item in plan:
        if item["id"] in seen_ids:
            continue
        seen_ids.add(item["id"])
        off = item["seg"]["offset"]
        manifest_segments.append({
            "id": item["id"],
            "input_hash": item["hash"],
            "file": f"segments/{item['id']}.ts",
            "edl_range": [round(off, 3), round(off + item["seg"]["duration"], 3)],
            "duration": round(item["seg"]["duration"], 3),
        })
    core.save_manifest(manifest_path, {
        "render_key": render_key,
        "segment_target_seconds": args.segment_target_seconds,
        "loudnorm": loudnorm,
        "segments": manifest_segments,
        "concat_order": [item["id"] for item in plan],
    })
    for stale in segments_dir.glob("seg-*.ts"):
        if stale.stem not in seen_ids:
            stale.unlink(missing_ok=True)

    rendered = sum(1 for item in plan if not item["cached"])
    summary = {
        "segments": len(edl["segments"]),
        "render_segments": len(plan),
        "segments_rendered": rendered,
        "segments_cached": len(plan) - rendered,
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
