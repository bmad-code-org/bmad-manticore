#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Import the rough cut into DaVinci Resolve Studio and prove it landed in sync.

Usage:
    uv run {skill-root}/scripts/resolve_import.py cut/rough.fcpxml \
        --edl cut/edl.json --project <slug> \
        [--timeline-name "<slug> rough"] [--cut-marker-min 2.0] \
        [-o cut/resolve-import.json]

Contract:
    input   cut/rough.fcpxml from edl_to_fcpxml.py and the cut/edl.json it was
            built from (they must agree: one spine clip per EDL segment).
    does    connects to a running Resolve Studio (resolve_api.py) and switches
            to the Resolve project named --project, creating it if missing.
            It never saves the open project, and refuses to switch away from
            one that holds work. Then it imports the fcpxml as a NEW timeline.
            An existing timeline with the same name is never touched; the new
            one takes the next free " vN" suffix. Then adds review markers:
              blue  at each beat change, named for the beat, the opening
                    quote as the note
              red   at each join that removed at least --cut-marker-min
                    seconds of source, the EDL reason as the note
            Markers carry customData "manticore-beat" / "manticore-cut".
    verify  every video item on V1 is checked against the fcpxml spine:
            record start, duration, and source in point, in whole frames.
            A1 must hold the same number of items when the source has audio.
            Any mismatch exits 1 and renames the timeline with a
            " (FAILED VERIFY)" suffix so it cannot be mistaken for the cut.
    output  -o report JSON (project, timeline, items, markers, mismatches);
            json.dumps summary on stdout either way.

Exit codes: 0 imported and verified, 1 import or verify failure, 2 usage,
4 Resolve unavailable (not running, scripting not Local, or not Studio). On 4
the fcpxml is still valid; the creator can import it by hand.

