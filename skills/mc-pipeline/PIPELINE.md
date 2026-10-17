# PIPELINE.md: The State Machine

The master spec for the Manticore pipeline, owned by mc-pipeline (the router). It defines the stages, what each hands to the next, the approval gates, and the `project.json` contract. Each stage skill is self-contained and carries its own steps; this is the contract they conform to.

This file is a contract, not a summary of the stages. It carries what crosses a stage boundary. Anything a stage produces and consumes entirely within itself belongs to that skill, and adding it here is how this file goes stale.

Three naming notes, because each has caused real confusion:

- Bracketed table names (`[owner]`, `[paths]`, `[render]`, `[editor]`, and the rest) are sub-tables of `[modules.manticore]`, the studio config. `[defaults.*]` names appear only in `mc-setup/assets/studio-defaults.toml`, the seed it copies from; a resolved studio config has no `[defaults]` table.
- `{projects-path}`, `{brand-path}`, `{formats-path}`, `{engines-path}` are the `[paths]` values resolved against `{project-root}`. If `[modules.manticore]` is empty, run mc-setup first; no stage skill proceeds without it.
- A bare path in any skill file is the current video project: `{video-path}` = `{projects-path}/<slug>/`. A file inside a skill's own folder always carries `{skill-root}`, so bare `assets/` is the project's farmed-asset folder and `{skill-root}/assets/` is the skill's own. A path led by a skill name (`mc-cut/scripts/preflight.py`) names a file in that skill's folder, and is the form any skill file uses to record which skill owns a script or a document; a skill still reads only its own folder.

## Stage sequence (master list)

