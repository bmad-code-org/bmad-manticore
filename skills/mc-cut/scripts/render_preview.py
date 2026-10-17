#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Render the fast low-res preview of cut/edl.json and self-verify its cut
boundaries; optionally composite the beat table's graphics onto it.

Usage:
    uv run {skill-root}/scripts/render_preview.py <edl.json> -o <preview.mp4> \
        [--project-dir <dir>] [--boundary-frames <dir>] [--height 720] \
        [--beats <beats.md> --graphics-dir <dir>]

Purpose:
    The render-first iteration artifact: a low-bitrate preview.mp4 the creator
    watches after EVERY cutplan approval, plus the boundary-frame stills the
    skill inspects to confirm no cut lands inside a word (the cutting rules'
    self-verify step). Unlike the FCPXML export, the preview uses the EDL's
    raw segment times (not frame-snapped boundaries) and bakes the fades in,
    so it is the ground truth for what the audience hears. Once the beats
    stage has run and graphics/ holds rendered overlays, pass --beats and
    --graphics-dir to re-render the preview WITH graphics composited at low
    res; the compositing core (composite_core.py) is shared with
    render_final.py, so the composited preview shows exactly what the final
    will bake.

Contract:
    input   edl.json: {source, fade_ms, pad_ms, segments[]}; each segment is
            {source, start, end, ...} with start/end in seconds against its
            source. N distinct sources become N ffmpeg inputs.
    output  a draft H.264/AAC mp4 at -o. One ffmpeg invocation builds the
            whole timeline via filter_complex: per segment a trim/atrim from
            its source, an afade in and out of fade_ms (edl.json, default
            30ms) at every boundary, scaled to --height (default 720, aspect
            kept, even width), then concat; encoded libx264 crf 28 preset
            veryfast + aac. The exact ffmpeg command is printed to stderr on
            failure.
    beats   optional composited mode: --beats beats/beats.md (the
            engine-neutral beat table; anchors measured against the EDITED
            timeline) plus --graphics-dir graphics/ holding one rendered
            overlay per beat id (<id>.mov ProRes 4444 alpha, or
            .webm/.mp4/.mkv/.png). Overlays are scaled to the preview frame
            and composited in their beat windows. Beats without a matching
            file are reported in the summary as overlays_missing, never
            fatal.
    speed   Two optimizations make the composited preview minutes rather than
            an hour on a long 4K cut; both are explained in full in
            composite_core.py's "fast preview compositing" block.
            PROXY MASTERS: each source is transcoded once, linearly, to the
            preview height into <output dir>/proxy/, and the preview is cut
            from that instead of seeking into a 4K master once per segment.
            Timecodes are identical, so the EDL is untouched. The proxy is
            reused by every later preview and rebuilt only when the source
            content changes. --no-proxy opts out; the final render never
            uses proxies.
            OVERLAY LANES: overlays are packed into the fewest
            non-overlapping time lanes, each built as a cheap concat of
            transparent gaps and overlay clips, and only the lanes are
            stacked onto the base cut. Stack depth becomes max concurrent
            overlays rather than total overlays (56 became 2 on the project
            that exposed this). --lane-codec picks the intermediate codec.
    boundary-frames
            optional dir. After the render, one frame just before and one just
            after each internal cut boundary of the OUTPUT is extracted to
            <dir>/boundary-<n>-a.jpg (before) and boundary-<n>-b.jpg (after),
            n starting at 1, so the skill can inspect each cut.
    safety  the deliverable path is NEVER written directly. The encode goes
            to a per-process temp file, which is then decode-validated
            (`ffmpeg -v error -f null -`, zero errors) and duration-checked,
            checked for supersede (has the EDL changed since this render
            started?), and only then atomically moved into place with a
            <output>.key sidecar recording the render identity. See the
            "render identity, atomic publish, output validation" block in
            composite_core.py for why each of the three exists.
    summary json.dumps on stdout: segments, expected_duration (sum of raw
            segment durations), actual_duration (ffprobe of the output),
            validated, render_key, boundary_frames, overlays /
            overlays_missing (composited mode), output path.

Exit codes: 0 ok (published and validated), 1 failure (encode failed, output
failed validation, or the render was superseded; in every case the existing
output is left untouched), 2 usage.

