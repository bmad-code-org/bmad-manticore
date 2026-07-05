---
name: mc-graphics
description: Execute the approved beat table in HyperFrames/Remotion/OGraf, render, frame-verify, and deliver alpha overlays plus a HANDOFF for the creator's editor. Use at the graphics stage, only after gate 3 (beats) is approved.
---

# mc-graphics

## Steps

1. Load the studio config (`uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`; empty means mc-setup has not run: stop and route the creator there) and this skill's own surface (`uv run {project-root}/_bmad/scripts/resolve_customization.py --skill {skill-root}`; run `{workflow.activation_steps_prepend}` now, `{workflow.activation_steps_append}` after this step, and hold `{workflow.persistent_facts}` as standing context). Resolve `paths` values against `{project-root}`. Read `project.json` (confirm `approvals.beats` is a date, stage `graphics`), `beats/beats.md`, `beats/STORYBOARD.md`, `{brand-path}/tokens.json`, the format profile, and `{skill-root}/engines/<engine>.md` for each engine the table names. Beats marked OGraf route through the mc-ograf skill, and ONLY if `[editor] ograf-editable = true`; otherwise build them as baked alpha overlays like everything else (baked alpha works in every editor).
2. Engine workspaces live at `{engines-path}/<engine>/`; initialize on first use per the engine README (pin versions).
3. Source before authoring: for each beat, check the engine registry/library for a fitting block (`npx hyperframes add`, existing brand-themed blocks in the engine workspace); author from scratch only when nothing fits. Everything themes through tokens.json, no hardcoded colors or fonts.
4. Build per engine in the project's `graphics/` folder. Follow the loop: edit, lint, preview, draft render (CRF 28), single-frame verify, final render.
5. Verify every final render with `uv run {skill-root}/scripts/render_verify.py`, passing expectations explicitly: `--pixfmt` per the delivery target, `--expect-dur` from the beat's dur, `--expect-fps` and `--expect-res` from the format profile (extracted frames visually checked over checkerboard for alpha). A render without checked frames is not done.
6. Write `graphics/HANDOFF.md`: per beat, the rendered file, its timeline position (from the beat table), track suggestion, and any editor notes (OGraf items go in as editable graphics, not MOVs).
7. Update project.json artifacts, advance stage per the profile (usually `assets`, or `package` where assets is absent), and report.

## Rules

- The beat table is law. A composition that wants different timing goes back through the creator, not silently changed.
- Overlay exports are ProRes 4444 with alpha; anything else is a bug (except OGraf, which ships as its own editable format).
- New reusable compositions get promoted to the engine workspace and noted in the format profile's Templates section.