STATUS: implemented (pure logic covered by
scripts/tests/test-resolve_import.py).
"""

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import edl_to_fcpxml as fx
import resolve_api

DEFAULT_CUT_MARKER_MIN = 2.0
FAILED_SUFFIX = " (FAILED VERIFY)"


def read_spine(fcpxml_text):
    """fps and the spine clips as whole-frame dicts (pure)."""
    root = ET.fromstring(fcpxml_text)
    fmt = root.find("./resources/format")
    if fmt is None:
        raise ValueError("fcpxml has no <format>")
    frame = fx.parse_time(fmt.get("frameDuration"))
    fps = 1 / frame
    has_audio = any(a.get("hasAudio") == "1"
                    for a in root.findall("./resources/asset"))
    clips = []
    for c in root.iter("asset-clip"):
        clips.append({
            "name": c.get("name", ""),
            "offset": int(fx.parse_time(c.get("offset", "0s")) * fps),
            "start": int(fx.parse_time(c.get("start", "0s")) * fps),
            "duration": int(fx.parse_time(c.get("duration")) * fps),
        })
    return fps, has_audio, clips


def build_markers(segments, clips, cut_marker_min=DEFAULT_CUT_MARKER_MIN):
    """Review markers at timeline frames (pure)."""
    out = []
    for i, (seg, clip) in enumerate(zip(segments, clips)):
        prev = segments[i - 1] if i else None
        if prev is None or seg.get("beat") != prev.get("beat"):
            out.append({"frame": clip["offset"], "color": "Blue",
                        "name": str(seg.get("beat") or "beat"),
                        "note": str(seg.get("quote") or "")[:240],
                        "custom": "manticore-beat"})
        if prev is not None and seg.get("source") == prev.get("source"):
            removed = float(seg["start"]) - float(prev["end"])
            if removed >= cut_marker_min:
                cut = f"cut {removed:.1f}s"
                reason = str(seg.get("reason") or "")[:240]
                # Resolve holds one marker per frame; a cut on a beat change
                # joins the beat marker.
                if out and out[-1]["frame"] == clip["offset"]:
                    out[-1]["name"] += f" / {cut}"
                    out[-1]["note"] = f"{out[-1]['note']}\n{cut}: {reason}"
                else:
                    out.append({"frame": clip["offset"], "color": "Red",
                                "name": cut, "note": reason,
                                "custom": "manticore-cut"})
    return out


def unique_name(wanted, existing):
    """`wanted`, or `wanted vN` for the first N >= 2 not taken (pure)."""
    if wanted not in existing:
        return wanted
    n = 2
    while f"{wanted} v{n}" in existing:
        n += 1
    return f"{wanted} v{n}"


def compare(clips, items, timeline_start):
    """Mismatches between spine clips and Resolve items (pure).

    `items` are dicts {start, duration, left_offset} in frames, as read from
    TimelineItem.GetStart/GetDuration/GetLeftOffset.
    """
    problems = []
    if len(items) != len(clips):
        problems.append({"what": "item count", "expected": len(clips),
                         "got": len(items)})
    for i, (c, it) in enumerate(zip(clips, items)):
        got = {"offset": round(it["start"]) - timeline_start,
               "duration": round(it["duration"]),
               "start": round(it["left_offset"])}
        for key in ("offset", "duration", "start"):
            if got[key] != c[key]:
                problems.append({"what": key, "clip": i, "name": c["name"],
                                 "expected": c[key], "got": got[key]})
    return problems


def is_empty_project(timeline_count, clip_count):
    """True when switching away from a project can lose nothing (pure)."""
    return timeline_count == 0 and clip_count == 0


def _open_project(pm, name):
    """The target project, or (None, reason) when switching is unsafe.

    Never saves the open project: saving an unsaved one opens a Save As
    dialog in Resolve that blocks every scripting call behind it.
    """
    current = pm.GetCurrentProject()
    if current and current.GetName() == name:
        return current, None
    if current:
        root = current.GetMediaPool().GetRootFolder()
        if not is_empty_project(current.GetTimelineCount(),
                                len(root.GetClipList() or [])
                                + len(root.GetSubFolderList() or [])):
            return None, (f"Resolve has project {current.GetName()!r} open "
                          "with work in it. Save and close it, or open "
                          f"{name!r}, then re-run.")
    project = pm.LoadProject(name) or pm.CreateProject(name)
    if not project:
        return None, f"could not open or create Resolve project {name!r}"
    return project, None


def _item_frames(item):
    return {"start": item.GetStart(), "duration": item.GetDuration(),
            "left_offset": item.GetLeftOffset()}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("fcpxml", help="path to cut/rough.fcpxml")
    p.add_argument("--edl", required=True, help="path to cut/edl.json")
    p.add_argument("--project", required=True,
                   help="Resolve project name (the video slug)")
    p.add_argument("--timeline-name", default=None,
                   help='timeline name (default "<project> rough")')
    p.add_argument("--cut-marker-min", type=float,
                   default=DEFAULT_CUT_MARKER_MIN,
                   help="seconds of removed source that earn a red marker "
                        f"(default {DEFAULT_CUT_MARKER_MIN})")
    p.add_argument("-o", "--output", default=None,
                   help="optional path to write the report JSON")
    args = p.parse_args(argv)

    fcpxml = Path(args.fcpxml).resolve()
    try:
        fps, has_audio, clips = read_spine(fcpxml.read_text(encoding="utf-8"))
        edl = json.loads(Path(args.edl).read_text(encoding="utf-8"))
    except (OSError, ValueError, ET.ParseError) as e:
        print(f"resolve_import: cannot read inputs: {e}", file=sys.stderr)
        return 2
    segments = edl.get("segments", [])
    if len(segments) != len(clips):
        print(f"resolve_import: {fcpxml.name} has {len(clips)} clips but "
              f"the EDL has {len(segments)} segments. Regenerate the fcpxml "
              "from this EDL first.", file=sys.stderr)
        return 2
    markers = build_markers(segments, clips, args.cut_marker_min)

    try:
        resolve = resolve_api.connect()
    except resolve_api.ResolveUnavailable as e:
        print(f"resolve_import: {e}", file=sys.stderr)
        return resolve_api.UNAVAILABLE_EXIT

    pm = resolve.GetProjectManager()
    project, problem = _open_project(pm, args.project)
    if not project:
        print(f"resolve_import: {problem}", file=sys.stderr)
        return 1
    existing = {project.GetTimelineByIndex(i + 1).GetName()
                for i in range(project.GetTimelineCount())}
    name = unique_name(args.timeline_name or f"{args.project} rough",
                       existing)
    timeline = project.GetMediaPool().ImportTimelineFromFile(
        str(fcpxml), {"timelineName": name, "importSourceClips": True})
    if not timeline:
        print(f"resolve_import: Resolve refused to import {fcpxml}",
              file=sys.stderr)
        return 1
    project.SetCurrentTimeline(timeline)

    video = [_item_frames(it) for it in
             (timeline.GetItemListInTrack("video", 1) or [])]
    problems = compare(clips, video, timeline.GetStartFrame())
    audio_count = len(timeline.GetItemListInTrack("audio", 1) or [])
    if has_audio and audio_count != len(clips):
        problems.append({"what": "A1 item count", "expected": len(clips),
                         "got": audio_count})

    placed = 0
    if not problems:
        for m in markers:
            if timeline.AddMarker(m["frame"], m["color"], m["name"],
                                  m["note"], 1, m["custom"]):
                placed += 1
        if placed != len(markers):
            problems.append({"what": "markers", "expected": len(markers),
                             "got": placed})
    if problems:
        timeline.SetName(name + FAILED_SUFFIX)
        name += FAILED_SUFFIX
    pm.SaveProject()

    report = {"ok": not problems, "project": args.project, "timeline": name,
              "fps": float(fps),
              "clips": len(clips), "video_items": len(video),
              "audio_items": audio_count, "markers": placed,
              "mismatches": problems[:50],
              "mismatch_count": len(problems)}
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "mismatches"},
                     indent=2))
    if problems:
        print(f"\nRESOLVE TIMELINE OUT OF SYNC with {fcpxml.name}: "
              f"{len(problems)} mismatch(es), first: {problems[0]}. The "
              f"timeline is renamed {name!r}; do not edit on it.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
