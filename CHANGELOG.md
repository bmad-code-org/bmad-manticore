# Changelog

All notable changes to BMad Manticore are documented here. Dates are ISO (YYYY-MM-DD).

## 3.1.0 - Unreleased

Reviewing a cut no longer means waiting for a render. Everything here came out of one real 20-minute 4K project whose gate-2 preview took 22 minutes to encode — twice, because the first one failed validation at the finish line.

### Current HyperFrames

- `render_verify.py` now fails an alpha render whose sampled frames are all fully opaque. HyperFrames alpha renders now keep any background on the composition root, so an overlay with a root background came out as a solid full-frame clip. Pass `--allow-opaque` for a comp meant to cover the frame. WebM VP9 alpha is decoded with libvpx so its alpha plane is read.
- mc-graphics and mc-beats search the HyperFrames registry (`npx hyperframes catalog --query`) before hand-building a beat, and the build loop adds `check` and `snapshot`.
- Manticore's gates replace HyperFrames' own "render now, or what changes?" step before a final render.
- The engine doc names the new `hyperframes-audio` skill for mixing inside a composition and `hyperframes-studio` for timeline layout, and opens Studio with `npx hyperframes preview`.
- mc-retro's wrap lane lists HyperFrames scratch with `npx hyperframes clean --dry-run`.
- The skills install command drops the removed `--full-depth` flag.

### You do not render a file to review a cut any more

- New `edl_to_ffconcat.py` writes the EDL as a **virtual timeline** — an ffconcat playlist (and optionally an mpv EDL) that plays the cut with no encode at all: `ffplay -f concat -safe 0 -i cut/preview.ffconcat`. On a 379-segment 16-minute cut the timeline resolves in 33ms against 22 minutes for the equivalent render, and was verified pixel-identical (PSNR inf) to source ground truth at four points including random seeks. It carries video and audio together, so A/V sync is reviewable — which an audio-only preview cannot show.
- mc-cut now builds the virtual timeline at gate 2 and does **not** render a preview file. Rendering a full preview before the creator has approved a single call was the most expensive habit the stage had. A file gets rendered when something actually needs a file: a composited preview once overlays exist, something to share, or the gate-4 final.
- `render_preview.py --proxy-only` builds the proxy and exits, so the timeline can be built without a render.

### Preview proxies are all-intra, and that is a correctness fix

- `build_proxy_command` now encodes proxies all-intra (`-g 1 -keyint_min 1 -sc_threshold 0`). This is not a speed tweak: the concat demuxer can only cut on keyframes, so against a long-GOP proxy the virtual timeline silently plays the WRONG frames while still reporting the exact right duration. Measured at an 8.333s GOP against segments averaging 2.51s (163 of 379 under 2s), every segment began up to 8.3s early — a wrong cut that passes every check except a frame comparison. `edl_to_ffconcat.py` refuses a non-intra source unless you pass `--allow-long-gop`.
- Proxy paths carry the encode family (`<stem>-720p-intra.mp4`) and sidecars now record a recipe, so a proxy built by an older version can never be reused as if it were intra. A sidecar with no recipe line is stale by definition.
- The cost is disk: roughly 10x a long-GOP proxy (698MB vs 65MB for 20 minutes at 720p), built once per source and reused by every later preview and render.

### Two EDL bugs that only appear on long cuts

- **EDL boundaries must be quantized to the source frame grid.** They come from the audio map at 0.1s granularity and from word timestamps, so they are sub-frame by default; ffmpeg rounds each `trim` to the nearest frame and the error accumulates. A 379-segment cut rendered 954.766s against an expected 952.460s and the output gate correctly refused to publish it — the cut was fine, only the arithmetic disagreed. Past roughly 150 segments this happens every time. Now a cutting rule, with the tail-clamp for boundaries that round past `source_duration`.
- **A trailing silence must be truncated, not tightened.** Tightening the final silence at both ends like an interior gap leaves a junk segment scraped off the absolute end of the file, past the last detected silence and past `source_duration` once frame-aligned. Now a cutting rule: end the final segment on a real tail inside the verified silence.

