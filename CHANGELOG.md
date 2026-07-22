# Changelog

All notable changes to BMad Manticore are documented here. Dates are ISO (YYYY-MM-DD).

## Unreleased

### Cross-platform: Windows, Linux, and Intel Mac lanes (code-complete, pending real-hardware validation)

- Transcription is cross-platform: transcribe.py gains an onnx-asr provider running the same parakeet-tdt-0.6b-v3 weights as an ONNX conversion, so verbatim fillers and 80 ms word timestamps carry over on Windows, Linux, and Intel Macs. New default provider `auto` picks parakeet-mlx on macOS Apple Silicon (byte-identical to the 1.0 reference lane) and onnx-asr everywhere else; dependencies select per platform via PEP 508 markers, CUDA machines escalate with `uv run --with "onnx-asr[gpu,hub]" python <script-path>` (the `python` command skips the script's cpu-extra dependency so onnxruntime-gpu never co-installs with onnxruntime, and the script warns when an NVIDIA GPU is visible but CUDA is unavailable), long audio is chunked in 20 s windows with 2 s overlap and merged deterministically with a seam-repair pass so boundary words are never duplicated or dropped, and words.json stays byte-compatible across lanes. Honesty rule: when the ONNX runtime exposes no per-token scores, every confidence reads 1.0 (no signal), never a fabricated number. The `[transcription]` default moved from "parakeet-mlx" to "auto"; whisper.cpp and faster-whisper are no longer the documented fallbacks.
- Install-time platform detection: check_deps.py now detects OS, CPU architecture, and GPU vendor (nvidia-smi, /sys/class/drm PCI ids, wmic/PowerShell fallbacks) and emits a platform verdict, in `--json` as a kebab-case `platform` object (os, arch, apple-silicon, gpu, gpu-detail, recommended{stack-file, transcription, torch-index, encoder-ladder, svg-rasterizer, fonts}) and as a table-mode stack block. Three new stack reference files at skills/mc-setup/references/stack-{macos,windows,linux}.md carry the per-OS defaults, Windows notes (short engines-path, LongPathsEnabled, gyan.dev build, OBS over Game Bar), Linux notes (PipeWire/Wayland capture, noto-color-emoji, the free-Resolve H.264/HEVC/AAC codec caveat), vMix and Wirecast alpha notes, and the per-OS DaVinci Resolve Fusion Scripts paths for the free-edition scripted-import lane. mc-setup reads the recommended stack file during the interview.
- Hardware encoder ladders: the final render and the VFR remux pick per-OS hardware encoders validated by a real one-frame test encode (Windows: h264_nvenc, then h264_qsv, then h264_amf; Linux: h264_nvenc, then h264_vaapi wired end to end with hwupload; libx264 fallback everywhere). The preview render, plain and graphics-composited alike, stays libx264 crf 28 veryfast by design on every OS. macOS videotoolbox behavior is byte-for-byte unchanged.
- mc-audio portability: the audio-lab venv interpreter resolves per OS (.venv/bin/python vs .venv\Scripts\python.exe), Windows machines with an NVIDIA GPU install torch from the PyTorch cu126 index (roughly 2.5 to 3 GB extra, surfaced in the consent message and the `--dry-run` torch field), and MusicGen/AudioLDM2 pick cuda, then mps, then cpu. macOS behavior is unchanged.
- Small portability fixes: edl_to_fcpxml.py emits valid Windows file URIs (file:///C:/... and UNC shares) via Path.as_uri() with byte-identical POSIX output; farm_asset.py resolves registered tools with a PATH lookup (Windows npm .cmd/.exe shims launch by bare name), documents POSIX quoting for headless templates on every OS, and refuses to pass arguments containing cmd.exe metacharacters (embedded double quotes, % ^ & | < >) to a .cmd/.bat shim, failing loudly with a re-register hint instead of letting cmd.exe corrupt or expand them; verify_ograf.py prints per-OS manual verification steps and, from a human terminal, serves the package and opens preview.html in the default browser itself. transcribe.py, edl_to_fcpxml.py, and render_final.py read and write their JSON, FCPXML, and concat-list artifacts with explicit UTF-8 so non-ASCII transcripts and paths survive on Windows locale codecs (cp1252).

### Added

- Final renders are loudness-normalized by default to -14 LUFS (two-pass ffmpeg loudnorm, TP -1.5, LRA 11; audio-only second pass with the video stream copied), configurable via `[render]` loudness-target and disable-able via `[render]` loudnorm = false or `--no-loudnorm`. Preview renders are never normalized. Silent audio skips the pass with a warning instead of failing.
- mc-package emits uploadable captions and a publishable transcript from the edited timeline: the new stdlib-only captions.py maps each EDL segment's word spans onto output-timeline times (reordered and multi-source edits included) and writes packaging/captions/final.srt, final.vtt, and transcript.md. Caption defaults: 42 chars per line, 2 lines, 1 to 7 s cues, splits at sentence ends, pauses, and cuts. A light filler/stutter cleanup applies to the caption rendition only (`--no-clean` keeps verbatim); transcript/words.json is never modified.
- mc-stream-pack produces and verifies the OBS WebM VP9 alpha deliverable from a ProRes 4444 master in one command (render_verify.py `--transcode-webm`, with `--webm-crf`), with clear errors when ffmpeg lacks libvpx-vp9 or the master has no alpha, plus a pixfmt-failure hint carrying the exact re-encode flags. The stream-pack copy of render_verify.py is now a superset of mc-graphics' copy.

### Documentation

- README platform matrix and the user guide rewritten for the cross-platform reality; the Resolve handoff reference gains the free-edition Fusion Scripts install paths, the Linux free-edition codec caveat, and an opt-in pointer to the community samuelgursky/davinci-resolve-mcp server for Studio users (Manticore itself still requires no MCP server).

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
- If your recorded footage uses the old marker cue, set the cue at setup (mc-setup records it as a `--marker-cues` override in mc-cut's `cutplan_flags`, in `_bmad/custom/mc-cut.toml`) or pass `--marker-cues "question from claude"` directly.

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
