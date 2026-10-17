---
name: mc-package
description: Produce titles, thumbnails, description, chapters, and metadata. Use at the package stage or any time after gate 1, or when the user says "title ideas", "thumbnail", or "package this".
---

# mc-package

Act as the creator's packaging partner. The outcome is the click surface for the video, under
`packaging/`: titles, thumbnails, description, CTA metadata, chapters, and captions.

Two consumers set the bar. A browse feed decides in a fraction of a second at thumbnail size, so
every candidate has to survive being small. The creator uploading has to be able to paste each
file straight into YouTube. Packaging pays off the promise approved at gate 1; it is never
invented fresh here.

## Resolution rules

- Bare paths resolve against `{video-path}`, the current video project at `{projects-path}/<slug>/`.
- `{skill-root}` → this skill's installed directory; files in it always carry it (`{skill-root}/references/cta-placement.md`).
- `{project-root}` → the project working directory.

## On Activation

1. Load the studio config: `uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`. Empty means mc-setup has not run; stop and route the creator there. Resolve `paths` values against `{project-root}`.
2. Read `project.json` and find the promise. When `stages` includes `outline`, `approvals.outline` must be an ISO date; null or `"pending"` stops the run and routes the creator back to the outline gate. Then read `outline.md` (the packaging promise) and `script.md`. When `stages` has no outline stage (footage-first and livestream projects), the promise comes from the footage: derive it from the final transcript and package only what the video actually delivers.
3. Read the final transcript if the cut exists, the format profile at `{formats-path}/<format>.md`, and from the studio config `[packaging]` (the caps and counts below), `[cta]` (inventory and appetite), `[owner]` `links` and `[editor]` `name`.
   The Resolve lane is on when `[editor] name` is `resolve` and `cut/resolve-import.json` exists with `"ok": true`. Its `project` and `timeline` values are the `--project` and `--timeline` arguments for every `resolve_*.py` script below. Those scripts exit 4 when Resolve Studio is not reachable: say so, name the fix the script printed, and use the other lane for that deliverable.
4. Read `{brand-path}/tokens.json`. If it does not exist, tell the creator it is missing and that theming the thumbnail drafts cannot happen without it, then route to mc-setup and stop.
5. Read `{brand-path}/production-bible.md`. If it does not exist, tell the creator it is missing and that thumbnail style, the series templates it records, and the CTA section cannot happen without it, then route to mc-setup and stop.
6. Read `{brand-path}/headshots/`. If it does not exist, tell the creator it is missing and that face-plus-hook thumbnails cannot happen without it, then route to mc-setup and stop. Its `index.md` catalogs which expression is which.
7. Read `{brand-path}/blacklist.md`. If it does not exist, tell the creator it is missing and that the blacklist lint on the written copy cannot happen without it, then route to mc-setup and stop.
8. Route the branches. Format `livestream-pack` (or `stages` containing `stream-pack`) is a scheduled broadcast: work from `{skill-root}/references/live-event.md` instead of the sections below. A non-null `series` binds every candidate to that series' locked anchors: load `{skill-root}/references/series-template.md`. Read `{skill-root}/references/cta-placement.md` in full before writing the description, the pinned comment, or the end-screen guidance.

## Titles

`[packaging] candidates` candidates that pay off the approved promise, under
`[packaging] title-max-chars` characters, front-loaded, no clickbait the video cannot cash. In a
series, every candidate conforms to the template's locked title pattern.

## Thumbnails

Face-plus-hook is the default treatment: an approved headshot plus a 2 to 4 word hook, capped at
`[packaging] hook-words-max` words. The flow is locked.

- Headshots come ONLY from `{brand-path}/headshots/`, picking the expression from `index.md` that
  matches the hook's emotion. When `{brand-path}/headshots/` is missing or empty, flag it loudly: face-plus-hook
  is blocked and mc-setup's headshot collection step is the fix. A non-face treatment proceeds only
  on the creator's explicit say-so.
- Draft programmatically: author each draft as a self-contained SVG or HTML composition themed from
  `{brand-path}/tokens.json`, with the chosen headshot placed in the layout, render it, and land it in
  `packaging/work/`. The draft exists so the text is pixel-accurate; never ask a generative model to
  render the hook text.
