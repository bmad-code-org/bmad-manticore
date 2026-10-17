# Transcription lanes

Which transcriber to run and how. Load this when choosing a lane, when the
source is already published, or when the transcript gate reports dropped
speech.

The windowing invariant that governs every lane lives in SKILL.md, because it
looks like a tuning knob and is not.

## Choosing the lane

The source is already published (a livestream VOD, a footage-first project,
anything with captions on YouTube). Pull the captions:

```
yt-dlp --write-auto-subs <url>
```

Free, effectively perfect, any length, and it avoids local ASR entirely. Local
ASR is only for raw unpublished recordings, which is exactly where the
transcription bugs lived and why they went unnoticed for so long. Record the
provenance in the transcript header either way.

The source is a raw recording. Run local ASR:

```
uv run {skill-root}/scripts/transcribe.py raw/<take> \
    -o transcript/words.json --provider <[transcription] provider>
```

Suffix the output `<source-id>.words.json` when the project has multiple
sources.

| Provider | Behaviour |
|---|---|
| `auto` | The default. parakeet-mlx on macOS Apple Silicon, onnx-asr everywhere else |
| `parakeet-mlx` | Forces the MLX lane |
| `onnx-asr` | Forces the ONNX lane |
| `elevenlabs-scribe` | Metered, opt-in behind `[transcription]`. Sends audio to a third party, so never a default |

Both local lanes run the same parakeet-tdt-0.6b-v3 weights and window
identically. All local lanes are free; the model downloads once on first run.

## CUDA machines

```
uv run --with "onnx-asr[gpu,hub]" python {skill-root}/scripts/transcribe.py ...
```

PEP 508 markers cannot detect GPUs, so the GPU extra has to be requested
explicitly. The `python` command rather than `uv run` is required: it skips the
script's cpu-extra dependency so onnxruntime-gpu never co-installs alongside
onnxruntime, which would silently fall back to CPU. The script warns on stderr
when an NVIDIA GPU is visible but CUDA is unavailable.

## When the transcript gate fails

`verify_transcript.py` finds audio above the silence floor that produced no
words and names the regions. The default reading is dropped speech, and the
recovery is to re-run the take and re-verify.

Not every flagged region is lost speech. A laugh, a music bed, or an off-mic
aside reads the same way to a coverage scan. When the creator has listened to a
region and confirmed it:

```
uv run {skill-root}/scripts/verify_transcript.py transcript/words.json \
    --audio-map cut/audio-map.json --wpm <[owner] wpm> \
    -o cut/transcript-check.json \
    --accept-region <start>-<end> --reason "<why>"
```

Repeatable. The acceptance must fully cover the flagged region, and both the
region and the reason land in `transcript-check.json`. That is the only way past
this gate, and it is a recorded decision rather than a silent one.

The gate also fails a transcript with no fillers ("um", "uh") over two minutes of speech: the transcriber normalized them away, and the cut needs them to find stumbles and restarts. Re-transcribe with the reference lane. Only when the creator confirms the take genuinely has none, such as a tightly scripted read, add `--accept-no-fillers "<why>"`; the reason lands in `transcript-check.json`.
