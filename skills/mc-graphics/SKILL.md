---
name: mc-graphics
description: Render the beat table into alpha overlays. Use at the graphics stage after gate 3, or when the user says "build the graphics" or "render the overlays".
---

# mc-graphics

The approved beat table comes in; rendered overlays go out. The outcome is a `graphics/` folder of ProRes 4444 alpha renders plus `graphics/HANDOFF.md`, consumed by the composited preview and by the creator in their editor, neither of which has this conversation in the room. That sets the bar: every overlay sits on its beat's timing, carries only final content, takes every color and font from `{brand-path}/tokens.json`, and has passed both `render_verify.py` and your own eyes before the creator sees it. This is the expensive stage, so nothing here is a draft.

## Resolution rules

- Bare paths resolve against `{video-path}`, the current video project at `{projects-path}/<slug>/`.
- `{skill-root}` → this skill's installed directory; files in it always carry it (`{skill-root}/references/motion-recipes.md`).
- `{project-root}` → the project working directory.
- `{skill-name}` → the skill directory's basename.

## On Activation

1. Load the studio config (`uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`; empty means mc-setup has not run: stop and route the creator there). Resolve `paths` values against `{project-root}`.
2. Read `project.json` (confirm `approvals.beats` is a date, stage `graphics`), `beats/beats.md`, `beats/STORYBOARD.md`, the format profile, and `{skill-root}/engines/<engine>.md` for each engine the table names.
3. Read `{brand-path}/tokens.json`. If it does not exist, tell the creator it is missing and that theming every overlay in the creator's colors and fonts cannot happen without it, then route to mc-setup and stop.
4. Read `{brand-path}/production-bible.md`. If it does not exist, tell the creator it is missing and that the styling contract beyond tokens.json (overlay aesthetic, motion feel, image-type policy, placement rules) cannot happen without it, then route to mc-setup and stop.
5. Confirm the beat table passed its anchor placement gate: `beats/anchor-check.json` exists and reports `"ok": true`. Missing or failing is a stop, hand back to mc-beats. Every overlay here is positioned by a beat time, so authoring against unverified times spends the expensive stage on graphics that land off their phrases. This skill never runs mc-beats' scripts; it only checks the artifact.

## Engine workspaces

Engine workspaces live at `{engines-path}/<engine>/`; initialize on first use per the engine README, installing the latest published version at that moment and recording what it resolved. For HyperFrames, refresh its Agent Skills now (`npx hyperframes init`); mc-setup installs them, but install them here if setup was skipped or predates them (`npx skills add heygen-com/hyperframes --all`, or `npx hyperframes skills update` for the core set).

## Source before authoring

For each beat, reach first for a fitting HyperFrames block or installed skill across the whole catalog and its footage-facing effects (`npx hyperframes catalog --query "<the beat in plain words>"`, then `npx hyperframes add`; existing brand-themed blocks in the engine workspace), per `{skill-root}/engines/hyperframes.md`. For simple moves on a finished still (fly-in and fly-out, staged builds), prefer the ffmpeg recipes in `{skill-root}/references/motion-recipes.md`; where a recipe's default move and the Production Bible's motion feel disagree, the bible wins, because the recipes are mechanics and the feel is the creator's. Author from scratch via the html lane (`{skill-root}/engines/html.md`) or the design-prompting loop (`{skill-root}/engines/design-prompting.md`) only when nothing fits. Everything themes through tokens.json; no hardcoded colors or fonts.

## Build and verify

Build per engine in the project's `graphics/` folder, running the loop: edit, `npx hyperframes lint`, `npx hyperframes check` (layout, contrast, and text behind transparent video; scope it with `--range start,end` while iterating), `npx hyperframes snapshot` for key frames, preview, draft render (`--quality draft`), single-frame verify, final render. The shipped toolkit does the mechanical parts: `{skill-root}/scripts/html_to_png.py` (exact-size HTML render, separate `--guides` pass, alpha verify) and `{skill-root}/scripts/snug_frame.py` (native-aspect photo framing).

Verify every final render with `uv run {skill-root}/scripts/render_verify.py`, passing expectations explicitly: `--pixfmt` per the delivery target, `--expect-dur` from the beat's dur, `--expect-fps` and `--expect-res` from the format profile, or `--meta` pointing at the comp's meta.json render contract carrying the same keys. Then look at the extracted frames over checkerboard; a render without checked frames is not done.

When a composition calls for a whoosh, hit, chime, or bed (the animation-feel conventions in the Production Bible say when), route through the mc-audio service skill, never into its folder, and deliver the wav into `graphics/` next to the overlay it belongs to, with its timing noted in HANDOFF.md.

## Self-review before the creator sees anything

Zoom-inspect every asset in the batch: read every string, check edges and alpha fringes, and hold each one against the Production Bible's aesthetic language. Fix what fails before presenting.

## Handoff and advance

Write `graphics/HANDOFF.md`: per beat, the rendered file, its timeline position (from the beat table), track suggestion, and any editor notes.

`graphics/` now holds rendered overlays, so `renders/preview.mp4` must re-render with them composited. That render belongs to mc-cut, and this skill never runs another skill's scripts: hand back to mc-pipeline, which routes through mc-cut's composited preview re-entry before the next stage skill runs. The same hand-back applies whenever a later overlay fix re-renders anything in `graphics/`.

Then update project.json artifacts, advance stage per the profile (usually `assets`, or `package` where assets is absent), and report, naming the composited preview hand-back so it is not skipped.

## Rules

- The beat table is law. A composition that wants different timing goes back through the creator, not silently changed.
- Overlay exports are ProRes 4444 with alpha and a transparent root; anything else is a bug. render_verify.py fails an alpha render whose sampled frames are all fully opaque.
- Relevance gate: a viewer pausing at the anchor frame must see WHY this graphic is on screen. One that needs the storyboard to explain it fails.
- Transcript fact-check: any claim-bearing graphic (numbers, dates, quotes, names, titles, announcements) is checked against the transcript verbatim before render. Never invent an announcement, statistic, or quote the speaker did not say.
- Never guess external references. Video IDs, channel names, people's names, and spellings come from a source, from the project's people-glossary when one exists, or from asking the creator.
- Deliverable images never contain placeholder or helper text: no lorem ipsum, no "TODO", no safe-zone markers, no annotation arrows. Guides go in a separate `--guides` render plus a written spec.
- QC sweep: when the creator corrects one asset, treat the correction as a defect CLASS. Audit the whole library for the same defect and fix every instance before re-rendering anything.
- Never shrink or letterbox the source video to make room for graphics. Composite over the full frame in detected safe zones (find the talking-head region and place around it); photos get snug native-aspect frames, never uniform letterboxed panels.
- New reusable compositions get promoted to the engine workspace and noted in the format profile's Templates section.
