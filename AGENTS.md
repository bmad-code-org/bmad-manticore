# BMAD MANTICORE (module development context)

This repo is a shareable BMad Method module: an AI video production pipeline distributed as skills. This file is context for working ON the module. The runtime contract for USING it lives in `skills/mc-pipeline/PIPELINE.md`. The roadmap and open work live in `TODO.md`; read it before starting anything.

## The one design constraint

Taste lives in files. Mechanics live in scripts. Skills are thin routers between them. A stage skill never needs judgment: it loads the config, reads `project.json`, runs the named script, checks a checklist, writes the artifact, advances the state, and stops at gates.

## Module layout

| Path | Purpose |
|---|---|
| `.claude-plugin/marketplace.json` | Module manifest (install via `npx bmad-method install --custom-source <repo>`) |
| `skills/module.yaml` + `skills/module-help.csv` | Module identity and the help-catalog rows (canonical BMad schema). The installer merges every module's help csv into `{project-root}/_bmad/_config/bmad-help.csv`; mc-agent and the bmad-help core skill read that merged catalog. A new or changed skill must update its module-help.csv row |
| `skills/mc-agent/` | Manny the Manticore, the persona agent and studio front door: its SKILL.md carries the persona and the capabilities menu; routes to the other skills, never does stage mechanics itself |
| `skills/mc-pipeline/` | The router; owns `PIPELINE.md`, the master stage/gate/project.json contract |
| `skills/mc-setup/` | Configuration skill: writes the studio config (`[modules.manticore]` in `{project-root}/_bmad/custom/config.toml`); `assets/studio-defaults.toml` carries the full `[defaults]` seed, and the rest of `assets/` holds the templates it copies into the studio (tokens, blacklist starter, craft checklist, the production-bible and voice-bible specs, format profiles) |
| `skills/mc-audio/` | Audio service skill, not a stage: farms sound for other skills (Kokoro TTS/dialogue, MusicGen beds, AudioLDM2 SFX) from the `[audio]` lanes, local-first with paid rungs opt-in; heavy venv and model caches live in the creator's `{engines-path}/audio-lab` workspace |
| `skills/mc-*/` | The 11 stage skills; each resolves the studio config on activation and stops at gates |
| `docs/user-guide.md` | "Configure your own Manticore studio", the end-user walkthrough |

## Conventions (binding when editing this module)

Reviewers: [docs/review-rules.md](docs/review-rules.md) is the ship-hygiene checklist, checked by code review agents rather than any lint.

- Nothing user-specific ships in the module. The creator's identity, brand, voice, paths, and tools live in their project via the studio config (`[modules.manticore]` in `_bmad/custom/config.toml`) and `{brand-path}`. If you find a personal name, brand color, or machine path in module content, that is a bug.
- Config keys are kebab-case (`brand-path`). API keys never appear in the TOML or any file; only env var names.
- A file that shapes the creator's output is theirs: it ships in `mc-setup/assets/` as the install source (`tokens.template.json`, `blacklist-starter.md`, `craft-checklist.md`, the two bible specs, the format profiles), mc-setup copies it into `{brand-path}` or `{formats-path}` on first run and never overwrites it after, and the consuming skill reads only the creator's copy. A template that shapes a pipeline artifact rather than the creator's taste stays module-owned in the skill that writes it: `mc-cut/assets/editorial-review-template.md` is the shape of `cut/editorial-review.md`, which mc-beats reads for its hand-to-beats seeds, so its sections are a downstream contract and it stays where it is (decided 2026-07-26).
- A skill reads ONLY its own folder, the installed core scripts (`{project-root}/_bmad/scripts/`), and project files. Never another skill's folder (some harnesses forbid it). Config resolution uses the installed `resolve_config.py` (studio config, `_bmad/custom/config.toml` with `config.user.toml` layered over it); the module bundles no resolver of its own, and no skill carries a private override surface. Skills must work under any harness that resolves skill folders; nothing may depend on Claude-specific features beyond the SKILL.md format itself.
- Scripts are invoked ONLY via `uv run` (never bare `python3`), and every script carries PEP 723 inline metadata (`# /// script` block with `requires-python = ">=3.11"`; declare dependencies there when a script needs any, so uv provisions them with no venv setup). Prefer stdlib. Every script lives in the skill that runs it; a script needed by more than one skill is duplicated into each. Scripts take explicit arguments (resolved paths, blacklist path) from the calling skill and do no config discovery of their own.
- Editor-agnosticism: `cut/edl.json` is the neutral source of truth; editor-specific behavior keys off `[editor]` in the config (timeline-format). Never hardwire Resolve into a stage that other editors' users run.
- Stubs carry their full I/O contract in the docstring and exit with a pointer to it.
- Gates are sacred: no edit may let a stage proceed past a gate without the creator's recorded approval.
- A check the pipeline claims to perform must be a script that exits non-zero. "Inspect X before proceeding" in a skill file is only acceptable next to a script that FAILS when X is wrong. This is the lesson of the 2026-07-24 cut-pipeline failures, where four separate defects shipped through the same hole: QC frames that were extracted but never asserted on, boundary frames eyeballed while the audio underneath was wrong, beat anchors that were a checklist line with no script, and a transcript nothing ever checked. Every one was documented and none could halt. Taste lives in files and mechanics live in scripts; an assertion is a mechanic, never a judgment call left to whoever is running the stage.