- Improvement pass, ALWAYS: run every draft through the creator's configured image lane (`[assets]`
  `image-provider`, resolved to a `[[tools]]` entry and driven EXACTLY per its `headless` string and
  `notes`, or the API provider). Pass the draft plus the ORIGINAL reference images (the chosen
  headshot photo, past blessed thumbnails when they exist) and an explicit mandate: use the person
  in the headshot image, optimize this thumbnail for clicks, keep the hook text verbatim. When it
  mangles the text, composite the text back programmatically over the improved image; never
  regenerate just to fix text.
- Revisions start clean: re-send the SAME original inputs (draft, original headshot, references)
  with one improved prompt. Never pass a previous improved output back in as the base; a revision of
  a revision degrades like a photocopy of a photocopy.
- 120px verification, mandatory, on every candidate:
  `uv run {skill-root}/scripts/verify_thumb.py <image> --out-dir <project>/packaging/work --width <[packaging] verify-width>`.
  Then LOOK at the proof image it writes before presenting anything. A hook that is not instantly
  readable in the proof goes back to the draft or the improvement step.

Presented candidates go to `packaging/thumbs/`; every draft, retry, and proof stays in
`packaging/work/`.

Footage frames, Resolve lane: grab full-resolution frames from the cut, at the moments that carry the hook's emotion or at the beat markers, with Resolve's still export. It returns the clip as graded in Resolve and leaves out overlays and titles from upper tracks, which is the plate a thumbnail wants. Frames land in `packaging/work/frames/`, since they are inputs, not candidates.

```
uv run {skill-root}/scripts/resolve_frames.py --project <project> --timeline "<timeline>" --out-dir packaging/work/frames/ --at <M:SS.s> [--at ...]
uv run {skill-root}/scripts/resolve_frames.py --project <project> --timeline "<timeline>" --out-dir packaging/work/frames/ --from-markers packaging/work/markers.json --marker-color Blue
```

Look at every frame before using it. Pass the chosen frames to the improvement pass as reference images for the set and lighting. A frame becomes the base of a non-face treatment only on the creator's say-so, and never stands in for a headshot; a frame the creator wants as a headshot goes into `{brand-path}/headshots/` through mc-setup. Exit 1 names each moment that failed to export or decode; re-pick those moments.

## Pairing and A/B

Present the candidates as title+thumbnail PAIRS (title A with thumbnail A). Within a pair the two
complement and never repeat each other: they share attention, not words. Series projects present
exactly `[packaging] candidates` pairs built on the template's locked anchors, ready for YouTube's
Test & Compare to run as pairs; recommend which pair to lead with and why.

## Description

The first 2 lines carry the hook and the search terms (they show before the fold), and when the
video has a conversion CTA its link goes there too, because description-top is half of its click
surface. Then the CTA lines drawn from `[cta]` items in priority order (imperative plus benefit, 7
words or fewer of ask copy per item); then the creator's `[owner]` `links`, in order; then the
chapters block.

Copy matches the lane. Never live framing on a VOD ("enjoying the stream?", "link in chat" are
wrong on a replay; use "comment below", "link in the description", schedule-tied subscribe
framing), and livestream-vod projects get replay framing throughout.

## Pinned comment and end screen

To `packaging/cta.md`: a paste-ready pinned-comment suggestion pointing at the same next step as
the description-top link (identical URL; end screen, cards, pinned comment, and description-top all
point at one next step), plus end-screen guidance for upload. The final 10 to 20 seconds are the
outro runway, a 2-element layout (one watch-next plus one subscribe) beats cluttered screens, the
watch-next target must be topically continuous, and the narration must verbally bridge to it. Check
the script or transcript for that verbal bridge and flag loudly when it is missing.

## Chapters

From the edited transcript's beat boundaries: first chapter 0:00, honest labels, no keyword
stuffing.

On the Resolve lane, read the cut timeline's markers first:

```
uv run {skill-root}/scripts/resolve_markers.py --project <project> --timeline "<timeline>" -o packaging/work/markers.json
```

Blue `manticore-beat` markers sit at the beat changes mc-cut imported, and any marker the creator added or moved in Resolve comes back where it is now. Where a marker and a transcript beat boundary disagree, the marker wins: it is the creator's later call. Marker names are beat ids, not labels; write the labels here.

Every chapter list, from either lane, passes the gate before it ships:

```
uv run {skill-root}/scripts/verify_chapters.py packaging/work/chapters-edited.md --duration <clean_duration from cut/edited-words.json>
```

