---
name: mc-beats
description: Build the graphics beat table (id, timing, anchor words, composition per beat) from the approved cut, then STOP for gate 3 approval. Use at the beats stage. Never writes graphics code.
---

# mc-beats

Gate 3. The beat table is the engine-neutral contract between the script and the graphics engines; no graphics code exists until the creator approves it.

## Steps

1. Load the studio config (`uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`; empty means mc-setup has not run: stop and route the creator there) and this skill's own surface (`uv run {project-root}/_bmad/scripts/resolve_customization.py --skill {skill-root}`; run `{workflow.activation_steps_prepend}` now, `{workflow.activation_steps_append}` after this step, and hold `{workflow.persistent_facts}` as standing context). Resolve `paths` values against `{project-root}`. Read `project.json` (confirm `approvals.cutplan` is a date, stage is `beats`), `script.md`, `cut/edl.json`, `transcript/`, and the format profile (which beat types this format uses and its density target).
2. Walk the EDITED timeline (times derive from edl.json, not the raw take). For every moment that earns a graphic, add a row: id, start, dur, end, anchor word with its transcript timestamp, the spoken phrase it rides on, and the composition (named registry block or a one-line description).
3. Mark each row's engine per the format profile defaults; note rows needing farmed assets (they become the mc-assets shopping list).
4. Write `beats/beats.md` (the table) and `beats/STORYBOARD.md` (one short paragraph per beat: what the viewer sees, in plain words).
5. Update `artifacts` in project.json (`"beats": "beats/beats.md"`, `"storyboard": "beats/STORYBOARD.md"`), set `approvals.beats = "pending"`, present the table, and STOP for gate 3.
6. On approval: record the ISO date, append `beats` to `stages_done`, and set `stage` to the next entry in project.json's `stages` array.

## Checklist

- Every beat has an anchor word that exists in the transcript at that timestamp.
- No overlapping beats unless the composition is explicitly layered.
- Density matches the format profile; justify outliers in STORYBOARD.md.
- Generated-asset rows are absent in formats where `generated_broll: banned`.