## Design invariants (settled decisions; change only with the maintainer's sign-off)

- Manticore always renders (render-first; maintainer sign-off recorded 2026-07-07, replacing the earlier editable-timeline-never-baked invariant). Every cut iteration produces a fast low-res preview; once the graphics stage has rendered overlays, the preview re-renders with them composited; at gate 4 a final-quality render is offered. The editor timeline export (per `[editor] timeline-format`) and all assets (edl.json, cutplan, overlays) are ALWAYS still produced alongside, so the creator can move into their editor at any step. The creator confirms this default during mc-setup.
- Local-first defaults, paid vendors opt-in only: no paid or metered vendor (ElevenLabs or any future TTS/SFX/music provider) ships in any default, key names included. Paid lanes exist only as explicit opt-in choices made during setup, and their key sourcing is mentioned only inside the opt-in branch of the interview.
- parakeet-mlx (model parakeet-tdt-0.6b-v3) is the reference cutting transcript: free, local, word timestamps, and empirically preserves verbatim fillers (validated on real footage 2026-07-05). Generic Whisper is not a substitute because it normalizes fillers away. Alternative providers (elevenlabs-scribe, deepgram-nova3) go behind the `[transcription]` switch with the same output shape.
- EVERY transcription lane windows in short isolated windows (20s, 3s overlap). This is not an implementation detail to tune: parakeet silently drops whole paragraphs inside long windows, with no error, and long-window transcription is what corrupted a real project on 2026-07-24. Measured on that take: 120s chunks lost three paragraphs, 90s windows still lost content, 20s windows were complete. Nothing may transcribe a whole file in one pass, and `verify_transcript.py` must pass before any transcript is consumed.
- The two-source rule: the TRANSCRIPT is the authority on CONTENT, the AUDIO is the authority on TIMING. parakeet absorbs pauses into the preceding word's end, so transcript gaps read about 0.0 across real dead air and word ends reach past the sound. No stage derives a cut time, a beat time, or a silence from transcript timestamps; silence comes from `analyze_audio.py` and cut edges snap into it.
- A deliverable path is never written directly. Renders go to a per-process temp file, decode-validate, check they have not been superseded, then atomically move into place. Any shared artifact a concurrent process may read (edl.json, preview proxies) is written the same way. Two processes interleaving into one output path handed a creator an unplayable preview on 2026-07-24.
- Generated footage never depicts UI or text that must be accurate; real UI comes from screen recordings.
- Baked alpha is the graphics deliverable that works in every editor. The one exception, signed off 2026-10-16, is Fusion titles on the DaVinci Resolve lane: editable text graphics from creator-owned templates, placed by script and editable in Resolve or through Blackmagic's MCP. The editable-graphics lane (OGraf, gated on `[editor] ograf-editable`) was removed 2026-07-26: it was a second authoring path, a second spec to conform to, and a second set of verification scripts, all to serve one editor version. `ograf` remains a permanent compatibility alias for `hyperframes`, and `ograf-editable` is ignored wherever a pre-3.0.0 config still carries it.
- Four approval gates (outline, cutplan, beats, final) are hard stops; nothing weakens them.
- Absence is never silent. A skill loads the creator files it needs on activation; when one is missing it names the file, says what it cannot do without it, routes to mc-setup, and stops. The only permitted alternative is a fallback the skill states out loud at the moment it bites, and no fallback may change creative output without saying so.
- Bare paths are the current video project. A bare path in skill prose (`beats/beats.md`, `cut/edl.json`, `project.json`) resolves against `{video-path}`, the video project being worked on. Files inside the skill's own folder always carry `{skill-root}`, and `{project-root}` keeps its meaning of the repo working directory. A path led by a skill name (`mc-cut/scripts/preflight.py`) records which skill owns a file; a skill still reads only its own folder.
- Every file in a skill addresses the model executing it, never a human reader. This binds everything a skill ships and progressively discloses, references, asset prose, templates and engine docs, not only SKILL.md: no citation blocks, no source URLs, no provenance or dated research claims. A URL a skill may name is one it installs from or runs, never one it cites; provenance worth keeping goes in the commit message or `TODO.md`.
- Taste in files, mechanics in scripts (via uv), skills as thin routers, so lesser models can run the pipeline.

## Repo rules

- Changes land via PR; do not push directly to main.
- Version discipline: bump `version` in marketplace.json on release; tag releases before marketplace submission.