### Applying approved cuts invalidates everything downstream, loudly

- New section in mc-cut spelling out that the moment `cut/edl.json` is rewritten, every derived artifact is stale — the virtual timeline, the FCPXML, the edited transcript, the preview, the boundary frames, and **every beat anchor in `beats/beats.md`**. Nothing on disk announces this: a stale FCPXML imports cleanly and a stale beat table renders overlays that land off their phrase, discovered after the graphics are paid for. The regeneration order is now explicit, `verify_edl.py` runs first and its non-zero exit stops the rest, and when a beat table already exists the anchor check must re-run and be reported before graphics continue.

### Not shipped, and why

- A concat-demuxer **stream copy** off the intra proxy renders the same frames ~470x faster (2.8s vs 22min, 340x realtime). It is not the preview path: it emits duplicate DTS wherever a segment runs only a frame or two, and the file then fails `render_preview.py`'s own decode validation — 125 errors on that cut, and 3 with every timestamp remedy applied (`+genpts`, `avoid_negative_ts`, `video_track_timescale`, `fps_mode passthrough`, `muxdelay 0`), never zero, from just 2 sub-0.1s segments. Publishing it would mean loosening a correctness gate to buy a speed number. `composite_core.build_streamcopy_command` keeps it, tested and documented, for a caller that wants a scratch file and accepts the caveat. The speed moved to the virtual timeline instead, which is both faster and correct.

## 3.0.0 - Unreleased

The big release: one motion-graphics engine with the full HyperFrames toolkit behind it, cross-platform support, a final render that only re-does what changed, delivery polish (loudness, captions, OBS alpha), and a cut stage rebuilt from the ground up after its first real project. Upgrading from 1.x is a clean reinstall (see README): your brand, voice bible, and format profiles live in your studio folder, not in `_bmad/`, so they survive and onboarding picks them back up.

### The cut stage, rebuilt after its first real project

The cut pipeline had never been run end to end on a long 4K take; the first one that was corrupted the edit in ways nothing detected. What follows fixes the causes and, more importantly, makes each of them impossible to ship silently again.

### The critical one: transcription was silently losing speech

- Every transcription lane now works in short isolated windows (20s, 3s overlap). The Apple Silicon lane previously handed whole files to the model in one pass, which ran out of Metal memory above roughly 15 minutes of 4K, and the obvious workaround (larger chunks) made it far worse: parakeet silently drops whole paragraphs inside long windows, with no error and no warning. On a 20.5 minute take, 120s chunks lost three paragraphs of clearly-spoken content and 90s windows still lost some; 20s windows were complete. The cross-platform lane had been windowing correctly all along, so the reference lane was the broken one. Both lanes now share one windowing driver.
- New `verify_transcript.py`, and no transcript reaches the cutter without passing it. It finds audio above the silence floor that produced no words, which is dropped speech by definition, and names the regions with timecodes. Nothing had ever checked that a transcript was complete; that single missing check is what let everything else happen. Note the check is built from the audio side deliberately: scanning transcript word gaps would inherit the very bug it exists to catch.

### The cut is built on the audio now, not on the transcript's idea of time

- New `analyze_audio.py` produces a silence map from the audio, and it is the timing source of truth for the whole stage. The old detector computed silence from transcript timestamps, but parakeet absorbs a pause into the preceding word's end, so those gaps read about 0.0 across real dead air. On a take with over five minutes of dead air it found 12 silences; the audio has about 400, totalling around 300 seconds. The resulting edits were loose and full of stalls.
- Every cut candidate's edges now snap into an audio-verified silence. "Never cut inside a word" stops being an assertion about timestamps and becomes structural: a cut inside real silence cannot clip a word. This also fixes an audible artifact where a stutter trim clipped the repeat's onset, because its end came from a pause-absorbed word end.

