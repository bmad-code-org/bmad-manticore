---
name: mc-setup
description: Configure the studio, brand, and generation tools. Use on a missing-config report, or when the user says "set up manticore", "change my tools", or "update my studio".
---

# mc-setup

Act as the studio's configurator. The outcome is a studio config every mc-* skill can resolve, a brand folder with real content in it rather than placeholders, and an honest report of what is still missing.

Two consumers set the bar. Every other mc-* skill reads `[modules.manticore]` and fails closed without it, so a half-written config is worse than none. The creator needs to know what will actually happen on their first project before they start one, which is why this stage ends in a runnability report rather than a success message.

Idempotent throughout: on re-run, existing values are the defaults offered, only what the creator wants changed is changed, and a re-run with no changes writes nothing. Never clobber, never silently overwrite.

## Resolution rules

- Bare paths resolve against `{video-path}`, the current video project at `{projects-path}/<slug>/`.
- `{skill-root}` → this skill's installed directory; files in it always carry it (`{skill-root}/references/bootstrap.md`).
- `{project-root}` → the project working directory.
- `{brand-path}`, `{formats-path}`, `{projects-path}`, `{engines-path}` → the `[paths]` values from the studio config, resolved against `{project-root}`.

## On Activation

1. Check three paths: `{project-root}/_bmad/config.toml`, `{project-root}/_bmad/scripts/resolve_config.py`, `{project-root}/_bmad/custom/`. Any missing means the project is not BMad-initialized; load `{skill-root}/references/bootstrap.md` and finish it before continuing.
2. Resolve the current state: `uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`.
3. Read `{skill-root}/assets/studio-defaults.toml`. Its `[defaults]` tables are the seed values for the whole interview and the authority on every default.

Then route on the resolved config:

| State | What to do |
|---|---|
| Empty | First run. Work through everything below. |
| Present, missing any of `[render]`, `[style]`, `[cta]`, `[live]`, `[audio]` | A 0.x studio. Load `{skill-root}/references/migration-0x.md`. |
| Present and complete | An update pass. Say so and offer the sections below as a menu; do not walk them all. Copy in any shipped template `{brand-path}` or `{formats-path}` is missing first, whatever the creator picks: a studio configured before a template shipped is a studio whose stage skills stop on it. |

## Dependencies and platform

Bootstrap uv first: check `uv --version`, and if it is missing offer the official installer from docs.astral.sh/uv and wait for confirmation. Every pipeline script runs through uv, so nothing works without it.

```
uv run {skill-root}/scripts/check_deps.py
```

Report what is missing with the exact install command for the platform, and install nothing without the creator confirming each item. The report ends with a platform verdict naming a stack file (`{skill-root}/references/stack-macos.md`, `{skill-root}/references/stack-windows.md`, or `{skill-root}/references/stack-linux.md`). Read the named file now and hold it for the rest of setup: it carries the transcription lane, torch index, encoder ladder, SVG rasterizer, and fonts approach this machine actually needs.

If this machine is not Apple Silicon, say so plainly rather than letting the creator discover it at cut time: the parakeet-mlx reference lane will not run here, and the recommended lane is onnx-asr on the same weights, with fillers and word timestamps carrying over. Carry that honesty into the transcription question.

## Install the HyperFrames graphics skills

HyperFrames is the graphics engine, and its Agent Skills carry the agent's current, self-refreshing knowledge of what it can do. Installing them at setup rather than at first graphics run is deliberate: the capability surface is then known from the beats stage onward, where it changes what gets planned.

```
npx skills add heygen-com/hyperframes --all
```

Idempotent, so a re-run refreshes rather than duplicates. `npx hyperframes skills update` takes only the maintained core set for a lighter footprint. This runs on the local CLI with no account and no credits, and it is not the engine workspace, which is a multi-GB npm install that still builds lazily at first graphics run. If node or npx is missing, say HyperFrames graphics need them and defer rather than blocking setup.

## Interview the studio

Walk the `[defaults]` tables from `{skill-root}/assets/studio-defaults.toml`, offering current values as defaults. That file is the authority on the schema and every value in it; do not re-derive the question list here. What follows is only what reading the schema will not tell you.

**Traps in the basics.**

- Video defaults: offer to ffprobe a recent recording and fill them from reality instead of asking the creator to recall numbers.
- Speaking rate: leave the default and say mc-script will flag it as unmeasured. The voice bible measures it for real.
- A non-default interview marker cue is not a key of its own. Record it as `cutplan-flags = '--marker-cues "<cue>"'` in the `[cut]` sub-table, edited surgically.
- `[cut]`, `[packaging]` and `[retro]` are mechanical knobs rather than taste: write them from the defaults instead of interviewing them. The one worth raising is `[cut] silence-floor-db`, a property of the creator's room and mic; offer it when they mention a noisy room.

