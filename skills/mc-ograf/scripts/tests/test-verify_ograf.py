#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for verify_ograf.py — the deterministic, browser-free parts: manifest
discovery, usage/structure errors, the per-OS manual-step strings, and the
graceful no-headless fallback.

The full headless render check requires Playwright + a browser and is exercised
by running the script directly on a package; it is not unit-tested here. The
interactive serve-and-open branch is tty-gated and preview.html-gated, so it
never fires under the test harness; only its gating is asserted."""
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "verify_ograf.py"

spec = importlib.util.spec_from_file_location("verify_ograf", SCRIPT)
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


def make_pkg(tmp, *, with_main=True):
    pkg = Path(tmp) / "pkg"
    pkg.mkdir()
    (pkg / "g.ograf.json").write_text(json.dumps({
        "id": "g", "name": "G", "main": "g.mjs",
        "supportsNonRealTime": True, "schema": {"type": "object", "properties": {}},
    }))
    if with_main:
        (pkg / "g.mjs").write_text(
            "class G extends HTMLElement{\n"
            "  constructor(){super();this.attachShadow({mode:'open'});}\n"
            "  async load(){this.shadowRoot.innerHTML="
            "\"<div style='position:absolute;inset:0'>x</div>\";return undefined;}\n"
            "  async goToTime(){return undefined;}\n"
            "  async dispose(){return undefined;}\n"
            "  async updateAction(){return undefined;}\n"
            "  async playAction(){return {currentStep:1};}\n"
            "  async stopAction(){return undefined;}\n"
            "  async customAction(){return undefined;}\n"
            "  async setActionsSchedule(){return undefined;}\n"
            "}\nexport default G;\n")
    return pkg


def run(args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


class TestVerifyHelpers(unittest.TestCase):
    def test_find_manifest_returns_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = make_pkg(tmp)
            self.assertEqual(verify.find_manifest(pkg).name, "g.ograf.json")

    def test_find_manifest_exits_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            with self.assertRaises(SystemExit):
                verify.find_manifest(empty)


class TestManualSteps(unittest.TestCase):
    def test_steps_per_os(self):
        pkg = Path("/x/pkg")
        expected_open = {"Darwin": "open http://",
                         "Windows": "start http://",
                         "Linux": "xdg-open http://"}
        for system, needle in expected_open.items():
            steps = verify.manual_verify_steps(pkg, system=system)
            joined = "\n".join(steps)
            self.assertIn(needle, joined, system)
            # No shell chaining and no bare python3: both are POSIX-shaped.
            self.assertNotIn("&&", joined, system)
            self.assertNotIn("python3", joined, system)
            self.assertIn("uv run python -m http.server 8771", joined, system)
            self.assertIn(str(pkg), joined, system)

    def test_windows_cd_crosses_drives(self):
        steps = verify.manual_verify_steps(Path("/x/pkg"), system="Windows")
        self.assertIn("cd /d", "\n".join(steps))

    def test_default_system_is_this_machine(self):
        a = verify.manual_verify_steps(Path("/x/pkg"))
        b = verify.manual_verify_steps(Path("/x/pkg"),
                                       system=verify.platform.system())
        self.assertEqual(a, b)

    def test_manual_steps_prints_json_and_never_blocks_without_tty(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = make_pkg(tmp)  # no preview.html and no tty: both gates hold
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                verify.manual_steps(pkg, "Playwright not installed")
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["status"], "skipped-no-headless")
            self.assertEqual(payload["reason"], "Playwright not installed")
            self.assertEqual(payload["manual_verify"],
                             verify.manual_verify_steps(pkg))

    def test_open_preview_noop_without_preview_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = make_pkg(tmp)
            # Must return immediately (no server, no browser, no input()).
            self.assertIsNone(verify.open_preview_if_interactive(pkg))


class TestVerifyCli(unittest.TestCase):
    def test_missing_main_fails_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = make_pkg(tmp, with_main=False)
            r = run([str(pkg)])
            self.assertEqual(r.returncode, 2)
            self.assertIn("folder", r.stderr.lower() + r.stdout.lower())

    def test_not_a_directory(self):
        r = run(["/nonexistent/path/xyz"])
        self.assertEqual(r.returncode, 2)

    def test_full_run_is_ok_or_graceful(self):
        # With a valid package: either headless verifies (exit 0) or Playwright is
        # absent and it degrades with manual steps (exit 3). Never crashes (1/2).
        with tempfile.TemporaryDirectory() as tmp:
            pkg = make_pkg(tmp)
            r = run([str(pkg)])
            self.assertIn(r.returncode, (0, 3), f"unexpected: {r.returncode}\n{r.stdout}\n{r.stderr}")
            payload = json.loads(r.stdout)
            self.assertIn("status", payload)


if __name__ == "__main__":
    unittest.main()
