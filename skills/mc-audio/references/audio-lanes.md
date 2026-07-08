# Audio Lanes

The provider ladder for every kind of generated sound, following the settled module pattern: a free local provider is the default, paid vendors are explicit opt-ins whose key names never ship in defaults. The three local lanes below were validated end to end on Apple Silicon (M4 Pro, 24 GB) on 2026-07-07: fully local, no cloud, no API keys, no gated Hugging Face models.

## TTS: narration and multi-host dialogue

Default `kokoro-local`: Kokoro-82M via kokoro-onnx 0.5.0, model files kokoro-v1.0.onnx (~310 MB) plus voices-v1.0.bin (~27 MB) from the kokoro-onnx GitHub releases, stored in the workspace `models/`. Validated at roughly 6.6x realtime on CPU (79 s of finished two-host audio in 12 s wall clock). This settles the earlier open provider choice (Kokoro over Chatterbox).

The two-host realism recipe (implemented in the tts_kokoro.py payload; what makes it sound like a conversation instead of two stitched TTS files):

- Two distinct voices, one per host (for example am_michael and af_heart).
- Per-line speed variation so the pacing breathes.
- Variable inter-line gaps; humans do not leave uniform silence. A negative gap overlaps the previous line.
- Backchannels ("Mm-hm.", "Right.") rendered at 0.4x gain and overlapped under the other speaker WITHOUT advancing the timeline cursor.
- Constant-power stereo panning per host, like a two-mic room.
- Soft-limited master normalized to about -1 dBFS.

Script JSON shape for `--kind podcast`:

```json
{
  "hosts": {"A": {"voice": "am_michael", "pan": -0.25},
            "B": {"voice": "af_heart", "pan": 0.25}},
  "lines": [
    {"host": "A", "text": "First line.", "speed": 1.0, "gap": 0.0},
    {"host": "B", "text": "Mm-hm.", "gap": -0.45, "backchannel": true},
    {"host": "B", "text": "A real reply.", "speed": 1.03, "gap": 0.3}
  ]
}
```

Known limits, stated honestly wherever TTS is offered: the voice set is fixed (no cloning, so narration is stock voices, never "your voice"), and there is no true crosstalk (overlap is simulated in the mix, convincing for backchannels, not for genuine talk-over arguments). Narration in the creator's own voice means recording it themselves or a paid voice-cloning lane, opt-in and not yet implemented.

Paid/cloud rungs, opt-in only, planned: `gemini-tts` (cheap cloud two-host), `elevenlabs-dialogue` (premium multi-speaker), professional voice cloning for creator-voice narration.

## Music beds

Default `musicgen-local`: facebook/musicgen-small via transformers, ungated (no token, no license click-through). Validated: 10.2 s of usable intro-theme music in 55 s on MPS. Instrumentals only: beds, stingers, intro themes; no vocals or lyrics. Fine for beds and stingers; long-form structured music needs musicgen-medium (slower) or an external tool.

Stable Audio Open produces better output but is gated behind a Hugging Face license click-through, which breaks a zero-friction install; it is demoted to opt-in, never the default. `eleven-music` is the paid opt-in rung (planned).

## SFX

Default `audioldm2-local`: cvssp/audioldm2 via diffusers, ungated. Validated: 7 to 14 s per 4 s effect on MPS once cached. Output is 16 kHz: fine for whooshes, chimes, and ambience under a mix, thin when exposed solo; upsample/EQ or layer it, and say so when delivering an exposed effect.

CRITICAL DEPENDENCY PIN: AudioLDM2's diffusers pipeline breaks with transformers >= 4.44. The validated pair is `diffusers==0.31.0` + `transformers==4.43.4`. Kokoro and MusicGen are unaffected by the pin, so one workspace venv serves all three engines; ensure_workspace.py installs exactly this pair and verifies it.

`elevenlabs-sfx` (SFX v2) is the paid opt-in rung (planned).

## Full songs with vocals (rap, sung lyrics): PLANNED, NOT VALIDATED

`song-provider` ships empty; no local lane is implemented or promised. The leading candidate is ACE-Step 1.5 (MIT license, ungated, native Mac support via an MLX backend; the XL 4B models want the 12 to 20 GB memory tier and run about 2 minutes per 60 s clip on an M1 Max). It is NOT yet validated; mark it planned wherever it comes up and never promise it works.

Do NOT plan around YuE for local use: no MLX/Metal port exists, the community floor is 32 to 64 GB unified memory, and Mac wall clock is hours per 30 s of audio. YuE is cloud or rented GPU only, if ever.

## First-run costs (state before bootstrapping)

- Workspace venv: torch-class wheels, several GB of disk.
- Kokoro model files: ~340 MB download at workspace build.
- MusicGen + AudioLDM2: roughly 5 GB into the workspace HF cache on the first music/sfx run (shared between the two).
