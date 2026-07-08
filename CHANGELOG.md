# Changelog

All notable changes to BMad Manticore are documented here. Dates are ISO (YYYY-MM-DD).

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
