"""MusicGen-small music-bed engine payload (local, ungated).

ENGINE PAYLOAD, not an entry point: runs INSIDE the audio-lab workspace venv
(invoked by farm_audio.py), never via uv run. facebook/musicgen-small
downloads from Hugging Face on first run (no token, no license click-through)
into the cache the driver points HF_HOME at.

Instrumentals only: beds, stingers, intro themes. No vocals or lyrics (a
full-song lane is separate and unvalidated; see references/audio-lanes.md).
Validated 2026-07-07 on Apple Silicon MPS: about 10 s of audio in 55 s once
the model is loaded.

Args: --prompt, --out <wav>, --seconds (default 10, capped at 30).
Exit 0 ok, 1 generation failure, 2 usage error.
"""

import argparse
import sys
import time

import numpy as np
import scipy.io.wavfile as wavfile
import torch

TOKENS_PER_SECOND = 51.2  # 512 tokens ~= 10 s
MAX_SECONDS = 30.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=float, default=10.0)
    args = ap.parse_args()
    if args.seconds <= 0:
        print("error: --seconds must be positive", file=sys.stderr)
        sys.exit(2)
    seconds = min(args.seconds, MAX_SECONDS)

    from transformers import AutoProcessor, MusicgenForConditionalGeneration

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    t0 = time.time()
    processor = AutoProcessor.from_pretrained("facebook/musicgen-small")
    model = MusicgenForConditionalGeneration.from_pretrained(
        "facebook/musicgen-small").to(device)
    print(f"musicgen loaded in {time.time() - t0:.0f}s on {device}")

    inputs = processor(text=[args.prompt], padding=True,
                       return_tensors="pt").to(device)
    t0 = time.time()
    audio = model.generate(**inputs, do_sample=True, guidance_scale=3.0,
                           max_new_tokens=int(seconds * TOKENS_PER_SECOND))
    sr = model.config.audio_encoder.sampling_rate
    music = audio[0, 0].cpu().float().numpy()
    music = music / max(1e-9, float(np.abs(music).max())) * 0.89
    wavfile.write(args.out, sr, music)
    print(f"wrote {args.out} ({len(music) / sr:.1f}s in {time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
