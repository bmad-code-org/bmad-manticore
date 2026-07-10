---
name: mc-prompter
description: Browser teleprompter for the record stage and standalone shows. A service skill like mc-audio, no stage, no gate, no project.json state. Launch a local prompter server, feed it the project script.md or any text, and the creator records at their own pace with a phone remote over LAN. Classic teleprompter only in this version; voice-follow and producer mode are planned tiers.
---

# mc-prompter

The record stage is creator-owned; this skill hands the creator a teleprompter for it. It is a service skill: it owns no stage, stops at no gate, and writes no project state. It launches a local web server that serves a fullscreen prompter display, a phone remote, a home page for loading and editing the script, and an OBS overlay placeholder. Everything runs offline on the creator's machine; no models, no downloads, no external requests.

## Steps

1. Load this skill's own surface (`uv run {project-root}/_bmad/scripts/resolve_customization.py --skill {skill-root}`; run `{workflow.activation_steps_prepend}` now, `{workflow.activation_steps_append}` after this step, and hold `{workflow.persistent_facts}` as standing context). Take the default port from `[prompter] port`. If a studio config exists, also load it (`uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`) and take `[prompter] port` and `[owner] wpm` when present; a missing studio config is fine here, unlike the stage skills, because standalone shows need no studio.
2. Locate the script. Inside a pipeline project ("record with the teleprompter"), it is the project's `script.md` under the project folder. Standalone, it is any file path the creator names, markdown or plain text. No file at all is also valid: launch without `--script` and the creator pastes text on the home page.
3. Launch: `uv run {skill-root}/scripts/run_prompter.py --script <path>` with `--port <N>` when the config or the creator sets one and `--owner-wpm <N>` when `[owner] wpm` is known. Add `--lan` only when the creator wants the phone remote or a tablet display; it binds the LAN and on Windows triggers a firewall consent dialog. The launcher probes the port, prints the local URL, the remote URL with its session token, and the session file path, then keeps the server running until Ctrl-C.
4. Give the creator the URLs from the launcher output: the prompt page for the recording display, and the remote URL (token included) to open on a phone when `--lan` is on. Briefly explain the pages: `/` is home (load a file, paste text, edit in place, copy the remote URL), `/prompt` is the fullscreen scroller with keyboard controls and a settings drawer (press `?` there for all shortcuts), `/remote` is the phone controller, `/overlay` is an OBS browser-source placeholder for now.
5. Explain what the display does with pipeline markers: paragraphs carrying a `[TAKE ...]` marker render dimmed with a "have it already" badge because that line was already spoken well in the interview footage, and a toggle hides them entirely; sentences flagged `[INVENTED]` get a subtle badge, toggleable off; other bracketed text renders as a dimmed note and is never counted in timing.
6. If the creator edits the script from the home page and saves, the server first copies the current file to a timestamped backup under the temp session directory, then writes the edit back to the source file, so the prompted text and the pipeline artifact never silently diverge. "Session only" applies the edit without touching the file.
7. When the creator asks for voice-follow scrolling or producer mode, say plainly that those are planned tiers that have not landed yet and the classic prompter is what works today. Never pretend they work and never improvise a substitute.

## Rules

- The prompter never advances pipeline state; when a pipeline project is being recorded, the record stage remains the creator's, and mc-pipeline stays the source of truth.
- The server binds localhost by default; `--lan` is opt-in and the remote URL carries a per-session token so a random LAN device cannot drive the prompter mid-show.
- Loading files by path and saving edits work only from the server machine, never from a LAN device.
- Stop and relay the launcher's guidance when it exits nonzero: a missing or unreadable script path, an explicit port already held by another session, or a server that failed to start are all creator-facing problems, not things to retry silently.

## Checklist

- The port and wpm came from config when a studio config exists; defaults otherwise.
- The creator got both URLs (prompt page, remote with token) and knows the pages.
- Take and invented markers were explained if the script contains them.
- Any in-place save was backed up first (the server does this; confirm the backup path in its response).
- No planned tier was presented as working.