STATUS: implemented (plain mode validated on real footage; composited mode
covered by the scripts/tests suite).
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # noqa: E402
import composite_core as core  # noqa: E402

# Re-exported so callers and tests can use the script as the single surface.
segment_durations = core.segment_durations
boundary_times = core.boundary_times
build_filter_complex = core.build_filter_complex
build_command = core.build_command
probe_duration = core.probe_duration
extract_boundary_frames = core.extract_boundary_frames


def ensure_proxies(sources, project_dir, proxy_dir, height, intra=True):
    """Build (or reuse) a preview proxy per source. Returns {source: rel_path}.

    A proxy is one linear transcode of the whole source to the preview
    height. Cutting 234 segments out of a 720p proxy is dramatically cheaper
    than seeking into a 4K master 234 times, and the proxy survives across
    every later re-render. Timecodes are identical, so the EDL needs no
    adjustment (see core.proxied_edl).

    Sources that fail to transcode are simply left unproxied: the render then
    cuts them from the master, slower but correct.
    """
    proxy_dir = Path(proxy_dir)
    proxy_dir.mkdir(parents=True, exist_ok=True)
    mapping = {}
    for src in sources:
        abs_src = project_dir / src
        if not abs_src.is_file():
            continue
        proxy = core.proxy_path(proxy_dir, src, height, intra=intra)
        if not core.proxy_is_fresh(proxy, abs_src):
            print(f"render_preview: building {height}p proxy for {src} "
                  "(once; reused by every later preview)", file=sys.stderr)
            # Per-process temp then atomic replace. Proxies are shared state
            # keyed on source and height, so two renders started close
            # together would otherwise interleave writes into one path and
            # hand each other a corrupt proxy. Same failure the deliverable
            # path had; it deserves the same defence.
            staged = core.temp_render_path(proxy, "proxy")
            cmd = core.build_proxy_command(abs_src, staged, height,
                                           intra=intra)
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                core.discard_render(staged)
                print(f"render_preview: proxy build failed for {src}; "
                      "falling back to the master for this source",
                      file=sys.stderr)
                print(proc.stderr.strip()[-800:], file=sys.stderr)
                continue
            core.publish_render(staged, proxy)
            core.write_proxy_sidecar(proxy, abs_src)
        try:
            mapping[src] = str(proxy.relative_to(project_dir))
        except ValueError:
            mapping[src] = str(proxy)
    return mapping


def render_lanes(overlays, total, size, fps, work_dir, codec):
    """Render one file per overlay lane. Returns (lane_files, lane_count).

    Raises RuntimeError with the ffmpeg tail when a lane fails to build.
    """
    lanes = core.plan_overlay_lanes(overlays)
    files = []
    for i, lane in enumerate(lanes):
        out = Path(work_dir) / f"lane{i + 1}.mov"
        cmd = core.build_lane_command(lane, total, size, fps, out, codec)
        if not cmd:
            continue
        print(f"render_preview: lane {i + 1}/{len(lanes)} "
              f"({len(lane)} overlays)", file=sys.stderr)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"lane {i + 1} failed: "
                               f"{proc.stderr.strip()[-800:]}")
        files.append(out)
    return files, len(lanes)


