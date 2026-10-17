---
name: mc-cut
description: Cut raw takes into an approved, rendered edit. Use at the cut stage with recordings in raw/, or when the user says "cut the takes", "make the cutplan", "render the preview", or "render the final".
---

# mc-cut

Act as the creator's editor. The outcome is an approved cut: `cut/edl.json`, plus the cutplan, editorial review, preview render and editor timeline built from it.

Three consumers set the bar. The creator at gate 2 must be able to accept or reject every call without re-watching the raw footage. Their editor must import the timeline in sync. mc-beats builds visuals on the edited transcript and the editorial review. This stage owns gate 2 on the cutplan and the offered final render at gate 4.

## The rule that is not inferable

The TRANSCRIPT is the authority on CONTENT (which words, in what order). The AUDIO is the authority on TIMING (where silence is, and therefore where a cut is safe). Never derive a cut time, a beat time, or a silence from transcript timestamps: parakeet absorbs pauses into the preceding word's end, so transcript gaps read about 0.0 across real dead air and word ends reach past the sound.

Everything else in this stage follows from that, and from one convention: a check this stage claims to perform is a script that exits non-zero.

## Resolution rules

- Bare paths resolve against `{video-path}`, the current video project at `{projects-path}/<slug>/`.
- `{skill-root}` → this skill's installed directory; files in it always carry it (`{skill-root}/references/rendering.md`).
- `{project-root}` → the project working directory.

## On Activation

1. Load the studio config: `uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`. Empty means mc-setup has not run; stop and route the creator there. Resolve `paths` values against `{project-root}`.
2. Read `project.json` (stage `cut`) and `script.md`.
3. Read `{brand-path}/production-bible.md`. If it does not exist, tell the creator it is missing and that judging the cut against their taste cannot happen without it, then route to mc-setup and stop. The Production Bible is the taste contract for the calls you make below.
4. Read `{brand-path}/voice-bible.md`. If it does not exist, tell the creator it is missing and that cadence-aware filler detection cannot happen without it, then route to mc-setup and stop.

## Prepare the sources

Every source in `raw/` passes preflight before anything reads it:

```
uv run {skill-root}/scripts/preflight.py raw/<take> [...] --remux --qc-frames cut/qc/
```

It is slow, so run it in the background and let transcription wait on it. Record the reported `cfr_master` in `project.json` `sources`; every later step reads that path, never the VFR original, because the two have different frame timing and the desync only shows up once the creator scrubs the timeline.

Exit 3 is source QC failing, and it is a hard stop: do not transcribe, cut, or render against it. A false `disk.ok` is also a stop. For either, and for the spatial fix, load `{skill-root}/references/source-prep.md`.

## Transcribe and verify

Needs the CFR master from the previous section.

Pick the lane from `{skill-root}/references/transcription.md` (published sources take captions, not local ASR), then build the audio map and prove the transcript:

```
uv run {skill-root}/scripts/analyze_audio.py raw/<take> -o cut/audio-map.json --noise <[cut] silence-floor-db>
uv run {skill-root}/scripts/verify_transcript.py transcript/words.json --audio-map cut/audio-map.json --wpm <[owner] wpm> -o cut/transcript-check.json
```

The audio map is the timing source of truth for the whole stage, built once per source.

A non-zero exit from `verify_transcript.py` is a HARD STOP: it finds audio above the silence floor that produced no words and names the regions. Nothing may be built on a transcript that has not passed, because downstream a hole in the transcript looks exactly like dead air and the cut deletes real content. `{skill-root}/references/transcription.md` carries the override for a region the creator has listened to and confirmed.

Every lane windows in 20s isolated windows with 3s overlap. This is not a tuning knob, and never raise `--window` to go faster: parakeet drops whole paragraphs inside long windows with no error at all. Measured: 120s chunks lost three paragraphs, 90s still lost content, 20s was complete.

## Propose the cut

Needs a passing transcript and the audio map. This section ends at gate 2.

```
uv run {skill-root}/scripts/cutplan.py transcript/words.json --audio-map cut/audio-map.json --voice-bible {brand-path}/voice-bible.md -o cut/candidates.json
```

Plus `[cut] cutplan-flags` from the studio config. It finds the mechanical candidates and snaps each edge into an audio-verified silence. Two things it does that are easy to undo by accident: the voice bible's `cadence` block marks the connective words that are the creator's rhythm, so those are keeps and not filler; and on an `interview` source each spoken interviewer question becomes a `marker` candidate, where the marker and question go and the answer stays. Anything reported `unsnapped` never reached a silence and needs an ear.

