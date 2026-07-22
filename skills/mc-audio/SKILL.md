---
name: mc-audio
description: Farm sound for the studio, local-first: single-voice TTS narration and multi-host dialogue (Kokoro-82M), instrumental music beds (MusicGen-small), and SFX (AudioLDM2), with paid lanes strictly opt-in. A service skill like mc-ograf, not a pipeline stage: no gate, no project.json state; call it from mc-graphics (whooshes, stingers), mc-stream-pack (beds, stinger audio), the voiceover-explainer format (narration), or whenever the creator asks for sound.
---

# mc-audio

mc-assets farms pictures; this skill farms sound. It is a service skill: it owns no stage, stops at no gate, and writes no project state. The caller (another skill or the creator directly) says what sound is needed and where it lands; this skill resolves the lane, runs the engine, and hands back files with provenance. Read `references/audio-lanes.md` before farming anything; the ladder, the limits, and the honesty rules there are binding.

## Steps

1. Load the studio config (`uv run {project-root}/_bmad/scripts/resolve_config.py --project-root {project-root} --key modules.manticore`; empty means mc-setup has not run: stop and route the creator there) and this skill's own surface (`uv run {project-root}/_bmad/scripts/resolve_customization.py --skill {skill-root}`; run `{workflow.activation_steps_prepend}` now, `{workflow.activation_steps_append}` after this step, and hold `{workflow.persistent_facts}` as standing context). Resolve `paths` values against `{project-root}`. From `[audio]` take the lane values (`tts-provider`, `music-provider`, `sfx-provider`, `song-provider`) and `workspace`; the engine workspace is `{engines-path}/{audio.workspace}`.
2. Resolve the lane for the requested kind. The implemented 1.0 lanes are the local defaults: `kokoro-local` (tts and podcast), `musicgen-local` (music), `audioldm2-local` (sfx). A paid or planned value (`gemini-tts`, `elevenlabs-*`, `stable-audio-open`, `ace-step-local`) means the creator opted into a lane that has not landed: say so plainly and stop; never substitute a paid lane the creator did not choose, and never pretend an unvalidated lane works. An empty `song-provider` is the shipped state: full songs with vocals have no validated local lane yet (ACE-Step is the planned candidate; see the reference).
3. Workspace check: `uv run {skill-root}/scripts/ensure_workspace.py --workspace <resolved workspace> --check`. Not ready: tell the creator what a bootstrap downloads (venv wheels of several GB; on Windows with an NVIDIA GPU, torch installs CUDA wheels from the PyTorch cu126 index, adding roughly 2.5 to 3 GB more; ~340 MB of Kokoro models now, ~5 GB of Hugging Face cache on the first music/sfx run), get their go-ahead, then run it without `--check`. The script's `--dry-run` JSON includes a `torch` field stating which wheel source this machine will use; relay it verbatim during consent. Idempotent: an existing validated workspace (a lab the creator built by hand counts) is used as-is, never rebuilt or duplicated.
4. Farm through the entry script, one call per asset: `uv run {skill-root}/scripts/farm_audio.py --kind tts|podcast|music|sfx --provider <the [audio] lane value> --workspace <resolved workspace> --out-dir <where the caller wants it> [--name <basename>]` plus the kind's arguments (`--text/--voice/--speed`, `--script lines.json`, `--prompt/--seconds/--seed`). For podcast dialogue, write the script JSON per the shape in the reference and apply the realism recipe knobs (speed variation, gaps, backchannels) rather than uniform lines. The script appends provenance to `<out-dir>/manifest.json` (same row shape as mc-assets; cost is null on local lanes).
5. Listen before presenting: play or inspect every output (duration matches the request, no silent or truncated file, dialogue lines land in order). Deliver with the honest caveats from the reference where they apply: SFX are 16 kHz (fine under a mix, thin exposed solo), music is instrumental only, TTS voices are stock (no cloning, no "your voice" claims), crosstalk is simulated.
6. First-run model downloads are long: run them in the background with proactive progress reports; never leave the creator staring at a silent stage. Report where every file landed and what was appended to the manifest.

## Checklist

- The lane came from `[audio]` in the studio config; no paid or metered lane ran without the creator's explicit configuration, and no planned lane was presented as working.
- Workspace bootstrap (and its downloads) was consented to before anything was fetched; an existing workspace was reused, not rebuilt.
- Every delivered clip was listened to or duration-verified against the request; nothing silent, truncated, or out of order shipped.
- Caveats delivered with the assets they apply to: 16 kHz SFX, instrumental-only music, stock TTS voices, simulated crosstalk.
- Podcast scripts used the realism recipe (distinct voices, varied speed and gaps, backchannels under the other speaker), not uniform stitched lines.
- `manifest.json` in the destination gained one row per file.
