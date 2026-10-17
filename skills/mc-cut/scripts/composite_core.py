#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Shared compositing core for mc-cut's renderers (library module, not a CLI).

Imported by render_preview.py and render_final.py, which sit in this same
folder (a script's own directory is on sys.path when invoked via uv run, and
both scripts insert it explicitly for safety). This module holds everything
the two renderers share so the composited preview and the final render are
guaranteed to bake the same thing:

    - EDL timeline math (segment durations, internal boundary times)
    - timecode parsing/formatting
    - beat-table (beats/beats.md) parsing, tolerant of 0.x rows missing
      type/engine/asset per the PIPELINE.md tolerance rule
    - overlay resolution (one rendered file per beat id in a graphics dir)
    - ffmpeg filter_complex and command construction, with optional overlay
      compositing (ProRes 4444 / WebM / mp4 / PNG over the concat output)
    - chunk planning for segment-parallel final renders
    - encoder selection (videotoolbox on macOS; probed nvenc/qsv/amf ladder
      on Windows and nvenc/vaapi ladder on Linux; libx264 fallback)
    - disk-space estimation and preflight
    - ffmpeg -progress output parsing
    - ffprobe wrappers and boundary-frame extraction

No config discovery: every function takes explicit arguments. Stdlib only.
"""

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# --- timeline math ----------------------------------------------------------


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


def even(x):
    """Nearest even integer, minimum 2 (codec-safe dimensions)."""
    return max(2, int(round(x / 2)) * 2)


# --- timecode ---------------------------------------------------------------


def parse_timecode(text):
    """'90', '90.5', '12.5s', '1:30', '01:02:03.25' -> seconds (float)."""
    s = str(text).strip()
    if s.endswith("s") and ":" not in s:
        s = s.rstrip("s")
    if not s:
        raise ValueError("empty timecode")
    parts = s.split(":")
    if len(parts) > 3:
        raise ValueError(f"unparseable timecode: {text!r}")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p.strip())
    if total < 0:
        raise ValueError(f"negative timecode: {text!r}")
    return total


def format_timecode(seconds, precision=0):
    """Seconds -> 'm:ss' or 'h:mm:ss', with optional fractional digits."""
    seconds = max(0.0, seconds)
    total = round(seconds, precision) if precision else int(round(seconds))
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = total - h * 3600 - m * 60
    if precision:
        sec_str = f"{s:0{3 + precision}.{precision}f}"
    else:
        sec_str = f"{int(s):02d}"
    if h:
        return f"{h}:{m:02d}:{sec_str}"
    return f"{m}:{sec_str}"


# --- beat table -------------------------------------------------------------


def parse_beats_table(text):
    """Parse the beats.md markdown table into beat dicts.

    Finds the first pipe table whose header row contains 'id' and 'start'.
    Columns are matched by header name, so extra columns and any column order
    are fine. Per the PIPELINE.md tolerance rule, rows missing type/engine/
    asset still parse (type defaults to 'overlay', asset to None). dur comes
    from the dur column, or end - start when only end is present.
    Returns (beats, skipped): beats as {id, start, dur, type, asset} with
    seconds as floats, skipped as human-readable reasons for unusable rows.
    """
    header = None
    col = {}
    beats, skipped = [], []
    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        low = [c.lower() for c in cells]
        if header is None:
            if "id" in low and "start" in low:
                header = low
                col = {name: i for i, name in enumerate(low)}
            continue
        if set("".join(cells)) <= set("-: "):
            continue  # separator row

        def cell(name):
            i = col.get(name)
            return cells[i] if i is not None and i < len(cells) else ""

        bid = cell("id")
        if not bid:
            skipped.append("row with empty id")
            continue
        try:
            start = parse_timecode(cell("start"))
        except ValueError:
            skipped.append(f"{bid}: unparseable start {cell('start')!r}")
            continue
        dur = None
        if cell("dur"):
            try:
                dur = parse_timecode(cell("dur"))
            except ValueError:
                dur = None
        if dur is None and cell("end"):
            try:
                dur = parse_timecode(cell("end")) - start
            except ValueError:
                dur = None
        if dur is None or dur <= 0:
            skipped.append(f"{bid}: no usable dur")
            continue
        asset = cell("asset")
        if asset.lower() in ("", "null", "none", "-"):
            asset = None
        beats.append({
            "id": bid,
            "start": round(start, 3),
            "dur": round(dur, 3),
            "type": cell("type").lower() or "overlay",
            "asset": asset,
        })
    return beats, skipped


OVERLAY_EXTS = (".mov", ".webm", ".mp4", ".mkv", ".png")


def title_ids(graphics_dir):
    """Beat ids built as Fusion titles in Resolve (graphics/titles.json).

    Those beats have no baked file by design, so they are never missing; this
    pipeline's renders do not show them, only Resolve's do.
    """
    path = Path(graphics_dir) / "titles.json"
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(t["id"]) for t in data if isinstance(t, dict) and t.get("id")}


def resolve_overlays(beats, graphics_dir):
    """Match each beat id to a rendered overlay file in graphics_dir.

    Looks for <id>.mov / .webm / .mp4 / .mkv / .png (first hit wins, in that
    order). Returns (found, missing): found as overlay dicts {id, path, start,
    dur, image} sorted by start, missing as the beat ids with no file and no
    entry in graphics/titles.json.
    """
    graphics_dir = Path(graphics_dir)
    titles = title_ids(graphics_dir)
    found, missing = [], []
    for b in beats:
        path = None
        for ext in OVERLAY_EXTS:
            cand = graphics_dir / f"{b['id']}{ext}"
            if cand.is_file():
                path = cand
                break
        if path is None:
            if b["id"] not in titles:
                missing.append(b["id"])
            continue
        found.append({
            "id": b["id"],
            "path": str(path),
            "start": b["start"],
            "dur": b["dur"],
            "image": path.suffix.lower() == ".png",
        })
    found.sort(key=lambda o: o["start"])
    return found, missing


# --- fast preview compositing: proxies + overlay lanes -----------------------
#
# The first real project's preview render took 25+ minutes and stalled twice.
# Two costs multiplied:
#   (a) the cut sliced a 4K master into 234 segments, so every render did 234
#       seek+decodes into 4K video to build a 720p preview;
#   (b) all 56 overlays went into ONE filtergraph as a 56-deep overlay stack,
#       so every output frame walked 56 compositing steps whether or not any
#       overlay was actually on screen.
#
# Both have a cheap fix and neither needs a different tool:
#   (a) PROXY MASTERS. Transcode each source once, linearly, to the preview
#       height, and cut the preview from the proxy. A linear read plus 234
#       cheap seeks beats 234 expensive ones, the proxy is reused by every
#       later re-render, and the final render still cuts from the true master.
#   (b) OVERLAY LANES. Overlays are intervals in time and mostly do not
#       overlap. Pack them into the fewest non-overlapping lanes (greedy
#       interval scheduling), build each lane as a cheap time-sequential
#       CONCAT of transparent gaps and overlay clips, then stack only the
#       lanes. Stack depth becomes max-concurrent-overlays instead of
#       total-overlays: 56 became 2 on the real project.
#
# Measured together on that project: about 3 minutes instead of about an hour.

LANE_CODEC_ARGS = {
    # Lossless with alpha, and it run-length encodes flat transparency, so a
    # mostly-empty overlay lane costs almost nothing on disk. The default.
    "qtrle": ["-c:v", "qtrle", "-pix_fmt", "argb"],
    # The validated original. Much larger files (a 16 min 720p lane runs to
    # gigabytes), kept for anyone who needs ProRes intermediates.
    "prores": ["-c:v", "prores_ks", "-profile:v", "4444",
               "-pix_fmt", "yuva444p10le"],
}
DEFAULT_LANE_CODEC = "qtrle"


def plan_overlay_lanes(overlays):
    """Pack overlays into the fewest non-overlapping time lanes (pure).

    Greedy interval scheduling over overlays sorted by start: each overlay
    goes into the first lane whose last overlay has already ended, otherwise
    it opens a new lane. The lane count is exactly the maximum number of
    overlays on screen at once, which is the whole point: it becomes the
    depth of the final overlay stack.

    Returns a list of lanes, each a list of overlay dicts sorted by start.
    """
    lanes = []
    for ov in sorted(overlays, key=lambda o: (o["start"], o["dur"])):
        start = ov["start"]
        for lane in lanes:
            last = lane[-1]
            if last["start"] + last["dur"] <= start + 1e-6:
                lane.append(ov)
                break
        else:
            lanes.append([ov])
    return lanes


def build_lane_filter(lane, total, size, fps):
    """(inputs, filter_complex) for one overlay lane (pure).

    The lane is a single video the length of the whole timeline: transparent
    gap, overlay, transparent gap, overlay, ..., trailing gap, concatenated.
    Gaps are lavfi color sources zeroed to full transparency (a color source
    carries no usable alpha of its own, so aa=0 is not optional). Every
    element is normalized to the same size, rate and rgba format because
    concat refuses mismatched inputs.
    """
    if not lane:
        # A lane with no overlays is a fully-transparent pass, which costs a
        # whole encode to composite nothing. The caller skips it.
        return [], ""
    w, h = size
    inputs, filters, labels = [], [], []
    n = 0
    cursor = 0.0

    def add_gap(dur):
        nonlocal n
        if dur <= 0.001:
            return
        inputs.extend(["-f", "lavfi", "-t", f"{dur:.3f}",
                       "-i", f"color=c=black:s={w}x{h}:r={fps}"])
        filters.append(f"[{n}:v]format=rgba,colorchannelmixer=aa=0,"
                       f"fps={fps},setpts=PTS-STARTPTS[l{n}]")
        labels.append(f"[l{n}]")
        n += 1

    def add_overlay(ov):
        nonlocal n
        if ov.get("image"):
            inputs.extend(["-loop", "1", "-t", f"{ov['dur']:.3f}",
                           "-i", str(ov["path"])])
        else:
            inputs.extend(["-i", str(ov["path"])])
        filters.append(
            f"[{n}:v]format=rgba,scale={w}:{h}:force_original_aspect_ratio="
            f"decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=#00000000,"
            f"fps={fps},trim=duration={ov['dur']:.3f},setpts=PTS-STARTPTS"
            f"[l{n}]")
        labels.append(f"[l{n}]")
        n += 1

    for ov in lane:
        add_gap(ov["start"] - cursor)
        add_overlay(ov)
        cursor = ov["start"] + ov["dur"]
    add_gap(total - cursor)

    if not labels:
        return [], ""
    graph = ";".join(filters) + ";" + "".join(labels) + \
        f"concat=n={len(labels)}:v=1:a=0[laneout]"
    return inputs, graph


def build_lane_command(lane, total, size, fps, out_path,
                       codec=DEFAULT_LANE_CODEC):
    """ffmpeg argv rendering one overlay lane to an alpha-bearing file (pure)."""
    inputs, graph = build_lane_filter(lane, total, size, fps)
    if not graph:
        return []
    return (["ffmpeg", "-y", "-hide_banner", "-v", "error"] + inputs +
            ["-filter_complex", graph, "-map", "[laneout]",
             "-t", f"{total:.3f}"] +
            LANE_CODEC_ARGS.get(codec, LANE_CODEC_ARGS[DEFAULT_LANE_CODEC]) +
            [str(out_path)])


def build_lane_composite_command(base, lane_files, output, crf=28,
                                 preset="veryfast",
                                 extra_output_flags=()):
    """ffmpeg argv stacking the (few) lane files onto the base cut (pure).

    This is the pass whose depth used to be the overlay count. It is now the
    lane count, and the base carries the audio through untouched.
    """
    argv = ["ffmpeg", "-y", "-hide_banner", "-v", "error", "-i", str(base)]
    for lf in lane_files:
        argv += ["-i", str(lf)]
    graph, prev = "", "0:v"
    for i in range(len(lane_files)):
        tag = f"c{i + 1}"
        graph += f"[{prev}][{i + 1}:v]overlay=format=auto:shortest=0[{tag}];"
        prev = tag
    graph += f"[{prev}]format=yuv420p[vout]"
    argv += ["-filter_complex", graph, "-map", "[vout]", "-map", "0:a?",
             "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
             "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]
    argv += list(extra_output_flags)
    argv += [str(output)]
    return argv


def proxy_path(proxy_dir, source, height, intra=True):
    """Where a source's preview proxy lives (pure).

    Named by the source stem, height and encode family so a project's proxies
    are readable on disk AND an all-intra proxy can never collide with a
    long-GOP one built by an older version. Freshness is still decided by the
    sidecar, not the name.
    """
    suffix = "-intra" if intra else ""
    return Path(proxy_dir) / f"{Path(source).stem}-{height}p{suffix}.mp4"


def proxy_is_fresh(proxy, source, recipe=None):
    """True when this proxy was built from this source UNDER THIS RECIPE.

    The sidecar records the source's content digest at build time, so a
    re-recorded take with the same filename correctly invalidates its proxy.

    It also records the proxy RECIPE, because content is only half of
    freshness: a proxy built by an older version is long-GOP, and the stream
    copy lane is silently WRONG against it (right duration, wrong frames). A
    sidecar with no recipe line predates the intra lane and is therefore
    stale by definition.
    """
    recipe = PROXY_RECIPE if recipe is None else recipe
    proxy = Path(proxy)
    sidecar = proxy.with_name(proxy.name + ".src")
    if not proxy.is_file() or not sidecar.is_file():
        return False
    try:
        lines = sidecar.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    if not lines:
        return False
    if lines[0].strip() != content_digest(source, cheap=True):
        return False
    stored = lines[1].strip() if len(lines) > 1 else ""
    return stored == recipe


# The proxy recipe. Bumping this string invalidates every existing proxy via
# the sidecar, which is what you want whenever the encode settings change:
# a proxy built under an older recipe is silently wrong for the current one.
PROXY_RECIPE = "intra-v1"


def build_proxy_command(source, out, height, crf=26, preset="veryfast",
                        intra=True):
    """ffmpeg argv transcoding a source to a preview proxy (pure).

    One linear pass. Audio is re-encoded rather than copied so the proxy is
    seekable and self-contained; the preview's audio comes from here too, and
    the final render never touches proxies.

    ALL-INTRA (intra=True, the default) is what makes the fast preview lane
    possible, and it is not an optimisation detail -- it is a correctness
    precondition:

    - Every frame is a keyframe, so the concat demuxer's inpoint/outpoint are
      frame-exact. Against a long-GOP proxy they are NOT: measured at an
      8.333s GOP against segments averaging 2.51s, every segment began up to
      8.3s early while total duration still matched the EDL exactly, so the
      wrong cut looked right in every check except a frame comparison.
    - Because every cut lands on a keyframe by definition, the preview can be
      a STREAM COPY instead of a re-encode. Measured on a 379-segment 16-min
      cut: 22 minutes of filter_complex re-encode became 2.8 seconds of
      remux, at 340x realtime.

    The cost is disk: all-intra 720p runs roughly 10x a long-GOP proxy (698MB
    vs 65MB for 20 minutes). That is the trade this lane makes deliberately --
    the proxy is built once per source and reused by every later preview,
    while the re-encode was paid on every single iteration.
    """
    cmd = ["ffmpeg", "-y", "-hide_banner", "-v", "error", "-i", str(source),
           "-vf", f"scale=-2:{height}", "-c:v", "libx264", "-preset", preset,
           "-crf", str(crf), "-pix_fmt", "yuv420p"]
    if intra:
        # -g 1 alone is not enough: x264 can still emit non-IDR frames, and
        # scenecut detection inserts its own keyframes on top. All three
        # together give exactly one I-frame per frame.
        cmd += ["-g", "1", "-keyint_min", "1", "-sc_threshold", "0"]
    cmd += ["-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            str(out)]
    return cmd


def build_streamcopy_command(concat_file, out):
    """ffmpeg argv remuxing an ffconcat virtual timeline into a file (pure).

    No filtergraph and no encoder: the segments are cut on keyframes so their
    packets are copied straight through. Valid ONLY against an all-intra
    source (see build_proxy_command) and ONLY when nothing needs compositing
    -- an overlay changes pixels, which forces a re-encode.

    The EDL's fade_ms is deliberately not applied here. Audio fades exist to
    stop a click when a cut lands mid-waveform, and verify_edl already
    guarantees every boundary rests in audio-verified silence, so there is no
    discontinuity for a fade to hide. Measured across 8 random joins on a
    379-segment cut: unfaded peaks ran -31 to -44 dB against speech peaking
    at -9 dB, and tracked the faded render within 0.5 dB on most joins. If a
    future format ever cuts outside silence, this assumption dies with it.

    NOT WIRED INTO render_preview, and the reason is worth keeping:
        This is ~470x faster than the encode (2.8s vs 22min measured on a
        379-segment 16-minute cut, 340x realtime) and it is genuinely the
        same frames. But the concat demuxer emits DUPLICATE DTS wherever a
        segment runs only a frame or two, and the resulting file fails
        render_preview's decode validation. On that same cut: 125 decode
        errors plain, and 3 with every timestamp remedy tried (+genpts,
        avoid_negative_ts make_zero, video_track_timescale 30000, fps_mode
        passthrough, muxdelay 0). Never zero. Only 2 of 379 segments were
        under 0.1s, which is all it takes.

        Publishing that file would mean loosening a correctness gate to buy a
        speed number, so the speed moved instead of the gate: gate 2 review
        runs on the virtual timeline (edl_to_ffconcat.py), which is never
        muxed and therefore cannot have this problem, and which verifies
        frame-exact. Kept here, tested, and available for a caller that wants
        a scratch file and accepts the caveat.
    """
    return ["ffmpeg", "-y", "-hide_banner", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-c", "copy", "-movflags", "+faststart", str(out)]


def write_proxy_sidecar(proxy, source, recipe=None):
    """Record which source and which recipe built this proxy, for
    proxy_is_fresh. Line 1 is the source digest, line 2 the recipe."""
    recipe = PROXY_RECIPE if recipe is None else recipe
    proxy = Path(proxy)
    proxy.with_name(proxy.name + ".src").write_text(
        content_digest(source, cheap=True) + "\n" + recipe + "\n",
        encoding="utf-8")


def proxied_edl(edl, mapping):
    """A copy of the EDL with every source swapped for its proxy (pure).

    Timecodes are untouched: a proxy is the same footage at a smaller frame
    size, so every EDL time stays valid against it. Sources with no proxy in
    the mapping are left pointing at the original.
    """
    out = json.loads(json.dumps(edl))
    for seg in out.get("segments", []):
        seg["source"] = mapping.get(seg["source"], seg["source"])
    if out.get("source") in mapping:
        out["source"] = mapping[out["source"]]
    return out


# --- ffmpeg filtergraph and command -----------------------------------------


def build_filter_complex(edl, source_index, height, overlays=(), overlay_size=None,
                         hwupload=False, target=None, audio_map=None,
                         silence_index=None, streams="av"):
    """Build the filter_complex string for the whole timeline.

    streams selects which output streams the graph emits: "av" (default, the
    historic behavior, [outv] and [outa]), "video" ([outv] only, the audio
    chains and the audio concat omitted), or "audio" ([outa] only, the video
    chains and every overlay omitted). The video-only and audio-only forms
    back the incremental segment render, which persists video-only segments
    and rebuilds the audio whole every render; the "av" form is byte-for-byte
    unchanged so the preview and any av caller stay identical.

    source_index maps each source path to its ffmpeg -i input index. Each
    segment is trimmed from its source, PTS-reset, sized, given an in/out
    afade of fade_ms at its boundaries, and audio-normalized; all segments
    then concat.

    Sizing: when target=(W,H) is given every segment is normalized to that one
    frame (scale to fit with force_original_aspect_ratio=decrease, then pad and
    centre to WxH, setsar=1), so sources of different frame sizes or aspect
    ratios all become identical WxH inputs and the concat filter accepts them
    (mixed cam + screencast). target is set by the caller only when the
    timeline draws on more than one distinct source; the single-source fast
    path keeps the plain scale=-2:height (even width, square pixels; height
    None keeps native size), unchanged.

    Audio: a source with no audio stream (audio_map[source] is False and a
    silence_index is supplied) draws silence from the shared anullsrc input at
    silence_index instead of a real [idx:a], so screen recordings without audio
    do not fail with "Stream specifier :a matches no streams". Every audio
    chain ends in aresample=48000,aformat=channel_layouts=stereo so sources
    with different sample rates or channel layouts (44.1k cam + 48k screencast)
    concat cleanly. audio_map None means every source has audio (the historic
    behavior).

    overlays (optional) are dicts {index, start, dur, image} whose 'index' is
    the ffmpeg input index of the overlay file; each is composited over the
    concat output in start order (format=rgba, scaled to overlay_size when
    given, PTS shifted to its timeline start, overlay with eof_action=pass and
    an enable window). With overlays the chain ends in format=yuv420p so the
    output stays player-safe. Final labels are always [outv]/[outa].

    hwupload=True ends the video chain in format=nv12,hwupload instead, for
    encoders that only take hardware frames (vaapi); the caller must also set
    up the device (encoder_init_flags).
    """
    want_v = streams in ("av", "video")
    want_a = streams in ("av", "audio")
    if not want_v:
        overlays = ()  # overlays are a video-only concern
    fade = edl.get("fade_ms", 30) / 1000.0
    tw, th = target if target else (None, None)
    parts, vlabels, alabels = [], [], []
    for i, seg in enumerate(edl["segments"]):
        idx = source_index[seg["source"]]
        start, end = seg["start"], seg["end"]
        dur = end - start
        # Never let the two fades overlap on a very short segment.
        f = min(fade, dur / 2) if dur > 0 else 0.0
        vlab, alab = f"v{i}", f"a{i}"
        if want_v:
            vchain = (
                f"[{idx}:v]trim=start={_fmt(start)}:end={_fmt(end)},"
                f"setpts=PTS-STARTPTS"
            )
            if target:
                # Normalize every segment to one frame so mixed-size sources concat.
                vchain += (
                    f",scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                    f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1[{vlab}]"
                )
            elif height:
                vchain += f",scale=-2:{height},setsar=1[{vlab}]"
            else:
                vchain += f",setsar=1[{vlab}]"
            parts.append(vchain)
            vlabels.append(f"[{vlab}]")
        if want_a:
            # Audio: real stream, or shared silence for an audio-less source.
            if (silence_index is not None and audio_map is not None
                    and not audio_map.get(seg["source"], True)):
                aidx, a_start, a_end = silence_index, 0.0, dur
            else:
                aidx, a_start, a_end = idx, start, end
            achain = (
                f"[{aidx}:a]atrim=start={_fmt(a_start)}:end={_fmt(a_end)},"
                f"asetpts=PTS-STARTPTS"
            )
            if f > 0:
                achain += (
                    f",afade=t=in:st=0:d={_fmt(f)}"
                    f",afade=t=out:st={_fmt(dur - f)}:d={_fmt(f)}"
                )
            achain += ",aresample=48000,aformat=channel_layouts=stereo"
            achain += f"[{alab}]"
            parts.append(achain)
            alabels.append(f"[{alab}]")
    n = len(edl["segments"])
    if not want_v:
        # Audio-only: concat the audio chains straight to [outa].
        parts.append(f"{''.join(alabels)}concat=n={n}:v=0:a=1[outa]")
        return ";".join(parts)
    # A stage after the video concat is needed when overlays composite over it
    # or a hardware encoder demands hardware frames.
    need_post = bool(overlays) or hwupload
    if want_a:
        concat_inputs = "".join(v + a for v, a in zip(vlabels, alabels))
        head = f"{concat_inputs}concat=n={n}:v=1:a=1"
        parts.append(f"{head}[basev][outa]" if need_post else f"{head}[outv][outa]")
    else:
        concat_inputs = "".join(vlabels)
        head = f"{concat_inputs}concat=n={n}:v=1:a=0"
        parts.append(f"{head}[basev]" if need_post else f"{head}[outv]")
    if not need_post:
        return ";".join(parts)
    prev = "basev"
    for k, ov in enumerate(overlays):
        lab = f"ov{k}"
        chain = f"[{ov['index']}:v]format=rgba"
        if overlay_size:
            chain += f",scale={overlay_size[0]}:{overlay_size[1]}"
        chain += f",setpts=PTS-STARTPTS+{_fmt(ov['start'])}/TB[{lab}]"
        parts.append(chain)
        out_lab = f"base{k + 1}"
        end_t = ov["start"] + ov["dur"]
        parts.append(
            f"[{prev}][{lab}]overlay=eof_action=pass:"
            f"enable='between(t,{_fmt(ov['start'])},{_fmt(end_t)})'[{out_lab}]"
        )
        prev = out_lab
    if hwupload:
        parts.append(f"[{prev}]format=nv12,hwupload[outv]")
    else:
        parts.append(f"[{prev}]format=yuv420p[outv]")
    return ";".join(parts)


PREVIEW_ENCODE = ["-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
                  "-c:a", "aac"]


def build_command(edl, project_dir, output, height, overlays=(),
                  overlay_size=None, encode=None, extra_output_flags=(),
                  encoder=None, target=None, audio_map=None, streams="av"):
    """Assemble (ffmpeg_argv, source_index) for one render invocation.

    streams selects the output streams (passed through to build_filter_complex
    and the -map flags): "av" (default, unchanged), "video" (video-only, no
    audio silence input, maps [outv] only), or "audio" (audio-only, no overlay
    inputs, maps [outa] only). The video/audio split backs the incremental
    segment render (video segments persisted, audio rebuilt whole).

    encode replaces the default preview encode args (libx264 crf 28 veryfast
    + aac). Every looped image overlay input carries an explicit -t duration
    cap (looped/synthetic sources must never run open-ended); video overlay
    inputs are -t capped to the beat's dur too, so decode stops at the enable
    window. -movflags +faststart is added for .mp4/.mov outputs.

    target (optional) is the (W,H) frame every segment is normalized to, for
    mixed-size sources; the caller sets it only for multi-source timelines and
    passes the SAME value to every chunk so the chunk concat stays exact.

    audio_map (optional) maps each source path to whether it has an audio
    stream (probe_has_audio). Any audio-less source is fed synthesized silence
    from a single trimmed anullsrc input added after the real sources; the -t
    cap on that input keeps the synthetic source from running open-ended.
    audio_map None means every source has audio.

    encoder (optional) is the encoder name the encode args target; it only
    matters for encoders that need device setup and hardware frames (vaapi
    gets -init_hw_device flags and an hwupload filtergraph tail). Software
    and videotoolbox/nvenc/qsv/amf encoders need nothing here.
    """
    want_v = streams in ("av", "video")
    want_a = streams in ("av", "audio")
    if not want_v:
        overlays = ()  # overlays are a video-only concern
    distinct = []
    for seg in edl["segments"]:
        if seg["source"] not in distinct:
            distinct.append(seg["source"])
    source_index = {src: i for i, src in enumerate(distinct)}
    need_silence = want_a and bool(audio_map) and any(
        not audio_map.get(src, True) for src in distinct)
    hwupload = want_v and encoder_needs_hwupload(encoder)
    argv = ["ffmpeg", "-y", *(encoder_init_flags(encoder) if want_v else [])]
    for src in distinct:
        argv += ["-i", str((project_dir / src).resolve())]
    silence_index = None
    if need_silence:
        silence_index = len(distinct)
        total = sum(seg["end"] - seg["start"] for seg in edl["segments"])
        argv += ["-f", "lavfi", "-t", _fmt(total),
                 "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    base = len(distinct) + (1 if need_silence else 0)
    ovs = []
    for k, ov in enumerate(overlays):
        entry = dict(ov)
        entry["index"] = base + k
        if entry.get("image"):
            argv += ["-loop", "1", "-t", _fmt(entry["dur"]),
                     "-i", str(entry["path"])]
        else:
            argv += ["-t", _fmt(entry["dur"]), "-i", str(entry["path"])]
        ovs.append(entry)
    argv += [
        "-filter_complex",
        build_filter_complex(edl, source_index, height, ovs, overlay_size,
                             hwupload=hwupload, target=target,
                             audio_map=audio_map, silence_index=silence_index,
                             streams=streams),
    ]
    if want_v:
        argv += ["-map", "[outv]"]
    if want_a:
        argv += ["-map", "[outa]"]
    argv += list(encode) if encode else list(PREVIEW_ENCODE)
    if str(output).endswith((".mp4", ".mov")):
        argv += ["-movflags", "+faststart"]
    argv += list(extra_output_flags)
    argv.append(str(output))
    return argv, source_index


# --- segment planning (persistent incremental render) ------------------------


def plan_segments(edl, overlays=(), target_seconds=600.0):
    """Partition the EDL into persistent render-segments with STABLE, sticky
    boundaries for the incremental render cache.

    Greedy, left to right: accumulate EDL segments until the running duration
    since the last cut reaches target_seconds, then close the render-segment
    at the next SAFE boundary, a hard cut between two EDL segments that no
    overlay window spans (the boundary-safety rule the parallel render already
    relied on). A boundary chosen this way depends only on the content BEFORE
    it, so an edit later in the timeline cannot move an earlier boundary and
    its persisted segment survives; because a render-segment's identity is the
    content it contains (not its timeline offset), even the segments after an
    edit keep their identity as long as the same EDL segments still group
    together. The final render-segment always runs to the end.

    target_seconds is a floor, not an exact size: an unsafe boundary (an
    overlay straddles it) is skipped and the segment grows until the next safe
    cut. Returns the same dict shape plan_chunks returned {seg_start, seg_end,
    offset, duration, overlays}, overlays carrying chunk-local start times, so
    the renderer treats a segment exactly like the old parallel chunk.
    """
    durs = segment_durations(edl)
    n = len(durs)
    if n == 0:
        return []
    bounds = boundary_times(edl)  # times of the n-1 internal boundaries

    def spans_boundary(t):
        return any(ov["start"] < t < ov["start"] + ov["dur"] for ov in overlays)

    # Decide at each internal boundary i (between EDL seg i and i+1) whether to
    # cut: the first SAFE boundary once the run since the last cut reaches the
    # target. An unsafe boundary does not reset the accumulator, so the segment
    # keeps growing to the next safe cut.
    cut_after = [False] * max(0, n - 1)
    running = 0.0
    for i in range(n - 1):
        running += durs[i]
        if running >= target_seconds and not spans_boundary(bounds[i]):
            cut_after[i] = True
            running = 0.0
    segments = []
    seg_start = 0
    offset = 0.0
    for i in range(n):
        if i == n - 1 or cut_after[i]:
            seg_end = i + 1
            dur = sum(durs[seg_start:seg_end])
            segments.append({
                "seg_start": seg_start,
                "seg_end": seg_end,
                "offset": round(offset, 6),
                "duration": round(dur, 6),
                "overlays": [],
            })
            seg_start = seg_end
            offset += dur
    for ov in overlays:
        for ch in segments:
            last = ch is segments[-1]
            if ch["offset"] <= ov["start"] < ch["offset"] + ch["duration"] or last:
                local = dict(ov)
                local["start"] = round(ov["start"] - ch["offset"], 6)
                ch["overlays"].append(local)
                break
    return segments


# --- content addressing (the incremental render cache identity) --------------


def content_digest(path, cheap=False):
    """A content fingerprint for a file, or 'missing' when it is absent.

    cheap=True returns size+mtime_ns, for large source media that is expensive
    to hash and rarely changes silently. cheap=False (the default) returns a
    truncated sha256 of the bytes, for small overlay and asset files whose
    regeneration MUST dirty the segment that consumes them even when the path
    is unchanged (a re-rendered graphic keeps its name)."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return "missing"
    if cheap:
        return f"size:{st.st_size}:mtime:{st.st_mtime_ns}"
    h = hashlib.sha256()
    try:
        with open(p, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
    except OSError:
        return "missing"
    return "sha256:" + h.hexdigest()[:32]


def ffmpeg_version():
    """The ffmpeg build version token (part of the render identity: a bump can
    change encoded output, so it must dirty every cached segment). 'unknown'
    when ffmpeg cannot be run or the banner is unparseable."""
    try:
        proc = subprocess.run(["ffmpeg", "-version"],
                              capture_output=True, text=True)
    except OSError:
        return "unknown"
    if proc.returncode != 0 or not proc.stdout:
        return "unknown"
    parts = proc.stdout.splitlines()[0].split()
    # "ffmpeg version 8.1.2 Copyright ..."
    return parts[2] if len(parts) >= 3 and parts[0] == "ffmpeg" else "unknown"


# --- render identity, atomic publish, output validation ---------------------
#
# On 2026-07-24 a stale background render (an EDL that had since been
# superseded) finished late and wrote renders/preview.mp4 CONCURRENTLY with
# the current render. Two processes interleaved bytes into one path and the
# creator was handed an unplayable file: "Invalid NAL unit size", "missing
# picture in access unit". Nothing detected it, because the only post-render
# check was the container duration, which a corrupt file still reports
# happily.
#
# Three defences, all needed:
#   1. Never write the deliverable path directly. Render to a unique temp
#      path and os.replace it into place, which is atomic on one filesystem,
#      so a reader sees either the old file or the new one and never a
#      half-written one.
#   2. Key a render by its CONTENT, and re-check that key just before
#      publishing. A render whose EDL changed underneath it is stale and must
#      discard its output instead of clobbering a newer one. Content keying
#      beats process cancellation because it also catches a crashed, detached,
#      or forgotten job, which is exactly what happened.
#   3. Prove the file decodes before calling it a deliverable.


def render_key(edl, params=None, overlay_digests=None, ffmpeg=None):
    """Content identity of a render: what is being rendered, never when.

    Two renders agree on a key exactly when they would produce the same
    bytes: same EDL, same parameters, same overlay file contents, same
    ffmpeg. The key is what makes supersede detection work without locks.
    """
    payload = {
        "edl": edl,
        "params": dict(sorted((params or {}).items())),
        "overlays": dict(sorted((overlay_digests or {}).items())),
        "ffmpeg": ffmpeg if ffmpeg is not None else ffmpeg_version(),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                     default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def temp_render_path(output, key):
    """A per-process temp path beside the output.

    The pid is load-bearing: keying the temp file on content alone would give
    two concurrent renders of the SAME edl one shared temp path, recreating
    the interleaved-write corruption one level down.
    """
    output = Path(output)
    return output.with_name(f".{output.stem}.{key[:12]}.{os.getpid()}.part"
                            f"{output.suffix}")


def validate_render(path, expected_duration=None, tolerance=0.5):
    """Decode the whole file and check its duration. Returns (ok, problems).

    The decode pass is the check that was missing: `ffmpeg -v error -f null -`
    walks every packet and prints on any decode error. Container duration
    alone cannot see a corrupt bitstream.
    """
    problems = []
    path = Path(path)
    if not path.is_file():
        return False, [f"output not written: {path}"]
    if path.stat().st_size == 0:
        return False, [f"output is empty: {path}"]
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True, text=True)
    except OSError as e:
        return False, [f"cannot run ffmpeg to validate: {e}"]
    stderr = proc.stderr.strip()
    if proc.returncode != 0 or stderr:
        problems.append("decode errors: " + (stderr[-800:] or
                                             f"exit {proc.returncode}"))
    actual = probe_duration(path)
    if actual is None:
        problems.append("no readable duration")
    elif expected_duration is not None and \
            abs(actual - expected_duration) > tolerance:
        problems.append(f"duration {actual:.3f}s vs expected "
                        f"{expected_duration:.3f}s (tolerance {tolerance}s)")
    return not problems, problems


def current_render_key(edl_path, params=None, overlay_digests=None,
                       ffmpeg=None):
    """Re-read the EDL from disk and compute its key now, or None.

    None means the EDL is unreadable, in which case the caller must NOT treat
    the render as superseded: an unreadable EDL is a separate problem and
    discarding a good render over it would lose work.
    """
    try:
        edl = json.loads(Path(edl_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return render_key(edl, params, overlay_digests, ffmpeg)


def publish_render(tmp_path, output, key=None):
    """Atomically move a validated temp render into place.

    Also writes a <output>.key sidecar naming the render identity now in the
    file. Derived artifacts (fcpxml, boundary frames, cutplan summary) can
    record the same key, so drift after a re-cut is detectable rather than
    silent.
    """
    tmp_path, output = Path(tmp_path), Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp_path, output)
    if key:
        output.with_name(output.name + ".key").write_text(key + "\n",
                                                          encoding="utf-8")
    return output


def write_json_atomic(path, payload):
    """Write JSON via a temp file and an atomic replace.

    Any file a running render might read concurrently must be written this
    way. A plain write truncates first, so a reader arriving mid-write sees a
    torn or empty file: cut/edl.json in particular is read by every render,
    including ones already in flight. Torn reads here are not corruption of
    the deliverable (current_render_key treats an unreadable EDL as "cannot
    tell" and declines to supersede), but they do abort renders for no
    reason.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


def discard_render(tmp_path):
    """Remove an abandoned temp render, ignoring an already-gone file."""
    try:
        Path(tmp_path).unlink()
    except OSError:
        pass


def segment_identity(edl, seg):
    """Position-independent content identity of a render-segment: its EDL
    slice as ordered (source, source-relative start, end) plus fade_ms. Never
    includes the output-timeline offset, so an upstream edit that only shifts a
    segment later in time leaves its identity unchanged."""
    slice_ = edl["segments"][seg["seg_start"]:seg["seg_end"]]
    return {
        "fade_ms": edl.get("fade_ms", 30),
        "segments": [{"source": s["source"],
                      "start": round(s["start"], 6),
                      "end": round(s["end"], 6)} for s in slice_],
    }


def overlay_placement(seg):
    """The position-independent overlay layout of a render-segment: for each
    overlay landing in it, its id, chunk-local start, dur, and image flag,
    sorted. The overlay FILE digest is deliberately excluded (that lives in
    segment_input_hash): re-rendering a graphic must dirty the segment's cache
    entry but must NOT change its filename, so the persisted .ts is reused."""
    return sorted(
        ([ov.get("id"), round(ov["start"], 6), round(ov["dur"], 6),
          bool(ov.get("image"))] for ov in seg.get("overlays", [])),
        key=lambda o: (o[1], str(o[0])),
    )


def segment_id(edl, seg):
    """Stable filesystem-safe id for a render-segment, derived from its content
    identity and its overlay layout (never its timeline position), so an
    identical slice carrying identical overlays keeps the same id and persisted
    file across runs. Two content-identical slices are distinct ids when
    DIFFERENT overlays land on them, so each overlay configuration owns its own
    persisted .ts and neither is deduped onto the other. Duplicate slices with
    the same overlay layout (the same span kept twice, same graphics) legitimately
    share one id and one file."""
    key = {"identity": segment_identity(edl, seg),
           "overlays": overlay_placement(seg)}
    raw = json.dumps(key, sort_keys=True).encode("utf-8")
    return "seg-" + hashlib.sha256(raw).hexdigest()[:16]


def segment_input_hash(edl, seg, render_key, source_digests, overlay_digests):
    """sha256 over the content-bearing, position-independent inputs of one
    render-segment, the cache key: its slice identity, the digest of every
    source it draws on, each overlay landing inside it (id, chunk-local start,
    dur, image flag, and the overlay FILE's digest, so a re-rendered graphic
    dirties the segment), and the shared render_key (resolved encoder, ffmpeg
    version, output dims, video encode args). Absolute/edited-timeline offsets
    are never hashed, so an edit upstream cannot dirty a segment whose own
    content is unchanged.

    source_digests maps source path -> digest; overlay_digests maps overlay id
    -> digest. render_key is any JSON-serializable dict of shared render state.
    """
    seen, slice_sources = set(), []
    for s in edl["segments"][seg["seg_start"]:seg["seg_end"]]:
        if s["source"] not in seen:
            seen.add(s["source"])
            slice_sources.append([s["source"],
                                  source_digests.get(s["source"], "missing")])
    ovs = sorted(
        ({"id": ov.get("id"),
          "start": round(ov["start"], 6),
          "dur": round(ov["dur"], 6),
          "image": bool(ov.get("image")),
          "digest": overlay_digests.get(ov.get("id"), "missing")}
         for ov in seg["overlays"]),
        key=lambda o: (o["start"], str(o["id"])))
    payload = {
        "identity": segment_identity(edl, seg),
        "sources": sorted(slice_sources),
        "overlays": ovs,
        "render_key": render_key,
    }
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def load_manifest(path):
    """The prior segment manifest dict, or None when absent or unreadable."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_manifest(path, manifest):
    """Write the segment manifest as pretty JSON."""
    Path(path).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


# --- encoder selection and disk preflight ------------------------------------


def list_encoders():
    """Names of the encoders this ffmpeg build offers (empty set on failure)."""
    try:
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                              capture_output=True, text=True)
    except OSError:
        return set()
    if proc.returncode != 0:
        return set()
    names = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and set(parts[0]) <= set("VASFXBD."):
            names.add(parts[1])
    return names


# Hardware-encode ladders, probed in order on auto selection. Darwin is not
# in the table: videotoolbox is picked on listing alone (the long-validated
# reference behavior), no test encode.
HW_LADDERS = {
    "Windows": ("h264_nvenc", "h264_qsv", "h264_amf"),
    "Linux": ("h264_nvenc", "h264_vaapi"),
}

# Encoders whose rate control is a bitrate from the ladder (no dependable
# CRF mode across drivers).
HW_SUFFIXES = ("_videotoolbox", "_nvenc", "_qsv", "_amf", "_vaapi")


def is_hardware_encoder(encoder):
    """True for encoders that take the bitrate ladder instead of -crf."""
    return bool(encoder) and encoder.endswith(HW_SUFFIXES)


def encoder_needs_hwupload(encoder):
    """True for encoders that only accept hardware frames, so the video
    chain must end in format=nv12,hwupload (vaapi)."""
    return bool(encoder) and encoder.endswith("_vaapi")


def encoder_init_flags(encoder):
    """Global ffmpeg flags an encoder needs before any input (vaapi device
    init and the filter device binding); empty for everything else."""
    if encoder_needs_hwupload(encoder):
        return ["-init_hw_device", "vaapi=va", "-filter_hw_device", "va"]
    return []


def encoder_probe_command(encoder):
    """ffmpeg argv for a one-frame test encode: lavfi color source to the
    null muxer. Listing an encoder proves the build has it; only a real
    encode proves the driver/hardware behind it works."""
    argv = ["ffmpeg", "-hide_banner", "-v", "error",
            *encoder_init_flags(encoder),
            "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=30"]
    if encoder_needs_hwupload(encoder):
        argv += ["-vf", "format=nv12,hwupload"]
    argv += ["-frames:v", "1", "-c:v", encoder, "-f", "null", "-"]
    return argv


_probe_cache = {}


def probe_encoder(encoder, cache=None):
    """One-frame test encode of `encoder`, cached per process so each
    encoder is probed at most once per run. cache=None uses the module
    cache; tests pass their own dict."""
    cache = _probe_cache if cache is None else cache
    if encoder in cache:
        return cache[encoder]
    try:
        proc = subprocess.run(encoder_probe_command(encoder),
                              capture_output=True, text=True, timeout=30)
        ok = proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    cache[encoder] = ok
    return ok


def pick_encoder(requested="auto", available=None, system=None, probe=None):
    """Resolve the encoder for this run.

    Explicit request: returned when the local ffmpeg lists it, libx264
    otherwise (unchanged). Auto on Darwin: h264_videotoolbox when listed,
    libx264 otherwise (unchanged, never probed). Auto elsewhere: the first
    HW_LADDERS entry for the OS that is both listed by ffmpeg AND passes a
    one-frame test encode (probe_encoder, cached per run); libx264 when the
    whole ladder fails. probe is injectable for tests."""
    system = system or platform.system()
    if available is None:
        available = list_encoders()
    if requested and requested != "auto":
        return requested if requested in available else "libx264"
    if system == "Darwin":
        if "h264_videotoolbox" in available:
            return "h264_videotoolbox"
        return "libx264"
    if probe is None:
        probe = probe_encoder
    for enc in HW_LADDERS.get(system, ()):
        if enc in available and probe(enc):
            return enc
    return "libx264"


def bitrate_for(height):
    """Delivery video bitrate ladder (kbps) by output height."""
    if height >= 2160:
        return 40000
    if height >= 1440:
        return 24000
    if height >= 1080:
        return 12000
    if height >= 720:
        return 8000
    return 5000


AUDIO_ENCODE = ["-c:a", "aac", "-b:a", "192k"]


def encode_args(encoder, crf=18, height=1080, streams="av"):
    """Encode argv fragment for the final render. Hardware encoders take a
    bitrate from the ladder (no dependable CRF mode across drivers); libx264
    takes -crf. -pix_fmt is not forced for nvenc/qsv/amf (each negotiates
    its own supported format from the yuv420p filtergraph output) nor for
    vaapi (it receives hardware frames via the hwupload chain).

    streams selects the fragment: "av" (default, video + aac audio, unchanged),
    "video" (video only, for the persisted video segments), or "audio" (aac
    only, for the whole-timeline audio pass)."""
    if is_hardware_encoder(encoder):
        v = ["-c:v", encoder, "-b:v", f"{bitrate_for(height)}k"]
        if encoder.endswith("_videotoolbox"):
            v += ["-allow_sw", "1", "-pix_fmt", "yuv420p"]
        if encoder == "hevc_videotoolbox":
            v += ["-tag:v", "hvc1"]
    else:
        v = ["-c:v", encoder, "-crf", str(crf), "-preset", "medium",
             "-pix_fmt", "yuv420p"]
    if streams == "video":
        return v
    if streams == "audio":
        return list(AUDIO_ENCODE)
    return v + list(AUDIO_ENCODE)


def estimate_output_bytes(duration_s, height, encoder="libx264"):
    """Rough output size estimate from the bitrate ladder plus audio."""
    kbps = bitrate_for(height) + 192
    return int(duration_s * kbps * 1000 / 8)


def check_disk(dir_path, needed_bytes, factor=2.0):
    """(ok, free_bytes) for writing ~needed_bytes (with headroom) under dir_path."""
    free = shutil.disk_usage(str(dir_path)).free
    return free >= int(needed_bytes * factor), free


# --- progress parsing ---------------------------------------------------------


def parse_progress(text):
    """Parse ffmpeg -progress key=value output. Returns {} or a dict with
    'seconds' (rendered output time) and/or 'state' ('continue'/'end')."""
    info = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if not sep:
            continue
        k, v = k.strip(), v.strip()
        if k in ("out_time_us", "out_time_ms"):
            # both fields are microseconds (a long-standing ffmpeg quirk)
            try:
                info["seconds"] = int(v) / 1_000_000
            except ValueError:
                pass
        elif k == "out_time" and "seconds" not in info:
            try:
                info["seconds"] = parse_timecode(v)
            except ValueError:
                pass
        elif k == "progress":
            info["state"] = v
    return info


# --- ffprobe / frame extraction (thin subprocess wrappers) --------------------


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


def probe_dims(path):
    """(width, height) of the first video stream, or None."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-print_format", "json", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    streams = json.loads(proc.stdout).get("streams") or []
    if not streams:
        return None
    w, h = streams[0].get("width"), streams[0].get("height")
    return (int(w), int(h)) if w and h else None


def probe_has_audio(path):
    """True if the file has at least one audio stream.

    On a probe failure (missing or unreadable file, or no ffprobe) returns
    True, so ffmpeg surfaces the real open error at render time rather than
    this wrapper silently synthesizing silence for a file that will fail
    anyway. A file that exists but carries no audio stream returns False,
    which is the signal for the renderers to feed it synthesized silence."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=index", "-print_format", "json",
             str(path)],
            capture_output=True, text=True,
        )
    except OSError:
        return True
    if proc.returncode != 0:
        return True
    try:
        streams = json.loads(proc.stdout).get("streams") or []
    except json.JSONDecodeError:
        return True
    return bool(streams)


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


if __name__ == "__main__":
    sys.exit("composite_core.py is a library module; it is imported by "
             "render_preview.py and render_final.py, never invoked directly.")
