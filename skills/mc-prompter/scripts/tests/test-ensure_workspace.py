#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for ensure_workspace.py (mc-prompter prompter-lab builder).

Run directly:
    uv run skills/mc-prompter/scripts/tests/test-ensure_workspace.py

Pure stdlib unittest. Exercises the check/dry-run/argument logic and the
layout verification against temp directories. Nothing downloads, nothing
builds a venv, nothing touches the network; ready-looking workspaces are
fabricated with sparse files truncated to the size floors.
"""

import contextlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent))

import ensure_workspace as ew  # noqa: E402


def fabricate_models(workspace: Path) -> None:
    """Create model files at exactly the size floors (sparse, instant)."""
    for rel, min_size in ew.MODEL_MIN_SIZES.items():
        path = workspace / "models" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.truncate(min_size)


def fabricate_venv(workspace: Path) -> None:
    py = ew.venv_python(workspace)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_bytes(b"")


class TestVenvPython(unittest.TestCase):

    def test_posix_shape(self):
        p = ew.venv_python(Path("/ws"), platform="darwin")
        self.assertEqual(p, Path("/ws/.venv/bin/python"))

    def test_windows_shape(self):
        p = ew.venv_python(Path("/ws"), platform="win32")
        self.assertEqual(p, Path("/ws/.venv/Scripts/python.exe"))


class TestVerifyLayout(unittest.TestCase):

    def test_empty_workspace_lists_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            problems = ew.verify_layout(Path(tmp))
            self.assertEqual(len(problems), 1 + len(ew.MODEL_MIN_SIZES))
            self.assertTrue(any("venv missing" in p for p in problems))
            for rel in ew.MODEL_MIN_SIZES:
                self.assertTrue(
                    any(rel in p for p in problems), rel)

    def test_models_only_skips_the_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            problems = ew.verify_layout(Path(tmp), models_only=True)
            self.assertFalse(any("venv" in p for p in problems))
            self.assertEqual(len(problems), len(ew.MODEL_MIN_SIZES))

    def test_fabricated_layout_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            fabricate_models(ws)
            fabricate_venv(ws)
            self.assertEqual(ew.verify_layout(ws), [])

    def test_truncated_model_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            fabricate_models(ws)
            fabricate_venv(ws)
            short = ws / "models" / ew.NEMOTRON_DIR / "encoder.int8.onnx"
            with open(short, "wb") as f:
                f.truncate(1000)
            problems = ew.verify_layout(ws)
            self.assertEqual(len(problems), 1)
            self.assertIn("truncated", problems[0])
            self.assertIn("encoder.int8.onnx", problems[0])


class TestPlannedCommands(unittest.TestCase):

    def test_venv_then_pinned_install(self):
        cmds = ew.planned_commands(Path("/ws"), "3.12")
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0][:3], ["uv", "venv", "--python"])
        self.assertEqual(cmds[0][3], "3.12")
        self.assertEqual(cmds[1][:3], ["uv", "pip", "install"])
        self.assertIn("aiohttp==3.12.15", cmds[1])
        self.assertIn("sherpa-onnx==1.13.4", cmds[1])
        self.assertIn("numpy", cmds[1])
        self.assertIn("soundfile", cmds[1])


class TestPlannedDownloads(unittest.TestCase):

    def test_empty_workspace_needs_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            urls = ew.planned_downloads(Path(tmp))
            self.assertEqual(len(urls), 2)
            self.assertTrue(urls[0].endswith(".tar.bz2"))
            self.assertTrue(urls[1].endswith(ew.VAD_FILE))

    def test_vad_present_needs_only_the_tarball(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            vad = ws / "models" / ew.VAD_FILE
            vad.parent.mkdir(parents=True)
            with open(vad, "wb") as f:
                f.truncate(ew.MODEL_MIN_SIZES[ew.VAD_FILE])
            urls = ew.planned_downloads(ws)
            self.assertEqual(len(urls), 1)
            self.assertIn(ew.NEMOTRON_TARBALL, urls[0])

    def test_complete_models_need_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            fabricate_models(ws)
            self.assertEqual(ew.planned_downloads(ws), [])


class TestCheckMode(unittest.TestCase):

    def test_check_not_ready_exits_4(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ew.main(["--workspace", tmp, "--check"])
            self.assertEqual(rc, 4)
            self.assertIn("missing", out.getvalue())

    def test_check_ready_models_only_exits_0(self):
        # --models-only --check verifies the layout without running the
        # venv import subprocess, so a fabricated layout passes.
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            fabricate_models(ws)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ew.main(
                    ["--workspace", tmp, "--check", "--models-only"])
            self.assertEqual(rc, 0)
            self.assertIn("ready", out.getvalue())


class TestDryRun(unittest.TestCase):

    def test_dry_run_reports_the_full_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ew.main(["--workspace", tmp, "--dry-run"])
            self.assertEqual(rc, 0)
            plan = json.loads(out.getvalue())
            self.assertEqual(plan["workspace"], tmp)
            self.assertEqual(len(plan["commands"]), 2)
            self.assertEqual(len(plan["downloads"]), 2)
            self.assertEqual(plan["total-download-mb"], 465)

    def test_dry_run_models_only_plans_no_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = ew.main(
                    ["--workspace", tmp, "--dry-run", "--models-only"])
            self.assertEqual(rc, 0)
            plan = json.loads(out.getvalue())
            self.assertEqual(plan["commands"], [])
            self.assertEqual(len(plan["downloads"]), 2)

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ew.main(["--workspace", tmp, "--dry-run"])
            self.assertEqual(list(Path(tmp).iterdir()), [])


def fabricate_tarball(tmp, filename="tokens.txt", content="a\nb\n"):
    """A tiny nemotron-shaped tar.bz2 under tmp/models; returns (models,
    tar_path, dated_dir_name)."""
    models = Path(tmp) / "models"
    models.mkdir(exist_ok=True)
    dated = ew.NEMOTRON_TARBALL[: -len(".tar.bz2")]
    src = Path(tmp) / "src" / dated
    src.mkdir(parents=True)
    (src / filename).write_text(content, encoding="utf-8")
    tar_path = models / ew.NEMOTRON_TARBALL
    with tarfile.open(tar_path, "w:bz2") as tf:
        tf.add(src, arcname=dated)
    return models, tar_path, dated


class TestExtractNemotron(unittest.TestCase):

    def test_extract_renames_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            models, tar_path, dated = fabricate_tarball(tmp)
            ew.extract_nemotron(tar_path, models)
            target = models / ew.NEMOTRON_DIR
            self.assertTrue((target / "tokens.txt").is_file())
            self.assertFalse(tar_path.exists())
            self.assertFalse((models / dated).exists())

    def test_extract_replaces_a_stale_target_dir(self):
        # repair scenario: an interrupted earlier bootstrap left a truncated
        # nemotron-streaming dir; re-extraction must replace it instead of
        # crashing on rename-onto-nonempty-directory after a 464 MB download
        with tempfile.TemporaryDirectory() as tmp:
            models, tar_path, dated = fabricate_tarball(tmp)
            stale = models / ew.NEMOTRON_DIR
            stale.mkdir()
            (stale / "encoder.int8.onnx").write_bytes(b"truncated junk")
            ew.extract_nemotron(tar_path, models)
            target = models / ew.NEMOTRON_DIR
            self.assertTrue((target / "tokens.txt").is_file())
            self.assertFalse((target / "encoder.int8.onnx").exists())
            self.assertFalse(tar_path.exists())
            self.assertFalse((models / dated).exists())

    def test_corrupt_tarball_dies_with_recovery_pointer(self):
        # a garbage tarball (interrupted or corrupted download) must die
        # with the manual-recovery message, not an unhandled traceback
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp) / "models"
            models.mkdir()
            tar_path = models / ew.NEMOTRON_TARBALL
            tar_path.write_bytes(b"this is not a bzip2 tarball")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as ctx:
                    ew.extract_nemotron(tar_path, models)
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("extraction failed", err.getvalue())
            self.assertIn(ew.NEMOTRON_DIR, err.getvalue())


class TestDownloadOverwrite(unittest.TestCase):

    def test_download_replaces_an_existing_truncated_file(self):
        # a truncated leftover at the destination (interrupted bootstrap)
        # must be overwritten; Path.rename raises FileExistsError on
        # Windows, so download() uses os.replace
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / ew.VAD_FILE
            dest.write_bytes(b"short")

            def fake_retrieve(url, filename, reporthook=None):
                Path(filename).write_bytes(b"full new content")

            original = ew.urllib.request.urlretrieve
            ew.urllib.request.urlretrieve = fake_retrieve
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    ew.download("http://example.invalid/vad", dest)
            finally:
                ew.urllib.request.urlretrieve = original
            self.assertEqual(dest.read_bytes(), b"full new content")
            part = dest.with_suffix(dest.suffix + ".part")
            self.assertFalse(part.exists())


if __name__ == "__main__":
    unittest.main()
