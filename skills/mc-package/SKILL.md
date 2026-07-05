---
name: mc-package
description: Produce 3 title+thumbnail packages, the description, and chapters for a Manticore project. Use at the package stage (may start any time after gate 1, since the packaging promise exists from the outline).
---

# mc-package

Packaging pays off the promise approved at gate 1; it is not invented fresh here.

## Steps

1. Load the studio config (`uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`; empty means mc-setup has not run: stop and route the creator there) and this skill's own surface (`uv run {project-root}/_bmad/scripts/resolve_customization.py --skill {skill-root}`; run `{workflow.activation_steps_prepend}` now, `{workflow.activation_steps_append}` after this step, and hold `{workflow.persistent_facts}` as standing context). Resolve `paths` values against `{project-root}`. Read `project.json` (confirm `approvals.outline` is an ISO date; if null or `"pending"`, stop and route the creator back to the outline gate), `outline.md` (the packaging promise), `script.md`, the final transcript if the cut exists, the format profile, and `{brand-path}/` (tokens.json, headshots/).
2. Titles: 3 candidates that pay off the approved promise. Under 60 characters, front-loaded, no clickbait the video cannot cash.
3. Thumbnails: 3 concepts as prompt + layout notes; generate drafts via the creator's configured image lane (a `[[tools]]` CLI or the API provider), using `{brand-path}/headshots/` for face consistency when the creator is the subject. Rules: 3 words max on-image, readable at 120px wide, brand palette, face+emotion when the creator is the subject. Files to `packaging/thumbs/`.
4. Description: first 2 lines carry the hook and the search terms (they show before the fold); then the creator's links from `[owner] links` in the config, in order; then chapters.
5. Chapters: from the edited transcript's beat boundaries; first chapter 0:00, honest labels, no keyword stuffing. If the cut does not exist yet (early run), chapters are pending: skip this step and the description's chapter block, and tell the creator to re-run mc-package after the cut to finish them.
6. Write `packaging/titles.md`, `packaging/description.md`, and `packaging/chapters.md` (only when chapters were produced); update `artifacts` in project.json. If the project's stage is `package` and chapters are done, append `package` to `stages_done` and set `stage` to the next stage in the project's `stages`; on an early run, leave `stage` and `stages_done` untouched. Recommend which package to A/B against which via YouTube's native Test & Compare.

## Checklist

- Every title pays off something the video actually delivers.
- Thumbnail text and title do not repeat each other (they share attention, not words).
- Run `uv run {skill-root}/scripts/lint_script.py <file> --blacklist {brand-path}/blacklist.md` on titles.md and description.md.
