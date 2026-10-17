---
name: mc-audio
description: Farm narration, music beds, and SFX locally. Use when another skill needs sound, or when the user says "add narration", "music bed", or "sound effect".
---

# mc-audio

mc-assets farms pictures; this skill farms sound. A caller (another skill, or the creator directly) says what sound is needed and where it lands; you resolve the lane, run the engine, and hand back files with provenance. It is a service skill: it owns no stage, stops at no gate, and writes no project state. The caller mixes what you hand back without hearing it first, so the bar is honesty about what came back. Mixing is not this skill's job: inside a HyperFrames composition the `hyperframes-audio` skill mixes, and elsewhere the caller does.

Read `{skill-root}/references/audio-lanes.md` before farming anything; the ladder, the limits, and the honesty rules there are binding.

## Resolution rules

- Bare paths resolve against `{video-path}`, the current video project at `{projects-path}/<slug>/`.
- `{skill-root}` → this skill's installed directory; files in it always carry it (`{skill-root}/references/audio-lanes.md`).
- `{project-root}` → the project working directory.

## On Activation

1. Load the studio config: `uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`. Empty means mc-setup has not run: stop and route the creator there.
2. Resolve `paths` values against `{project-root}`. From `[audio]` take the lane values (`tts-provider`, `music-provider`, `sfx-provider`, `song-provider`) and `workspace`; the engine workspace is `{engines-path}/{audio.workspace}`.

## The lane

The implemented 1.0 lanes are the local defaults: `kokoro-local` (tts and podcast), `musicgen-local` (music), `audioldm2-local` (sfx). A paid or planned value (`gemini-tts`, `elevenlabs-*`, `stable-audio-open`, `ace-step-local`) means the creator opted into a lane that has not landed: say so plainly and stop. Never substitute a paid lane the creator did not choose, and never pretend an unvalidated lane works. An empty `song-provider` is the shipped state, not a misconfiguration: full songs with vocals have no validated local lane yet.

## Workspace

`uv run {skill-root}/scripts/ensure_workspace.py --workspace <resolved workspace> --check`. Not ready: tell the creator what a bootstrap downloads (venv wheels of several GB; on Windows with an NVIDIA GPU, torch installs CUDA wheels from the PyTorch cu126 index, adding roughly 2.5 to 3 GB more; ~340 MB of Kokoro models now, ~5 GB of Hugging Face cache on the first music/sfx run), relay the `torch` field of the script's `--dry-run` JSON verbatim so they know which wheel source this machine will use, get their go-ahead, then run it without `--check`. An existing validated workspace is used as-is, a lab the creator built by hand included: never rebuilt, never duplicated.

## Farming

One call per asset:

`uv run {skill-root}/scripts/farm_audio.py --kind tts|podcast|music|sfx --provider <the [audio] lane value> --workspace <resolved workspace> --out-dir <where the caller wants it> [--name <basename>]` plus the kind's arguments (`--text/--voice/--speed`, `--script lines.json`, `--prompt/--seconds/--seed`). The script appends provenance to `<out-dir>/manifest.json` (same row shape as mc-assets; cost is null on local lanes).

Podcast dialogue takes a script JSON in the shape the reference gives, with its realism knobs applied (speed variation, gaps, backchannels) rather than uniform lines.

First-run model downloads are long: run them in the background with proactive progress reports.

## Delivery

Listen to or inspect every output before presenting it: duration matches the request, nothing silent or truncated, dialogue lines in order. Deliver with the caveats from the reference that apply: SFX are 16 kHz (fine under a mix, thin exposed solo), music is instrumental only, TTS voices are stock (no cloning, no "your voice" claims), crosstalk is simulated. Report where every file landed.

## Checklist

- The lane came from `[audio]` in the studio config; no paid or metered lane ran without the creator's explicit configuration, and no planned lane was presented as working.
- Workspace bootstrap (and its downloads) was consented to before anything was fetched; an existing workspace was reused, not rebuilt.
- Every delivered clip was listened to or duration-verified against the request; nothing silent, truncated, or out of order shipped.
- Caveats delivered with the assets they apply to: 16 kHz SFX, instrumental-only music, stock TTS voices, simulated crosstalk.
- Podcast scripts varied voices, speed, and gaps and put backchannels under the other speaker, rather than stitching uniform lines.