### The cut stage is an editor now, not a janitor

- Dead-air tightening: interior silences are trimmed down to a 200ms beat rather than left or flattened, with sub-threshold micro-beats preserved so the result is tight without sounding machine-gunned.
- Section re-reads are caught in full. The old matcher looked 16 words ahead for a 3-word repeat, which undersized a real paragraph redo from 34s to 11s and left the abandoned take in the video. The new one matches long runs inside a locality window measured in seconds, which is what tells a redo apart from a deliberate callback minutes later.
- Bloopers are their own candidate type. An explicit expletive sat in the first real cut and would have shipped; nothing was looking for one. Severity reflects context, so scripted usage is flagged for an ear rather than treated as a flub.
- Filler detection respects the creator's voice. It used to flag every sentence-initial "so" as filler, including all 19 of them on one take, while that creator's voice bible named "so" as their natural connective glue. Cadence words are taste, so they now live in the voice bible's machine-readable `cadence` block, and the built-in list no longer contains them.
- New editorial pass, between the mechanical cut and gate 2. It reads the EDITED transcript (what survived the cut, which is not the script) as an argument against the brief, and recommends content-level changes under a subtract-only constraint: cut, re-record, hand-to-beats, or consent-gated generate. Nothing is auto-applied; gate 2 now presents the mechanical trims and the content calls as one list. Its hardest rule is written from experience: never apply a finding from a transcript-read timecode without re-detecting the span against the audio first.

### Renders stop corrupting themselves, and stop taking an hour

- No render writes its output path directly. Each goes to a per-process temp file, decode-validates with zero tolerance for errors, checks it has not been superseded by a newer EDL, and only then atomically moves into place. A stale background render previously finished late and interleaved bytes with the current one, handing over an unplayable file. A corrupt mp4 still reports a plausible container duration, which is why the old duration-only check saw nothing.
- The composited preview is minutes instead of an hour. Overlays pack into the fewest non-overlapping time lanes and only the lanes are stacked, so compositing depth is the number of overlays on screen at once rather than the total (56 became 2 on the real project), and previews cut from a cached low-res proxy of each source instead of seeking into a 4K master once per segment.

### Baked-in frame defects are caught and can be fixed

- Source QC asserts and halts. It samples frames across each take (not just the first and last, which cannot see a defect that starts mid-recording), detects a flat decorative border ring or an active area whose aspect does not match the container, reports the inferred content rectangle, and stops the stage. A recorded-in border and off-centre framing previously passed preflight, transcription, the EDL, gate 2 and the render untouched, and were caught by eye after the cut was locked.
- New `normalize_source.py` gives the pipeline a spatial capability it simply did not have: corrective crop and reframe, emitting a corrected master registered as the project source. The EDL is time-only, so a baked-in border had nowhere to be fixed. It runs before beats and graphics, since overlays are positioned against the canvas, and it changes no timecodes (the script asserts this and refuses to publish otherwise), so an existing transcript, EDL and cutplan stay valid: no re-transcribe, no re-cut.

### Beat timing is verified, not asserted

- New `verify_anchors.py` re-derives every beat's time from its anchor word through the EDL and fails on any beat that does not land within half a second of it, on any anchor missing from the transcript, and on any anchor sitting in a span the cut removed. mc-beats' checklist had claimed this for a long time with no script behind it, and mc-graphics now refuses to build against a table that has not passed.

### Thresholds calibrated against real footage, not guessed

Every threshold in the new cut path was set by measuring the 20.5 minute take that exposed these bugs, in both its known-good and known-broken transcripts. The first-pass values were guesses, and three of them were wrong:

