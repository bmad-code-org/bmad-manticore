#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Check the DaVinci Resolve lane and validate the configured Resolve presets.

Usage:
    uv run {skill-root}/scripts/check_resolve.py \
        [--project-preset "<name>"] [--render-preset "<name>"] \
        [--fairlight-preset "<name>"] [--loudness "<mode>"]

Contract:
    does    connects to the running Resolve Studio (resolve_api.py) and reads,
            without switching, saving or changing anything: product and
            version, render presets and project settings presets of the open
            project, Fairlight presets, and the audio normalization modes of
            the open timeline. Each flag is the matching [editor] value from
            the studio config; an empty value is reported with the fallback
            the consuming stage states out loud.
    output  json.dumps report on stdout: {"ok", "product", "version",
            "project", "lists": {...}, "checks": {key: {"value", "ok",
            "note"}}}.

A configured name that Resolve does not list fails the check. It is never
replaced with a nearby name: the creator either creates the preset in Resolve
or changes the config. Normalization modes come from a timeline, so with no
timeline open the loudness value cannot be checked and the report says so.

Exit codes: 0 connected and every configured name exists, 1 a configured name
is missing, 2 usage, 4 Resolve unavailable (not running, scripting not Local,
or not Studio).

STATUS: implemented (pure logic covered by
scripts/tests/test-check_resolve.py).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve_api

DEFAULT_RENDER_PRESET = "H.264 Master"


def names(value):
    """Preset names from a Resolve list, dict, or list of info dicts (pure)."""
    if not value:
        return []
    if isinstance(value, dict):
        value = list(value.values())
    out = []
    for v in value:
        if isinstance(v, dict):
            v = v.get("Name") or v.get("name")
        if v:
            out.append(str(v))
    return out


def check_one(value, available, fallback_note):
    """One configured name against what Resolve lists (pure).

    `available` is None when Resolve could not list it at all.
    """
    if not value:
        return {"value": "", "ok": True, "note": fallback_note}
    if available is None:
        return {"value": value, "ok": True,
                "note": "not checked: Resolve could not list these here"}
    if value in available:
        return {"value": value, "ok": True, "note": "found"}
    return {"value": value, "ok": False,
            "note": f"{value!r} is not in Resolve's list; create it in "
                    "Resolve or change the config"}


def validate(config, lists):
    """Checks for every configured [editor] Resolve value (pure)."""
    render = lists.get("render-presets")
    return {
        "resolve-project-preset": check_one(
            config.get("project-preset", ""), lists.get("project-presets"),
            "empty: new Resolve projects use Resolve's default settings"),
        "resolve-render-preset": check_one(
            config.get("render-preset", ""), render,
            f"empty: the final render uses {DEFAULT_RENDER_PRESET!r}"
            + ("" if render is None or DEFAULT_RENDER_PRESET in render
               else f", which this Resolve does not list")),
        "resolve-fairlight-preset": check_one(
            config.get("fairlight-preset", ""), lists.get("fairlight-presets"),
            "empty: no Fairlight preset is applied before the final render"),
        "resolve-loudness": check_one(
            config.get("loudness", ""), lists.get("normalize-modes"),
            "empty: the final render skips loudness normalization"),
    }


def read_lists(resolve):
    project = resolve.GetProjectManager().GetCurrentProject()
    timeline = project.GetCurrentTimeline() if project else None
    return project, {
        "render-presets": names(project.GetRenderPresetList())
        if project else None,
        "project-presets": names(project.GetProjectSettingsPresetList())
        if project else None,
        "fairlight-presets": names(resolve.GetFairlightPresets()),
        "normalize-modes": names(timeline.GetNormalizeAudioModes())
        if timeline else None,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project-preset", default="")
    p.add_argument("--render-preset", default="")
    p.add_argument("--fairlight-preset", default="")
    p.add_argument("--loudness", default="")
    args = p.parse_args(argv)

    try:
        resolve = resolve_api.connect()
    except resolve_api.ResolveUnavailable as e:
        print(f"check_resolve: {e}", file=sys.stderr)
        return resolve_api.UNAVAILABLE_EXIT

    project, lists = read_lists(resolve)
    checks = validate({"project-preset": args.project_preset,
                       "render-preset": args.render_preset,
                       "fairlight-preset": args.fairlight_preset,
                       "loudness": args.loudness}, lists)
    report = {"ok": all(c["ok"] for c in checks.values()),
              "product": resolve.GetProductName(),
              "version": resolve.GetVersionString(),
              "project": project.GetName() if project else None,
              "lists": lists, "checks": checks}
    print(json.dumps(report, indent=2))
    if lists["normalize-modes"] is None:
        print("note: no timeline is open, so the loudness mode was not "
              "checked", file=sys.stderr)
    for key, c in checks.items():
        if not c["ok"]:
            print(f"check_resolve: {key}: {c['note']}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
