"""Kokoro-82M TTS engine payload (local, ONNX): narration and two-host dialogue.

ENGINE PAYLOAD, not an entry point: this file runs INSIDE the audio-lab
workspace venv (invoked by farm_audio.py as <workspace>/.venv/bin/python
tts_kokoro.py ...), never via uv run. The venv carries kokoro-onnx,
soundfile, and numpy; ensure_workspace.py builds it.

Modes:
    single  --text "..." [--voice af_heart] [--speed 1.0]
            One narration take, mono 24 kHz WAV.
    script  --script lines.json
            Two-host (or N-host) dialogue with the validated realism recipe
            (2026-07-07): per-line speed variation, variable inter-line gaps
            (negative gap overlaps the previous line), backchannels rendered
            at 0.4x gain and overlapped under the other speaker WITHOUT
            advancing the timeline cursor, constant-power stereo panning per
            host, soft-limited master. Stereo 24 kHz WAV.

Script JSON shape:
    {
      "hosts": {"A": {"voice": "am_michael", "pan": -0.25},
                "B": {"voice": "af_heart",  "pan": 0.25}},
      "lines": [{"host": "A", "text": "...", "speed": 1.0,
                 "gap": 0.3, "backchannel": false}, ...]
    }
    gap is seconds of silence BEFORE the line; negative overlaps the
    previous line. speed defaults 1.0, gap 0.3, backchannel false.

Args: --models-dir (holds kokoro-v1.0.onnx + voices-v1.0.bin), --out <wav>.
Known limits: fixed voice set (no cloning), no true crosstalk (overlap is
simulated in the mix). Exit 0 ok, 1 synthesis failure, 2 usage error.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro

SR = 24000
BACKCHANNEL_GAIN = 0.4
BACKCHANNEL_TAIL_S = 0.2
TAIL_SILENCE_S = 0.8


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def load_kokoro(models_dir: Path) -> Kokoro:
    model = models_dir / "kokoro-v1.0.onnx"
    voices = models_dir / "voices-v1.0.bin"
    for f in (model, voices):
        if not f.exists():
            die(f"error: {f} missing; run ensure_workspace.py first", 2)
    return Kokoro(str(model), str(voices))


def render_line(kokoro: Kokoro, text: str, voice: str, speed: float) -> np.ndarray:
    samples, sr = kokoro.create(text, voice=voice, speed=speed, lang="en-us")
    if sr != SR:
        die(f"error: engine returned {sr} Hz, expected {SR}", 1)
    return samples.astype(np.float32)


def pan_stereo(mono: np.ndarray, pan: float) -> np.ndarray:
    """pan in [-1, 1]; constant-power."""
    theta = (pan + 1) * np.pi / 4
    return np.stack([mono * np.cos(theta), mono * np.sin(theta)], axis=1)


def master(mix: np.ndarray) -> np.ndarray:
    """Soft limiter, then normalize to about -1 dBFS."""
    mix = np.tanh(mix * 1.1)
    return mix * (0.89 / max(1e-9, float(np.abs(mix).max())))


def run_single(kokoro: Kokoro, args: argparse.Namespace) -> None:
    mono = render_line(kokoro, args.text, args.voice, args.speed)
    sf.write(args.out, master(mono), SR)
    print(f"wrote {args.out} ({len(mono) / SR:.1f}s, mono)")


def run_script(kokoro: Kokoro, args: argparse.Namespace) -> None:
    spec = json.loads(Path(args.script).read_text(encoding="utf-8"))
    hosts = spec.get("hosts") or {}
    lines = spec.get("lines") or []
    if not hosts or not lines:
        die("error: script JSON needs non-empty 'hosts' and 'lines'", 2)

    clips = []  # (start_sample, stereo_array, mono_len)
    cursor = 0
    for i, line in enumerate(lines):
        host = hosts.get(line.get("host", ""))
        if host is None:
            die(f"error: line {i} names unknown host {line.get('host')!r}", 2)
        mono = render_line(kokoro, line["text"], host["voice"],
                           float(line.get("speed", 1.0)))
        is_bc = bool(line.get("backchannel", False))
        if is_bc:
            mono *= BACKCHANNEL_GAIN
        stereo = pan_stereo(mono, float(host.get("pan", 0.0)))
        start = max(0, cursor + int(float(line.get("gap", 0.3)) * SR))
        clips.append((start, stereo))
        if is_bc:
            # a backchannel does not advance the conversation cursor
            cursor = max(cursor, start + len(mono) - int(BACKCHANNEL_TAIL_S * SR))
        else:
            cursor = start + len(mono)
        print(f"{line.get('host'):>8} @ {start / SR:6.2f}s "
              f"({len(mono) / SR:.2f}s) {line['text'][:60]}")

    total = max(s + len(a) for s, a in clips) + int(TAIL_SILENCE_S * SR)
    mix = np.zeros((total, 2), dtype=np.float32)
    for start, arr in clips:
        mix[start:start + len(arr)] += arr
    sf.write(args.out, master(mix), SR)
    print(f"wrote {args.out} ({total / SR:.1f}s, stereo, {len(lines)} lines)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models-dir", required=True, type=Path)
    ap.add_argument("--out", required=True)
    sub = ap.add_subparsers(dest="mode", required=True)
    s1 = sub.add_parser("single")
    s1.add_argument("--text", required=True)
    s1.add_argument("--voice", default="af_heart")
    s1.add_argument("--speed", type=float, default=1.0)
    s2 = sub.add_parser("script")
    s2.add_argument("--script", required=True)
    args = ap.parse_args()

    kokoro = load_kokoro(args.models_dir)
    if args.mode == "single":
        run_single(kokoro, args)
    else:
        run_script(kokoro, args)


if __name__ == "__main__":
    main()
