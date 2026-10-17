# TODO / Roadmap

## Fast-follows

- Per-episode stream packs and the Ecamm lane: pre-show topic popups, CTAs and lower thirds mined from the episode plan, delivered as switchable scenes, with baked PNG / ProRes 4444 alpha for the Ecamm/other lane.
- Scheduled-livestream packaging: mc-package live-event mode and the two-asset thumbnail rule.
- farm_asset.py metered API lane (xAI Imagine, Veo 3.1 as escalation), opt-in only, never a default.
- Script the beat-table quota check: read the variety quota, static-card cap and beats-per-minute floor from the Production Bible and exit non-zero on a plan that misses them.
- HyperFrames skills install: `npx skills add heygen-com/hyperframes` leaves Git LFS pointer text in place of binary assets (changelog-video's bgm.mp3 and bg-pattern.mp4) and drops fonts and SFX from embedded-captions, hyperframes-creative, media-use, music-to-video and talking-head-recut. `npx skills check` reinstalls and breaks them again. Find an install path that keeps the binaries, or a setup check that fails on a pointer file.
- resolve_import.py: push the exported timeline into a running DaVinci Resolve (Studio for external scripting, Fusion Scripts menu for free edition).

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
