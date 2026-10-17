---
name: mc-retro
description: Turn post-publish notes into pipeline improvements. Use after a video ships, or when the user says "retro", "here is my feedback", or "wrap up this project".
---

# mc-retro

The compounding mechanism: feedback edits FILES, not just memory. Every note lands in a taste file that the next run obeys, which is why a note left in the conversation is lost work. The consumer of your edits is a later stage reading those files cold, with none of this session in the room.

Two lanes. Retro routes the creator's notes into the files that would have prevented them. Wrap does post-publish cleanup and asset promotion. Retro runs first and offers wrap after; the creator can also ask for wrap alone on an already-retroed project.

## Resolution rules

- Bare paths resolve against `{video-path}`, the current video project at `{projects-path}/<slug>/`.
- `{skill-root}` → this skill's installed directory; files in it always carry it.
- `{project-root}` → the project working directory.

## On Activation

1. Studio config: `uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`. Empty means mc-setup has not run: stop and route the creator there. Resolve `paths` values against `{project-root}`.
2. Establish the target. With a project, read `project.json` (stage `retro`) and take the format from it. On an ad-hoc run with no `project.json`, ask which format profile in `{formats-path}` the notes concern, or which brand file directly (`{brand-path}/voice-bible.md`, `{brand-path}/blacklist.md`, `{brand-path}/production-bible.md`), and route to that. Ad-hoc runs skip only the `project.json` bookkeeping, so ad-hoc feedback still compounds.
3. Read `{brand-path}/voice-bible.md`. If it does not exist, tell the creator it is missing and that routing a voice or wording note cannot happen without it, then route to mc-setup and stop.
4. Read `{brand-path}/blacklist.md`. If it does not exist, tell the creator it is missing and that routing a new blocked pattern cannot happen without it, then route to mc-setup and stop.
5. Read `{brand-path}/production-bible.md`. If it does not exist, tell the creator it is missing and that routing a visual-style note cannot happen without it, then route to mc-setup and stop.

## Collecting the notes

Read the target format profile too before asking for anything, so every file that may receive an edit is loaded.

When `[editor] name` is `resolve` and `cut/resolve-import.json` exists, measure what the creator changed in Resolve before asking. Confirm which timeline they finished on; the default is the current timeline in the Resolve project named for the slug. Then:

```
uv run {skill-root}/scripts/resolve_diff.py --edl cut/edl.json --project <slug> --timeline "<name>" --export retro/final-timeline.otio -o retro/resolve-diff.md --json retro/resolve-diff.json
```

It diffs track 1 against the EDL (segments trimmed, extended, split, joined, removed, added or moved, in source seconds) and counts the tracks the creator added above V1 and A1. Exit 1 means it could not parse the export, or the timeline does not use the EDL's sources: ask for the right timeline. Exit 4 means Resolve Studio is not reachable: the creator can export the timeline as OTIO by hand and you re-run with `--otio <file>` in place of `--project`. Show the creator `retro/resolve-diff.md` and ask what drove the changes. A pattern across several segments (every head trimmed, removed asides restored) is one note under the repeated-override rule below; the diff is evidence, never a note on its own.

Then take one round of notes: what felt wrong, what they re-edited in their editor, what they rewrote in the script, what looked off on screen, packaging performance (Test & Compare results if run). One round per session; do not fish for endless feedback.

## Routing

Every note goes to the file that would have prevented it.

