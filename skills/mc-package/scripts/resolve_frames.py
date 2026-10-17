#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Export full-resolution footage frames from the cut timeline in Resolve.

Usage:
    uv run {skill-root}/scripts/resolve_frames.py --project <slug> \
        --timeline "<cut timeline>" --out-dir packaging/work/frames/ \
        (--at 1:23.5 [--at 95] ... | --from-markers packaging/work/markers.json
         [--marker-color Blue] [--offset 0.5])

Contract:
    input   the Resolve project and cut timeline named in
            cut/resolve-import.json, and the moments to grab as edited
            timeline times (--at: seconds, M:SS or M:SS.s; repeatable), or
            the markers file from resolve_markers.py, each marker moved
            --offset seconds later so a grab never lands on a cut frame.
    does    parks the playhead on each frame and calls
            Project.ExportCurrentFrameAsStill. That export is the clip under
            the playhead as the Color page shows it: the creator's grade, no
            overlays or titles from upper tracks. That is the plate a
            thumbnail wants; the hook text is composited later.
    output  <out-dir>/frame-<MMmSSsFFf>.png per moment, written to a temp
            name, checked to decode at 1280x720 or larger, then moved into
            place; and <out-dir>/frames.json listing each grab with its
            time. json.dumps summary on stdout.

Exit codes: 0 every frame exported and decoded, 1 any frame failed, 2 usage
(bad time, drop-frame timeline), 4 Resolve unavailable (grab with ffmpeg
from the source instead and say so).

STATUS: implemented (pure logic covered by
scripts/tests/test-resolve_frames.py).
"""

import argparse
import json
import os
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve_api
import resolve_session as rs

MIN_W, MIN_H = 1280, 720


def parse_at(text):
    """Seconds from "95", "95.5", "1:35" or "1:35.5" (pure)."""
    raw = str(text).strip()
    m = re.fullmatch(r"(?:(\d+):)?(\d+(?:\.\d+)?)", raw)
    if not m:
        raise ValueError(f"bad time {text!r}")
    mins, secs = m.groups()
    if mins is not None and float(secs) >= 60:
        raise ValueError(f"bad time {text!r}")
    return int(mins or 0) * 60 + float(secs)


def to_frame(seconds, fps):
    return int(Fraction(seconds).limit_denominator(1000) * fps)


def timecode(abs_frame, fps):
    """Non-drop timecode for an absolute frame (pure)."""
    base = round(fps)
    f = int(abs_frame)
    return (f"{f // (base * 3600):02d}:{f // (base * 60) % 60:02d}:"
            f"{f // base % 60:02d}:{f % base:02d}")


def frame_name(rel_frame, fps):
    base = round(fps)
    s, ff = divmod(int(rel_frame), base)
    return f"frame-{s // 60:02d}m{s % 60:02d}s{ff:02d}f.png"


def moments_from_markers(markers, color=None, offset=0.5):
    """Edited-timeline seconds from resolve_markers.py output (pure)."""
    return [m["seconds"] + offset for m in markers.get("markers", [])
            if not color or m.get("color") == color]


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of",
                        "json", str(path)], capture_output=True, text=True)
    if r.returncode:
        return None
    streams = json.loads(r.stdout or "{}").get("streams") or [{}]
    return streams[0].get("width"), streams[0].get("height")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True)
    p.add_argument("--timeline", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--at", action="append", default=[])
    p.add_argument("--from-markers", default=None)
    p.add_argument("--marker-color", default=None)
    p.add_argument("--offset", type=float, default=0.5)
    args = p.parse_args(argv)
    try:
        moments = [parse_at(a) for a in args.at]
        if args.from_markers:
            moments += moments_from_markers(
                json.loads(Path(args.from_markers).read_text("utf-8")),
                args.marker_color, args.offset)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"resolve_frames: {e}", file=sys.stderr)
        return 2
    if not moments:
        print("resolve_frames: give --at or --from-markers", file=sys.stderr)
        return 2

    try:
        resolve, project, timeline = rs.open_timeline(args.project,
                                                      args.timeline)
    except resolve_api.ResolveUnavailable as e:
        print(f"resolve_frames: {e}", file=sys.stderr)
        return resolve_api.UNAVAILABLE_EXIT
    except rs.SessionError as e:
        print(f"resolve_frames: {e}", file=sys.stderr)
        return 1
    if str(timeline.GetSetting("timelineDropFrameTimecode")) == "1":
        print("resolve_frames: drop-frame timelines are not supported; "
              "switch the timeline to non-drop timecode", file=sys.stderr)
        return 2
    fps = rs.fps_of(timeline)
    start, end = timeline.GetStartFrame(), timeline.GetEndFrame()
    resolve.OpenPage("edit")
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    grabs, failed = [], []
    for t in moments:
        rel = to_frame(t, fps)
        if not 0 <= rel < end - start:
            failed.append({"at": t, "why": "outside the timeline"})
            continue
        tc = timecode(start + rel, fps)
        dest = out / frame_name(rel, fps)
        tmp = out / f".{dest.stem}.{os.getpid()}.png"
        if not timeline.SetCurrentTimecode(tc) or \
                timeline.GetCurrentTimecode() != tc:
            failed.append({"at": t, "why": f"could not park on {tc}"})
            continue
        if not project.ExportCurrentFrameAsStill(str(tmp)):
            failed.append({"at": t, "why": "Resolve refused the export"})
            continue
        dims = probe(tmp) if tmp.is_file() else None
        if not dims or dims[0] < MIN_W or dims[1] < MIN_H:
            tmp.unlink(missing_ok=True)
            failed.append({"at": t, "why": f"frame did not decode at "
                                           f"{MIN_W}x{MIN_H}+ ({dims})"})
            continue
        os.replace(tmp, dest)
        grabs.append({"at": round(t, 3), "frame": rel, "timecode": tc,
                      "width": dims[0], "height": dims[1],
                      "path": str(dest)})
    (out / "frames.json").write_text(
        json.dumps({"frames": grabs, "failed": failed}, indent=2) + "\n",
        encoding="utf-8")
    print(json.dumps({"ok": not failed, "frames": len(grabs),
                      "failed": failed}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
