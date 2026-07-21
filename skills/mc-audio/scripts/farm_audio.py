#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Farm one audio asset through the configured local engine.

The mc-audio entry point: resolves the requested kind to its engine payload,
runs it inside the audio-lab workspace venv, and records provenance. Engine
payloads live in engines/ next to this script and run as
<venv-python> <payload>, where <venv-python> is the workspace venv's
interpreter (.venv/bin/python on macOS and Linux, .venv\\Scripts\\python.exe
on Windows; torch-class deps live in that one persistent venv;
ensure_workspace.py builds it).

Kinds and their arguments:
    tts      --text "..." [--voice af_heart] [--speed 1.0]
             Single-voice narration, mono 24 kHz WAV (Kokoro-82M).
    podcast  --script <lines.json>
             Multi-host dialogue with the validated realism recipe, stereo
             24 kHz WAV (Kokoro-82M). JSON shape in the payload docstring
             and references/audio-lanes.md.
    music    --prompt "..." [--seconds 10]
             Instrumental bed/stinger (MusicGen-small; ungated).
    sfx      --prompt "..." [--seconds 4] [--seed 7]
             Sound effect, 16 kHz output (AudioLDM2; ungated).

Usage:
    uv run farm_audio.py --kind tts|podcast|music|sfx
        --provider <the [audio] lane value for this kind>
        --workspace <resolved {engines-path}/audio-lab>
        --out-dir <destination dir> [--name <basename>] [kind args]
        [--dry-run]

Provider values: kokoro-local (tts/podcast), musicgen-local (music), and
audioldm2-local (sfx) are the implemented 1.0 lanes. Any other value (paid
or planned lanes: gemini-tts, elevenlabs-*, stable-audio-open,
ace-step-local) exits 3 with a pointer; nothing bills by default and
nothing unvalidated pretends to run.

HF_HOME is pointed at <workspace>/hf-cache unless already set in the
environment, so heavy model caches live in the engine workspace.

Output lands at <out-dir>/<name>.wav; <out-dir>/manifest.json (a JSON list)
gains one row {file, kind, prompt, provider, model, cost, date} (cost is
null: local lanes are free). --dry-run prints the resolved command and env
as JSON and runs nothing.

Exit codes: 0 ok, 1 engine failed or produced no file, 2 usage error,
3 provider lane not implemented, 4 workspace not ready (run
ensure_workspace.py).
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

ENGINES = {
    "tts": ("kokoro-local", "tts_kokoro.py", "kokoro-82m"),
    "podcast": ("kokoro-local", "tts_kokoro.py", "kokoro-82m"),
    "music": ("musicgen-local", "music_musicgen.py", "facebook/musicgen-small"),
    "sfx": ("audioldm2-local", "sfx_audioldm2.py", "cvssp/audioldm2"),
}
DEFAULT_NAMES = {"tts": "narration", "podcast": "podcast",
                 "music": "music-bed", "sfx": "sfx"}


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def venv_python(workspace: Path) -> Path:
    """Per-OS venv interpreter path (Windows uses Scripts\\python.exe).

    Duplicated in ensure_workspace.py; keep the two in sync.
    """
    if os.name == "nt":
        return workspace / ".venv" / "Scripts" / "python.exe"
    return workspace / ".venv" / "bin" / "python"


def build_command(args: argparse.Namespace, out_file: Path) -> list[str]:
    payload = Path(__file__).parent / "engines" / ENGINES[args.kind][1]
    py = venv_python(args.workspace)
    if args.kind in ("tts", "podcast"):
        cmd = [str(py), str(payload), "--models-dir",
               str(args.workspace / "models"), "--out", str(out_file)]
        if args.kind == "tts":
            if not args.text:
                die("error: --kind tts needs --text")
            cmd += ["single", "--text", args.text, "--voice", args.voice,
                    "--speed", str(args.speed)]
        else:
            if not args.script:
                die("error: --kind podcast needs --script")
            cmd += ["script", "--script", args.script]
        return cmd
    if not args.prompt:
        die(f"error: --kind {args.kind} needs --prompt")
    cmd = [str(py), str(payload), "--prompt", args.prompt,
           "--out", str(out_file), "--seconds", str(args.seconds)]
    if args.kind == "sfx":
        cmd += ["--seed", str(args.seed)]
    return cmd


def append_manifest(out_dir: Path, row: dict) -> None:
    manifest = out_dir / "manifest.json"
    rows = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
    rows.append(row)
    manifest.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", required=True, choices=sorted(ENGINES))
    ap.add_argument("--provider", required=True)
    ap.add_argument("--workspace", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--name", default=None)
    ap.add_argument("--text", default=None)
    ap.add_argument("--voice", default="af_heart")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--script", default=None)
    ap.add_argument("--prompt", default=None)
    ap.add_argument("--seconds", type=float, default=None)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    implemented, _, model = ENGINES[args.kind]
    if args.provider != implemented:
        die(f"error: provider {args.provider!r} for kind {args.kind!r} is not "
            f"implemented in 1.0 ({implemented!r} is). Paid and planned lanes "
            "are explicit opt-ins that land later; see "
            "references/audio-lanes.md.", 3)
    if args.seconds is None:
        args.seconds = 4.0 if args.kind == "sfx" else 10.0

    name = args.name or DEFAULT_NAMES[args.kind]
    out_file = args.out_dir / f"{name}.wav"
    cmd = build_command(args, out_file)

    env = dict(os.environ)
    env.setdefault("HF_HOME", str(args.workspace / "hf-cache"))

    if args.dry_run:
        print(json.dumps({"command": cmd, "out": str(out_file),
                          "HF_HOME": env["HF_HOME"], "model": model}, indent=2))
        return

    py = venv_python(args.workspace)
    if not py.exists():
        die(f"error: workspace not ready ({py} missing); run "
            "ensure_workspace.py first", 4)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(cmd, env=env)
    if r.returncode != 0 or not out_file.exists():
        die(f"error: engine failed or produced no file ({out_file})", 1)

    append_manifest(args.out_dir, {
        "file": out_file.name,
        "kind": args.kind,
        "prompt": args.prompt or args.text or args.script,
        "provider": args.provider,
        "model": model,
        "cost": None,
        "date": datetime.date.today().isoformat(),
    })
    print(json.dumps({"file": str(out_file), "kind": args.kind,
                      "provider": args.provider, "model": model}))


if __name__ == "__main__":
    main()
