"""AudioLDM2 SFX engine payload (local, ungated).

ENGINE PAYLOAD, not an entry point: runs INSIDE the audio-lab workspace venv
(invoked by farm_audio.py), never via uv run. cvssp/audioldm2 downloads from
Hugging Face on first run (no token, no license click-through) into the
cache the driver points HF_HOME at.

CRITICAL DEPENDENCY PIN (the venv carries it; do not relax): AudioLDM2's
diffusers pipeline breaks on transformers >= 4.44. The validated pair is
diffusers==0.31.0 + transformers==4.43.4; ensure_workspace.py installs it.

Output is 16 kHz: fine for whooshes and ambience under a mix, thin when
exposed solo; upsample/EQ or layer it. Validated 2026-07-07 on Apple
Silicon MPS: 7 to 14 s per 4 s effect once cached. Device ladder: cuda if
available, else mps, else cpu (the CUDA branch is code-complete but awaits
a validation run on real NVIDIA hardware). The generator stays on cpu for
seed reproducibility.

Args: --prompt, --out <wav>, --seconds (default 4), --seed (default 7),
--steps (default 100), --negative (default quality guard).
Exit 0 ok, 1 generation failure, 2 usage error.
"""

import argparse
import sys
import time

import numpy as np
import scipy.io.wavfile as wavfile
import torch

SFX_SR = 16000


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--steps", type=int, default=100)
    ap.add_argument("--negative", default="low quality, muffled, distorted")
    args = ap.parse_args()
    if args.seconds <= 0:
        print("error: --seconds must be positive", file=sys.stderr)
        sys.exit(2)

    from diffusers import AudioLDM2Pipeline

    device = ("cuda" if torch.cuda.is_available()
              else "mps" if torch.backends.mps.is_available() else "cpu")
    t0 = time.time()
    pipe = AudioLDM2Pipeline.from_pretrained(
        "cvssp/audioldm2", torch_dtype=torch.float32).to(device)
    print(f"audioldm2 loaded in {time.time() - t0:.0f}s on {device}")

    t0 = time.time()
    out = pipe(args.prompt,
               negative_prompt=args.negative,
               num_inference_steps=args.steps,
               audio_length_in_s=args.seconds,
               generator=torch.Generator("cpu").manual_seed(args.seed))
    audio = out.audios[0]
    audio = audio / max(1e-9, float(np.abs(audio).max())) * 0.89
    wavfile.write(args.out, SFX_SR, audio)
    print(f"wrote {args.out} ({args.seconds:.1f}s @ {SFX_SR} Hz "
          f"in {time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