def gather_overlays(beats_path, graphics_dir):
    """Parse the beat table and resolve overlay files.

    Returns (overlays, missing, skipped) or raises OSError/ValueError with a
    readable message."""
    text = Path(beats_path).read_text(encoding="utf-8")
    beats, skipped = core.parse_beats_table(text)
    overlays, missing = core.resolve_overlays(beats, graphics_dir)
    return overlays, missing, skipped


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("edl", help="path to cut/edl.json")
    parser.add_argument("-o", "--output", required=True, help="output preview.mp4")
    parser.add_argument("--project-dir", default=None,
                        help="base for source paths (default: edl parent's parent)")
    parser.add_argument("--boundary-frames", default=None,
                        help="dir to write per-cut boundary stills into")
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--beats", default=None,
                        help="beats/beats.md to composite graphics from")
    parser.add_argument("--graphics-dir", default=None,
                        help="dir holding one rendered overlay per beat id")
    parser.add_argument("--proxy-only", action="store_true",
                        help="build (or reuse) the all-intra proxies and "
                             "exit without rendering. The virtual timeline "
                             "at gate 2 needs the proxy but not a render.")
    parser.add_argument("--no-proxy", action="store_true",
                        help="cut from the masters instead of building "
                             "preview proxies (slower on 4K sources)")
    parser.add_argument("--proxy-dir", default=None,
                        help="where preview proxies live "
                             "(default: <output dir>/proxy)")
    parser.add_argument("--lane-codec", default=core.DEFAULT_LANE_CODEC,
                        choices=sorted(core.LANE_CODEC_ARGS),
                        help="intermediate codec for overlay lanes "
                             f"(default {core.DEFAULT_LANE_CODEC})")
    parser.add_argument("--fps", type=float, default=30.0,
                        help="preview frame rate for overlay lanes "
                             "(default 30)")
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
    edl = json.loads(edl_path.read_text())
    if not edl.get("segments"):
        print("edl has no segments", file=sys.stderr)
        return 2

    distinct = []
    for seg in edl["segments"]:
        if seg["source"] not in distinct:
            distinct.append(seg["source"])
    multi = len(distinct) > 1

    overlays, missing, skipped = [], [], []
    if args.beats:
        try:
            overlays, missing, skipped = gather_overlays(args.beats,
                                                         args.graphics_dir)
        except (OSError, ValueError) as e:
            print(f"cannot read beat table: {e}", file=sys.stderr)
            return 1
        for reason in skipped:
            print(f"beat row skipped: {reason}", file=sys.stderr)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    # Render identity, fixed before the encode starts, and computed against
    # the ORIGINAL edl so a proxy swap never changes a render's identity.
    # See composite_core's "render identity, atomic publish, output
    # validation" block.
    params = {"height": args.height, "mode": "preview",
              "beats": bool(args.beats)}
    overlay_digests = {ov["id"]: core.content_digest(ov["path"])
                       for ov in overlays}
    ffmpeg_v = core.ffmpeg_version()
    key = core.render_key(edl, params, overlay_digests, ffmpeg_v)
    tmp = core.temp_render_path(output, key)

    # Preview proxies: cut the preview from small linear transcodes instead
    # of seeking into 4K masters once per segment. Timecodes are unchanged,
    # so only the source paths differ.
    proxy_map = {}
    if not args.no_proxy:
        proxy_dir = (Path(args.proxy_dir) if args.proxy_dir
                     else output.parent / "proxy")
        proxy_map = ensure_proxies(distinct, project_dir, proxy_dir,
                                   args.height)
    if args.proxy_only:
        print(json.dumps({"ok": True, "mode": "proxy-only",
                          "proxies": {k: str(v) for k, v in
                                      proxy_map.items()}}, indent=2))
        return 0
    render_edl = core.proxied_edl(edl, proxy_map) if proxy_map else edl

    # One target frame is needed to scale overlays and to normalize mixed-size
    # sources so the concat inputs match (cam + screencast). Probed from what
    # is actually being cut.
    overlay_size = None
    target = None
    if overlays or multi:
        dims = core.probe_dims(
            project_dir / render_edl["segments"][0]["source"])
        if dims is None:
            print("cannot probe source dimensions", file=sys.stderr)
            return 1
        frame = (core.even(dims[0] * args.height / dims[1]), args.height)
        if overlays:
            overlay_size = frame
        if multi:
            target = frame

    # Audio-less sources (a screen recording with no audio) get synthesized
    # silence so the filtergraph never references a missing :a stream.
    audio_map = {seg["source"]: core.probe_has_audio(project_dir /
                                                     seg["source"])
                 for seg in render_edl["segments"]}

    expected = sum(segment_durations(edl))
    lane_count = 0
    work = tempfile.TemporaryDirectory(prefix="mc-preview-",
                                       dir=str(output.parent))
    try:
        if not overlays:
            # No graphics: one pass. This path re-encodes, and that is
            # deliberate. A concat-demuxer STREAM COPY off the all-intra
            # proxy is ~470x faster (2.8s vs 22min on a 379-segment cut), but
            # it emits duplicate DTS wherever a segment is only a frame or
            # two long, and the resulting file fails this script's own decode
            # validation. Flags (+genpts, avoid_negative_ts, timescale,
            # fps_mode) take a 379-segment cut from 125 errors to 3, never to
            # zero. Weakening the validation gate to publish it would be
            # trading a real correctness check for a speed number.
            #
            # The speed is not lost, it just belongs elsewhere: gate 2 review
            # runs on the VIRTUAL TIMELINE (edl_to_ffconcat.py), which needs
            # no mux at all and is verified frame-exact. Render a file only
            # when you need a file.
            cmd, _ = build_command(render_edl, project_dir, tmp, args.height,
                                   target=target, audio_map=audio_map)
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                core.discard_render(tmp)
                print("ffmpeg render failed:", file=sys.stderr)
                print(" ".join(cmd), file=sys.stderr)
                print(proc.stderr.strip()[-2000:], file=sys.stderr)
                return 1
            lane_mode = "encode"
        else:
            # Overlay lanes: build the base cut, pack the overlays into the
            # fewest non-overlapping lanes, then stack only the lanes. Depth
            # is max-concurrent-overlays, not overlay count.
            lane_mode = "encode"
            base = Path(work.name) / "base.mp4"
            cmd, _ = build_command(render_edl, project_dir, base, args.height,
                                   target=target, audio_map=audio_map)
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                core.discard_render(tmp)
                print("ffmpeg base render failed:", file=sys.stderr)
                print(" ".join(cmd), file=sys.stderr)
                print(proc.stderr.strip()[-2000:], file=sys.stderr)
                return 1
            base_dur = probe_duration(base) or expected
            try:
                lane_files, lane_count = render_lanes(
                    overlays, base_dur, overlay_size, args.fps, work.name,
                    args.lane_codec)
            except RuntimeError as e:
                core.discard_render(tmp)
                print(f"overlay lane render failed: {e}", file=sys.stderr)
                return 1
            print(f"render_preview: compositing {len(overlays)} overlays as "
                  f"{lane_count} lane(s)", file=sys.stderr)
            cmd = core.build_lane_composite_command(base, lane_files, tmp)
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                core.discard_render(tmp)
                print("ffmpeg composite failed:", file=sys.stderr)
                print(" ".join(cmd), file=sys.stderr)
                print(proc.stderr.strip()[-2000:], file=sys.stderr)
                return 1
    finally:
        work.cleanup()

    # Prove the file decodes BEFORE it is called a deliverable. A corrupt
    # mp4 still reports a plausible container duration, so the duration check
    # alone (all this used to do) cannot see it.
    valid, problems = core.validate_render(tmp, expected)
    if not valid:
        core.discard_render(tmp)
        print("render failed validation, output NOT published:",
              file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    # Supersede: if the EDL changed while this render ran, this render is
    # stale. Discard it rather than clobber whatever newer render is now in
    # flight or already published.
    live_key = core.current_render_key(edl_path, params, overlay_digests,
                                       ffmpeg_v)
    if live_key is not None and live_key != key:
        core.discard_render(tmp)
        print(json.dumps({"superseded": True, "rendered_key": key,
                          "current_key": live_key,
                          "output": str(output.resolve())}, indent=2))
        print(f"SUPERSEDED: {edl_path.name} changed while this render ran; "
              "discarded it instead of overwriting a newer render. Re-run "
              "against the current EDL.", file=sys.stderr)
        return 1

    core.publish_render(tmp, output, key)

    actual = probe_duration(output)
    boundary_count = 0
    if args.boundary_frames:
        boundary_count = extract_boundary_frames(
            output, edl, Path(args.boundary_frames))

    summary = {
        "segments": len(edl["segments"]),
        "expected_duration_seconds": round(expected, 3),
        "actual_duration_seconds": round(actual, 3) if actual is not None else None,
        "validated": True,
        "render_key": key,
        "proxied_sources": len(proxy_map),
        "lane": lane_mode,
        "boundary_frames": boundary_count,
        "output": str(output.resolve()),
    }
    if args.beats:
        summary["overlays"] = len(overlays)
        summary["overlay_lanes"] = lane_count
        summary["overlays_missing"] = missing
        summary["titles_in_resolve_only"] = sorted(
            core.title_ids(args.graphics_dir)) if args.graphics_dir else []
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
