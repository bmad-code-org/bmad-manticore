#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Create the video's DaVinci Resolve project, or reuse it if it exists.

Usage:
    uv run {skill-root}/scripts/resolve_project.py --project <slug> \
        [--preset "<[editor] resolve-project-preset>"]

Contract:
    does    connects to the running Resolve Studio (resolve_api.py). Checks
            the preset name first, against Resolve's project settings
            presets, so a wrong name changes nothing. Then opens the Resolve
            project named --project, or creates it and applies --preset.
            A project that already exists is reused as is: the preset is not
            re-applied, because the creator may have changed its settings.
            It never saves or closes the open project, and refuses to switch
            away from one that holds work (timelines or media).
    output  json.dumps report on stdout: {"ok", "project", "created",
            "preset", "preset_applied", "note"}.

Exit codes: 0 project open, 1 failure (preset missing, open project holds
work, Resolve refused), 2 usage, 4 Resolve unavailable (not running,
scripting not Local, or not Studio). On 4 the cut stage creates the project
later; nothing else depends on it.

STATUS: implemented (pure logic covered by
scripts/tests/test-resolve_project.py).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve_api


def is_empty_project(timeline_count, clip_count):
    """True when switching away from a project can lose nothing (pure)."""
    return timeline_count == 0 and clip_count == 0


def preset_names(info):
    """Names from GetProjectSettingsPresetList() (pure)."""
    out = []
    for v in info or []:
        name = v.get("Name") if isinstance(v, dict) else v
        if name:
            out.append(str(name))
    return out


def plan(current_name, current_holds_work, target, existing_projects):
    """What to do about the open project and the target (pure).

    Returns ("reuse-open" | "load" | "create" | "refuse", note).
    """
    if current_name == target:
        return "reuse-open", f"{target!r} is already open"
    if current_name and current_holds_work:
        return "refuse", (f"Resolve has project {current_name!r} open with "
                          f"work in it. Save and close it, or open "
                          f"{target!r}, then re-run.")
    if target in existing_projects:
        return "load", f"reusing the existing project {target!r}"
    return "create", f"creating {target!r}"


def _holds_work(project):
    root = project.GetMediaPool().GetRootFolder()
    return not is_empty_project(project.GetTimelineCount(),
                                len(root.GetClipList() or [])
                                + len(root.GetSubFolderList() or []))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True,
                   help="Resolve project name (the video slug)")
    p.add_argument("--preset", default="",
                   help="project settings preset for a new project")
    args = p.parse_args(argv)
    if not args.project.strip():
        print("resolve_project: --project is empty", file=sys.stderr)
        return 2

    try:
        resolve = resolve_api.connect()
    except resolve_api.ResolveUnavailable as e:
        print(f"resolve_project: {e}", file=sys.stderr)
        return resolve_api.UNAVAILABLE_EXIT

    pm = resolve.GetProjectManager()
    current = pm.GetCurrentProject()
    if args.preset and current:
        available = preset_names(current.GetProjectSettingsPresetList())
        if args.preset not in available:
            print(f"resolve_project: project settings preset "
                  f"{args.preset!r} is not in Resolve's list {available}. "
                  "Create it in Resolve or change [editor] "
                  "resolve-project-preset.", file=sys.stderr)
            return 1

    action, note = plan(current.GetName() if current else None,
                        _holds_work(current) if current else False,
                        args.project,
                        pm.GetProjectListInCurrentFolder() or [])
    if action == "refuse":
        print(f"resolve_project: {note}", file=sys.stderr)
        return 1
    if action == "reuse-open":
        project = current
    elif action == "load":
        project = pm.LoadProject(args.project)
    else:
        project = pm.CreateProject(args.project)
    if not project:
        print(f"resolve_project: Resolve refused to {action} "
              f"{args.project!r}", file=sys.stderr)
        return 1

    applied = False
    if action == "create" and args.preset:
        applied = bool(project.SetProjectSettingsPreset(args.preset))
        if not applied:
            print(f"resolve_project: Resolve refused to apply preset "
                  f"{args.preset!r} to {args.project!r}", file=sys.stderr)
            return 1
    if action == "create":
        pm.SaveProject()

    print(json.dumps({"ok": True, "project": args.project,
                      "created": action == "create",
                      "preset": args.preset or None,
                      "preset_applied": applied,
                      "note": note if args.preset or action != "create"
                      else note + " with Resolve's default settings"},
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
