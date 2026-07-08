#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Create or verify the audio-lab engine workspace for mc-audio.

The local audio engines (Kokoro TTS, MusicGen, AudioLDM2) need torch-class
dependencies that must live in one persistent venv, not ephemeral uv script
environments, plus model files. All of it lives in the creator's engine
workspace (default {engines-path}/audio-lab), never in the skill folder:

    <workspace>/
      .venv/       one venv serving all three engines (pinned pair below)
      models/      kokoro-v1.0.onnx (~310 MB) + voices-v1.0.bin (~27 MB)
      hf-cache/    Hugging Face cache for MusicGen/AudioLDM2 (~5 GB on
                   first music/sfx run; farm_audio.py points HF_HOME here
                   unless HF_HOME is already set in the environment)
      out/         scratch output

Idempotent: an existing, verified workspace (for example a lab the creator
already validated by hand) is used as-is; only what is missing is built.

CRITICAL DEPENDENCY PIN: AudioLDM2's diffusers pipeline breaks on
transformers >= 4.44, so the venv installs diffusers==0.31.0 with
transformers==4.43.4 (validated pair, 2026-07-07). Kokoro and MusicGen work
at those versions too, which is why one venv serves all three engines.

The CALLING SKILL asks the creator before running this (the downloads are
large); the script itself just does the work.

Usage:
    uv run ensure_workspace.py --workspace <path>
        [--python 3.12] [--skip-models] [--check] [--dry-run]

--check reports readiness and changes nothing (exit 0 ready, 4 not ready).
--dry-run prints the planned commands as JSON and runs nothing.
--skip-models builds the venv but defers the Kokoro model download.
Exit codes: 0 ready, 1 a build step failed, 2 usage error, 4 not ready
(--check only).
"""

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

PIN_INSTALL = [
    "kokoro-onnx==0.5.0",
    "soundfile",
    "torch",
    "accelerate",
    "scipy",
    "diffusers==0.31.0",
    "transformers==4.43.4",
]
KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO_FILES = ["kokoro-v1.0.onnx", "voices-v1.0.bin"]
VERIFY_SNIPPET = (
    "import kokoro_onnx, soundfile, torch, scipy, diffusers, transformers; "
    "assert diffusers.__version__ == '0.31.0', diffusers.__version__; "
    "assert transformers.__version__ == '4.43.4', transformers.__version__; "
    "print('ok')"
)


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def venv_python(workspace: Path) -> Path:
    return workspace / ".venv" / "bin" / "python"


def verify(workspace: Path) -> list[str]:
    """Return a list of problems; empty means the workspace is ready."""
    problems = []
    py = venv_python(workspace)
    if not py.exists():
        problems.append(f"venv missing: {py}")
    else:
        r = subprocess.run([str(py), "-c", VERIFY_SNIPPET],
                           capture_output=True, text=True)
        if r.returncode != 0:
            problems.append(f"venv import check failed: {r.stderr.strip()[-300:]}")
    for name in KOKORO_FILES:
        if not (workspace / "models" / name).exists():
            problems.append(f"model missing: models/{name}")
    return problems


def planned_commands(workspace: Path, python_version: str) -> list[list[str]]:
    py = venv_python(workspace)
    return [
        ["uv", "venv", "--python", python_version, str(workspace / ".venv")],
        ["uv", "pip", "install", "--python", str(py), *PIN_INSTALL],
    ]


def download(url: str, dest: Path) -> None:
    print(f"downloading {url} -> {dest}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.rename(dest)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", required=True, type=Path)
    ap.add_argument("--python", default="3.12")
    ap.add_argument("--skip-models", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    ws = args.workspace

    if args.check:
        problems = verify(ws)
        if problems:
            for p in problems:
                print(p)
            sys.exit(4)
        print(f"ready: {ws}")
        return

    if args.dry_run:
        print(json.dumps({
            "workspace": str(ws),
            "commands": planned_commands(ws, args.python),
            "models": [] if args.skip_models else
                      [f"{KOKORO_RELEASE}/{n}" for n in KOKORO_FILES],
        }, indent=2))
        return

    for sub in ("models", "hf-cache", "out"):
        (ws / sub).mkdir(parents=True, exist_ok=True)

    if not venv_python(ws).exists():
        for cmd in planned_commands(ws, args.python):
            r = subprocess.run(cmd)
            if r.returncode != 0:
                die(f"error: {' '.join(cmd)} failed", 1)
    else:
        # Existing venv: install is idempotent and fixes a broken dep set.
        r = subprocess.run(planned_commands(ws, args.python)[1])
        if r.returncode != 0:
            die("error: dependency install into existing venv failed", 1)

    if not args.skip_models:
        for name in KOKORO_FILES:
            dest = ws / "models" / name
            if not dest.exists():
                try:
                    download(f"{KOKORO_RELEASE}/{name}", dest)
                except OSError as e:
                    die(f"error: model download failed ({e}); grab {name} from "
                        f"the kokoro-onnx GitHub release page into {dest}", 1)

    problems = verify(ws)
    if args.skip_models:
        problems = [p for p in problems if not p.startswith("model missing")]
    if problems:
        die("error: workspace still not ready:\n" + "\n".join(problems), 1)
    print(f"ready: {ws}")


if __name__ == "__main__":
    main()
