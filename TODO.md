# TODO / Roadmap

## Fast-follows

- Per-episode stream packs and the Ecamm lane: pre-show topic popups, CTAs and lower thirds mined from the episode plan, delivered as switchable scenes, with baked PNG / ProRes 4444 alpha for the Ecamm/other lane.
- Scheduled-livestream packaging: mc-package live-event mode and the two-asset thumbnail rule.
- farm_asset.py metered API lane (xAI Imagine, Veo 3.1 as escalation), opt-in only, never a default.
- Script the beat-table quota check: read the variety quota, static-card cap and beats-per-minute floor from the Production Bible and exit non-zero on a plan that misses them.
- HyperFrames skills install: `npx skills add heygen-com/hyperframes` leaves Git LFS pointer text in place of binary assets (changelog-video's bgm.mp3 and bg-pattern.mp4) and drops fonts and SFX from embedded-captions, hyperframes-creative, media-use, music-to-video and talking-head-recut. `npx skills check` reinstalls and breaks them again. Find an install path that keeps the binaries, or a setup check that fails on a pointer file.

## DaVinci Resolve lane

Decided 2026-10-16 with the maintainer: when `[editor] name = "resolve"`, Manticore uses Resolve Studio in every stage it can. Other editors keep the fcpxml lane unchanged, and `edl.json` stays the source of truth.

The split: pipeline mechanics are `uv run` scripts that drive Resolve's scripting API directly (Studio, Preferences > System > General > External scripting using: Local). They are deterministic, harness-neutral and exit non-zero on failure. Blackmagic's MCP server (shipped in Resolve 21.1 at `Contents/Applications/ResolveMCP`, set up from File > Setup AI Assistants) is the interactive lane for the creator's own work in Resolve: polish, looks, LUT and DCTL generation, Fusion title tweaks. It exposes `run_script`/`run_script_unsafe` over the same API plus LUT/DCTL and doc-lookup tools, so it adds no capability a script lacks. The free edition has neither: 21.1 moved external scripting and script-menu Python to Studio, so free users stay on the fcpxml lane.

Work, in order:

- Done 2026-10-16, Resolve transcription spike: failed as a cutting transcript. On a 7-minute take (Studio 21.1.1, `TranscribeAudio` is asynchronous, poll `GetTranscription`) it dropped every filler parakeet kept (4 "um", 2 "uh", 6 "you know") and every false start ("It c", "max m"), and normalized contractions both ways ("want to" to "wanna", "gonna" to "going to"). No paragraphs were lost, 96 percent of tokens agreed, and matched word starts sat within 13 ms median of parakeet. `verify_transcript.py` passed it, because the coverage gate measures dropped speech, not filler loss. It stays off the `[transcription]` switch. Its clean text suits captions in the package stage.
- Done 2026-10-16: `mc-cut/scripts/resolve_api.py`, the Resolve connection helper. It finds the API module per OS, connects, checks for Studio, and exits 4 with the fix when Resolve is unreachable. Copy it into each further skill that drives Resolve.
- Done 2026-10-16, mc-setup: `check_resolve.py` lists Resolve's render, project settings and Fairlight presets and normalize modes, and fails a configured name Resolve does not list. The interview covers Studio vs free, Local scripting, the Allow prompt and the Save As dialog, and offers Blackmagic's MCP (File > Setup AI Assistants), recorded in `[mcp] davinci-resolve`.
- Done 2026-10-16, new: `mc-new/scripts/resolve_project.py` creates or reuses the video's Resolve project and applies `resolve-project-preset` to a new one. Not yet live-tested on the create path (the open project held work, so the safety rule refused to switch). Organizing `raw/` into bins is still open; `resolve_import.py` already imports the sources.
- Done 2026-10-16, cut: `resolve_import.py` imports `cut/rough.fcpxml` as a new timeline, adds blue beat markers and red markers on large cuts, and checks every V1 item's record start, duration and source in point against the fcpxml, plus the A1 count. Live-tested on Studio 21.1.1. Lessons: the first external connection raises an Allow prompt in Resolve, and saving an unsaved project opens a Save As dialog; both block every API call silently. Resolve holds one marker per frame.
- Done 2026-10-16, beats: `mc-beats/scripts/resolve_beat_markers.py` puts the beat table on the cut timeline as gate 3 review markers, colored by type, merged when they share a frame, replaced on every re-run, and read back to verify. Marker frames count from the timeline start, whatever its start timecode.
- Done 2026-10-16, graphics: mc-graphics `resolve_place.py` duplicates the cut as '<slug> graphics', places alpha overlays on packed tracks above V1, and checks every placement against the plan. Farmed clips from mc-assets are placed only once rendered into `graphics/<id>.mov`.
- Done 2026-10-16, Fusion titles (maintainer sign-off): plain text beats become native Fusion titles on a nested '<slug> titles' timeline, listed in `graphics/titles.json`, test-rendered one by one. Lessons: inserting a title on the cut overwrites V1; a comp built on a placeholder clip does not render; mark in/out sets a title's length; a font set without a style it has fails only at render time; render MarkIn/MarkOut stay as timeline marks and a nested append honors them. Previews outside Resolve list these beats as `titles_in_resolve_only`.
- Done 2026-10-16 (unit-tested only, not yet run against Resolve), package: `resolve_captions.py` (Resolve subtitles to SRT, checked by `verify_captions.py`), `resolve_frames.py` (thumbnail frames), `resolve_markers.py` (chapters from markers, checked by `verify_chapters.py`). Run the live test on the next real project.
- Done 2026-10-16, final: `mc-cut/scripts/resolve_render.py` renders the creator's Resolve timeline for gate 4: Fairlight preset, normalization to `[editor] resolve-loudness` (undo with `--restore-gains`), their render preset, exact frame and loudness checks, atomic publish. Open: Resolve's normalization stops at the peak ceiling ("YouTube" gave -21.6 LUFS on a take peaking at -7 dBFS), so a dynamic take fails the -14 check unless the creator's Fairlight preset carries a limiter or they accept the miss.
- Done 2026-10-16, retro: `mc-retro/scripts/resolve_diff.py` exports the creator's finished timeline as OTIO and diffs track 1 against `edl.json` (trimmed, extended, split, joined, removed, added, moved), counting tracks added above V1/A1. Only removal is live-tested.
- Multicam (see below): `CreateMulticamClip` and `Timeline.AutoAlignClips` as the Resolve route for multi-source sync.

## Multitrack and multicam

- Ingest multiple numbered sources per project: talking-head takes, screen shares and loose assets.
- Sync audio-bearing sources by waveform correlation; place assets with no syncable audio by content.
- Extend edl.json with track and layout fields (full-screen, picture-in-picture, side-by-side).
- Decide switch points from context and present them as taste calls at gate 2.
- Export multitrack through the same editor lanes, FCPXML with stacked tracks first.

## mc-research and scheduled runs

- Daily intel briefings for the creator's niche, aggregated from web, X, YouTube transcripts and RSS into `manticore/research/YYYY-MM-DD-briefing.md`, layered on bmad-autopilot as an optional integration.
- Modes: scheduled daily, on demand, and a morning-podcast option rendered through mc-audio's two-host lane.
- A `[research]` sub-table in the studio config: sources, storage and retention explicit.
- mc-agent interviews the creator about their niche and installs the jobs.

## Audio: remaining lanes

- Full songs with vocals: `song-provider` ships empty; ACE-Step 1.5 is the leading local candidate, not yet validated. Do not plan around YuE on Mac.
- Paid opt-in rungs: ElevenLabs SFX v2 / Eleven Music / Text to Dialogue, Gemini TTS as a cheap cloud two-host lane, professional voice cloning.
- Long-form structured music is unaddressed; Stable Audio Open stays opt-in only.

## Editor export lanes

- xmeml (Premiere Pro) and edl (CMX3600) exporters alongside fcpxml, likely via OpenTimelineIO adapters.

## Transcription

- Metered API providers behind the `[transcription]` switch if demand shows up: deepgram-nova3, elevenlabs-scribe, plus a cloud tier for non-European languages.
- Real-hardware validation of the onnx-asr lane on Windows and Linux, A/B against parakeet-mlx on identical audio, now that both lanes share one windowing driver.

## Other ideas

- Stronger social tooling, either inside Manticore or as a separate module: cross-posting the packaged assets, per-platform copy and cuts, scheduling, and thread/carousel formats.
- Document when to use which visual lane: HTML decks and explainers versus Excalidraw whiteboards (generate it if it plays timed to the script, whiteboard it if the creator talks over it live).
- Retention analytics feedback: read YouTube retention and CTR through mc-retro to tune density tiers, CTA placement and packaging templates.
- YouTube API publishing and A/B test submission.
- Upstream fix in bmad-bmm for the accepted Copilot regression: teach `isAgentSkill()` to detect agents from the `agents:` roster in `skills/module.yaml` or SKILL.md frontmatter, so mc-agent reappears in the Custom Agents picker.

## Release path

- Bump version in marketplace.json, tag, then PR to the bmad-plugins-marketplace registry (registry/community/).
