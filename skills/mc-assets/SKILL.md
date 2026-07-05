---
name: mc-assets
description: Farm generated stills and b-roll clips via the configured providers (API lanes or the creator's CLI tools) for the beats that need them. Use at the assets stage. Never generates UI or text that must be accurate.
---

# mc-assets

## Steps

1. Load the studio config (`uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`; empty means mc-setup has not run: stop and route the creator there) and this skill's own surface (`uv run {project-root}/_bmad/scripts/resolve_customization.py --skill {skill-root}`; run `{workflow.activation_steps_prepend}` now, `{workflow.activation_steps_append}` after this step, and hold `{workflow.persistent_facts}` as standing context). Resolve `paths` values against `{project-root}`. Read `project.json` (stage `assets`), the asset rows flagged in `beats/beats.md`, and the format profile. If the profile says `generated_broll: banned`, stop and report; something upstream is wrong.
2. Pick the lane per `[assets]` in the config: an API provider (deterministic, cost-tracked, via `farm_asset.py`) or one of the creator's `[[tools]]` CLIs. For a CLI lane, follow that tool's `headless` invocation and `notes` EXACTLY as configured; the notes exist so you never have to rediscover how to drive the tool.
3. For each needed asset write a prompt: concrete subject, camera/framing, lighting, mood; brand-adjacent palette where it fits.
4. API lane: `uv run {skill-root}/scripts/farm_asset.py --kind image|video --prompt "..." --out-dir <resolved {projects-path}/<slug>/assets/>`, escalating to the configured escalation provider only for hero shots where realism must not wobble.
5. Review every result: reject and re-prompt gibberish text, warped hands/UI, off-mood results. Standing rule: generated footage never depicts UI or accurate text; real UI comes from screen recordings.
6. Confirm `assets/manifest.json` has a row per kept file (prompt, provider, cost if known); report total spend for the project.
7. Update project.json: append `assets` to `stages_done` and set `stage` to the next stage in its `stages` list.

## Checklist

- Every kept asset maps to a beat row; no orphan generations.
- Manifest complete, costs summed where the lane reports them.
- Nothing in assets/ contains readable UI or text meant to be accurate.
