# TODO / Roadmap

State as of 2026-07-07, the 1.0.0 release. Read AGENTS.md first (module conventions and design invariants), then `skills/mc-pipeline/PIPELINE.md` (the runtime contract). CHANGELOG.md records what landed in 1.0. This file is the roadmap; delete items as they land.

## 1.0.x fast-follows

- Per-episode stream packs and the Ecamm lane (the named 1.0.x fast-follow): mc-stream-pack gains a pre-show per-episode pack lane (topic popups, CTAs, lower thirds mined from the episode plan before the show, delivered as switchable scenes) with the two-tier asset rule (evergreen chrome once into series `common/`, topic graphics per episode). The `[live]` tool key (obs, ecamm, other) already ships and is interviewed at setup; the OBS lane keeps HTML browser sources and WebM stingers; the Ecamm/other lane delivers baked PNG / ProRes 4444 alpha scene stills and loops, a ProRes stinger, a countdown safe-zone spec with a --guides render, and a tool-specific HANDOFF.md. Ecamm Live is macOS-only. Scheduled-livestream packaging (mc-package live-event mode, two-asset thumbnail rule) rides along.
- farm_asset.py metered API lane (xAI Imagine REST image ~$0.02 and video ~$0.05/s submit/poll/download; Veo 3.1 via the Gemini API as the escalation lane). Registered CLI tools are the only implemented farming lane in 1.0; the API lane ships opt-in only, never as a default.
- resolve_import.py: push the exported timeline into a running DaVinci Resolve. Requires Resolve Studio (the scripting API is not in the free edition); the mc-cut offer stays gated on the script's implemented status. Native scripting remains the documented path; no MCP dependency.
- HyperFrames engine workspace initialization at a pinned version on the first real graphics run (upstream is pre-1.0 and moves fast; v0.7.26 as of 2026-07-03).

## mc-prompter fast-follows

- Kokoro spoken cue tier: short formulaic phrases ("thirty seconds", "wrap") synthesized by a persistent kokoro instance, released only at pauses, headphones-only output (what keeps browser AEC unnecessary). Designed in mc-prompter's references/cueing.md; ships behind the `[prompter]` spoken-cues flag, which stays false until this lands.
- zipformer-small ASR validation: model exports are coupled to the sherpa-onnx runtime generation (the 2023 zipformer export silently produces garbage under the pinned 1.13.4; it decodes correctly only under 1.10.x). The lane stays planned and exits with a pointer until a current-generation small export is validated end to end.
- TLS for remote mic capture: getUserMedia needs a secure context, so tablet-as-mic over LAN requires shipping TLS; today the microphone is captured only on the server machine.
- Take-log consumption by mc-cut: mc-prompter's session take log (script positions and timestamps per take) pre-anchors cut plans.
- sherpa-onnx offline parakeet export: the candidate for the cross-platform transcription lane (see below); the prompter's sherpa-onnx dependency makes it cheaper to validate.

## 1.x roadmap

### Multitrack and multicam support

Many creators record multitrack: a full-screen talking-head file plus one or more screen-share files, usually sharing the same audio, so sources are waveform-syncable. Designed, not started:

- Ingest multiple numbered sources per project: talking-head takes, screen shares, plus loose assets to place where the discussion warrants (the project.json `sources` registry already exists).
- Sync audio-bearing sources automatically by waveform correlation; fall back to content-based placement for assets with no syncable audio.
- Extend `cut/edl.json` with track and layout fields so it stays the neutral source of truth: which source is live, and in what composition (full-screen talking head, picture-in-picture over the screen share, side-by-side).
- Decide the switch points unassisted from context (when the words reference the screen, switch to it; when it is story or opinion, come back to the face). The proposed switches are taste calls presented at gate 2 like any other cut decision.
- Export the multitrack result through the same `[editor]` lanes (FCPXML with stacked tracks first).

### mc-research and scheduled runs