Judge the candidates against `script.md` and the Production Bible, then write `cut/edl.json` as `{source, source_duration, fade_ms: 30, pad_ms: 60, segments: [...]}` with ordered segments of `{source, start, end, beat, quote, reason}`. Prove it:

```
uv run {skill-root}/scripts/verify_edl.py cut/edl.json --audio-map cut/audio-map.json --words transcript/words.json -o cut/edl-check.json
```

A non-zero exit is a HARD STOP: it fails any boundary not resting in an audio-verified silence, and any segment missing its quote or reason. Re-run it after every EDL change.

Then reconstruct what the viewer will actually hear, and read it as an argument:

```
uv run {skill-root}/scripts/edited_transcript.py transcript/words.json --edl cut/edl.json -o cut/edited-transcript.md -j cut/edited-words.json
```

Both clean and source timecodes come from here; never convert between them by hand. Run the editorial pass on that transcript per `{skill-root}/references/editorial-pass.md`, writing `cut/editorial-review.md` from `{skill-root}/assets/editorial-review-template.md`. Nothing it recommends is auto-applied. RE-RECORD items are the one exception to "the cut applies the calls": there is no pickup re-entry path, so they hand over as a shoot list and the cut proceeds without them.

Write `cut/cutplan.md` carrying both tiers, each call with its timestamp and the quoted words. Routine silence trims group into one line. Always itemize section re-reads, bloopers and every content-tier recommendation, whatever their size.

Then build the VIRTUAL TIMELINE and give the creator that to watch. Do NOT render a file for gate 2.

```
uv run {skill-root}/scripts/edl_to_ffconcat.py cut/edl.json -o cut/preview.ffconcat --source renders/proxy/<source>-<height>p-intra.mp4
```

They watch it with no render at all:

```
ffplay -f concat -safe 0 -i cut/preview.ffconcat
```

When `[editor] name` is `resolve`, also put the cut in Resolve so the creator can review it there. Export the fcpxml, then import it as a new timeline with blue markers at beat changes and red markers at large cuts:

```
uv run {skill-root}/scripts/edl_to_fcpxml.py cut/edl.json -o cut/rough.fcpxml
uv run {skill-root}/scripts/resolve_import.py cut/rough.fcpxml --edl cut/edl.json --project <slug> -o cut/resolve-import.json
```

Exit 1 means the Resolve timeline is out of sync with the EDL: report the first mismatch and do not offer that timeline. Exit 4 means Resolve Studio is not reachable: say so, name the fix it printed, and continue with the virtual timeline; the fcpxml still imports by hand.

Set `approvals.cutplan = "pending"`, present it, and STOP for gate 2.

## Apply the approved calls

Write the creator's decisions to `cut/approved-spans.json` as `{start, end, quote, reason}`, with times re-detected against the audio, because transcript-read timecodes drift and a blind apply cuts the wrong spans. Then snap them mechanically. Never snap an edge by hand; an eyeballed snap is how a cut lands inside a word.

```
uv run {skill-root}/scripts/snap_spans.py cut/approved-spans.json --audio-map cut/audio-map.json -o cut/snapped-spans.json
```

Back up the prior EDL to `cut/edl.pre-editorial.json`, rewrite `cut/edl.json` from the snapped spans, re-run `verify_edl.py`, and append the APPLIED section to `cut/editorial-review.md`.

## THE EDL CHANGED, SO EVERYTHING DERIVED FROM IT IS NOW A LIE

The moment `cut/edl.json` is rewritten, every artifact built from the previous one is stale: the virtual timeline, the FCPXML, the edited transcript, the preview, the boundary frames, and — once the beats stage has run — **every beat anchor in `beats/beats.md`**. Nothing on disk announces this. A stale FCPXML imports cleanly. A stale beat table renders overlays that land off their phrase, and that is discovered after the graphics are paid for.

So regenerate the whole derived set together, in this order, every single time the EDL changes:

```
uv run {skill-root}/scripts/verify_edl.py cut/edl.json --audio-map cut/audio-map.json --words transcript/words.json -o cut/edl-check.json
uv run {skill-root}/scripts/edited_transcript.py transcript/words.json --edl cut/edl.json -o cut/edited-transcript.md -j cut/edited-words.json
uv run {skill-root}/scripts/edl_to_ffconcat.py cut/edl.json -o cut/preview.ffconcat --source <intra proxy>
uv run {skill-root}/scripts/edl_to_fcpxml.py cut/edl.json -o cut/rough.fcpxml
```

When `[editor] name` is `resolve`, re-run `resolve_import.py` as above. It adds a new timeline with a " vN" suffix and never touches the earlier ones, so tell the creator which timeline is current.

