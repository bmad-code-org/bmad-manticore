#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Scaffold a new video project from a format profile.

Usage:
    uv run new_project.py <slug> --format talking-head \
        --projects-dir {projects-path} --formats-dir {formats-path} \
        [--title "..."] [--parent <slug>]

The calling skill resolves the two directories from the studio config
([modules.manticore] in {project-root}/_bmad/custom/config.toml) and passes
them explicitly; this script does no config discovery. Format profiles must
already exist in --formats-dir (mc-setup copies the defaults there).

Reads the stage list from the format profile's frontmatter, creates the
project folder with the standard subfolders, and writes project.json per the
pipeline contract (skills/mc-pipeline/PIPELINE.md in the module).
"""

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

SUBDIRS = ["raw", "transcript", "cut", "beats", "graphics", "assets", "packaging", "renders"]
GATES = ["outline", "cutplan", "beats", "final"]


def read_profile_stages(formats_dir: Path, fmt: str) -> list[str]:
    profile = formats_dir / f"{fmt}.md"
    if not profile.exists():
        names = sorted(p.stem for p in formats_dir.glob("*.md")) if formats_dir.is_dir() else []
        hint = f"available: {', '.join(names)}" if names else \
            f"{formats_dir} has no profiles. Run the mc-setup skill to populate it."
        sys.exit(f"error: no format profile for {fmt!r}\n{hint}")
    m = re.search(r"^stages:\s*\[(.*?)\]", profile.read_text(), re.MULTILINE)
    if not m:
        sys.exit(f"error: {profile} has no 'stages: [...]' line in frontmatter")
    return [s.strip() for s in m.group(1).split(",") if s.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("slug", help="kebab-case project slug")
    ap.add_argument("--format", required=True, dest="fmt", help="format profile name")
    ap.add_argument("--projects-dir", required=True, help="resolved {projects-path}")
    ap.add_argument("--formats-dir", required=True, help="resolved {formats-path}")
    ap.add_argument("--title", default="", help="working title")
    ap.add_argument("--parent", default=None, help="parent project slug (shorts)")
    args = ap.parse_args()

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.slug):
        sys.exit("error: slug must be kebab-case (lowercase letters, digits, hyphens)")

    projects_dir = Path(args.projects_dir)
    formats_dir = Path(args.formats_dir)

    stages = read_profile_stages(formats_dir, args.fmt)
    if not stages or stages[0] != "new":
        sys.exit(
            f"error: {formats_dir / f'{args.fmt}.md'} stages must start with 'new' "
            f"(got {stages!r}); fix the profile's 'stages: [...]' line"
        )
    proj = projects_dir / args.slug
    if proj.exists():
        sys.exit(f"error: {proj} already exists")

    for sub in SUBDIRS:
        (proj / sub).mkdir(parents=True)

    state = {
        "slug": args.slug,
        "title": args.title,
        "format": args.fmt,
        "created": datetime.date.today().isoformat(),
        "parent": args.parent,
        "stage": stages[1] if len(stages) > 1 else stages[0],
        "stages": stages,
        "stages_done": ["new"],
        "approvals": {g: None for g in GATES},
        "artifacts": {},
        "notes": "",
    }
    (proj / "project.json").write_text(json.dumps(state, indent=2) + "\n")

    brief = (
        f"# Brief: {args.title or args.slug}\n\n"
        f"Format: {args.fmt}\n"
        f"Created: {state['created']}\n\n"
        "## The idea (one paragraph, in the creator's words)\n\n\n"
        "## Why now\n\n\n"
        "## Source material\n\n(idea notes, links, prior material)\n"
    )
    (proj / "brief.md").write_text(brief)

    print(f"created {proj} (format={args.fmt}, next stage: {state['stage']})")


if __name__ == "__main__":
    main()
