#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for farm_audio.py: command construction per kind, per-OS venv
interpreter paths, provider gating, workspace readiness, manifest rows,
and the local lane end to end. The fake workspace is a real throwaway
venv (works on POSIX and Windows alike) whose site-packages carries stub
numpy/scipy/torch/diffusers modules, so the real sfx engine payload runs
without heavy deps. No models, no network, no downloads."""
import importlib.util
import json
import os
import subprocess
import sys
import sysconfig
import tempfile
import unittest
import unittest.mock
import venv
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "farm_audio.py"

spec = importlib.util.spec_from_file_location("farm_audio", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Stub modules planted in the fake venv so the real sfx_audioldm2.py
# payload runs end to end on the cpu rung of the device ladder and
# writes the --out file, with no heavy deps.
ENGINE_STUBS = {
    "numpy.py": "def abs(x):\n    return x\n",
    "scipy/__init__.py": "",
    "scipy/io/__init__.py": "",
    "scipy/io/wavfile.py": (
        "def write(path, sr, data):\n"
        "    with open(path, 'wb') as f:\n"
        "        f.write(b'RIFF')\n"),
    "torch.py": (
        "float32 = 'float32'\n"
        "class _Flag:\n"
        "    @staticmethod\n"
        "    def is_available():\n"
        "        return False\n"
        "class _Backends:\n"
        "    mps = _Flag()\n"
        "cuda = _Flag()\n"
        "backends = _Backends()\n"
        "class Generator:\n"
        "    def __init__(self, device=None):\n"
        "        pass\n"
        "    def manual_seed(self, seed):\n"
        "        return self\n"),
    "diffusers.py": (
        "class _Audio:\n"
        "    def max(self):\n"
        "        return 1.0\n"
        "    def __truediv__(self, other):\n"
        "        return self\n"
        "    def __mul__(self, other):\n"
        "        return self\n"
        "class _Out:\n"
        "    def __init__(self):\n"
        "        self.audios = [_Audio()]\n"
        "class AudioLDM2Pipeline:\n"
        "    @classmethod\n"
        "    def from_pretrained(cls, *a, **k):\n"
        "        return cls()\n"
        "    def to(self, device):\n"
        "        return self\n"
        "    def __call__(self, *a, **k):\n"
        "        return _Out()\n"),
}


def make_venv_shim(ws: Path, stubs: dict[str, str]) -> Path:
    """Create a real throwaway venv at ws/.venv and plant stub modules in
    its site-packages. Returns the per-OS venv interpreter path.

    Duplicated in test-ensure_workspace.py; keep the two in sync."""
    venv.create(ws / ".venv", with_pip=False, symlinks=(os.name != "nt"))
    site = Path(sysconfig.get_path(
        "purelib", scheme="venv",
        vars={"base": str(ws / ".venv"), "platbase": str(ws / ".venv")}))
    site.mkdir(parents=True, exist_ok=True)
    for rel, body in stubs.items():
        dest = site / Path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
    return mod.venv_python(ws)


def make_args(**kw):
    import argparse
    base = dict(kind="sfx", provider="audioldm2-local",
                workspace=Path("/ws"), out_dir=Path("/out"), name=None,
                text=None, voice="af_heart", speed=1.0, script=None,
                prompt=None, seconds=4.0, seed=7, dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


def fake_workspace(root: Path) -> Path:
    """A workspace whose venv runs the real sfx payload against stubs."""
    ws = root / "audio-lab"
    ws.mkdir(parents=True)
    (ws / "models").mkdir()
    make_venv_shim(ws, ENGINE_STUBS)
    return ws


class TestVenvPython(unittest.TestCase):
    def test_posix_layout(self):
        ws = Path("/ws")
        with unittest.mock.patch.object(os, "name", "posix"):
            py = mod.venv_python(ws)
        self.assertEqual(py, ws / ".venv" / "bin" / "python")

    def test_windows_layout(self):
        ws = Path("/ws")
        with unittest.mock.patch.object(os, "name", "nt"):
            py = mod.venv_python(ws)
        self.assertEqual(py, ws / ".venv" / "Scripts" / "python.exe")


class TestBuildCommand(unittest.TestCase):
    def test_tts_single(self):
        cmd = mod.build_command(
            make_args(kind="tts", provider="kokoro-local", text="hello",
                      voice="am_michael", speed=1.1), Path("/out/n.wav"))
        self.assertIn("single", cmd)
        self.assertIn("--models-dir", cmd)
        self.assertIn("am_michael", cmd)
        self.assertIn("1.1", cmd)
        self.assertTrue(cmd[1].endswith("tts_kokoro.py"))

    def test_command_runs_the_venv_interpreter(self):
        cmd = mod.build_command(
            make_args(prompt="whoosh"), Path("/out/s.wav"))
        self.assertEqual(cmd[0], str(mod.venv_python(Path("/ws"))))

    def test_podcast_uses_script_mode(self):
        cmd = mod.build_command(
            make_args(kind="podcast", provider="kokoro-local",
                      script="lines.json"), Path("/out/p.wav"))
        self.assertIn("script", cmd)
        self.assertIn("lines.json", cmd)

    def test_music_and_sfx(self):
        cmd = mod.build_command(
            make_args(kind="music", provider="musicgen-local",
                      prompt="calm bed", seconds=12.0), Path("/out/m.wav"))
        self.assertTrue(cmd[1].endswith("music_musicgen.py"))
        self.assertIn("12.0", cmd)
        self.assertNotIn("--seed", cmd)
        cmd = mod.build_command(
            make_args(prompt="whoosh", seed=9), Path("/out/s.wav"))
        self.assertTrue(cmd[1].endswith("sfx_audioldm2.py"))
        self.assertIn("--seed", cmd)
        self.assertIn("9", cmd)

    def test_missing_required_arg_is_usage_error(self):
        for args in (make_args(kind="tts", provider="kokoro-local"),
                     make_args(kind="podcast", provider="kokoro-local"),
                     make_args(prompt=None)):
            with self.assertRaises(SystemExit) as ctx:
                mod.build_command(args, Path("/out/x.wav"))
            self.assertEqual(ctx.exception.code, 2)


class TestManifest(unittest.TestCase):
    def test_append_creates_then_appends(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            mod.append_manifest(out, {"file": "a.wav"})
            mod.append_manifest(out, {"file": "b.wav"})
            rows = json.loads((out / "manifest.json").read_text())
            self.assertEqual([r["file"] for r in rows], ["a.wav", "b.wav"])


class TestCli(unittest.TestCase):
    def run_script(self, *argv, env=None):
        e = dict(os.environ)
        e.pop("HF_HOME", None)
        if env:
            e.update(env)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *argv],
            capture_output=True, text=True, env=e)

    def test_unimplemented_provider_exits_3(self):
        with tempfile.TemporaryDirectory() as td:
            r = self.run_script("--kind", "sfx", "--provider", "elevenlabs-sfx",
                                "--workspace", td, "--out-dir", td,
                                "--prompt", "whoosh")
            self.assertEqual(r.returncode, 3)
            self.assertIn("opt-in", r.stderr)

    def test_dry_run_reports_command_and_hf_home(self):
        with tempfile.TemporaryDirectory() as td:
            r = self.run_script("--kind", "sfx", "--provider", "audioldm2-local",
                                "--workspace", td, "--out-dir", td,
                                "--prompt", "whoosh", "--dry-run")
            self.assertEqual(r.returncode, 0, r.stderr)
            info = json.loads(r.stdout)
            self.assertEqual(info["HF_HOME"], str(Path(td) / "hf-cache"))
            self.assertIn("sfx_audioldm2.py", info["command"][1])

    def test_missing_workspace_exits_4(self):
        with tempfile.TemporaryDirectory() as td:
            r = self.run_script("--kind", "sfx", "--provider", "audioldm2-local",
                                "--workspace", str(Path(td) / "nowhere"),
                                "--out-dir", td, "--prompt", "whoosh")
            self.assertEqual(r.returncode, 4)
            self.assertIn("ensure_workspace", r.stderr)

    def test_local_lane_end_to_end_with_fake_venv(self):
        with tempfile.TemporaryDirectory() as td:
            ws = fake_workspace(Path(td))
            out_dir = Path(td) / "sounds"
            r = self.run_script("--kind", "sfx", "--provider", "audioldm2-local",
                                "--workspace", str(ws),
                                "--out-dir", str(out_dir),
                                "--prompt", "a fast cinematic whoosh",
                                "--name", "whoosh-01")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((out_dir / "whoosh-01.wav").exists())
            rows = json.loads((out_dir / "manifest.json").read_text())
            self.assertEqual(rows[0]["provider"], "audioldm2-local")
            self.assertEqual(rows[0]["model"], "cvssp/audioldm2")
            self.assertIsNone(rows[0]["cost"])


if __name__ == "__main__":
    unittest.main()
