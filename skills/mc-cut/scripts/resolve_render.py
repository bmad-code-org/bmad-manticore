#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Gate 4 final render in DaVinci Resolve Studio, validated and published atomically.

Usage:
    uv run {skill-root}/scripts/resolve_render.py --project <slug> \
        -o renders/final.mp4 [--timeline "<name>"] \
        [--render-preset "<[editor] resolve-render-preset>"] \
        [--loudness "<[editor] resolve-loudness>"] \
        [--fairlight-preset "<[editor] resolve-fairlight-preset>"] \
        [--report renders/final-render.json]

    uv run {skill-root}/scripts/resolve_render.py --restore-gains renders/final-render.json

Contract:
    input   the creator's Resolve project named --project and the timeline to
            render: --timeline by name, else the project's current timeline.
            The creator finishes in Resolve, so this renders what they have,
            not cut/edl.json.
    does    connects (resolve_api.py), opens the project with the same rules
            as resolve_import.py (never saves the open project, refuses to
            leave one that holds work), then in order:
              fairlight  --fairlight-preset applied to the timeline; "" skips
              loudness   --loudness is a mode from
                         Timeline.GetNormalizeAudioModes() (default "YouTube").
                         It sets one clip gain (AudioVolume) on every audio
                         item across all audio tracks so the program hits the
                         mode's target; re-running gives the same gains. This
                         CHANGES THE CREATOR'S TIMELINE. The gains before and
                         after are recorded in the report, and --restore-gains
                         puts the before values back. "" skips normalization.
              render     --render-preset loaded ("" means "H.264 Master",
                         stated in the output), single-clip mode, all frames,
                         into a per-process temp folder inside the output's
                         folder.
    verify  the rendered file must decode end to end with no errors, its
            video stream must hold exactly the timeline's frames, and its
            audio must cover the timeline, overrunning by no more than AAC
            packet padding. Before publishing, the timeline is fingerprinted again
            (every item's position, source in point, name and clip gain); a
            change since the render started means the creator edited during
            the render, and the file is discarded. So is a file whose output
            already holds a render published after this one started. With a
            loudness mode whose target is known (YouTube -14, EBU R128 -23,
            ATSC A/85 -24 LUFS), the render is measured with ebur128 and a
            miss of more than 1 LU fails unless --accept-loudness "<reason>"
            records the creator's sign-off. Resolve's normalization is
            peak-limited, so a dynamic take misses without a limiter in the
            creator's Fairlight preset.
    output  the deliverable at -o, moved into place atomically with a
            <output>.key sidecar. When the preset renders another container
            (ProRes renders .mov), the output takes that extension, stated in
            the summary. --report writes the render report JSON; json.dumps
            summary on stdout either way.

Exit codes: 0 rendered and published, 1 failure (nothing published), 2 usage,
4 Resolve unavailable (not running, scripting not Local, or not Studio).

STATUS: implemented (pure logic covered by
scripts/tests/test-resolve_render.py).
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import composite_core as core  # noqa: E402
import resolve_api  # noqa: E402
import resolve_import  # noqa: E402

DEFAULT_PRESET = "H.264 Master"
DEFAULT_LOUDNESS = "YouTube"
DEFAULT_RENDER_TIMEOUT = 4 * 3600
SINGLE_CLIP = 1


def resolve_preset(name):
    """(preset, notice) for a configured preset name (pure)."""
    if name and name.strip():
        return name.strip(), None
    return DEFAULT_PRESET, (f'no render preset configured; rendering with '
                            f'Resolve\'s "{DEFAULT_PRESET}"')


def expected_seconds(start_frame, end_frame, fps):
    """Timeline length in seconds; GetEndFrame is exclusive (pure)."""
    return (end_frame - start_frame) / float(fps)


def fingerprint(rows, params):
    """Identity of what is being rendered (pure).

    `rows` describe every timeline item; any edit to position, source in
    point, name or clip gain changes the key.
    """
    raw = json.dumps({"rows": rows, "params": dict(sorted(params.items()))},
                     sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def superseded_by_sidecar(key_path, started_at, key):
    """True when a different render was published after this one began (pure
    apart from one stat)."""
    key_path = Path(key_path)
    try:
        if key_path.stat().st_mtime <= started_at:
            return False
        return key_path.read_text(encoding="utf-8").strip() != key
    except OSError:
        return False


def publish_path(output, rendered):
    """The deliverable path, taking the rendered container's extension (pure)."""
    output, rendered = Path(output), Path(rendered)
    if rendered.suffix and rendered.suffix.lower() != output.suffix.lower():
        return output.with_suffix(rendered.suffix)
    return output


# AAC pads its last packet and adds encoder priming, so the audio stream runs
# up to about 80 ms past the picture on a correct render (measured on Resolve
# 21.1 H.264 Master: video 198.300s, audio 198.379s).
AUDIO_OVERRUN = 0.15


def check_streams(streams, expected_frames, fps):
    """Problems with the rendered streams (pure).

    `streams` is ffprobe's stream list. Video must hold exactly the
    timeline's frames; audio must cover the timeline and overrun it by no
    more than AAC padding.
    """
    problems = []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    expected = expected_frames / float(fps)
    if video is None:
        problems.append("no video stream")
    else:
        frames = video.get("nb_frames")
        if frames not in (None, "N/A") and int(frames) != expected_frames:
            problems.append(f"video has {frames} frames, timeline has "
                            f"{expected_frames}")
        elif frames in (None, "N/A"):
            dur = float(video.get("duration") or 0)
            if abs(dur - expected) > 1.0 / fps:
                problems.append(f"video runs {dur:.3f}s, timeline "
                                f"{expected:.3f}s")
    if audio is None:
        problems.append("no audio stream")
    else:
        dur = float(audio.get("duration") or 0)
        if not (expected - 1.0 / fps <= dur <= expected + AUDIO_OVERRUN):
            problems.append(f"audio runs {dur:.3f}s, timeline "
                            f"{expected:.3f}s")
    return problems


# Integrated targets for the normalize modes whose target is a published
# number. Resolve's normalization is peak-limited: it stops raising gain at the
# mode's peak ceiling, so a dynamic take lands well under target (measured:
# "YouTube" gave -21.6 LUFS on a take peaking at -7 dBFS). The render is
# measured, never assumed.
MODE_TARGETS = {"YouTube": -14.0, "EBU R128": -23.0, "ATSC A/85": -24.0}
LOUDNESS_TOLERANCE = 1.0


def parse_ebur128(stderr_text):
    """(integrated LUFS, peak dBFS) from ffmpeg's ebur128 summary (pure)."""
    integrated = peak = None
    for line in stderr_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "I:":
            integrated = float(parts[1])
        elif len(parts) >= 2 and parts[0] == "Peak:":
            peak = float(parts[1])
    return integrated, peak


def loudness_problem(mode, integrated, tolerance=LOUDNESS_TOLERANCE):
    """Text when a measured render misses its mode's target, else None (pure)."""
    target = MODE_TARGETS.get(mode)
    if target is None or integrated is None:
        return None
    if abs(integrated - target) <= tolerance:
        return None
    return (f"the render measures {integrated:.1f} LUFS against the "
            f"{target:.0f} LUFS {mode} target")


def measure_loudness(path):
    proc = subprocess.run(
        ["ffmpeg", "-nostats", "-i", str(path), "-vn", "-af",
         "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True)
    summary = proc.stderr.rsplit("Summary:", 1)[-1]
    return parse_ebur128(summary)


def probe_streams(path):
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,duration,nb_frames", "-print_format", "json",
         str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    return json.loads(proc.stdout).get("streams", [])


def _items(timeline, kind):
    out = []
    for t in range(1, (timeline.GetTrackCount(kind) or 0) + 1):
        out.extend((t, it) for it in
                   (timeline.GetItemListInTrack(kind, t) or []))
    return out


def _gain(item):
    value = item.GetProperty("AudioVolume")
    return float(value) if isinstance(value, (int, float)) else None


def _rows(timeline):
    rows = []
    for kind in ("video", "audio"):
        for track, it in _items(timeline, kind):
            rows.append([kind, track, it.GetStart(), it.GetDuration(),
                         it.GetLeftOffset(), it.GetName(),
                         _gain(it) if kind == "audio" else None])
    return rows


def _open(resolve, project_name, timeline_name):
    project, problem = resolve_import._open_project(
        resolve.GetProjectManager(), project_name)
    if not project:
        return None, None, problem
    if timeline_name:
        for i in range(project.GetTimelineCount()):
            tl = project.GetTimelineByIndex(i + 1)
            if tl.GetName() == timeline_name:
                project.SetCurrentTimeline(tl)
                return project, tl, None
        return None, None, (f"no timeline named {timeline_name!r} in "
                            f"project {project_name!r}")
    tl = project.GetCurrentTimeline()
    if not tl:
        return None, None, f"project {project_name!r} has no timeline open"
    return project, tl, None


def restore_gains(report_path):
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"resolve_render: cannot read {report_path}: {e}",
              file=sys.stderr)
        return 2
    gains = (report.get("loudness") or {}).get("gains_before")
    if not gains:
        print("resolve_render: the report records no normalization to undo",
              file=sys.stderr)
        return 2
    try:
        resolve = resolve_api.connect()
    except resolve_api.ResolveUnavailable as e:
        print(f"resolve_render: {e}", file=sys.stderr)
        return resolve_api.UNAVAILABLE_EXIT
    project, tl, problem = _open(resolve, report["project"],
                                 report["timeline"])
    if not project:
        print(f"resolve_render: {problem}", file=sys.stderr)
        return 1
    by_id = {it.GetUniqueId(): it for _, it in _items(tl, "audio")}
    restored, missing = 0, []
    for uid, gain in gains.items():
        it = by_id.get(uid)
        if it is None or gain is None or not it.SetProperty("AudioVolume",
                                                            gain):
            missing.append(uid)
        else:
            restored += 1
    resolve.GetProjectManager().SaveProject()
    print(json.dumps({"restored": restored, "not_restored": len(missing),
                      "timeline": report["timeline"]}, indent=2))
    return 1 if missing else 0


def _write_report(path, report):
    if path:
        rp = Path(path)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def _render(project, timeout):
    job = project.AddRenderJob()
    if not job:
        return None, "Resolve refused to add the render job"
    try:
        if not project.StartRendering([job], False):
            return None, "Resolve refused to start the render job"
        deadline = time.monotonic() + timeout
        while project.IsRenderingInProgress():
            if time.monotonic() > deadline:
                project.StopRendering()
                return None, f"render exceeded {timeout}s and was stopped"
            time.sleep(1.0)
        status = project.GetRenderJobStatus(job) or {}
        if status.get("JobStatus") != "Complete":
            return None, (f"render ended {status.get('JobStatus')!r}: "
                          f"{status.get('Error', '')}").strip()
        return status, None
    finally:
        project.DeleteRenderJob(job)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", help="Resolve project name (the video slug)")
    p.add_argument("-o", "--output", help="deliverable path, e.g. "
                   "renders/final.mp4")
    p.add_argument("--timeline", default=None,
                   help="timeline name (default: the project's current one)")
    p.add_argument("--render-preset", default="",
                   help=f'[editor] resolve-render-preset ("" = '
                        f'"{DEFAULT_PRESET}")')
    p.add_argument("--loudness", default=DEFAULT_LOUDNESS,
                   help="[editor] resolve-loudness: a Resolve normalize mode "
                        f'(default "{DEFAULT_LOUDNESS}"; "" skips)')
    p.add_argument("--fairlight-preset", default="",
                   help='[editor] resolve-fairlight-preset ("" skips)')
    p.add_argument("--accept-loudness", metavar="REASON", default=None,
                   help="publish a render that misses its loudness target; "
                        "the creator's reason is recorded in the report")
    p.add_argument("--report", default=None,
                   help="path to write the render report JSON")
    p.add_argument("--render-timeout", type=float,
                   default=DEFAULT_RENDER_TIMEOUT,
                   help="seconds before a running render is stopped")
    p.add_argument("--restore-gains", metavar="REPORT", default=None,
                   help="undo a normalization: put back the clip gains a "
                        "previous report recorded, then exit")
    args = p.parse_args(argv)

    if args.restore_gains:
        return restore_gains(args.restore_gains)
    if not args.project or not args.output:
        p.error("--project and -o are required")

    output = Path(args.output).resolve()
    preset, preset_notice = resolve_preset(args.render_preset)
    loudness = (args.loudness or "").strip()
    fairlight = (args.fairlight_preset or "").strip()
    notices = [preset_notice] if preset_notice else []

    try:
        resolve = resolve_api.connect()
    except resolve_api.ResolveUnavailable as e:
        print(f"resolve_render: {e}", file=sys.stderr)
        return resolve_api.UNAVAILABLE_EXIT

    project, tl, problem = _open(resolve, args.project, args.timeline)
    if not project:
        print(f"resolve_render: {problem}", file=sys.stderr)
        return 1
    fps = float(tl.GetSetting("timelineFrameRate")
                or project.GetSetting("timelineFrameRate"))
    expected_frames = tl.GetEndFrame() - tl.GetStartFrame()
    expected = expected_seconds(tl.GetStartFrame(), tl.GetEndFrame(), fps)

    if fairlight:
        if not project.ApplyFairlightPresetToCurrentTimeline(fairlight):
            print(f"resolve_render: Resolve refused Fairlight preset "
                  f"{fairlight!r}", file=sys.stderr)
            return 1
    else:
        notices.append("no Fairlight preset configured; none applied")

    loud = None
    if loudness:
        modes = tl.GetNormalizeAudioModes() or []
        if loudness not in modes:
            print(f"resolve_render: {loudness!r} is not a Resolve normalize "
                  f"mode. Modes: {', '.join(modes)}", file=sys.stderr)
            return 2
        audio = [it for _, it in _items(tl, "audio")]
        before = {it.GetUniqueId(): _gain(it) for it in audio}
        print(f"resolve_render: normalizing {len(audio)} audio item(s) on "
              f"timeline {tl.GetName()!r} to {loudness!r}. This sets clip "
              "gain in the creator's timeline; --restore-gains with this "
              "run's report puts it back.", file=sys.stderr)
        if audio and not tl.NormalizeAudioLevel(
                audio, {"normalizationMode": loudness}):
            print("resolve_render: Resolve refused to normalize the audio",
                  file=sys.stderr)
            return 1
        after = {it.GetUniqueId(): _gain(it) for it in audio}
        loud = {"mode": loudness, "items": len(audio),
                "gains_before": before, "gains_after": after}
    else:
        notices.append("no loudness mode configured; audio not normalized")

    params = {"project": args.project, "timeline": tl.GetUniqueId(),
              "preset": preset, "loudness": loudness,
              "fairlight": fairlight}
    key = fingerprint(_rows(tl), params)
    started = time.time()
    tmpdir = output.parent / f".resolve-render-{key[:12]}-{os.getpid()}"
    tmpdir.mkdir(parents=True, exist_ok=True)

    def fail(message):
        shutil.rmtree(tmpdir, ignore_errors=True)
        _write_report(args.report, {
            "ok": False, "error": message, "project": args.project,
            "timeline": tl.GetName(), "preset": preset, "loudness": loud,
            "notices": notices})
        print(f"resolve_render: {message}. Nothing was published.",
              file=sys.stderr)
        if loud:
            print("resolve_render: the normalized clip gains stay in the "
                  "timeline; --restore-gains with the report puts them "
                  "back.", file=sys.stderr)
        return 1

    if not project.LoadRenderPreset(preset):
        return fail(f"Resolve has no render preset named {preset!r}")
    project.SetCurrentRenderMode(SINGLE_CLIP)
    if not project.SetRenderSettings({"SelectAllFrames": True,
                                      "TargetDir": str(tmpdir),
                                      "CustomName": output.stem}):
        return fail("Resolve refused the render settings")
    status, problem = _render(project, args.render_timeout)
    if problem:
        return fail(problem)

    files = [f for f in tmpdir.iterdir() if f.is_file()]
    if len(files) != 1:
        return fail(f"expected one rendered file in {tmpdir}, found "
                    f"{len(files)}")
    rendered = files[0]
    valid, problems = core.validate_render(rendered)
    problems += check_streams(probe_streams(rendered), expected_frames, fps)
    if not valid or problems:
        return fail("render failed validation: " + "; ".join(problems))

    integrated, peak = measure_loudness(rendered)
    measured = {"integrated_lufs": integrated, "peak_dbfs": peak,
                "target_lufs": MODE_TARGETS.get(loudness)}
    miss = loudness_problem(loudness, integrated)
    if miss:
        accept = (args.accept_loudness or "").strip()
        if not accept:
            return fail(f"{miss} (peak {peak} dBFS). Resolve's normalization "
                        "stops at the peak ceiling. Add a limiter through "
                        "the creator's Fairlight preset and re-run, or pass "
                        '--accept-loudness "<reason>" when the creator '
                        "accepts this level")
        measured["accepted"] = accept
        notices.append(f"{miss}; accepted by the creator ({accept})")

    if fingerprint(_rows(tl), params) != key:
        return fail("the timeline changed while it rendered; re-run on the "
                    "current timeline")
    target = publish_path(output, rendered)
    if superseded_by_sidecar(target.with_name(target.name + ".key"),
                             started, key):
        return fail(f"a newer render was published to {target.name} while "
                    "this one ran")
    if target != output:
        notices.append(f"preset rendered {rendered.suffix}; published as "
                       f"{target.name}")
    core.publish_render(rendered, target, key)
    shutil.rmtree(tmpdir, ignore_errors=True)

    report = {
        "ok": True, "project": args.project, "timeline": tl.GetName(),
        "preset": preset, "fairlight": fairlight or None, "loudness": loud,
        "measured": measured,
        "fps": fps, "expected_duration_seconds": round(expected, 3),
        "actual_duration_seconds": round(core.probe_duration(target) or 0, 3),
        "render_ms": status.get("TimeTakenToRenderInMs"),
        "output": str(target), "render_key": key, "notices": notices,
    }
    _write_report(args.report, report)
    summary = {k: v for k, v in report.items() if k != "loudness"}
    summary["loudness"] = loud and {k: loud[k] for k in ("mode", "items")}
    print(json.dumps(summary, indent=2))
    for n in notices:
        print(f"note: {n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
