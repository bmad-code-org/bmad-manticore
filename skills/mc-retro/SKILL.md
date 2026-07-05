---
name: mc-retro
description: Post-publish feedback ratchet. Takes the creator's notes on a finished video and edits the format profile, blacklist, and offending skill files so the pipeline compounds. Use after a video ships or whenever the creator gives pipeline feedback.
---

# mc-retro

The compounding mechanism: feedback edits FILES, not just memory. Every note improves the taste files the next run obeys.

## Steps

1. Load the studio config (`uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`; empty means mc-setup has not run: stop and route the creator there) and this skill's own surface (`uv run {project-root}/_bmad/scripts/resolve_customization.py --skill {skill-root}`; run `{workflow.activation_steps_prepend}` now, `{workflow.activation_steps_append}` after this step, and hold `{workflow.persistent_facts}` as standing context). Resolve `paths` values against `{project-root}`. Read `project.json` (stage `retro`) and collect the creator's notes: what felt wrong, what they re-edited in their editor, what they rewrote in the script, packaging performance (Test & Compare results if run).
2. Route every note to the file that would have prevented it:
   - voice/wording miss: `{brand-path}/voice-bible.md` (new rule with the verbatim example) and/or a new pattern in `{brand-path}/blacklist.md`,
   - structural/retention miss: the Learnings section of `{formats-path}/<format>.md` (format from project.json; ISO-dated, newest first),
   - a stage doing the wrong thing: that skill's SKILL.md (note: skill edits apply to the installed module and may be overwritten by module updates; prefer profile/brand files when the note fits there; if the harness blocks access to another skill's folder, record the note in the format profile's Learnings instead),
   - a tool being driven wrong: the `notes` field of that tool's `[[tools]]` entry in the studio config (`[modules.manticore]` in `{project-root}/_bmad/custom/config.toml`),
   - a mechanical failure: an issue note in the relevant engine README or script docstring.
3. Make the edits. Small and surgical: one note, one edit, at the point of failure. Show the creator the diff summary.
4. If the cut stage's judgment was overridden repeatedly in the editor, mine the pattern (e.g. "always keep pre-demo breaths") into the format profile's Learnings.
5. In project.json, append `retro` to `stages_done` and set `stage` to `done`, append retro notes to its `notes` field, and report.

## Rules

- One round of notes per session; do not fish for endless feedback.
- Never weaken a gate or remove a hard rule in response to convenience feedback; flag those for the creator explicitly.
- Blacklist and Learnings only grow; deletions are the creator's call.
