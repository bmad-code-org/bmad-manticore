#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for ensure_workspace.py: planned commands carry the critical
dependency pin, --check readiness reporting, and verify() against a fake
workspace. No venvs are built, no models downloaded."""
import importlib.util
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "ensure_workspace.py"

spec = importlib.util.spec_from_file_location("ensure_workspace", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def ready_workspace(root: Path) -> Path:
    """Fake workspace: shim venv python that passes the import check, plus
    empty model files."""
    ws = root / "audio-lab"
    (ws / ".venv" / "bin").mkdir(parents=True)
    (ws / "models").mkdir()
    shim = ws / ".venv" / "bin" / "python"
    shim.write_text("#!/bin/sh\necho ok\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
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
