---
name: mc-cut
description: Turn raw takes into a cut plan, edl.json, and an editable timeline (FCPXML) for the creator's editor. Presents the taste calls for gate 2 approval. Use at the cut stage once recordings are in raw/.
---

# mc-cut

The Descript replacement. Ends in a trimmable timeline in the creator's editor, NEVER a baked mp4.

## Steps

1. Load the studio config (`uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`; empty means mc-setup has not run: stop and route the creator there) and this skill's own surface (`uv run {project-root}/_bmad/scripts/resolve_customization.py --skill {skill-root}`; run `{workflow.activation_steps_prepend}` now, `{workflow.activation_steps_append}` after this step, and hold `{workflow.persistent_facts}` as standing context). Resolve `paths` values against `{project-root}`. Read `project.json` (stage `cut`), `script.md`, and the cutting rules below. Verify every file in `raw/` is constant frame rate (ffprobe); re-mux with ffmpeg if not.
2. Transcribe each take: `uv run {skill-root}/scripts/transcribe.py raw/<take> -o transcript/words.json --provider <[transcription] provider from the config>` (suffix the output `<source-id>.words.json` when the project has multiple sources). Default provider parakeet-mlx runs local and free; the model downloads once on first run.
3. Candidates: `uv run {skill-root}/scripts/cutplan.py transcript/words.json -o cut/candidates.json` plus any `{workflow.cutplan_flags}` finds silences, filler runs, stutters, and retakes mechanically. On an `interview` source (project.json `sources`), it also flags each spoken "question from Claude" read as a `marker` candidate: cut the marker and question, keep the answer.
4. Make the taste calls: against `script.md`, pick best takes, order segments, decide keep-or-cut on every candidate in `cut/candidates.json`. Write `cut/cutplan.md` as a short human-readable plan whose spine is the judgment calls, each with a timestamp and the quoted words (the "trailing 'so' at 42:20, keep or cut?" shape). Group the obvious silence trims into one line; itemize only what the creator might disagree with.
5. Write `cut/edl.json`: `{source, source_duration, fade_ms: 30, pad_ms: 60, segments: [...]}` with ordered segments of {source, start, end, beat, quote, reason} obeying the cutting rules below.
6. Set `approvals.cutplan = "pending"`, present cutplan.md, and STOP for gate 2.
7. After approval: export the timeline per `[editor] timeline-format` in the config: `fcpxml` via `uv run {skill-root}/scripts/edl_to_fcpxml.py cut/edl.json -o cut/rough.fcpxml` (Resolve, Final Cut; refuses VFR sources loudly); `xmeml`/`edl` are planned lanes (see TODO); `none` (Descript and manual workflows) skips export, and the deliverables are cutplan.md + edl.json + preview.mp4 as the cut map. Render the watch copy: `uv run {skill-root}/scripts/render_preview.py cut/edl.json -o cut/preview.mp4 --boundary-frames cut/boundaries/`, then inspect the boundary frames per the cutting rules below. If `[mcp] davinci-resolve` is true, offer `uv run {skill-root}/scripts/resolve_import.py --project <slug> --timeline-only`. Record the ISO date in `approvals.cutplan`, append `cut` to `stages_done`, and set `stage` to the next entry in project.json's `stages` array.

## Cutting rules (non-negotiable)

- Never cut inside a word. Pad cut edges 30 to 200 ms.
- 30 ms audio fades on every cut boundary.
- Every EDL segment records: source, start, end, the quoted words, and the reason for the cut.
- Self-verify: extract frames at each cut boundary and inspect; up to 3 retries per cut.
- Raw recordings must be constant frame rate (mitigates FCPXML desync). Check before transcribing; ffmpeg re-mux if needed.

## Checklist

- No cut lands inside a word (check edl times against word timestamps).
- Every edl segment has quote + reason.
- preview.mp4 watched (spot-check at minimum three boundaries) before declaring done.
- FCPXML sync verified in the editor on the first project this converter touches.