- Dead-air floor 0.45s to 0.30s. That take has 383 seconds of silence across 929 intervals. A 0.45s floor reaches 87 percent of the trimmable dead air; 0.30s reaches 99 percent, worth about 29 extra seconds in a 20 minute video, which is exactly the "loose" quality the first cut was criticized for. Below 0.30s the gain is under a percent and it starts eating the speaker's rhythm (their median silence is 0.19s, which is cadence, not dead air).
- Dropped-speech threshold 2.5s to 1.0s. Sweeping it over both transcripts, the good one produces ZERO false positives all the way down to 0.75s, because anything under the silence floor is already classified as silence and never reaches the check. The cautious 2.5s bought no safety and missed a real dropped region that 1.0s catches. On the broken transcript the gate now reports five dropped regions, including one at 2:36 that the original bug report never found.
- Audio map granularity 0.3s to 0.10s. The map has to be finer than anything that consumes it. Edge snapping needs the 0.1 to 0.2s gaps between doubled words, and those are precisely the intervals a coarse map omits. The gate now also warns when it is handed a map too coarse to scan against, rather than failing a good transcript with no explanation.
- Blooper context tightened. Asking for "a 0.5s pause within 3s" flagged the scripted line "that damn term" as almost certainly a flub. Measured, the separation is not subtle: 0.77s of silence beside the scripted line, 7.65s beside the real "Oh fuck." A blooper is next to a STOP, not a breath, so it now asks for 2s of silence within 1.5s and the two classify correctly.

### OGraf removed: one graphics deliverable, every editor

- The mc-ograf skill and the editable-graphics lane are gone. OGraf produced graphics that stayed editable inside DaVinci Resolve 21+ and could be click-triggered live in OBS/SPX-GC, but it cost a second authoring path, a second spec to conform to, and its own scaffold and verify scripts, all to serve one editor version. Baked alpha overlays, which every editor imports, are now the only deliverable.
- Nothing in an existing studio breaks. `ograf` joins `remotion` as a permanent compatibility alias for `hyperframes` wherever an engine is named, so an in-flight beat table or a copied format profile keeps working and no creator file is rewritten. `[editor] ograf-editable` is retired: a config written before 3.0.0 may still carry it and it is simply ignored.
- Livestream lower thirds and topic cards are now self-contained local HTML styled from tokens.json, alongside the scenes that already worked that way. SPX-GC and OBS browser sources drive them the same as before, without an editor-specific package format.

### The rule behind all of it

- New binding convention in AGENTS.md: a check the pipeline claims to perform must be a script that exits non-zero. Four separate defects here shipped through the same hole, QC frames extracted but never asserted on, boundary frames eyeballed while the audio underneath was wrong, beat anchors as a checklist line with no script, and a transcript nothing ever checked. All four were documented, and none could halt.
- New `verify_edl.py`, which applies that rule to the cut's own deliverable. The EDL is written by hand and rewritten when the creator's editorial calls come back, and nothing had ever read it back: "no cut lands inside a word" and "every segment has quote and reason" were checklist prose with no enforcement, on the one artifact every later stage depends on. It now fails any boundary that does not rest in an audio-verified silence, reporting how far off it is and whether it landed mid-word. It follows the two-source rule rather than fighting it: because a pause-absorbed word end reaches past the sound, a correct cut can sit inside a word's timestamps, so the audio decides and the word span is context on an already-failing boundary, never a verdict of its own.
- New `snap_spans.py`. Applying the creator's approved editorial cuts used to say "snap the edges into silences" as an instruction to follow by hand, at the exact point where getting it wrong cuts the wrong words. The arithmetic is now a script, snapping is directional so a span can only widen and never collapse onto itself, and anything that cannot reach a silence is reported rather than quietly treated as safe.
- Blocking gates now carry an acknowledged override. The transcript gate takes `--accept-region <start>-<end> --reason "<why>"`, matching what source QC already had, because not every audible span with no words is lost speech: a laugh, a music bed, or an off-mic aside reads the same way to a coverage scan. A gate with no way past it gets worked around in ways that leave no trace, which is worse than one that records who signed off and why.
- `cutplan.py` refuses to run when `-o`, `--audio-map` or `--voice-bible` are supplied twice. The skill appends the customization flag string after its own arguments and argparse lets the later one win, so an override file could have pointed the audio map somewhere else and broken the two-source rule with nothing to show for it. The boundary was a comment; it is now enforced.
- Published or on-YouTube source should pull captions with `yt-dlp` rather than running local ASR, which is faster, free at any length, and sidesteps the local-model failure mode entirely. Local ASR only ever ran on raw unpublished recordings, which is exactly why these bugs went unnoticed for so long.

