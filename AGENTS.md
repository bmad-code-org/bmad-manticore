# BMAD MANTICORE (module development context)

This repo is a shareable BMad Method module: an AI video production pipeline distributed as skills. This file is context for working ON the module. The runtime contract for USING it lives in `skills/mc-pipeline/PIPELINE.md`. The roadmap and open work live in `TODO.md`; read it before starting anything.

## The one design constraint

Taste lives in files. Mechanics live in scripts. Skills are thin routers between them. A stage skill never needs judgment: it loads the config, reads `project.json`, runs the named script, checks a checklist, writes the artifact, advances the state, and stops at gates.

## Module layout

| Path | Purpose |
|---|---|
| `.claude-plugin/marketplace.json` | Module manifest (install via `npx bmad-method install --custom-source <repo>`) |
| `skills/module.yaml` + `skills/module-help.csv` | Module identity and the help-catalog rows (canonical BMad schema). The installer merges every module's help csv into `{project-root}/_bmad/_config/bmad-help.csv`; mc-agent and the bmad-help core skill read that merged catalog. A new or changed skill must update its module-help.csv row |
| `skills/mc-agent/` | Manny the Manticore, the persona agent and studio front door: a skill whose `[agent]` block in `customize.toml` carries the persona and capabilities menu (the BMad agent pattern); routes to the other skills, never does stage mechanics itself |
| `skills/mc-pipeline/` | The router; owns `PIPELINE.md`, the master stage/gate/project.json contract |
| `skills/mc-setup/` | Configuration skill: writes the studio config (`[modules.manticore]` in `{project-root}/_bmad/custom/config.toml`); its `customize.toml` carries the full `[defaults]`; `assets/` holds the templates it copies into the studio (tokens, blacklist starter, voice-bible spec, format profiles) |
| `skills/mc-ograf/` | OGraf graphics authoring (scaffold, verify, spec references); gated on `[editor] ograf-editable` for the editor lane, always available for the OBS/SPX-GC live lane |
| `skills/mc-audio/` | Audio service skill, not a stage: farms sound for other skills (Kokoro TTS/dialogue, MusicGen beds, AudioLDM2 SFX) from the `[audio]` lanes, local-first with paid rungs opt-in; heavy venv and model caches live in the creator's `{engines-path}/audio-lab` workspace |
| `skills/mc-*/` | The 12 stage skills; each resolves the studio config + its own `customize.toml` on activation and stops at gates |
| `docs/user-guide.md` | "Configure your own Manticore studio", the end-user walkthrough |

## Conventions (binding when editing this module)

- Nothing user-specific ships in the module. The creator's identity, brand, voice, paths, and tools live in their project via the studio config (`[modules.manticore]` in `_bmad/custom/config.toml`) and `{brand-path}`. If you find a personal name, brand color, or machine path in module content, that is a bug.
- Config keys are kebab-case (`brand-path`). API keys never appear in the TOML or any file; only env var names.
- A skill reads ONLY its own folder, the installed core scripts (`{project-root}/_bmad/scripts/`), and project files. Never another skill's folder (some harnesses forbid it). Config resolution uses the installed `resolve_config.py` (studio config) and `resolve_customization.py` (per-skill trio: packaged `customize.toml` defaults, `_bmad/custom/<skill>.toml`, `<skill>.user.toml`); the module bundles no resolver of its own. Skills must work under any harness that resolves skill folders; nothing may depend on Claude-specific features beyond the SKILL.md format itself.
- Scripts are invoked ONLY via `uv run` (never bare `python3`), and every script carries PEP 723 inline metadata (`# /// script` block with `requires-python = ">=3.11"`; declare dependencies there when a script needs any, so uv provisions them with no venv setup). Prefer stdlib. Every script lives in the skill that runs it; a script needed by more than one skill is duplicated into each. Scripts take explicit arguments (resolved paths, blacklist path) from the calling skill and do no config discovery of their own.
- Editor-agnosticism: `cut/edl.json` is the neutral source of truth; editor-specific behavior keys off `[editor]` in the config (timeline-format, ograf-editable). Never hardwire Resolve into a stage that other editors' users run.
- Stubs carry their full I/O contract in the docstring and exit with a pointer to it.
- Gates are sacred: no edit may let a stage proceed past a gate without the creator's recorded approval.
- Docs style: no em-dashes, blank line after every heading, no bold in list items, ISO dates.

## Design invariants (settled decisions; change only with the maintainer's sign-off)

- Manticore always renders (render-first; maintainer sign-off recorded 2026-07-07, replacing the earlier editable-timeline-never-baked invariant). Every cut iteration produces a fast low-res preview; once the graphics stage has rendered overlays, the preview re-renders with them composited; at gate 4 a final-quality render is offered. The editor timeline export (per `[editor] timeline-format`) and all assets (edl.json, cutplan, overlays) are ALWAYS still produced alongside, so the creator can move into their editor at any step. The creator confirms this default during mc-setup.
- Local-first defaults, paid vendors opt-in only: no paid or metered vendor (ElevenLabs or any future TTS/SFX/music provider) ships in any default, key names included. Paid lanes exist only as explicit opt-in choices made during setup, and their key sourcing is mentioned only inside the opt-in branch of the interview.
- parakeet-mlx (model parakeet-tdt-0.6b-v3) is the reference cutting transcript: free, local, word timestamps, and empirically preserves verbatim fillers (validated on real footage 2026-07-05). Generic Whisper is not a substitute because it normalizes fillers away. Alternative providers (elevenlabs-scribe, deepgram-nova3) go behind the `[transcription]` switch with the same output shape.
- Generated footage never depicts UI or text that must be accurate; real UI comes from screen recordings.
- OGraf output only where the target supports it: `[editor] ograf-editable` for the editor lane, always for the OBS/SPX-GC live lane. Default deliverable is baked alpha, which works everywhere.
- Four approval gates (outline, cutplan, beats, final) are hard stops; nothing weakens them.
- Taste in files, mechanics in scripts (via uv), skills as thin routers, so lesser models can run the pipeline.

## Repo rules

- Changes land via PR; do not push directly to main.
- Version discipline: bump `version` in marketplace.json on release; tag releases before marketplace submission.
