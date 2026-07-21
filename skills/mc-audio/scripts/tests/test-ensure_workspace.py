#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for ensure_workspace.py: planned commands carry the critical
dependency pin, per-OS venv interpreter paths, the Windows CUDA torch
index plan, --check readiness reporting, and verify() against a fake
workspace. The fake workspace is a real throwaway venv (works on POSIX
and Windows alike) whose site-packages carries stub modules so the
import check passes. No heavy deps are installed, no models downloaded,
no network."""
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

SCRIPT = Path(__file__).resolve().parent.parent / "ensure_workspace.py"

spec = importlib.util.spec_from_file_location("ensure_workspace", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Stub modules planted in the fake venv so VERIFY_SNIPPET's imports and
# version asserts pass without installing anything.
VERIFY_STUBS = {
    "kokoro_onnx.py": "",
    "soundfile.py": "",
    "torch.py": "",
    "scipy.py": "",
    "diffusers.py": "__version__ = '0.31.0'\n",
    "transformers.py": "__version__ = '4.43.4'\n",
}


def make_venv_shim(ws: Path, stubs: dict[str, str]) -> Path:
    """Create a real throwaway venv at ws/.venv and plant stub modules in
    its site-packages. Returns the per-OS venv interpreter path.

    Duplicated in test-farm_audio.py; keep the two in sync."""
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


def ready_workspace(root: Path) -> Path:
    """Fake workspace: venv shim that passes the import check, plus empty
    model files."""
    ws = root / "audio-lab"
    ws.mkdir(parents=True)
    (ws / "models").mkdir()
    make_venv_shim(ws, VERIFY_STUBS)
    for name in mod.KOKORO_FILES:
        (ws / "models" / name).touch()
    return ws


class TestPin(unittest.TestCase):
    def test_install_carries_the_critical_pair(self):
        self.assertIn("diffusers==0.31.0", mod.PIN_INSTALL)
        self.assertIn("transformers==4.43.4", mod.PIN_INSTALL)
        self.assertIn("kokoro-onnx==0.5.0", mod.PIN_INSTALL)

    def test_verify_snippet_asserts_the_pair(self):
        self.assertIn("0.31.0", mod.VERIFY_SNIPPET)
        self.assertIn("4.43.4", mod.VERIFY_SNIPPET)


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


class TestPlannedCommands(unittest.TestCase):
    def test_default_lane_installs_all_pins_from_pypi(self):
        cmds = mod.planned_commands(Path("/ws"), "3.12", cuda_torch=False)
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0][:2], ["uv", "venv"])
        self.assertIn("torch", cmds[1])
        self.assertNotIn("--index-url", cmds[1])

    def test_cuda_lane_installs_torch_from_cu126_index_first(self):
        cmds = mod.planned_commands(Path("/ws"), "3.12", cuda_torch=True)
        self.assertEqual(len(cmds), 3)
        self.assertIn("--index-url", cmds[1])
        self.assertIn(mod.TORCH_CUDA_INDEX, cmds[1])
        self.assertIn("torch", cmds[1])
        # The PyPI install still carries the critical pin, minus torch.
        self.assertIn("diffusers==0.31.0", cmds[2])
        self.assertIn("transformers==4.43.4", cmds[2])
        self.assertNotIn("torch", cmds[2])
        self.assertNotIn("--index-url", cmds[2])

    def test_cuda_detection_is_windows_only(self):
        with unittest.mock.patch.object(os, "name", "posix"):
            self.assertFalse(mod.wants_cuda_torch())


class TestCli(unittest.TestCase):
    def run_script(self, *argv):
        return subprocess.run([sys.executable, str(SCRIPT), *argv],
                              capture_output=True, text=True)

    def test_dry_run_plans_venv_install_and_models(self):
        with tempfile.TemporaryDirectory() as td:
            r = self.run_script("--workspace", td, "--dry-run")
            self.assertEqual(r.returncode, 0, r.stderr)
            plan = json.loads(r.stdout)
            flat = [" ".join(c) for c in plan["commands"]]
            self.assertTrue(any(c.startswith("uv venv") for c in flat))
            self.assertTrue(any("diffusers==0.31.0" in c and
                                "transformers==4.43.4" in c for c in flat))
            self.assertEqual(len(plan["models"]), 2)
            self.assertIn("torch", plan)

    def test_dry_run_skip_models_plans_no_downloads(self):
        with tempfile.TemporaryDirectory() as td:
            r = self.run_script("--workspace", td, "--dry-run", "--skip-models")
            plan = json.loads(r.stdout)
            self.assertEqual(plan["models"], [])

    def test_check_on_empty_dir_exits_4_listing_gaps(self):
        with tempfile.TemporaryDirectory() as td:
            r = self.run_script("--workspace", str(Path(td) / "nowhere"),
                                "--check")
            self.assertEqual(r.returncode, 4)
            self.assertIn("venv missing", r.stdout)
            self.assertIn("model missing", r.stdout)

    def test_check_on_ready_workspace_exits_0(self):
        with tempfile.TemporaryDirectory() as td:
            ws = ready_workspace(Path(td))
            r = self.run_script("--workspace", str(ws), "--check")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("ready", r.stdout)


class TestVerify(unittest.TestCase):
    def test_missing_model_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            ws = ready_workspace(Path(td))
            (ws / "models" / mod.KOKORO_FILES[0]).unlink()
            problems = mod.verify(ws)
            self.assertEqual(len(problems), 1)
            self.assertIn(mod.KOKORO_FILES[0], problems[0])


if __name__ == "__main__":
    unittest.main()