### Motion graphics: one engine, the whole HyperFrames toolkit

- Consolidated on HyperFrames as the single motion-graphics engine; Remotion is retired. Remotion's license carried a real obligation for teams of four or more, and its React model bought nothing in a frame-deterministic renderer where state is just a function of frame index. Every job it held (brand stinger, karaoke captions, HTML/SVG comps) is a HyperFrames comp, and the OBS-plus-editor dual alpha target is unchanged. Nothing to migrate: `remotion` is a permanent alias for `hyperframes` everywhere an engine is named, so existing studios keep working untouched.
- HyperFrames' own Agent Skills are installed at setup and favored, so the agent knows the engine's full, current capability surface from the start instead of a hand-written summary that goes stale. That surface is large and mostly new to the pipeline: the 100-plus block catalog (code animations, WebGL shader transitions, caption styles, lower thirds, social cards, data-viz maps, VFX, 3D device mockups) plus footage-facing effects the pipeline never surfaced before (color grading with LUTs, grain, and vignette; background removal; HTML-in-Canvas WebGL), and HDR10 and 4K delivery. It all runs locally; no HeyGen account or credits, ever.

### Faster iteration

- The final render is incremental. The timeline is cached as content-addressed segments, so a re-render re-encodes only what actually changed (a tweaked graphic, a re-cut region) and reuses the rest. A fix on a long video is a seconds-long render instead of an end-to-end one, and boundaries are stable, so an edit early in the timeline does not invalidate everything after it. `--no-cache` forces a full rebuild.
- Final renders are loudness-normalized to -14 LUFS by default (the YouTube reference), so deliverables land at a consistent, platform-ready level with no manual pass. The preview is never normalized; `[render]` loudness-target and loudnorm are the knobs.

### Cross-platform: Windows, Linux, and Intel Mac

Code-complete and unit-tested; still pending validation on real Windows and Linux hardware, so treat the first run as a shakedown and report what you hit.

- Transcription runs everywhere: the reference parakeet weights via parakeet-mlx on Apple Silicon and onnx-asr (same weights) on Windows, Linux, and Intel Macs, CPU by default with a one-flag CUDA escalation. Verbatim fillers and word timestamps carry over identically. The default lane is now `auto`.
- The final render uses per-OS hardware encoders validated by a real one-frame test encode (nvenc, qsv, amf on Windows; nvenc, vaapi on Linux) with a libx264 fallback everywhere; macOS videotoolbox is unchanged.
- Setup detects your OS, CPU, and GPU and recommends the right stack, with per-OS notes for Windows, Linux, vMix and Wirecast, and the free DaVinci Resolve edition.
- The audio farm, timeline export, and asset farming are portable to Windows and Linux paths, shells, and locales.

### Packaging and delivery

- mc-package emits uploadable captions and a publishable transcript from the edited timeline (SRT, VTT, and a cleaned Markdown transcript), mapped onto output-timeline times across reordered and multi-source edits; the source words.json is never modified.
- mc-stream-pack produces and verifies the OBS WebM (VP9 alpha) deliverable from a ProRes 4444 master in one command, with clear errors when ffmpeg lacks libvpx-vp9 or the master has no alpha.

### Documentation

- README carries a 2.0 announcement and the upgrade path; the platform matrix and user guide are rewritten for cross-platform reality; the Resolve handoff reference gains free-edition Fusion Scripts paths and an optional Resolve-MCP pointer (Manticore itself needs no MCP server).

## 1.0.1 - 2026-07-07

### Fixed