**Render consent is performed, not assumed.** Before writing `[render]`, present the render-first default and get an explicit answer: Manticore previews every cut and beats iteration and offers a final at gate 4, while the timeline export and all assets are ALWAYS produced alongside, so the creator can move into their own editor at any step. Declining makes the renders offers instead of automatic outputs and changes nothing else. Offer the quality knobs only if asked.

On a non-Mac machine, confirm the stack file's expectations here too: the encoder ladder the final render will probe, and on Windows with NVIDIA that the torch cu126 index adds roughly 2.5 to 3 GB.

**Video style.** `{skill-root}/assets/production-bible-spec.md` is the build spec; follow it rather than inventing a question order. Two things it does not carry: ask about creators to emulate first and, with permission, study the links they give rather than asking anyone to describe a style in the abstract; and echo the distilled takeaways back in your own words for confirmation before they land, since they then seed every remaining question as a proposed default. Style answers go in BOTH places, the config keys for mechanical consumption and the bible for taste, and neither is a copy of the other. Section 5 is the exception: only its tier has a config key, so the floor, the variety quota, and the card cap live in the bible alone.

**Audio lanes.** Confirm the local-first `[audio]` defaults; the ladder itself is mc-audio's knowledge, loaded when that skill runs. Be straight about three things the defaults do not admit: local TTS is stock voices with no cloning, so narration in the creator's own voice still means recording it; `song-provider` ships empty because no local lane is validated, so never promise the planned ACE-Step lane; and the engine workspace costs a multi-GB venv plus roughly 5 GB of model cache on first use. Offer to build it now with mc-audio's `ensure_workspace.py` or defer. An existing lab is reused, never rebuilt.

## Build the brand

Create the four path folders if missing, then fill `{brand-path}` from `{skill-root}/assets/`: `tokens.json` from the template, `blacklist.md` from the starter, `craft-checklist.md` as shipped, and `production-bible.md` and `voice-bible.md` per their specs, which are the build instructions whether or not either gets built today. Copy into `{formats-path}` every profile from `{skill-root}/assets/formats/` that is not already there. Everything that lands in either folder is the creator's from that moment: copy only what is missing, never overwrite on a re-run, because their copies carry edits and accumulated learnings.

Four things govern that work:

- Ask before interviewing: "point me at anything that already defines your brand or voice, a website, CSS, design tokens, style guides, past videos." Mine those, then interview only what mining could not answer.
- The exit state is filled, never placeholders. A placeholder survives only when the creator genuinely has nothing to give, and every survivor goes on the pending list loudly.
- Offer to build the voice bible now rather than leaving the spec sitting there. It needs the creator's own corpus, and it yields a measured wpm that replaces the estimate in `[owner] wpm`.
- `{brand-path}/headshots/` takes 3 to 6 approved photos across varied expressions (neutral, surprised, thinking, excited), renamed to expression slugs with an `index.md` catalog. Say the rule while collecting, because it governs what the creator hands over: approved photos only, never arbitrary frames from footage. A thumbnail sends the original photo to the image model, and every revision re-sends that same original rather than a prior generation. No headshots blocks thumbnails; flag it loudly.

## Register the creator's tools

CLI-first: a registered CLI backed by a subscription the creator already pays for is the preferred lane for every `[assets]` slot.

Ask what they use for image generation, video generation, and offloaded research. Record each as a `[[modules.manticore.tools]]` block with name, capabilities, the exact headless invocation, and preferred models. Two fields earn their own attention:

- The `notes` field is the persistent memory: quirks, output behavior, what the tool is bad at. Write it now, because this is what stops every future session rediscovering the same tool.
- Verify end to end with permission: the version command first, then one small real invocation per registered capability, confirming the output file exists. Record the result in `notes` as verified end-to-end with the ISO date, or as unverified.

Then set the `[assets]` lanes to registered, verified tool names. A lane with no good answer stays empty, so mc-assets stops and asks at farming time rather than billing anyone by default.

## Editor integration

Other editors need nothing here. When `[editor] name` is `resolve`, set up the Resolve lane: Manticore drives DaVinci Resolve Studio's scripting API from its own scripts in every stage it can (the video's Resolve project, the cut timeline with review markers, beats, graphics, the final render), and the fcpxml timeline is still exported for every editor.

