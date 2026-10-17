#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for resolve_diff.py: OTIO parsing and the cut-versus-finished diff.
The Resolve export runs against a live Studio instance, not here."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "resolve_diff.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("resolve_diff", SCRIPT)
rd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rd)

FPS = 30.0
SRC = "/media/take.mp4"


def rt(seconds):
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": FPS,
            "value": round(seconds * FPS)}


def clip(start, end, path=SRC, media_start=0.0):
    ref = {"OTIO_SCHEMA": "ExternalReference.1", "target_url": path,
           "available_range": {"start_time": rt(media_start),
                               "duration": rt(600)}}
    return {"OTIO_SCHEMA": "Clip.2", "name": Path(path).name,
            "source_range": {"start_time": rt(media_start + start),
                             "duration": rt(end - start)},
            "media_references": {"DEFAULT_MEDIA": ref},
            "active_media_reference_key": "DEFAULT_MEDIA", "enabled": True}


def gap(seconds):
    return {"OTIO_SCHEMA": "Gap.1", "source_range": {"start_time": rt(0),
                                                     "duration": rt(seconds)}}


def title(seconds):
    return {"OTIO_SCHEMA": "Clip.2", "name": "Text+",
            "source_range": {"start_time": rt(0), "duration": rt(seconds)},
            "media_references": {"DEFAULT_MEDIA": {
                "OTIO_SCHEMA": "GeneratorReference.1"}},
            "active_media_reference_key": "DEFAULT_MEDIA"}


def timeline(v1, extra_video=(), extra_audio=()):
    tracks = [{"OTIO_SCHEMA": "Track.1", "kind": "Video", "name": "Video 1",
               "children": v1}]
    tracks += [{"OTIO_SCHEMA": "Track.1", "kind": "Video",
                "name": f"Video {i + 2}", "children": c}
               for i, c in enumerate(extra_video)]
    tracks += [{"OTIO_SCHEMA": "Track.1", "kind": "Audio", "name": "Audio 1",
                "children": v1}]
    tracks += [{"OTIO_SCHEMA": "Track.1", "kind": "Audio",
                "name": f"Audio {i + 2}", "children": c}
               for i, c in enumerate(extra_audio)]
    return {"OTIO_SCHEMA": "Timeline.1", "global_start_time": rt(3600),
            "tracks": {"OTIO_SCHEMA": "Stack.1", "children": tracks}}


EDL = [{"source": SRC, "start": 10.0, "end": 20.0},
       {"source": SRC, "start": 30.0, "end": 40.0},
       {"source": SRC, "start": 50.0, "end": 60.0},
       {"source": SRC, "start": 70.0, "end": 80.0}]


def run(v1, **kw):
    tracks = rd.parse_otio(timeline(v1, **kw))
    return rd.diff(EDL, tracks[0]["items"], FPS), tracks


class TestParse(unittest.TestCase):
    def test_record_times_skip_gaps_and_media_start_is_removed(self):
        tracks = rd.parse_otio(timeline([clip(10, 20, media_start=3600),
                                         gap(2), clip(30, 40)]))
        items = tracks[0]["items"]
        self.assertAlmostEqual(items[0]["src_start"], 10.0)
        self.assertAlmostEqual(items[1]["rec_start"], 12.0)

    def test_file_urls_match_plain_paths(self):
        self.assertEqual(rd.norm_path("file:///media/take%20one.mp4"),
                         "/media/take one.mp4")

    def test_not_a_timeline_raises(self):
        with self.assertRaises(ValueError):
            rd.parse_otio({"OTIO_SCHEMA": "Clip.2"})


class TestDiff(unittest.TestCase):
    def test_identical_cut_has_no_changes(self):
        result, _ = run([clip(s["start"], s["end"]) for s in EDL])
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["unchanged"], 4)

    def test_removed_segment(self):
        result, _ = run([clip(10, 20), clip(30, 40), clip(70, 80)])
        self.assertEqual(result["totals"], {"removed": 1})
        self.assertEqual(result["changes"][0]["segment"], 2)

    def test_trimmed_and_extended(self):
        result, _ = run([clip(11, 20), clip(29, 40), clip(50, 60),
                         clip(70, 80)])
        kinds = {c["segment"]: c["kind"] for c in result["changes"]}
        self.assertEqual(kinds, {0: "trimmed", 1: "extended"})
        self.assertEqual(result["changes"][0]["head"], 1.0)

    def test_joined_restores_the_removed_gap(self):
        result, _ = run([clip(10, 40), clip(50, 60), clip(70, 80)])
        joined = [c for c in result["changes"] if c["kind"] == "joined"]
        self.assertEqual(joined[0]["segments"], [0, 1])
        self.assertAlmostEqual(joined[0]["seconds"], 10.0)

    def test_split_reports_what_was_cut_inside(self):
        result, _ = run([clip(10, 14), clip(16, 20), clip(30, 40),
                         clip(50, 60), clip(70, 80)])
        self.assertEqual(result["totals"], {"split": 1})
        self.assertAlmostEqual(result["changes"][0]["seconds"], 2.0)

    def test_added_material(self):
        result, _ = run([clip(10, 20), clip(30, 40), clip(50, 60),
                         clip(70, 80), clip(5, 8, path="/media/broll.mp4")])
        self.assertEqual(result["totals"], {"added": 1})

    def test_moved_segment(self):
        result, _ = run([clip(10, 20), clip(50, 60), clip(30, 40),
                         clip(70, 80)])
        self.assertEqual(result["totals"], {"moved": 1})

    def test_wrong_timeline_does_not_match(self):
        result, _ = run([clip(0, 5, path="/other/file.mp4")])
        self.assertFalse(result["matched"])


class TestTracks(unittest.TestCase):
    def test_counts_tracks_above_v1_and_a1(self):
        _, tracks = run([clip(10, 20)],
                        extra_video=[[gap(1), title(2), clip(0, 3, "/g.mov")]],
                        extra_audio=[[clip(0, 30, "/music.wav")]])
        self.assertEqual(rd.track_counts(tracks), [
            {"track": "V2", "name": "Video 2", "media": 1, "generated": 1},
            {"track": "A2", "name": "Audio 2", "media": 1, "generated": 0}])


class TestCli(unittest.TestCase):
    def test_otio_mode_writes_markdown_and_fails_on_wrong_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "edl.json").write_text(json.dumps({"segments": EDL}))
            (tmp / "good.otio").write_text(json.dumps(
                timeline([clip(10, 20), clip(30, 40), clip(70, 80)])))
            (tmp / "bad.otio").write_text(json.dumps(
                timeline([clip(0, 5, path="/x.mp4")])))
            base = [sys.executable, str(SCRIPT), "--edl", str(tmp / "edl.json")]
            r = subprocess.run(base + ["--otio", str(tmp / "good.otio"),
                                       "-o", str(tmp / "d.md")],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("| removed | 2 |", (tmp / "d.md").read_text())
            r = subprocess.run(base + ["--otio", str(tmp / "bad.otio"),
                                       "-o", str(tmp / "e.md")],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            r = subprocess.run(base + ["-o", str(tmp / "f.md")],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