- 0.x migration now refreshes the creator's existing format profiles: the new `merge_profile_frontmatter.py` adds the frontmatter keys introduced in 1.0 (`beat-types`, `density`) that mc-beats requires, copying them from the shipped profiles without touching the creator's own key values, prose, or Learnings. Previously the never-overwrite rule left 0.x profiles missing keys the 1.0 stages need.
- 0.x migration offers to move a pre-1.0 brand-root series template (for example `thumbnail-template.md`) into `{brand-path}/templates/<series>.md`, where mc-package's series contract looks for it.

## 1.0.0 - 2026-07-07

### Breaking changes

- Render default inverted: Manticore now always renders. A fast low-res preview (`renders/preview.mp4`) is produced each cut iteration, re-rendered with graphics composited once the graphics stage has rendered overlays, and a final-quality render (`renders/final.mp4`) is offered at gate 4. The 0.x invariant (editable timeline only, never a baked mp4) is retired with maintainer sign-off (2026-07-07). Editor timeline export and all assets (edl.json, cutplan, overlays) are still always produced; the render-first default is confirmed by the creator at setup via `[render]` in the studio config.
- `ELEVENLABS_API_KEY` removed from shipped defaults. `[defaults.transcription] api-key-env` now ships blank; set it only when explicitly choosing a metered provider. ElevenLabs appears only inside documented opt-in branches. Paid vendors never ship in defaults.
- Interview marker cue default renamed from "question from claude" to "question from the interviewer". Configurable at setup and via cutplan.py `--marker-cues`; the old phrase remains a documented alternative for footage recorded with it.
- Beat table contract gains `type` (a beat type from the format profile's `beat-types` frontmatter list), `engine`, and `asset` columns. Consumers tolerate rows missing the new columns, so 0.x beat tables keep working (missing type reads as the reserved `overlay` placeholder, missing engine as the Engine policy default, missing asset as null).
- Asset lane defaults (`[defaults.assets]` image, video, and escalation providers) now ship empty, and the metered lane key names (`xai-api-key-env`, `gemini-api-key-env`) ship blank. Setup requires an explicit choice (a registered `[[tools]]` CLI preferred, a metered API only by explicit selection) and fills the vendor key name only inside that opt-in branch; mc-assets stops and asks when a lane is unset instead of billing a default.

### Upgrade notes for 0.x studios

- Run mc-setup against the existing studio. It detects the 0.x config and runs a delta interview: render consent, the video style interview, and the live-tool question, backfilling the `[render]`, `[style]`, `[cta]`, `[live]`, and `[audio]` studio-config tables from the shipped defaults, and scaffolding the Production Bible seeded from existing brand assets.
- In-flight projects need no migration: beat tables without the new columns are accepted, and existing `cut/` artifacts remain valid.
- If your recorded footage uses the old marker cue, set the cue at setup (mc-setup records it as a `--marker-cues` override in `cutplan-flags`, in the `[cut]` sub-table of the studio config) or pass `--marker-cues "question from claude"` directly.

### Headline features

- mc-agent (Manny) ships on main as the documented front door: onboarding, routing across the pipeline, ingest-first detection for creators arriving with existing footage.
- Render lane: implemented render_final.py (EDL plus beat-table graphics compositing, delivery resolution and codec) with the compositing core shared by the low-res preview.
- The Manticore Production Bible: an evolving visual taste artifact (brand scope, motion feel, overlay aesthetic, image-type policy, density, CTA config) built interactively at setup, read by every visual stage, ratcheted by mc-retro.
- Creativity mandates and density tiers (high, medium, low) in mc-beats, backed by a shipped density-and-creativity research reference; static text cards become the composition of last resort. mc-beats now riffs treatment ideas with the creator before writing the table, and the beat-medium mix (cards, SVG, imagery, clips, gifs, memes) follows the Production Bible plus that conversation, never a default.
- CTA system: `[cta]` inventory and appetite in the studio config, a shipped placement research reference, cta as a first-class beat type planned at gate 3, packaging and script wiring.
- Footage-first entry point: mc-new ingest mode writes a post-production stage list (new, cut, beats, graphics, assets, package, final, retro) and registers the source; new livestream-vod format profile.
- Series support: mc-new `--series`, an optional `series` field in project.json, series `common/` assets, per-series packaging templates, and 3 title plus thumbnail A/B pairs.
- Blessed-slot convention: deliverable folders hold exactly one blessed asset per slot, alternates in `work/`; mc-package writes picks to `packaging/final/`.
- Expanded setup interview: brand-source mining fills tokens.json for real, creator-emulation takeaways from video links (echoed back and confirmed), headshot collection with expression indexing, guided voice-bible build, `.env.example` scaffolding, honest runnability summary.
- Generative-editing safety rules reference applied to every asset lane; tools-registry consumption so farming drives the verified CLI tools.
- BMad help convention adopted: `skills/module.yaml` plus `skills/module-help.csv` (canonical schema) so the installer merges Manticore's catalog into `{project-root}/_bmad/_config/bmad-help.csv` alongside every other installed module.
- mc-agent (Manny) restructured for progressive disclosure: SKILL.md carries only the always-needed core (persona, pipeline map, gates, dispatch); routing cards, intent playbooks, onboarding, and studio-growing guidance load on demand from `references/`, and Manny reads the merged help catalog liberally to know everything installed, Manticore and beyond (new HP menu item).
- mc-audio service skill: local-first sound farming (Kokoro-82M narration and two-host dialogue with the validated realism recipe, MusicGen-small instrumental beds, AudioLDM2 SFX with the diffusers 0.31.0 + transformers 4.43.4 pin), one persistent engine-workspace venv built with consent, `[audio]` lanes in the studio config, paid audio lanes strictly opt-in. Called from graphics, stream packs, and voiceover narration; not a pipeline stage.
- render_verify.py implemented (ffprobe checks, checkerboard frames, JSON output); no SKILL.md step names an unimplemented script.
- Design-prompting engine lane: a documented brief template and deterministic frame-stepped render contract for authoring graphics with any capable design model.
- Graphics toolkit: html_to_png.py (Playwright HTML-to-PNG with checkerboard alpha proof and safe-zone guides) and snug_frame.py (content-fitting frame generator), each with tests.
- mc-cut preflight: VFR sources auto-remux to a CFR master in the background, and a disk-space preflight stops before any render when space runs short.
- Dual-timecode support: the cut stage's remap utility produces an edited-to-original timecode map, and mc-package writes dual-timeline chapters from it (published timecodes plus the original-source column).
- Deadline mode: mc-new records an external event date; downstream stages order deliverables by hard gates and cap iteration in favor of delivery.
- mc-retro runs with or without project.json, and offers the post-publish wrap lane (archive hygiene, asset promotion) after retro.
- mc-package thumbnail proofing: every presented thumbnail carries a verify_thumb.py proof viewed at 120px before it ships.
- mc-ograf scaffolds report placeholder_palette in their JSON output, so a placeholder look never ships unnoticed.

### Quality and release safety

- Platform honesty: README supported-platform matrix, and check_deps.py gates the Apple-Silicon-only default transcription lane with a clear message and documented whisper.cpp and faster-whisper fallback pointers.
- Genericity release gate: lint_genericity.py scans skills, docs, README, CHANGELOG, and format profiles for personal or show names, non-placeholder hex colors, and absolute machine paths; findings block release.
- Blacklist split: voice patterns bind every linted surface; spoken-cadence punctuation rules bind spoken scripts only. The starter blacklist adds commonly banned transition phrases (furthermore, moreover, in conclusion, and friends).
- Test policy: every implemented script is covered by a suite under scripts/tests/ (21 suites at release; the composite core is exercised through the render suites, and a duplicated script is guarded by its twin's suite plus a byte-identity check across copies). The one remaining stub, resolve_import.py, carries its full I/O contract in its docstring and its offer is gated on implementation status.
