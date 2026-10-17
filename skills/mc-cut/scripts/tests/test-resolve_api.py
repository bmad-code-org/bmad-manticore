#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for resolve_api.py: install-path lookup and the Studio check. The live
connection is exercised against a running Resolve, not here."""
import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "resolve_api.py"
spec = importlib.util.spec_from_file_location("resolve_api", SCRIPT)
ra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ra)


class TestPaths(unittest.TestCase):
    def test_defaults_per_platform(self):
        self.assertIn("Library/Application Support",
                      ra.module_dir("darwin", {}))
        self.assertTrue(ra.lib_path("darwin", {}).endswith("fusionscript.so"))
        self.assertTrue(ra.module_dir("win32", {"PROGRAMDATA": r"D:\PD"})
                        .startswith(r"D:\PD\Blackmagic Design"))
        self.assertTrue(ra.lib_path("win32", {}).endswith("fusionscript.dll"))
        self.assertEqual(ra.module_dir("linux", {}),
                         "/opt/resolve/Developer/Scripting/Modules")

    def test_env_overrides(self):
        env = {"RESOLVE_SCRIPT_API": "/x/Scripting",
               "RESOLVE_SCRIPT_LIB": "/y/fusionscript.so"}
        self.assertEqual(ra.module_dir("darwin", env),
                         str(Path("/x/Scripting") / "Modules"))
        self.assertEqual(ra.lib_path("linux", env), "/y/fusionscript.so")


class TestStudio(unittest.TestCase):
    def test_studio_detection(self):
        self.assertTrue(ra.is_studio("DaVinci Resolve Studio"))
        self.assertFalse(ra.is_studio("DaVinci Resolve"))
        self.assertFalse(ra.is_studio(None))


if __name__ == "__main__":
    unittest.main()
