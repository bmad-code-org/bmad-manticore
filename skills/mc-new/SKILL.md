---
name: mc-new
description: Scaffold a video project from a format profile. Use when the user says "new video", "start a project", "cut this VOD", or "make a video from this recording".
---

# mc-new

Scaffold the project every later stage runs against: `{projects-path}/<slug>/` holding `project.json` and `brief.md`. `project.json` is the contract every downstream skill reads without this conversation in the room, so the stage list, the mode, and the entry point have to be right at creation. `brief.md` carries the creator's own words and the links back to where the idea came from; nothing later recovers them.

## Resolution rules

- Bare paths resolve against `{video-path}`, the current video project at `{projects-path}/<slug>/`.
- `{skill-root}` → this skill's installed directory; files in it always carry it (`{skill-root}/scripts/new_project.py`).
- `{project-root}` → the project working directory.

## On Activation

1. Load the studio config: `uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`. Empty means mc-setup has not run: stop and route the creator there. Resolve `paths` values against `{project-root}`.

## Entry points

Settle which of the two this is before anything else: it decides the format profile and the next stage.

- Idea-first (default): the project runs the full pipeline from braindump. Any format profile works.
- Footage-first: the footage already exists (a livestream VOD, a recorded talk), ideation is skipped entirely, the source file is registered in `sources` in `project.json` at creation, and the next stage is cut. This needs a footage-first profile, one whose stage list contains no ideation stages (e.g. livestream-vod). If the studio has none, route the creator to mc-setup to add one rather than forcing an ideation profile.

## Scaffold

Collect the slug (kebab-case), the format (a profile in `{formats-path}/`), a working title, and for footage-first the absolute path to the source file. Then ask the two things the creator will not volunteer:

- Series: is this an episode of one? An episode lives in a series folder beside a shared `common/` for evergreen assets (chrome, stingers, recurring graphics), and that layout is fixed at creation.
- Deadline: does an external event gate delivery (a conference, a launch)? A date puts the project in deadline mode, where downstream stages cap iteration loops in favor of good-enough delivery. An aspirational date is not a deadline; leave it unset.

Then run:

`uv run {skill-root}/scripts/new_project.py <slug> --format <format> --title "<title>" --projects-dir {projects-path} --formats-dir {formats-path}`

adding the flags the answers call for: `--parent <slug>` for a short cut from a long-form parent, `--series <series-slug>` for an episode, `--deadline YYYY-MM-DD` for an event-gated project, and `--ingest <absolute-footage-path>` for footage-first (with `--source-id` and `--source-role primary|interview|screen` when the defaults do not fit).

When `[editor] name` is `resolve`, also open the video's Resolve project, named the slug:

`uv run {skill-root}/scripts/resolve_project.py --project <slug> --preset "<[editor] resolve-project-preset>"`

It reuses a project that already exists and never re-applies the preset to it. An empty preset creates the project with Resolve's default settings; say so. Exit 1 names what to fix (a preset Resolve does not list, or another project with work open in Resolve); relay it and re-run once the creator has fixed it. Exit 4 means Resolve Studio is not reachable: say so and continue, because the cut stage creates the project later.

## Brief

Fill `brief.md` from what the creator gives you, never invented; ask if it is thin. Idea-first: the idea in their words, why now, and links to the source material (idea notes, prior material). Footage-first: what the footage is, what the finished video should become, and any moments they already know matter.

## Handoff

Report the created project and route to the skill named by `stage` in `project.json`. Never assume the master stage list.

## Checklist

- `brief.md` is in the creator's language and links back to wherever the idea came from.
- The deadline field, if set, names a real external gate.

`new_project.py` exits non-zero on the rest: a non-kebab slug or series, an existing project path, a missing or malformed format profile, an ideation-bearing profile under `--ingest`, a missing footage file, and a non-ISO deadline.