Exit 1 means YouTube would drop every chapter: fix the list and re-run.

Whenever `cut/edl.json` exists, chapters are a dual-timeline deliverable. `packaging/chapters.md`
opens with the paste-ready block in edited (published) timecodes, followed by a clearly labeled
table adding the original-source timecode per chapter, for finding the moment in the raw footage or
VOD. The original column comes from this skill's own remap utility, run against `cut/edl.json`:
write the edited-timecode chapter list to `packaging/work/chapters-edited.md`, then

```
uv run {skill-root}/scripts/remap_timecode.py cut/edl.json --direction clean-to-orig --chapters packaging/work/chapters-edited.md -o packaging/work/chapters-orig.md
```

and pair the two files line by line into the table. On a multi-source EDL, use the script's
`--events` mode instead and add a source column (it records `source` on each remapped entry).

If the cut does not exist yet (an early run), chapters are pending: skip this section and the
description's chapter block, and tell the creator to re-run mc-package after the cut to finish them.

## Captions and transcript

Needs the same gate as chapters: `cut/edl.json` and `transcript/words.json` present. Skip like
chapters and finish on the re-run when they are not.

```
uv run {skill-root}/scripts/captions.py cut/edl.json --words transcript/words.json --out-dir packaging/captions/
```

Multi-source projects pass one `--words` per source words file
(`transcript/<source-id>.words.json`), binding explicitly when media fields differ:
`--words raw/<take>=transcript/<source-id>.words.json`.

The script emits `packaging/captions/final.srt`, `final.vtt`, and `transcript.md` for the EDITED
timeline. A light filler/stutter cleanup runs by default on this derived rendition only;
`transcript/words.json` is never modified. Offer the creator `--no-clean` if they want verbatim
captions. Present `transcript.md` for a skim before calling the deliverable done.

On the Resolve lane, Resolve captions the cut timeline instead. Its transcription drops fillers and false starts, which suits captions, and the creator can correct the subtitle track in Resolve. The script adds a subtitle track to the cut timeline and writes the same three files from it, with cue times taken from the timeline frames:

```
uv run {skill-root}/scripts/resolve_captions.py --project <project> --timeline "<timeline>" --out-dir packaging/captions/
```

After the creator corrects the track in Resolve, re-export it without generating a new one by adding `--reuse`. `--lines 1` gives single-line captions.

Captions from either lane pass the gate before they ship:

```
uv run {skill-root}/scripts/verify_captions.py packaging/captions/final.srt --words cut/edited-words.json -o packaging/work/captions-check.json
```

It fails malformed, overlapping or overrunning cues, and captions that skip speech: a stretch of two seconds or more of edited-timeline words with no cue, or more than 5 percent of words uncovered. On a Resolve-lane failure, re-run with captions.py and say which lane shipped.

## Finish

Lint the written copy: `uv run {skill-root}/scripts/lint_script.py <file> --blacklist {brand-path}/blacklist.md`
on `titles.md`, `description.md`, and `cta.md`.

Write `packaging/titles.md`, `packaging/description.md`, `packaging/cta.md`,
`packaging/chapters.md`, and `packaging/captions/` (the last two only when the cut existed to
produce them); update `artifacts` in `project.json`. If the project's stage is `package` and
chapters are done, append `package` to `stages_done` and set `stage` to the next stage in the
project's `stages`; on an early run, leave `stage` and `stages_done` untouched.

## The pick and blessed slots

Candidates accumulate in `packaging/thumbs/` and `titles.md`; the pick can happen immediately or
after Test & Compare results come back. Whenever the creator declares the winners, write exactly
one blessed asset per slot to `packaging/final/` (`packaging/final/title.txt`,
`packaging/final/thumbnail.png`), record them in `project.json` `artifacts` (`"title"`,
`"thumbnail"`), and set the project's `title` field to the blessed title. Alternates, drafts, and
retries stay in `packaging/thumbs/` and `packaging/work/`, so nothing downstream ever has to guess
which asset shipped.

## What no script can check

- The 120px proof was actually looked at. `verify_thumb.py` writes it; only you can judge whether
  the hook reads at that size.
- The narration verbally bridges to the watch-next target, checked in the script or transcript.
- `transcript.md` was skimmed before the captions deliverable was called done.
- Each footage frame was looked at. `resolve_frames.py` proves it decoded at full size; it cannot see a blink or a half-turned face.
