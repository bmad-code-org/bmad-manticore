#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for new_project.py — scaffolding must always yield a project.json
that satisfies the pipeline contract (skills/mc-pipeline/PIPELINE.md)."""
import datetime
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "new_project.py"

PROFILE = """---
stages: [new, braindump, outline, script, record, cut, beats, graphics, assets, package, final, retro]
---

# Talking head
"""

SUBDIRS = ["raw", "transcript", "cut", "beats", "graphics", "assets", "packaging", "renders"]


def run(args, expect_ok=True):
    r = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)
    if expect_ok:
        assert r.returncode == 0, f"new_project failed: {r.stderr}\n{r.stdout}"
    return r


class TestNewProject(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.projects = root / "projects"
        self.formats = root / "formats"
        self.projects.mkdir()
        self.formats.mkdir()
        (self.formats / "talking-head.md").write_text(PROFILE)

    def tearDown(self):
        self._tmp.cleanup()

    def _new(self, slug="demo-video", fmt="talking-head", extra=None, expect_ok=True):
        args = [slug, "--format", fmt,
                "--projects-dir", str(self.projects), "--formats-dir", str(self.formats)]
        return run(args + (extra or []), expect_ok=expect_ok)

    def test_project_json_matches_contract(self):
        self._new(extra=["--title", "Demo Video"])
        state = json.loads((self.projects / "demo-video" / "project.json").read_text())
        self.assertEqual(state["slug"], "demo-video")
        self.assertEqual(state["title"], "Demo Video")
        self.assertEqual(state["format"], "talking-head")
        self.assertEqual(state["created"], datetime.date.today().isoformat())
        self.assertIsNone(state["parent"])
        self.assertEqual(state["stages"][0], "new")
        self.assertEqual(state["stage"], "braindump")
        self.assertEqual(state["stages_done"], ["new"])
        self.assertIn(state["stage"], state["stages"])
        self.assertTrue(set(state["stages_done"]) <= set(state["stages"]))
        self.assertEqual(state["approvals"],
                         {"outline": None, "cutplan": None, "beats": None, "final": None})
        self.assertEqual(state["artifacts"], {})

    def test_creates_subdirs_and_brief(self):
        self._new()
        proj = self.projects / "demo-video"
        for sub in SUBDIRS:
            self.assertTrue((proj / sub).is_dir(), f"missing subfolder {sub}")
        self.assertIn("# Brief:", (proj / "brief.md").read_text())

    def test_stages_copied_from_profile_not_master_list(self):
        (self.formats / "short.md").write_text("---\nstages: [new, cut, final]\n---\n")
        self._new(slug="a-short", fmt="short")
        state = json.loads((self.projects / "a-short" / "project.json").read_text())
        self.assertEqual(state["stages"], ["new", "cut", "final"])
        self.assertEqual(state["stage"], "cut")

    def test_parent_recorded_for_shorts(self):
        self._new(extra=["--parent", "long-form-parent"])
        state = json.loads((self.projects / "demo-video" / "project.json").read_text())
        self.assertEqual(state["parent"], "long-form-parent")

    def test_missing_profile_lists_available(self):
        r = self._new(fmt="nope", expect_ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("available: talking-head", r.stderr)

    def test_empty_formats_dir_points_to_setup(self):
        (self.formats / "talking-head.md").unlink()
        r = self._new(fmt="talking-head", expect_ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("mc-setup", r.stderr)

    def test_profile_without_stages_line_fails(self):
        (self.formats / "broken.md").write_text("# No frontmatter here\n")
        r = self._new(fmt="broken", expect_ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("stages", r.stderr)

    def test_profile_not_starting_with_new_fails(self):
        (self.formats / "skipper.md").write_text("---\nstages: [cut, final]\n---\n")
        r = self._new(fmt="skipper", expect_ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("must start with 'new'", r.stderr)

    def test_rejects_non_kebab_slug(self):
        for bad in ["Bad_Slug", "has space", "-leading", "UPPER"]:
            r = self._new(slug=bad, expect_ok=False)
            self.assertNotEqual(r.returncode, 0, f"slug {bad!r} was accepted")
            if not bad.startswith("-"):  # argparse rejects leading-dash slugs itself
                self.assertIn("kebab-case", r.stderr)

    def test_rejects_existing_project(self):
        self._new()
        r = self._new(expect_ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already exists", r.stderr)


if __name__ == "__main__":
    unittest.main()
