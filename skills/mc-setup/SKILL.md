---
name: mc-setup
description: First-run (or any-time) configuration for the Manticore video pipeline. Creates and updates the studio config ([modules.manticore] in {project-root}/_bmad/custom/config.toml), verifies dependencies and MCPs, registers the creator's generation CLIs with usage notes, and scaffolds the brand/formats/projects folders. Run when any mc-* skill reports missing config, or to change tools later.
---

# mc-setup

Idempotent: on re-run, existing values are shown as defaults and only what the creator wants changed is changed. Never clobber, never silently overwrite.

The studio config is one table, `[modules.manticore]`, in `{project-root}/_bmad/custom/config.toml` (personal overrides in `config.user.toml` next to it). Every mc-* skill resolves it with the installed `{project-root}/_bmad/scripts/resolve_config.py`. The full default values ship in this skill's `customize.toml` under `[defaults]`.

## Steps

### 0. Bootstrap BMad core

Check four paths: `{project-root}/_bmad/config.toml`, `{project-root}/_bmad/scripts/resolve_config.py`, `{project-root}/_bmad/scripts/resolve_customization.py`, `{project-root}/_bmad/custom/`. All present: go to step 1. Any missing: the project is not BMad-initialized; say so and confirm before running the installer (it writes `{project-root}/_bmad/` plus IDE integration files for the chosen tool). Resolve the tool id first: `claude-code` under Claude Code; otherwise run `npx -y bmad-method install --list-tools` and let the creator pick.

- No `{project-root}/_bmad/` at all: `npx -y bmad-method@latest install --directory {project-root} --modules core --tools <tool-id> -y`. Never omit `--modules core` (a bare `-y` installs the default module set, not just core) or `--tools` (fresh `-y` installs fail without it).
- `{project-root}/_bmad/` exists but incomplete: `npx -y bmad-method@latest install --directory {project-root} -y` (quick-update re-syncs `{project-root}/_bmad/scripts/` and keeps configured tools). If that fails, retry with `--action update --modules core --tools <tool-id>` added.

Verify: both resolver scripts exist under `{project-root}/_bmad/scripts/`, `uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore` exits 0 (empty output just means the interview has not run), and `uv run {project-root}/_bmad/scripts/resolve_customization.py --skill {skill-root}` returns this skill's `[defaults]`. If uv itself is missing, do step 2's uv bootstrap before verifying. On verification failure, stop and surface the installer output; never hand-copy scripts or vendor a resolver. If npx, node, or the network is unavailable, or the creator declines, have them run `npx bmad-method install` interactively from the project root (core alone is enough for Manticore), then re-run mc-setup. A stale npx cache can serve an old CLI missing current flags; on unknown-option errors, run `npm cache clean --force` and retry.

### 1. Locate and load

Resolve the current state: `uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`. Load the defaults: `uv run {project-root}/_bmad/scripts/resolve_customization.py --skill {skill-root}` (run `{workflow.activation_steps_prepend}` now, `{workflow.activation_steps_append}` after this step, and hold `{workflow.persistent_facts}` as standing context). If `[modules.manticore]` already has values, tell the creator this is an update pass; offer the sections below as a menu instead of walking all of them.

### 2. Dependencies

Bootstrap first: check `uv --version`. If uv is missing, offer to install it; otherwise the official installer from docs.astral.sh/uv, and wait for the creator's confirmation; every pipeline script runs through uv, so nothing works without it.

Then run `uv run {skill-root}/scripts/check_deps.py`. Report what is missing with the exact install command (brew/apt/winget as fits the platform). Install nothing without the creator confirming each item.

### 3. The basics interview

Ask, offering current values (or the `[defaults]` from customize.toml) as defaults:

