---
name: mc-new
description: Scaffold a new Manticore video project from a format profile. Use when the creator greenlights an idea ("new video", "start a project", "let's make the X video").
---

# mc-new

## Steps

1. Load the studio config (`uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`; empty means mc-setup has not run: stop and route the creator there) and this skill's own surface (`uv run {project-root}/_bmad/scripts/resolve_customization.py --skill {skill-root}`; run `{workflow.activation_steps_prepend}` now, `{workflow.activation_steps_append}` after this step, and hold `{workflow.persistent_facts}` as standing context). Resolve `paths` values against `{project-root}`.
2. Establish slug (kebab-case), format (must match a profile in `{formats-path}/`), and working title.
3. Run: `uv run {skill-root}/scripts/new_project.py <slug> --format <format> --title "<title>" --projects-dir {projects-path} --formats-dir {formats-path}` (add `--parent <slug>` for a short cut from a long-form parent).
4. Fill `brief.md`: one paragraph of the idea in the creator's words, why now, and links to source material (idea notes, prior material). Do not invent content; ask if the brief is thin.
5. Report the created project and hand off: next stage is almost always `braindump` (mc-braindump).

## Checklist

- Slug is kebab-case and not already taken.
- Format profile exists and its stage list landed in `project.json`.
- brief.md links back to wherever the idea came from so its history stays findable.