- Voice or wording miss: a rule in `{brand-path}/voice-bible.md` carrying the verbatim example, and/or a new pattern in `{brand-path}/blacklist.md`.
- Visual style miss (overlay aesthetic, image-type choice, CTA placement): `{brand-path}/production-bible.md`, global section or the matching per-format override section.
- One beat type dominating, too many plain text cards, too few beats for the runtime: the same file's visual density and variety section, where the beats-per-minute floor, the variety quota, and the static-card cap are numbers. Change the number, globally or in the per-format override; nothing else in the module holds those three, so this edit is what actually moves the next plan.
- Every video running too dense or too sparse: that is the graphics-frequency tier, and the bible only mirrors it. Change `graphics-frequency` in `[style]` of the studio config for the whole studio, or the tier in a format profile's frontmatter for one format, then update the mirror in the bible's same section.
- Motion feeling wrong (how things enter, move, and exit): the same file's animation and motion look-and-feel section, which outranks the recipes any skill ships. A motion pattern that worked and should recur is the same edit, written as the convention rather than the complaint.
- Structural or retention miss: the Learnings section of `{formats-path}/<format>.md`.
- A stage doing the wrong thing: split the note. A mechanical value (a flag, a threshold) goes to that stage's sub-table of the studio config (`[cut]`, `[packaging]`, `[retro]` under `[modules.manticore]` in `{project-root}/_bmad/custom/config.toml`, the same file where mc-setup records mc-cut's `cutplan-flags`); edit it surgically, preserving existing keys, and module updates never touch it. Taste routes as above: the production bible when it is studio-wide, the format profile's Learnings when it is per-format. Editing that skill's installed SKILL.md is the last resort, only when the note fits neither, because module updates may overwrite it. If the harness blocks access to another skill's folder, record the note in the format profile's Learnings instead.
- A tool being driven wrong: the `notes` field of that tool's `[[tools]]` entry in the studio config (`[modules.manticore]` in `{project-root}/_bmad/custom/config.toml`).
- A mechanical failure: an issue note in the relevant engine README or script docstring.
- A pipeline gap, meaning a stage could not do its job because the module itself is missing a feature, has a wrong contract, or has a broken mechanic: an entry in the improvements log below, in addition to any local fix above.

Entries are ISO-dated, newest first, and one-way: the blacklist, Learnings, the production bible, and the improvements log only accumulate, so a change of taste is a new dated entry that supersedes by date rather than a deletion. Deletions are the creator's call.

Make the edits small and surgical, one note one edit at the point of failure, and show the creator the diff summary. A repeated override of the cut stage's judgment in the editor is itself one note: mine the pattern ("always keep pre-demo breaths") into the format profile's Learnings.

Never weaken a gate or remove a hard rule in response to convenience feedback; flag those for the creator explicitly.

## Improvements log

The structured upstream feedback channel, so module maintainers get comparable reports from every studio. mc-retro, and any stage that hits a pipeline gap mid-run, appends to `improvements-log.md` in the studio root, the parent folder of `{brand-path}` (with default paths that is `manticore/improvements-log.md`). Create it with a `# Improvements log` heading if missing. One line per entry, append-only, newest first under the heading:

`- YYYY-MM-DD [stage/skill]: what happened; why it is a gap; suggested fix; severity: low|medium|high`

Entries describe module gaps, not creator taste. Taste goes to the brand and format files per the routing above.

## Wrap lane

Post-publish cleanup for a shipped project; requires a project folder.

Nothing is reclaimed until the creator confirms the final master exists at its published or archived location. That confirmation is a hard stop: no part of this lane runs on assumption.

- Reclaim reproducible render scratch: preview renders, intermediate proxies, render caches, anything regenerable from `edl.json` plus the sources. For each HyperFrames workspace in the project, `npx hyperframes clean --snapshots --dry-run <dir>` lists what HyperFrames left behind, including the per-revision output in `.hyperframes/artifacts/`; add its list to the candidates and run it without `--dry-run` only after approval. List every candidate with its size, subtract anything matching `[retro] preserve` in the studio config, and delete only after the creator approves the list. Never candidates: source footage, transcripts, `edl.json`, the cutplan, overlays, `project.json`, and the master itself.
- Enforce one blessed asset per slot: for each asset slot (thumbnail, title card, any overlay with multiple candidates), keep only the shipped version; rejected candidates move to the reclaim list or an archive folder, the creator's choice.
- Promote evergreen assets: anything useful beyond this video (reusable overlays, diagrams, series templates) moves to the series `common/` folder beside its project folders under `{projects-path}`; anything brand-wide moves to `{brand-path}`. Write or update a `README.md` in each destination listing each promoted asset, the project it came from, and the ISO date.

Durable rules discovered during wrap route like any other note.

## Closing out

In `project.json` (skip on ad-hoc runs), append `retro` to `stages_done` and set `stage` to `done`, append the retro notes and whether wrap ran to its `notes` field, and report.
