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

import json
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


def resolve_overlays(beats, graphics_dir):
    """Match each beat id to a rendered overlay file in graphics_dir.

    Looks for <id>.mov / .webm / .mp4 / .mkv / .png (first hit wins, in that
    order). Returns (found, missing): found as overlay dicts {id, path, start,
    dur, image} sorted by start, missing as the beat ids with no file.
    """
    graphics_dir = Path(graphics_dir)
    found, missing = [], []
    for b in beats:
        path = None
        for ext in OVERLAY_EXTS:
            cand = graphics_dir / f"{b['id']}{ext}"
            if cand.is_file():
                path = cand
                break
        if path is None:
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


# --- ffmpeg filtergraph and command -----------------------------------------


def build_filter_complex(edl, source_index, height, overlays=(), overlay_size=None,
                         hwupload=False, target=None, audio_map=None,
                         silence_index=None):
    """Build the filter_complex string for the whole timeline.

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
        vlabels.append(f"[{vlab}]")
        alabels.append(f"[{alab}]")
    n = len(edl["segments"])
    concat_inputs = "".join(v + a for v, a in zip(vlabels, alabels))
    if not overlays:
        if hwupload:
            parts.append(f"{concat_inputs}concat=n={n}:v=1:a=1[basev][outa]")
            parts.append("[basev]format=nv12,hwupload[outv]")
        else:
            parts.append(f"{concat_inputs}concat=n={n}:v=1:a=1[outv][outa]")
        return ";".join(parts)
    parts.append(f"{concat_inputs}concat=n={n}:v=1:a=1[basev][outa]")
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
                  encoder=None, target=None, audio_map=None):
    """Assemble (ffmpeg_argv, source_index) for one render invocation.

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
    distinct = []
    for seg in edl["segments"]:
        if seg["source"] not in distinct:
            distinct.append(seg["source"])
    source_index = {src: i for i, src in enumerate(distinct)}
    need_silence = bool(audio_map) and any(
        not audio_map.get(src, True) for src in distinct)
    argv = ["ffmpeg", "-y", *encoder_init_flags(encoder)]
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
                             hwupload=encoder_needs_hwupload(encoder),
                             target=target, audio_map=audio_map,
                             silence_index=silence_index),
        "-map", "[outv]", "-map", "[outa]",
    ]
    argv += list(encode) if encode else list(PREVIEW_ENCODE)
    if str(output).endswith((".mp4", ".mov")):
        argv += ["-movflags", "+faststart"]
    argv += list(extra_output_flags)
    argv.append(str(output))
    return argv, source_index


# --- chunk planning (segment-parallel final render) --------------------------


def plan_chunks(edl, overlays=(), parallel=2):
    """Split the EDL into up to `parallel` contiguous chunks for parallel
    rendering. Split points are internal cut boundaries that fall strictly
    outside every overlay window, nearest to the equal-duration targets, so no
    overlay ever spans two chunks and the concat is sample-exact. Returns
    chunk dicts {seg_start, seg_end, offset, duration, overlays} where
    overlays carry chunk-local start times.
    """
    durs = segment_durations(edl)
    total = sum(durs)
    n = len(durs)
    parallel = max(1, min(parallel, n))
    bounds = boundary_times(edl)

    def inside_overlay(t):
        return any(ov["start"] < t < ov["start"] + ov["dur"] for ov in overlays)

    valid = [(i, t) for i, t in enumerate(bounds) if not inside_overlay(t)]
    cuts = []
    for k in range(1, parallel):
        target = total * k / parallel
        best = None
        for i, t in valid:
            if cuts and t <= cuts[-1][1]:
                continue
            if best is None or abs(t - target) < abs(best[1] - target):
                best = (i, t)
        if best is not None:
            cuts.append(best)
    chunks = []
    seg_start = 0
    offset = 0.0
    for i, _t in cuts + [(n - 1, total)]:
        seg_end = i + 1
        dur = sum(durs[seg_start:seg_end])
        chunks.append({
            "seg_start": seg_start,
            "seg_end": seg_end,
            "offset": round(offset, 6),
            "duration": round(dur, 6),
            "overlays": [],
        })
        seg_start = seg_end
        offset += dur
    for ov in overlays:
        for ch in chunks:
            last = ch is chunks[-1]
            if ch["offset"] <= ov["start"] < ch["offset"] + ch["duration"] or last:
                local = dict(ov)
                local["start"] = round(ov["start"] - ch["offset"], 6)
                ch["overlays"].append(local)
                break
    return chunks


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


def encode_args(encoder, crf=18, height=1080):
    """Encode argv fragment for the final render. Hardware encoders take a
    bitrate from the ladder (no dependable CRF mode across drivers); libx264
    takes -crf. -pix_fmt is not forced for nvenc/qsv/amf (each negotiates
    its own supported format from the yuv420p filtergraph output) nor for
    vaapi (it receives hardware frames via the hwupload chain)."""
    if is_hardware_encoder(encoder):
        v = ["-c:v", encoder, "-b:v", f"{bitrate_for(height)}k"]
        if encoder.endswith("_videotoolbox"):
            v += ["-allow_sw", "1", "-pix_fmt", "yuv420p"]
        if encoder == "hevc_videotoolbox":
            v += ["-tag:v", "hvc1"]
    else:
        v = ["-c:v", encoder, "-crf", str(crf), "-preset", "medium",
             "-pix_fmt", "yuv420p"]
    return v + ["-c:a", "aac", "-b:a", "192k"]


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
