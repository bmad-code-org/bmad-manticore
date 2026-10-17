---
name: mc-beats
description: Riff visuals, then build the graphics beat table. Use at the beats stage after gate 2, or when the user says "plan the graphics", "beat table", or "what visuals go here".
---

# mc-beats

Act as the creator's graphics planner. The outcome is an approved beat table: `beats/beats.md` plus `beats/STORYBOARD.md`.

The table is the engine-neutral contract between the script and the graphics engines, and no graphics code exists until the creator approves it at gate 3. Three consumers set the bar. The creator must be able to picture every beat from its storyboard paragraph alone. mc-assets farms from the `asset` column. mc-graphics renders from `type`, `engine`, and `composition` with nobody in the room to ask what was meant.

Read `{skill-root}/references/density-and-creativity.md` (overlay taxonomy, transcript triggers, tier character, pacing curve) and `{skill-root}/references/cta-placement.md` (zones, caps, spacing) in full before planning any beats.

## The rule that is not inferable

NEVER ESTIMATE A BEAT TIME. Every `start` is DERIVED: take the anchor word's timestamp from `transcript/words.json` (original source time) and remap it through `cut/edl.json` onto the clean edited timeline. An eyeballed time that looks right in the table is a graphic that lands off its phrase in the render, discovered after the graphics work is already paid for.

## Resolution rules

- Bare paths resolve against `{video-path}`, the current video project at `{projects-path}/<slug>/`.
- `{skill-root}` → this skill's installed directory; files in it always carry it (`{skill-root}/references/cta-placement.md`).
- `{project-root}` → the project working directory.

## On Activation

1. Load the studio config: `uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`. Empty means mc-setup has not run; stop and route the creator there. Resolve `paths` values against `{project-root}`.
2. Read `project.json` (confirm `approvals.cutplan` is a date and stage is `beats`), `script.md`, `cut/edl.json`, `transcript/`, `cut/editorial-review.md`, and the format profile at `{formats-path}/<format>.md`. Gate 2 has passed, so the editorial review exists; if it does not, hand back to mc-cut.
3. Read `{brand-path}/production-bible.md`. If it does not exist, tell the creator it is missing and that the medium mix, the composition ambition below, and the density and variety numbers this plan is held to cannot happen without it, then route to mc-setup and stop.
4. Read `{brand-path}/tokens.json`. If it does not exist, tell the creator it is missing and that brand-themed beats cannot happen without it, then route to mc-setup and stop.
5. Fix this plan's vocabulary and budget from the format profile frontmatter: `beat-types` is the whole type vocabulary for the format, and `density` maps tiers to seconds-per-beat budgets. The tier is `graphics-frequency` in `[style]` of the studio config (`medium` when unset), unless the profile frontmatter overrides it.

## Riff before you plan

Pitch your strongest ideas before writing any table, and ask what the creator already pictured. The hand-to-beats items in `cut/editorial-review.md` are moments gate 2 already settled need a visual rather than a cut, so lead with them: they are decided work, not pitches. The medium mix comes from the Production Bible and this conversation, never from habit.

## Build the table

Walk the EDITED timeline (times derive from `cut/edl.json`, not the raw take). Scan the transcript with the trigger heuristics in `{skill-root}/references/density-and-creativity.md`, and for every moment that earns a graphic add a row.

Propose the most visually ambitious composition the Production Bible allows before settling for less: the creator can downgrade a diagram to a card in seconds, but cannot upgrade a card to a diagram without doing the planner's job for it. Escalate the treatment to the content, so a number gets a stat treatment, a process gets a staged diagram, and a comparison gets a split or table build.

Across the plan as a whole, hold the numbers in the Production Bible's visual density and variety section: type variety, the cap on static text cards, and the beats-per-minute floor for the resolved tier, which times the edited runtime in minutes gives the minimum beat count. Resolve them for this format first, since a per-project-type section in the bible overrides the global one. Nothing scripted checks them, so they are yours to hold, and the first 30-60 seconds run at roughly double density per the pacing curve.

`engine` comes from the format profile defaults and PIPELINE.md's engine policy. A profile that still names `remotion` in its `engine_overlays`/`engine_stingers` frontmatter (a studio configured before 3.0.0) is written into the table as `hyperframes` per the engine policy's compatibility alias, never as `remotion`. Rows needing farmed assets carry the asset id in `asset` and become the mc-assets shopping list; all other rows carry `null`.

## The CTA pass

Read `[cta]` (inventory and appetite) from the studio config, scan the transcript for verbal CTAs and payoff seams, and plan `cta` beats within the zones, caps, and spacing in `{skill-root}/references/cta-placement.md`. CTA rows join the same table with timestamps, anchors, and rationale, approved at gate 3 like any other beat. No overlay beats in the final 20 seconds unless they ARE the end card. When the inventory includes a next-video or end-card item, optionally add an end-card beat themed from `{brand-path}/tokens.json`.

## Beat table format

One row per beat, per PIPELINE.md's engine-neutral contract:

| id | start | dur | end | anchor word | anchor ts | spoken phrase | type | engine | asset | composition |
|---|---|---|---|---|---|---|---|---|---|---|

`composition` is a named registry block or a one-line description. Before describing one, search the HyperFrames registry for the beat (`npx hyperframes catalog --query "<the beat in plain words>"`) and name the block when one fits. mc-beats always writes every column. The reserved placeholder `overlay` exists only for READING a legacy 0.x table under PIPELINE.md's tolerance rule (missing `type` reads as `overlay`, missing `engine` is the engine-policy default, missing `asset` is `null`); the revised table is written with all columns filled and every `overlay` replaced by a type from the profile's `beat-types`.

## Write and verify

`beats/beats.md` carries the table. `beats/STORYBOARD.md` gives each beat one short paragraph that doubles as a design brief: what the viewer sees, the motion character (how it enters, moves, and exits), and the anchor phrase it rides on, in plain words a design tool could execute from. It also carries what the table has no column for, and what the creator would otherwise have to ask about at the gate: any stretch exceeding the tier's seconds-per-beat budget, any specific ask from the riff or hand-to-beats item that did not become a row, any missing CTA.

Then the anchor gate:

```
uv run {skill-root}/scripts/verify_anchors.py beats/beats.md --edl cut/edl.json --words transcript/words.json -o beats/anchor-check.json
```

It independently re-derives every beat's time from its anchor word and fails on any beat that does not land within 0.5s of it, on any anchor that is not in the transcript, and on any anchor sitting in a span the cut removed. A non-zero exit is a hard stop: fix the rows it names and re-run. Do not present the table, and do not let graphics be rendered, against a table that has not passed.

## Before you present

verify_anchors.py covers anchor placement and nothing else. These are the checks nobody else makes:

- No overlapping beats unless the composition is explicitly layered.
- One visual system across the whole plan: every composition conforms to the Production Bible's overlay style and animation language.
- Every asset-bearing row respects the Production Bible's image-type policy (diagrammatic vs generative vs real) for its purpose.
- No generated-asset rows in formats whose profile bans generated b-roll.

## Gate 3

Update `artifacts` in project.json (`"beats": "beats/beats.md"`, `"storyboard": "beats/STORYBOARD.md"`), set `approvals.beats = "pending"`, present the table, and STOP. Gate 3 is a hard stop; only the creator's explicit approval moves it. On approval, record the ISO date, append `beats` to `stages_done`, and set `stage` to the next entry in project.json's `stages` array.