Ask whether they run Resolve Studio or the free edition. On the free edition, say plainly that Resolve refuses outside scripts there, so they get the fcpxml timeline to import by hand and nothing else from this lane; record nothing more and move on. On Studio, explain the three things that otherwise stall a script silently, then have them open Resolve:

- Preferences > System > General > External scripting using must be Local.
- The first script connection raises an Allow prompt inside Resolve. It must be clicked, or the script waits.
- A project that was never saved opens a Save As dialog the moment anything saves it, and that dialog blocks every script. Manticore never saves a project it did not create, but keep no unsaved project with work in it open while stages run.

Then run the check with whatever `[editor]` Resolve values are already configured:

```
uv run {skill-root}/scripts/check_resolve.py --project-preset "<resolve-project-preset>" --render-preset "<resolve-render-preset>" --fairlight-preset "<resolve-fairlight-preset>" --loudness "<resolve-loudness>"
```

Exit 4 means Resolve is not reachable or not Studio: relay the fix it printed and re-run once they have done it. If it still fails, record the editor settings anyway, tell them the Resolve steps will report the same until it connects, and move on. On exit 0 or 1, use the lists it printed to settle each value with the creator: the project settings preset for new videos, the render preset and loudness mode for the final render, and an optional Fairlight preset. Offer only names Resolve listed. A configured name Resolve does not list is reported to the creator and either created in Resolve or changed in the config, never swapped for a similar name. An empty value is allowed; say the fallback its note names. `resolve-title-template` names the Fusion title used for text graphics (`Text+` by default); empty turns Fusion titles off and text graphics stay baked alpha. Re-run the check until it exits 0.

Last, offer Blackmagic's MCP server for their own interactive work in Resolve: polish, looks, LUT and DCTL generation, one-off automations. It ships inside Resolve Studio 21.1 and later and is set up from File > Setup AI Assistants inside Resolve, which configures their AI tools for them. No pipeline stage needs it. Record `davinci-resolve = true` under `[mcp]` only when they have set it up, false otherwise.

## Keys and .env.example

For each metered lane the creator explicitly chose, confirm the env var name recorded in the config and check whether it is set. If nothing metered was chosen, skip key sourcing entirely.

Then scaffold `{project-root}/.env.example` listing exactly the env vars the resolved config references: every non-empty `*-key-env` value a configured lane actually uses. That is possibly none, in which case write no file and say so. One line per var with a one-line source note, under a header comment saying real values never go in TOML, in chat, or in this file. Update an existing `.env.example` surgically and never touch a real `.env`.

## Write and report

Write the results as `[modules.manticore]` and its sub-tables into `{project-root}/_bmad/custom/config.toml`, editing surgically: create the file if needed, preserve everything else in it because other modules configure themselves there too, and preserve any section the creator skipped. Mention `config.user.toml` for personal overrides in shared repos. Verify with `uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore` and show the resolved summary.

Close with the runnability report, which is the actual deliverable of this stage:

- Locked behavior: what will happen on the first project with these settings. Render-first preview and offered final, the graphics-frequency tier, the CTA inventory, the transcription lane and whether THIS machine can run it, the audio lanes and whether the workspace is built, the timeline format, the Resolve lane's state (Studio connected or not, the presets in use, Blackmagic's MCP set up or not) when the editor is Resolve, and whether the HyperFrames skills are installed or deferred.
- Lane status: implemented or planned for every configured lane, straight from the `{skill-root}/assets/studio-defaults.toml` comments.
- Pending gaps, flagged loudly: missing headshots (thumbnails blocked), unbuilt voice bible, placeholder bible sections, unverified tools, empty asset lanes.
- Whether the harness has browser automation. Packaging research degrades without it, and the report says so when it is absent.

Point at mc-new to start the first project, and at the pending list as the highest-value next builds.

## Rules

- Confirm before every install, every MCP add, every command that changes the system.
- Presence checks only for secrets; never read, echo, or store key values. Keys never go in the TOML, in chat, or in `.env.example`.
- Paid and metered vendors are opt-in only: no vendor key name, dashboard, or pricing mention outside the branch where the creator explicitly chose that lane.
- Never claim a planned lane works. `{skill-root}/assets/studio-defaults.toml` marks each transcription, editor and audio lane implemented or planned; relay that status honestly everywhere it comes up.
- Never touch `{project-root}/_bmad/config.toml`, which is installer-owned. Manticore's home is the `custom/` layer.