`verify_edl.py` runs FIRST and its non-zero exit stops the rest: never regenerate derived artifacts from an EDL that has not re-passed.

**If `beats/beats.md` exists, the beat table is now suspect.** Beat times are derived from the EDL, so a cut applied after the beats stage moves every anchor downstream of it. Re-run mc-beats' anchor check and report the result to the creator before any graphics work continues:

```
uv run {skill-root}/../mc-beats/scripts/verify_anchors.py beats/beats.md --edl cut/edl.json --words transcript/words.json -o beats/anchor-check.json
```

A non-zero exit means the beat table must go back to mc-beats before mc-graphics runs again. Say so plainly rather than letting the graphics stage build against moved anchors.

## Deliver

**Do not render a file to review a cut.** The virtual timeline above is what the creator watches at gate 2 and after every re-approval: it costs nothing, it is frame-exact, and it carries video AND audio so A/V sync is reviewable. Rendering a full preview before a single call has been approved is the single most expensive habit this stage ever had — on a 379-segment 16-minute cut it was 22 minutes, paid before the creator had said yes to anything.

Render a FILE only when something actually needs a file: a composited preview once overlays exist, something to upload or share, or the gate-4 final. `{skill-root}/references/rendering.md` carries the commands, the config wiring and the staleness check.

Whenever a file does get rendered, inspect the boundary frames for what they can see, black frames and straddles, up to 3 retries per cut. They see less than they appear to: on the corrupted project every frame looked clean while the cut underneath was built on the hole.

Chapters or log notes written against source timecode remap onto the edited timeline with `uv run {skill-root}/scripts/remap_timecode.py cut/edl.json --direction orig-to-clean --chapters <file> -o <out>`, and `--direction clean-to-orig` maps back.

Record the ISO date in `approvals.cutplan`, append `cut` to `stages_done`, and set `stage` to the next entry in project.json's `stages` array.

## Routed re-entries

Two entry points run after the `cut` stage has closed. Both touch no gates, approvals or stage fields.

Composited preview: mc-pipeline routes here once mc-graphics writes `graphics/HANDOFF.md`, and again whenever an overlay is re-rendered. Re-render the preview composited per `{skill-root}/references/rendering.md`, report any `overlays_missing`, present it, and stop.

Final render: when the project reaches the final stage, offer the final-quality render per `{skill-root}/references/rendering.md`. Finishing in the creator's own editor from the exported timeline is an equally supported path; either closes gate 4.

## Cutting rules (non-negotiable)

- Never cut inside a word. Edges land inside an audio-verified silence, which makes this structural rather than aspirational. Pad 30 to 200 ms.
- **Quantize every EDL boundary to the source frame grid** (`round(t * fps) / fps`) before writing `cut/edl.json`. Boundaries come from the audio map at 0.1s granularity and from word timestamps, so they are sub-frame by default; ffmpeg rounds each `trim` to the nearest frame and the error ACCUMULATES. A 379-segment cut rendered 954.766s against an expected 952.460s and the output gate correctly refused to publish it — the cut was fine, only the arithmetic disagreed. Past roughly 150 segments this happens every time. Max shift from quantizing is half a frame (16.67ms at 30fps), far inside the 200ms silence pad, and `verify_edl.py` re-passes. Clamp any boundary that rounds past `source_duration`.
- **Truncate a TAIL silence, never tighten it.** The trailing silence after the last word is not a gap between two utterances. Tightening it at both ends leaves a junk segment scraped off the absolute end of the file, past the last detected silence and past `source_duration` once frame-aligned. End the final segment on a real tail (roughly 0.5s after the last word), inside the verified silence.
- 30 ms audio fades on every cut boundary (`fade_ms` in the EDL).
- Never shrink or letterbox the source video to make room for graphics; overlays composite over the full frame in safe zones. Nothing enforces this one, and the beats and graphics stages inherit whatever canvas this stage leaves them.

## Gates

| Gate | Script | Where |
|---|---|---|
| Source QC | `preflight.py` (exit 3) | Prepare the sources |
| Transcript completeness | `verify_transcript.py` | Transcribe and verify |
| Cut integrity | `verify_edl.py` | Propose the cut, and again after applying |
| Output integrity | `render_preview.py` / `render_final.py` | Deliver |
| Resolve timeline sync | `resolve_import.py` | Propose the cut, when `[editor] name` is `resolve` |

Three things no script can check, so they are on you:

- Listen to the joins in the preview. A clipped word onset is inaudible in a still, and boundary frames cannot hear it.
- Check any span reported `unsnapped` by ear before it goes into the EDL.
- Verify FCPXML sync in the editor on the first project this converter touches, unless `resolve_import.py` already checked it.
