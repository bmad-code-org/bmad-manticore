# TODO / Roadmap

State as of 2026-07-04. Read AGENTS.md first (module conventions and design invariants), then `skills/mc-pipeline/PIPELINE.md` (the runtime contract). This file is the work queue and roadmap; delete items as they land.

## Where things stand

- The module is fully scaffolded in shareable BMad module shape: marketplace.json, 15 self-contained skills (12 stages + mc-setup + mc-ograf + mc-agent), the studio config layer ([modules.manticore] in _bmad/custom/config.toml via the installed resolve_config.py, plus per-skill customize.toml via resolve_customization.py), format profiles, and user docs.
- mc-agent (Manny the Manticore, added 2026-07-05) is the persona front door, built on the BMad agent-skill pattern (persona and menu in the `[agent]` block of customize.toml, mirroring bmm's bmad-agent-analyst). It routes to the other skills and owns onboarding, coaching, creator memory ({brand-path}/creator-profile.md), and the grow-the-studio guidance. Follow-on when wanted: the memory/sanctum agent archetype from bmad-agent-builder for cross-session autonomy.
- All scripts run via `uv run` and carry PEP 723 inline metadata; declare script dependencies there, never ask users to pip install.
- Tested and working: `mc-new/scripts/new_project.py`, `mc-script/scripts/lint_script.py` (duplicated into mc-outline and mc-package), `mc-setup/scripts/check_deps.py`, and the mc-ograf scaffold/verify pair (with tests). Config resolution rides the installed core scripts (resolve_config.py, resolve_customization.py); the module bundles no resolver.
- The cut lane is IMPLEMENTED and verified against real footage (2026-07-05, project camera-a-test): `transcribe.py` (parakeet-mlx lane, output byte-identical to the validated run), `cutplan.py` (silence/filler/stutter/retake/marker detectors, calibrated on the same run), `edl_to_fcpxml.py` (FCPXML 1.9, exact rational times, outward frame snapping, refuses VFR), `render_preview.py` (draft render + 30 ms fades + boundary-frame extraction), each with a unit suite in `mc-cut/scripts/tests/`. Editor-import sync verification in Resolve is still owed on the first real project (the checklist enforces it).
- Contract stubs, NOT implemented: `farm_asset.py`, `render_verify.py`, `resolve_import.py` (requires DaVinci Resolve Studio; the scripting API is not in the free edition). Each docstring carries the full I/O contract; keep those contracts, they match what the skills expect.
- User docs: README (the landing-page pitch, tool menu, honest status) + `docs/user-guide.md` (the studio walkthrough). Keep both in sync with config surface changes.

## Build order

1. The cut lane (the core payoff): DONE except the editor half of the acceptance test. Scripts implemented and verified against the camera-a-test footage (see Where things stand). Remaining: import `cut/rough.fcpxml` into Resolve (File > Import Timeline), VERIFY SYNC at the first, a middle, and the last cut boundary against preview.mp4 before trusting the converter. The known failure mode is FCPXML desync from variable frame rate sources; the converter now refuses VFR input outright.
2. Graphics lane: initialize `{engines-path}/hyperframes/` with a PINNED version (pre-1.0, moves fast; v0.7.26 as of 2026-07-03); implement `render_verify.py`; write MOTION_PHILOSOPHY.md with a pre-flight checklist wired in as a lint gate, plus the per-comp meta.json render contract and its loop (edit, lint, preview, draft render at CRF 28, single-frame verify, final render). Build the word-level karaoke caption system for the short format.
3. `farm_asset.py` (xAI Imagine API: image ~$0.02, video ~$0.05/s, submit/poll/download REST; Veo 3.1 via Gemini API as the escalation lane) and `resolve_import.py` (Resolve Studio external Python API; a community DaVinci Resolve MCP server is the interactive alternative).
4. Proving run: take one real video idea from brain dump to a rough cut sitting in an editor.

## New capabilities (designed 2026-07-04, not started)

### Multitrack and multicam support

Many creators record multitrack: a full-screen talking-head file plus one or more screen-share files, usually sharing the same audio, so sources are waveform-syncable. Manticore should handle this end to end:

- Ingest multiple numbered sources per project: talking-head takes (1..n), screen shares (1..n), plus loose assets to place where the discussion warrants.
- Sync audio-bearing sources automatically by waveform correlation; fall back to content-based placement for assets with no syncable audio.
- Extend `cut/edl.json` with track and layout fields so it stays the neutral source of truth: which source is live, and in what composition (full-screen talking head, small shaped picture-in-picture over the screen share, side-by-side, and so on).
- Decide the switch points unassisted from context (when the words reference the screen, switch to it; when it is story or opinion, come back to the face), with clean transitions. The proposed switches are taste calls presented at gate 2 like any other cut decision.
- Export the multitrack result through the same `[editor]` lanes (FCPXML with stacked tracks first).

### Scheduled agent runs: a harness-agnostic core skill, plus mc-research on top

Split into two pieces (decided 2026-07-05). The generic half belongs in BMad core so any module can use it; Manticore then ships a thin niche-research layer over it.

The core skill (target: the BMad core module, built in a bmad-bmm worktree, its own PR):

- Sets up scheduled headless agent runs on the user's own machine with whatever agent CLI they have. Researched 2026-07-05: anthropics/launch-your-agent is NOT this (it deploys cloud-hosted Claude Managed Agents with cloud-side scheduling); the local, harness-agnostic version is new ground.
- Detects installed CLIs and their headless forms: `claude -p "<prompt>" --output-format json`, `gemini -p "<prompt>" --output-format json` (add `--yolo` only in trusted envs), `codex exec "<prompt>"` (final message to stdout, progress to stderr). One wrapper parameterizing `{cli} {flags} "{prompt}"` covers all three; the user picks the default.
- Writes a PEP 723 uv wrapper script per job: invoke the CLI, write output to a dated file, capture stdout+stderr to a log.
- Installs the schedule per platform: launchd plist in ~/Library/LaunchAgents on macOS (survives sleep/reboot better than cron), crontab on Linux (systemd timer as the alternative). Handles the classic cron traps explicitly: minimal PATH and no shell profile, so absolute path to the CLI binary and exported env vars for keys; MAILTO="" against mail spam.
- Jobs are listed, edited, and removed by the same skill; every job's prompt, schedule, and output location are plain files the user can read.

mc-research (Manticore's layer, once the core skill exists; interim: mc-research can carry the scheduling mechanics itself and donate them to core later):

- Show prep for the creator's niche: topic lists, subscriptions, channel/URL/subreddit lists, maintained by the skill; the daily job aggregates (web, X, YouTube via yt-dlp transcripts, RSS), distills, and writes a dated intel briefing into the studio (e.g. `manticore/research/YYYY-MM-DD-briefing.md`).
- Modes: scheduled daily briefings, on-demand runs, and a morning-podcast option: the briefing agent writes a two-host script, then a TTS lane renders it (providers below).
- Config in the studio config (a `[research]` sub-table of `[modules.manticore]`): sources, where raw retrieved content is stored if desired, where distillations and briefings land, delivery options. Crons can retrieve a lot; storage and retention choices are explicit config, never a surprise.
- mc-agent (Manny) fronts all of this: interviews the creator about their niche and standing interests (creator-profile.md), proposes the jobs, and routes here to install them.

Podcast/TTS lanes for the morning-podcast option (researched 2026-07-05):

- Default, free and local: Kokoro (82M params, Apache 2.0, ~327MB, faster than realtime on CPU, 54 voices). Not a native dialogue model: synthesize each host's turns separately and stitch. No API key, fits the local-first default.
- Cheap cloud two-host lane: Gemini TTS (gemini-2.5-flash-preview-tts and successors) has native multi-speaker output, max 2 speakers per request, speaker-to-voice mapping in speech_config, roughly $10 per 1M audio output tokens on the Flash tier.
- Premium lane: ElevenLabs Text to Dialogue (Eleven v3): no hard speaker cap, emotion tags, best quality, roughly $0.10 per 1,000 characters.
- NotebookLM audio overviews are Enterprise-API-only (notebooks.audioOverviews.create + Podcast API on GCP); the Gemini CLI does not expose audio generation. Treat it as an optional fourth provider for users who already have GCP Enterprise, never a dependency. Community wrappers (notebooklm-py) drive the consumer UI headlessly but are brittle and ToS-gray; do not build on them.

### Audio lanes: music, SFX, and TTS

- Sound effects and music generation for stingers, beds, and whooshes, as optional providers alongside the image/video lanes. API lane: ElevenLabs SFX v2 and Eleven Music (licensed training data, commercially cleared on paid plans from $6/mo). Free local lane: Stable Audio 3 (open weights, runs on Apple Silicon via CoreML, commercial use permitted under $1M annual revenue).
- Local TTS lane for the voiceover-explainer format: Kokoro (Apache 2.0, faster than realtime on CPU) or Chatterbox (MIT, blind-test competitive with ElevenLabs). ElevenLabs professional voice cloning (Creator plan) is the upgrade when the narration must sound like the creator.

### Decks and whiteboards

Document (in format profiles, and possibly a beat type) when to use which visual lane:

- Bespoke HTML slide decks and explainers: rendered by the graphics engines, frame-accurate, brand-tokened, timed to spoken words; they end up IN the final video as footage or overlays.
- Excalidraw: a live virtual whiteboard the creator drives during a screen-share recording, or pre-generated .excalidraw scenes prepared for the creator to present and annotate live; also exportable as static SVG/PNG assets.
- Rule of thumb: if it plays in the final render timed to the script, generate it; if the creator talks over and around it while recording, whiteboard it.

## Transcription decision (updated 2026-07-05, supersedes the 2026-07-04 research)

- parakeet-mlx is the default and reference provider: free, local (Apple Silicon), word timestamps, and it empirically preserves verbatim fillers ("um", "uh", "hmm", "whoops") with accurate timestamps. Validated 2026-07-05 on real footage in a full raw-take-to-cut test (model mlx-community/parakeet-tdt-0.6b-v3, project camera-a-test). The prior research assumption that parakeet cleans fillers away did not hold for this model.
- ElevenLabs Scribe is no longer needed as the reference. It stays a possible metered API lane behind the `[transcription]` switch (~$0.22 per footage hour) if a use case shows up; unimplemented until then.
- CrisperWhisper note kept for the record: CC BY-NC licensed, no MLX port; moot while parakeet holds.

## Backlog

- xmeml (Premiere) and edl (CMX3600) export lanes in `edl_to_fcpxml.py`; OpenTimelineIO adapters are the likely implementation path. Until then Premiere users work from cutplan.md + edl.json.
- elevenlabs-scribe transcription provider (metered API) behind the `[transcription] provider` switch, same output shape as the parakeet lane, if demand shows up.
- deepgram-nova3 transcription provider (keyterm biasing) if demand shows up.

## Release path

- Bump version in marketplace.json, tag, then PR to the bmad-plugins-marketplace registry (registry/community/).
