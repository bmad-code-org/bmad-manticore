#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Read the cut timeline's markers from DaVinci Resolve Studio.

Usage:
    uv run {skill-root}/scripts/resolve_markers.py --project <slug> \
        --timeline "<cut timeline>" -o packaging/work/markers.json

Contract:
    input   the Resolve project and cut timeline named in
            cut/resolve-import.json.
    output  -o JSON: {"timeline_seconds", "fps", "markers": [{seconds, frame,
            color, name, note, custom, duration_frames}]}, sorted by time,
            frames and seconds relative to the timeline start. mc-cut's
            import places blue "manticore-beat" markers at beat changes and
            red "manticore-cut" markers at large cuts; any marker the creator
            added or moved in Resolve comes back as it is now.

Exit codes: 0 ok, 1 project or timeline missing, 2 usage, 4 Resolve
unavailable.

STATUS: implemented (pure logic covered by
scripts/tests/test-resolve_markers.py).
"""

import argparse
import json
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve_api
import resolve_session as rs


def marker_list(raw, fps):
    """Markers from Timeline.GetMarkers() output, sorted (pure).

    GetMarkers keys are frames relative to the timeline start.
    """
    out = []
    for frame, info in (raw or {}).items():
        f = int(round(float(frame)))
        out.append({"frame": f, "seconds": round(float(Fraction(f) / fps), 3),
                    "color": info.get("color", ""),
                    "name": info.get("name", ""),
                    "note": info.get("note", ""),
                    "custom": info.get("customData", ""),
                    "duration_frames": info.get("duration", 1)})
    return sorted(out, key=lambda m: m["frame"])


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True)
    p.add_argument("--timeline", required=True)
    p.add_argument("-o", "--output", required=True)
    args = p.parse_args(argv)
    try:
        _, _, timeline = rs.open_timeline(args.project, args.timeline)
    except resolve_api.ResolveUnavailable as e:
        print(f"resolve_markers: {e}", file=sys.stderr)
        return resolve_api.UNAVAILABLE_EXIT
    except rs.SessionError as e:
        print(f"resolve_markers: {e}", file=sys.stderr)
        return 1
    fps = rs.fps_of(timeline)
    seconds = float(Fraction(timeline.GetEndFrame()
                             - timeline.GetStartFrame()) / fps)
    report = {"timeline_seconds": round(seconds, 3), "fps": float(fps),
              "markers": marker_list(timeline.GetMarkers(), fps)}
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "markers": len(report["markers"]),
                      "timeline_seconds": report["timeline_seconds"],
                      "output": str(out)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