- Name and channel/brand name (`[owner]`).
- Description links, in order (`[owner] links`).
- Speaking rate: if they have a published transcript, offer to measure it (word count / duration); otherwise leave 145 with a note that mc-script will flag it as unmeasured.
- Paths: accept the defaults (`manticore/brand`, `manticore/formats`, `manticore/projects`, `manticore/engines`) unless they have a place they want things, e.g. an existing brand folder via `brand-path`.
- Editor (`[editor]`): which NLE they finish in. Set `timeline-format` accordingly: fcpxml for DaVinci Resolve or Final Cut Pro; xmeml/edl for Premiere; none for Descript or manual workflows (they get the cut plan, edl.json, and preview instead of a timeline file). Set `ograf-editable = true` ONLY for DaVinci Resolve 21+.
- Transcription (`[transcription]`): default parakeet-mlx (free, local on Apple Silicon, verbatim fillers preserved, no API key). Metered API lanes (elevenlabs-scribe, deepgram-nova3) exist behind the same switch if they ever need them.

Be honest about lane status: the comments in this skill's `customize.toml` under `[defaults.editor]` and `[defaults.transcription]` mark each timeline and transcription lane as implemented or planned; relay that, and never promise a planned lane as working.

### 4. Scaffold the instance folders

Create the four path folders if missing. Into `{brand-path}`:

- `tokens.json` from `{skill-root}/assets/tokens.template.json`, then walk the creator through their canvas/accent/text colors and fonts (if they have a brand system doc, read it and fill tokens from it instead of interviewing).
- `blacklist.md` from `{skill-root}/assets/blacklist-starter.md`.
- `voice-bible.md` from `{skill-root}/assets/voice-bible-spec.md` (the build spec; building the bible itself is a separate session).
- Empty `exemplars/` and `headshots/` folders.

Into `{formats-path}`: copy every profile from `{skill-root}/assets/formats/` that does not already exist there (never overwrite; the creator's copies accumulate learnings).

### 5. CLI tool registry (1-n)

Ask what they use for image generation, video generation, and offloaded research: grok, gemini, agy, codex, anything. For each tool:

- name, capabilities (image/video/research/...), the exact headless invocation, preferred models.
- The `notes` field: everything future sessions must remember about driving it (quirks, output behavior, what it is bad at). Write it down now; this is the memory that stops every session from rediscovering the tool.
- Verify with permission: run the tool's version/help command to confirm it exists on PATH; note verified/unverified in `notes`.

Write each as a `[[modules.manticore.tools]]` block. Also set `[assets]` lane defaults (image-provider, video-provider, escalation-provider) to API providers or tool names.

### 6. MCP servers

Ask which apply (eg: `davinci-resolve` for DaVinci Resolve users; extend this list as the module grows). For each wanted server: check `claude mcp list` (or the harness equivalent); if absent, show the exact add command and ask before running it. Record the result as true/false under `[mcp]`. If the harness is not Claude Code, record false and note the manual setup in conversation.

### 7. API lanes and keys

For the API lanes they will use (xAI images/video, Veo escalation, and transcription only if they switched off the local default): confirm the env var names in the config, then check whether each env var is set (presence only; NEVER read or echo values). For any missing, tell them where to get the key (console.x.ai, billed separately from a grok CLI subscription; Google AI Studio; ElevenLabs dashboard) and which env var to export. Keys never go in the TOML or in chat.

### 8. Write and confirm

Write the interview results as the `[modules.manticore]` table (with its sub-tables: owner, paths, video, editor, transcription, assets, mcp, and `[[modules.manticore.tools]]` entries) into `{project-root}/_bmad/custom/config.toml`. Edit surgically: create the file if needed, preserve everything else in it (other modules configure themselves there too), and preserve any sections the creator skipped. Mention `config.user.toml` for personal overrides in shared repos. Verify by running `uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore` and showing the resolved summary. Point at mc-new to start the first project, and at `{brand-path}/voice-bible.md` as the highest-value next build.

## Rules

- Confirm before every install, every MCP add, every command that changes the system.
- Presence checks only for secrets; never read, echo, or store key values.
- Re-runs edit the existing config surgically; a re-run with no changes writes nothing.
- Never touch `{project-root}/_bmad/config.toml` (installer-owned); Manticore's home is the `custom/` layer.