Format profiles select a subset of these stages (see the `stages:` frontmatter of `{formats-path}/<format>.md`). A lane format may also declare a stage that is not in the master list (the livestream-pack format's `stream-pack` stage, owned by mc-stream-pack). The routing rule is uniform either way: a stage named `<stage>` is owned by the `mc-<stage>` skill, except the creator-owned stages (`record`, `final`) marked in the table below. The master order is:

| # | Stage | Owner | Gate | Artifacts produced (inside `{projects-path}/<slug>/`) |
|---|---|---|---|---|
| 1 | new | mc-new | | `project.json`, `brief.md` |
| 2 | braindump | mc-braindump | | `braindump.md` (verbatim) |
| 3 | outline | mc-outline | gate 1: outline | `outline.md` (hooks + outline + packaging promise) |
| 4 | script | mc-script | | `script.md` (lint passed, craft QA passed) |
| 5 | record | the creator | | `raw/*` recordings, constant frame rate |
| 6 | cut | mc-cut | gate 2: cutplan | `transcript/words.json`, `cut/edl.json`, `cut/cutplan.md`, `cut/editorial-review.md`, `cut/rough.fcpxml` (per `[editor] timeline-format`; `none` skips), `renders/preview.mp4` |
| 7 | beats | mc-beats | gate 3: beats | `beats/beats.md` (the beat table), `beats/STORYBOARD.md` |
| 8 | assets | mc-assets | | `assets/` + `assets/manifest.json` |
| 9 | graphics | mc-graphics | | `graphics/` alpha MOVs + `graphics/HANDOFF.md` |
| 10 | package | mc-package | | `packaging/titles.md`, `packaging/thumbs/`, `packaging/description.md`, `packaging/chapters.md`, `packaging/captions/` |
| 11 | final | the creator, with an offered pipeline render | gate 4: final | `renders/final.mp4`, or the creator's own editor render into `renders/` |
| 12 | retro | mc-retro | | edits to `{formats-path}/<format>.md` learnings + offending skill files |

Stage 8 (assets) runs before stage 9 (graphics) so the farmed stills and clips exist before graphics composes with them; both unlock at gate 3. Stage 10 may start any time after gate 1 (the packaging promise exists from the outline).

## project.json contract

```json
{
  "slug": "example-video",
  "title": "",
  "format": "talking-head",
  "created": "2026-07-03",
  "parent": null,
  "stage": "braindump",
  "series": null,
  "stages": ["new", "braindump", "outline", "script", "record", "cut", "beats", "assets", "graphics", "package", "final", "retro"],
  "stages_done": ["new"],
  "approvals": {
    "outline": null,
    "cutplan": null,
    "beats": null,
    "final": null
  },
  "artifacts": {},
  "notes": ""
}
```

Field rules:

- `stage` is the stage currently in progress or next to run. When the last stage in `stages` completes (retro), it is set to `done`, the one terminal value not drawn from `stages`.
- `stages` is copied from the format profile at creation; never assume the master list. Footage-first projects (an existing recording, a livestream VOD) use the ingest-first variant written by mc-new's ingest mode: `["new", "cut", "beats", "assets", "graphics", "package", "final", "retro"]`. It skips the ideation stages entirely; the source file is registered in `sources` at creation.
- `series` (optional, default `null`) names the series this project belongs to, written by mc-new's `--series` mode. A series is a folder under `{projects-path}` holding a `common/` folder for evergreen shared assets and one subfolder per episode project. Stages that read brand templates (mc-package) check `series` to apply per-series packaging templates.
- `approvals` values are `null` (not reached), `"pending"` (artifact presented, waiting on the creator), or an ISO date string (approved that day). Only the creator's explicit say-so in conversation moves pending to a date.
- `artifacts` maps artifact names to paths as they are produced, e.g. `"edl": "cut/edl.json"`.
- `parent` links a short to its long-form parent project slug.
- `sources` (optional) registers media inputs as they arrive: `{"id": "camera-a", "file": "raw/camera-a.mp4", "role": "primary", "cfr": true}`. Roles are `primary` (a talking-head take), `interview` (a recorded braindump, segmented mechanically by its spoken marker cue), and `screen` (screen share). Stages that ingest media append here, and a source corrected by mc-cut's `normalize_source.py` is registered as the project's source of truth.

## The stage skill algorithm

Every mc-* stage skill follows the same shape. No exceptions, no creativity in the mechanics:

1. Resolve the studio config (`resolve_config.py --key modules.manticore`); if it is empty, stop and run mc-setup.
2. Read `project.json`. If the project's `stage` does not match this skill's stage, stop and say so (mc-pipeline routes; stage skills do not self-route). The one exception is a declared ROUTED ENTRY POINT: a section a skill declares for the router to re-enter after its own stage has closed. Those touch no gates, no approvals and no stage fields, so they cannot advance or rewind the project, and a skill that declares none has no exception. mc-cut is the only skill with any today.
3. Read the format profile at `{formats-path}/<format>.md` and any taste files it names (all under `{brand-path}`). A taste file that does not exist stops the stage: name the file, say what cannot happen without it, route to mc-setup.
4. Do the stage work, calling the stage skill's own scripts for anything mechanical.
5. Run the stage's checklist (in the skill file). Fix failures before presenting.
6. Write the artifacts to the paths in the table above. Update `artifacts` in project.json.
7. If the stage is a gate: set the approval to `"pending"`, present the artifact to the creator, and STOP. Do not proceed, do not start the next stage, do not summarize what the next stage will do.
8. If not a gate (or after approval is recorded): append the stage to `stages_done`, set `stage` to the next stage in `stages`, and stop.

If the config exists but a key this stage needs is missing or empty, ask for just that value, write it into `[modules.manticore]` in `{project-root}/_bmad/custom/config.toml`, and continue (as-needed setup); suggest a full mc-setup pass only if several keys are missing.

## Gate behavior

What each gate blocks is the part other stages depend on:

| Gate | Approves | Nothing may happen until it clears |
|---|---|---|
| 1: outline | hook, outline, the title/thumbnail promise | any script is written |
| 2: cutplan | the taste calls | the preview and timeline count as the rough cut |
| 3: beats | the beat table | any graphics code is written |
| 4: final | the deliverable | publishing |

Gate 4 has two equally supported paths: the offered `renders/final.mp4`, or the creator's own editor render from the always-exported timeline. Approval of either closes it, and because the timeline, edl.json, cutplan and overlays all exist regardless, switching paths never loses work.

## Engine policy

- HyperFrames: the graphics engine. Per-video overlay beats, stingers and transitions (dual render: VP9 alpha WebM for OBS + ProRes 4444 for the editor timeline lane), and shorts karaoke captions. Registry blocks before authoring (`npx hyperframes catalog --query`, then `npx hyperframes add`). Export overlay-only ProRes 4444 MOV with alpha. Apache 2.0, local, no commercial-use threshold.
- Baked alpha MOVs are the deliverable everywhere, unconditionally. There is no editable-graphics lane and no editor-dependent branch: what one editor gets, every editor gets.
- Everything is themed through `{brand-path}/tokens.json`. Component sourcing rule: registries and open libraries first, author from scratch only when nothing fits.
- Engine workspaces (the pinned HyperFrames project) live at `{engines-path}`; mc-setup or the first graphics run initializes them.
- Compatibility aliases (unconditional, any vintage): `remotion` and `ograf` are permanent aliases for `hyperframes` wherever an engine is named, whether a beat-table `engine` value or a format profile's `engine_overlays`/`engine_stingers` frontmatter. A studio configured before a given engine was dropped keeps its own copied profiles and beat tables naming it; every skill reads those as `hyperframes` and no creator file is ever rewritten. Neither has an engine doc or workspace to route to.
- `[editor] ograf-editable` is a retired key. A studio config written before 3.0.0 may still carry it; ignore it rather than acting on it, and never write it.

## The beat table (engine-neutral graphics contract)

One row per graphic beat, produced by mc-beats, consumed by mc-graphics and mc-assets regardless of engine:

| id | start | dur | end | anchor word | anchor ts | spoken phrase | type | engine | asset | composition |
|---|---|---|---|---|---|---|---|---|---|---|

Column rules:

- `type` is a beat type from the format profile's `beat-types` frontmatter list (e.g. `lower-third`, `diagram`, `stat-card`, `cta`); the profile is the single type vocabulary for its format. The reserved placeholder `overlay` is legal only when reading legacy tables (tolerance rule below) and is never written.
- `engine` names the engine that renders the beat, per the Engine policy below (e.g. `hyperframes`, `html`).
- `asset` is `null` or a farmed-asset id from `assets/manifest.json`; mc-assets farms the listed assets, mc-graphics composes with them.
- Tolerance rule: consumers MUST accept rows missing `type`, `engine`, or `asset` (beat tables written by 0.x projects). Treat a missing `type` as the reserved placeholder `overlay` (informational only; rendering keys off `engine` and `composition`), a missing `engine` as the Engine policy default, an `engine` of `remotion` (from any vintage of table, per the Engine policy's compatibility alias) as `hyperframes`, and a missing `asset` as `null`. A stage that rewrites the table (mc-beats) replaces every `overlay` placeholder with a type from the profile's `beat-types`. An in-flight 0.x project never breaks on the extended contract.

Anchors are measured against the EDITED timeline defined by `cut/edl.json`, not the raw take.

## Blessed-slot convention

Deliverable folders hold exactly one blessed asset per slot; alternates, drafts, and retries live in a `work/` folder beside them. The pattern for mc-package: candidates accumulate in `packaging/thumbs/` and `packaging/titles.md`; after the creator picks, exactly one blessed asset per slot is written to `packaging/final/` and recorded in `artifacts` in project.json. Any stage producing a pick-one-of-N deliverable follows the same rule: the deliverable path is unambiguous, the exploration stays in `work/`.

## Cutting rules

mc-cut is the only stage that cuts, so its rules and the gates enforcing them live there. One binds every stage, which is why it is here: the TRANSCRIPT is the authority on CONTENT, the AUDIO is the authority on TIMING. No stage derives a cut time, a beat time, or a silence from transcript timestamps.

## Verification contract

A check the pipeline claims to perform must be a script that exits non-zero; AGENTS.md carries the rule and the four shipped defects that produced it. What belongs here is the cross-stage view, because no single skill can see it: which gates block what, and where a blocked run can legitimately proceed.

Every blocking gate carries an acknowledged override (`preflight.py --allow-qc-defects`, `verify_transcript.py --accept-region ... --reason ...`), so a false positive becomes a recorded human decision rather than a reason to work around the gate silently.

| Check | Script | Blocks |
|---|---|---|
| Source edge defects | `mc-cut/scripts/preflight.py` (exit 3) | transcription and everything after |
| Transcript completeness | `mc-cut/scripts/verify_transcript.py` | candidate detection |
| Cut integrity | `mc-cut/scripts/verify_edl.py` | rendering, timeline export, gate 2 |
| Beat anchor placement | `mc-beats/scripts/verify_anchors.py` | gate 3 and graphics |
| Render output integrity | `mc-cut/scripts/render_preview.py` / `mc-cut/scripts/render_final.py` | publishing the output file |

## Spatial normalize

`cut/edl.json` is purely TEMPORAL: segments are `{source, start, end}` and the renderers expose no crop, scale, or position transform. A defect baked into the pixels (a recorded-in border, letterboxing, off-centre framing) therefore cannot be corrected anywhere downstream, and every later stage inherits whatever canvas the cut stage leaves it.

That is why correction is the cut stage's job and mc-cut owns the mechanics. Two consequences bind other stages: a normalize moves nothing in time, so an existing transcript, EDL, cutplan and beat table all stay valid across one; and corrective normalize is global and defect-driven, where creative reframing (punch-ins, motion zooms) is per-moment emphasis belonging to the beats stage, on the already-clean canvas.