Show prep for the creator's niche, layered on the harness-agnostic bmad-autopilot core skill (ships in bmad-bmm; Manticore references it as an optional integration only, never a hard dependency):

- Topic lists, subscriptions, channel/URL/subreddit lists maintained by the skill; a daily job aggregates (web, X, YouTube via yt-dlp transcripts, RSS), distills, and writes a dated intel briefing into the studio (e.g. `manticore/research/YYYY-MM-DD-briefing.md`).
- Modes: scheduled daily briefings, on-demand runs, and a morning-podcast option (the briefing agent writes a two-host script and mc-audio's implemented two-host lane renders it; the lane shipped in 1.0).
- Config in a `[research]` sub-table of the studio config: sources, storage and retention choices explicit, never a surprise.
- mc-agent (Manny) fronts it: interviews the creator about their niche (creator-profile.md), proposes the jobs, routes here to install them.

### Audio: remaining lanes

What mc-audio does not cover yet (the shipped ladder, validation record, and limits live in `skills/mc-audio/references/audio-lanes.md`):

- Full songs with vocals (rap, sung lyrics): `song-provider` ships empty. ACE-Step 1.5 is the leading local candidate (MIT license, ungated, native Mac support via the MLX backend; the XL 4B models want the 12 to 20 GB memory tier and run about 2 minutes per 60 s clip on an M1 Max). NOT yet validated; mc-audio marks it planned and never promises it. Do NOT plan around YuE for local use: no MLX/Metal port exists, the community floor is 32 to 64 GB unified memory, and Mac wall clock is hours per 30 s; YuE is cloud or rented GPU only, if ever.
- Paid opt-in rungs of the ladder: ElevenLabs SFX v2 / Eleven Music / Text to Dialogue, Gemini TTS as the cheap cloud two-host lane, professional voice cloning for creator-voice narration. Key names never ship in defaults. NotebookLM audio overviews are Enterprise-API-only and never a dependency.
- Stable Audio Open stays opt-in only (its Hugging Face license click-through breaks a zero-friction install). Long-form structured music (musicgen-medium or an external tool) is unaddressed.

### Editor export lanes

- xmeml (Premiere Pro) and edl (CMX3600) export lanes alongside the implemented fcpxml exporter; OpenTimelineIO adapters are the likely implementation path. Until then Premiere users work from cutplan.md, edl.json, and the always-rendered preview/final.

### Cross-platform transcription

- A supported non-Apple-Silicon local lane. Today the documented fallbacks are whisper.cpp and faster-whisper (they normalize fillers away, so cut quality drops); parakeet-mlx stays the reference on Apple Silicon.
- Metered API providers behind the same `[transcription]` switch if demand shows up, opt-in only: deepgram-nova3 (keyterm biasing), elevenlabs-scribe (same output shape as the parakeet lane).

### Shorts karaoke captions

- Word-level karaoke caption system for the short format, built on Remotion, driven by the same word timestamps the cut lane already produces.

### Decks and whiteboards

Document (in format profiles, and possibly a beat type) when to use which visual lane:

- Bespoke HTML slide decks and explainers: rendered by the graphics engines, frame-accurate, brand-tokened, timed to spoken words; they end up in the final video as footage or overlays.
- Excalidraw: a live virtual whiteboard the creator drives during a screen-share recording, or pre-generated scenes prepared to present and annotate live; also exportable as static SVG/PNG assets.
- Rule of thumb: if it plays in the final render timed to the script, generate it; if the creator talks over and around it while recording, whiteboard it.

### Retention analytics feedback

- Read the creator's YouTube analytics (retention curves, CTR) to tune density tiers, CTA placement, and packaging templates through mc-retro, closing the loop with data instead of memory.

### Upload and scheduling automation

- YouTube API publishing and A/B test submission. 1.0 produces blessed assets under the one-blessed-asset-per-slot convention; the creator uploads.

## Release path

- Bump version in marketplace.json, tag, then PR to the bmad-plugins-marketplace registry (registry/community/).
