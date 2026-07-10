#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Create or verify the prompter-lab engine workspace for mc-prompter.

Voice-follow (tier 2) needs streaming ASR: sherpa-onnx plus its model
files. Those live in one persistent venv and a models directory in the
creator's engine workspace (default {engines-path}/prompter-lab), never in
the skill folder:

    <workspace>/
      .venv/    aiohttp==3.12.15 (same pin as the server's PEP 723
                header), numpy, sherpa-onnx==1.13.4, soundfile
      models/
        nemotron-streaming/   encoder.int8.onnx, decoder.int8.onnx,
                              joiner.int8.onnx, tokens.txt (renamed from
                              the release tarball's dated directory)
        silero_vad.onnx       VAD model
      out/      session artifacts

Downloads (both from the k2-fsa/sherpa-onnx GitHub release "asr-models"):
the nemotron streaming tarball (~464 MB, deleted after extraction) and
silero_vad.onnx (~0.7 MB). Total download ~465 MB; extracted models take
~630 MB on disk.

DEPENDENCY PIN: sherpa-onnx is pinned to 1.13.4 because model exports must
match the runtime generation; the 2026-04-25 nemotron export is validated
under exactly this version (2026-07-09).

Idempotent: an existing, verified workspace is used as-is; only what is
missing is built. Existing venv and model files are verified (presence and
size), never rebuilt or re-downloaded.

The CALLING SKILL asks the creator before running this (the download is
large); the script itself just does the work.

Usage:
    uv run ensure_workspace.py --workspace <path>
        [--python 3.12] [--models-only] [--check] [--dry-run]

--check reports readiness and changes nothing (exit 0 ready, 4 not ready).
--dry-run prints the planned commands and downloads as JSON, runs nothing.
--models-only downloads and verifies the models but skips the venv build.
Exit codes: 0 ready, 1 a build step failed, 2 usage error, 4 not ready
(--check only).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

AIOHTTP_PIN = "aiohttp==3.12.15"
SHERPA_PIN = "sherpa-onnx==1.13.4"
PIN_INSTALL = [AIOHTTP_PIN, "numpy", SHERPA_PIN, "soundfile"]
ASR_RELEASE = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
               "asr-models")
NEMOTRON_TARBALL = ("sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-"
                    "int8-2026-04-25.tar.bz2")
NEMOTRON_DIR = "nemotron-streaming"
VAD_FILE = "silero_vad.onnx"
TOTAL_DOWNLOAD_MB = 465
# Size floors (bytes) for the canonical layout; real sizes measured from
# the validated 2026-04-25 release. A truncated download fails these.
MODEL_MIN_SIZES = {
    f"{NEMOTRON_DIR}/encoder.int8.onnx": 500_000_000,
    f"{NEMOTRON_DIR}/decoder.int8.onnx": 5_000_000,
    f"{NEMOTRON_DIR}/joiner.int8.onnx": 1_000_000,
    f"{NEMOTRON_DIR}/tokens.txt": 4_000,
    VAD_FILE: 500_000,
}
VERIFY_SNIPPET = (
    "import importlib.metadata as md; "
    "import aiohttp, numpy, sherpa_onnx, soundfile; "
    "assert md.version('aiohttp') == '3.12.15', md.version('aiohttp'); "
    "assert md.version('sherpa-onnx') == '1.13.4', "
    "md.version('sherpa-onnx'); "
    "print('ok')"
)


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def venv_python(workspace: Path, platform: str = sys.platform) -> Path:
    """The venv interpreter path, resolved portably."""
    if platform == "win32":
        return workspace / ".venv" / "Scripts" / "python.exe"
    return workspace / ".venv" / "bin" / "python"


def verify_layout(workspace: Path, models_only: bool = False) -> list[str]:
    """File-level problems (presence and size); empty means layout is ok."""
    problems = []
    if not models_only and not venv_python(workspace).exists():
        problems.append(f"venv missing: {venv_python(workspace)}")
    for rel, min_size in MODEL_MIN_SIZES.items():
        path = workspace / "models" / rel
        if not path.is_file():
            problems.append(f"model missing: models/{rel}")
        elif path.stat().st_size < min_size:
            problems.append(
                f"model truncated: models/{rel} "
                f"({path.stat().st_size} < {min_size} bytes)"
            )
    return problems


