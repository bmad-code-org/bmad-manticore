#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Open the video's Resolve project and cut timeline for the package scripts.

Imported by resolve_captions.py, resolve_markers.py and resolve_frames.py;
not run on its own.

Contract:
    open_timeline(project_name, timeline_name)
            returns (resolve, project, timeline), or raises SessionError with
            the reason. Connects through resolve_api.py (which raises
            ResolveUnavailable when Resolve Studio cannot be reached). It
            switches to the named project only when the open project is that
            project or is empty, and never saves the open project: saving an
            unsaved project opens a Save As dialog in Resolve that blocks
            every scripting call behind it.
    timeline_fps(text)
            the timeline frame rate as an exact Fraction ("29.97" is
            30000/1001).

Project and timeline names come from cut/resolve-import.json, which mc-cut
writes when it imports the cut; the calling skill passes them in.

STATUS: implemented (pure logic covered by
scripts/tests/test-resolve_session.py).
"""

import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve_api

NTSC = {"23.976": Fraction(24000, 1001), "29.97": Fraction(30000, 1001),
        "47.952": Fraction(48000, 1001), "59.94": Fraction(60000, 1001),
        "119.88": Fraction(120000, 1001)}


class SessionError(RuntimeError):
    pass


def timeline_fps(text):
    """Exact timeline frame rate from Resolve's setting value (pure)."""
    raw = str(text).strip()
    if raw in NTSC:
        return NTSC[raw]
    rate = Fraction(raw).limit_denominator(1001)
    if rate <= 0:
        raise ValueError(f"bad frame rate {text!r}")
    return rate


def is_empty_project(timeline_count, clip_count):
    """True when switching away from a project can lose nothing (pure)."""
    return timeline_count == 0 and clip_count == 0


def _open_project(pm, name):
    current = pm.GetCurrentProject()
    if current and current.GetName() == name:
        return current
    if current:
        root = current.GetMediaPool().GetRootFolder()
        if not is_empty_project(current.GetTimelineCount(),
                                len(root.GetClipList() or [])
                                + len(root.GetSubFolderList() or [])):
            raise SessionError(
                f"Resolve has project {current.GetName()!r} open with work in "
                f"it. Save and close it, or open {name!r}, then re-run.")
    project = pm.LoadProject(name)
    if not project:
        raise SessionError(f"Resolve has no project named {name!r}. Run "
                           "mc-cut's Resolve import first.")
    return project


def open_timeline(project_name, timeline_name):
    """(resolve, project, timeline) for the named project and timeline."""
    resolve = resolve_api.connect()
    project = _open_project(resolve.GetProjectManager(), project_name)
    for i in range(project.GetTimelineCount()):
        timeline = project.GetTimelineByIndex(i + 1)
        if timeline.GetName() == timeline_name:
            project.SetCurrentTimeline(timeline)
            return resolve, project, timeline
    raise SessionError(f"project {project_name!r} has no timeline named "
                       f"{timeline_name!r}")


def fps_of(timeline):
    return timeline_fps(timeline.GetSetting("timelineFrameRate"))