def verify_venv_imports(workspace: Path) -> list[str]:
    """Run the pinned-import check inside the venv; empty means ok."""
    py = venv_python(workspace)
    if not py.exists():
        return [f"venv missing: {py}"]
    r = subprocess.run([str(py), "-c", VERIFY_SNIPPET],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return [f"venv import check failed: {r.stderr.strip()[-300:]}"]
    return []


def verify(workspace: Path, models_only: bool = False) -> list[str]:
    """Full readiness check; empty means the workspace is ready."""
    problems = verify_layout(workspace, models_only=models_only)
    if not models_only and venv_python(workspace).exists():
        problems += verify_venv_imports(workspace)
    return problems


def planned_commands(workspace: Path, python_version: str) -> list[list[str]]:
    py = venv_python(workspace)
    return [
        ["uv", "venv", "--python", python_version,
         str(workspace / ".venv")],
        ["uv", "pip", "install", "--python", str(py), *PIN_INSTALL],
    ]


def planned_downloads(workspace: Path) -> list[str]:
    """URLs still needed to complete the models directory."""
    urls = []
    layout = verify_layout(workspace, models_only=True)
    if any(NEMOTRON_DIR in p for p in layout):
        urls.append(f"{ASR_RELEASE}/{NEMOTRON_TARBALL}")
    if any(VAD_FILE in p for p in layout):
        urls.append(f"{ASR_RELEASE}/{VAD_FILE}")
    return urls


def download(url: str, dest: Path) -> None:
    """Download url to dest with progress lines every ~10 percent."""
    print(f"downloading {url} -> {dest}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    last = [-1]

    def hook(blocks: int, block_size: int, total: int) -> None:
        if total <= 0:
            return
        done = min(blocks * block_size, total)
        pct = done * 100 // total
        step = pct // 10
        if step > last[0]:
            last[0] = step
            print(f"  {done // 1_000_000}/{total // 1_000_000} MB "
                  f"({pct}%)", flush=True)

    urllib.request.urlretrieve(url, tmp, reporthook=hook)
    # os.replace overwrites an existing (e.g. truncated) file on every
    # platform; Path.rename raises FileExistsError on Windows.
    os.replace(tmp, dest)


def extract_nemotron(tar_path: Path, models_dir: Path) -> None:
    """Extract the tarball, rename its dir to the stable name, clean up.

    A stale target directory (left by an interrupted earlier bootstrap) is
    removed before the rename, so repairing a truncated model layout works
    instead of crashing on a rename-onto-nonempty-directory. Extraction
    failures die with the manual-recovery pointer rather than a traceback.
    """
    print(f"extracting {tar_path.name}")
    target = models_dir / NEMOTRON_DIR
    try:
        with tarfile.open(tar_path, "r:bz2") as tf:
            top = tf.getnames()[0].split("/")[0]
            try:
                tf.extractall(models_dir, filter="data")
            except TypeError:  # Python without the filter kwarg
                tf.extractall(models_dir)
        extracted = models_dir / top
        if extracted != target:
            if target.exists():
                shutil.rmtree(target)
            extracted.rename(target)
        tar_path.unlink()
    except (tarfile.TarError, OSError, IndexError) as e:
        die(f"error: model extraction failed ({e}); grab "
            f"{NEMOTRON_TARBALL} from the sherpa-onnx asr-models release "
            f"page, extract it into {models_dir}, and rename the directory "
            f"to {NEMOTRON_DIR}", 1)
    print(f"models ready: {target}")


def ensure_models(workspace: Path) -> None:
    models_dir = workspace / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    layout = verify_layout(workspace, models_only=True)
    need_nemotron = any(NEMOTRON_DIR in p for p in layout)
    need_vad = any(VAD_FILE in p for p in layout)
    if not (need_nemotron or need_vad):
        return
    print(f"total download ~{TOTAL_DOWNLOAD_MB} MB (the calling skill "
          "asked for consent before running this)")
    if need_nemotron:
        tar_path = models_dir / NEMOTRON_TARBALL
        try:
            download(f"{ASR_RELEASE}/{NEMOTRON_TARBALL}", tar_path)
        except OSError as e:
            die(f"error: model download failed ({e}); grab "
                f"{NEMOTRON_TARBALL} from the sherpa-onnx asr-models "
                f"release page, extract it into {models_dir}, and rename "
                f"the directory to {NEMOTRON_DIR}", 1)
        extract_nemotron(tar_path, models_dir)
    if need_vad:
        try:
            download(f"{ASR_RELEASE}/{VAD_FILE}", models_dir / VAD_FILE)
        except OSError as e:
            die(f"error: VAD download failed ({e}); grab {VAD_FILE} from "
                f"the sherpa-onnx asr-models release page into "
                f"{models_dir}", 1)


def ensure_venv(workspace: Path, python_version: str) -> None:
    commands = planned_commands(workspace, python_version)
    if not venv_python(workspace).exists():
        for cmd in commands:
            r = subprocess.run(cmd)
            if r.returncode != 0:
                die(f"error: {' '.join(cmd)} failed", 1)
    else:
        # Existing venv: install is idempotent and fixes a broken dep set.
        r = subprocess.run(commands[1])
        if r.returncode != 0:
            die("error: dependency install into existing venv failed", 1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", required=True, type=Path)
    ap.add_argument("--python", default="3.12")
    ap.add_argument("--models-only", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    ws = args.workspace

    if args.check:
        problems = verify(ws, models_only=args.models_only)
        if problems:
            for p in problems:
                print(p)
            return 4
        print(f"ready: {ws}")
        return 0

    if args.dry_run:
        print(json.dumps({
            "workspace": str(ws),
            "commands": [] if args.models_only
            else planned_commands(ws, args.python),
            "downloads": planned_downloads(ws),
            "total-download-mb": TOTAL_DOWNLOAD_MB,
        }, indent=2))
        return 0

    for sub in ("models", "out"):
        (ws / sub).mkdir(parents=True, exist_ok=True)

    if not args.models_only:
        ensure_venv(ws, args.python)
    ensure_models(ws)

    problems = verify(ws, models_only=args.models_only)
    if problems:
        die("error: workspace still not ready:\n" + "\n".join(problems), 1)
    print(f"ready: {ws}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
