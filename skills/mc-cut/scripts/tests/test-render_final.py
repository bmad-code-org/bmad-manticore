#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for render_final.py and the shared composite_core.py: pure logic
(timecode parsing, beat-table parsing, overlay resolution, chunk planning,
encoder selection with the per-OS hardware ladders and injected probes,
loudnorm spec/stats parsing, disk estimation, progress parsing) plus CLI
exit codes, and end-to-end renders over fixtures synthesized with ffmpeg
color/test sources including a real two-pass loudnorm run (skipped when
ffmpeg is not installed). No model downloads, no real footage."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import composite_core as core  # noqa: E402

spec = importlib.util.spec_from_file_location("render_final",
                                              SCRIPTS / "render_final.py")
render_final = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render_final)

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


class TestTimecode(unittest.TestCase):
    def test_parse_forms(self):
        self.assertEqual(core.parse_timecode("90"), 90.0)
        self.assertEqual(core.parse_timecode("90.5"), 90.5)
        self.assertEqual(core.parse_timecode("12.5s"), 12.5)
        self.assertEqual(core.parse_timecode("1:30"), 90.0)
        self.assertEqual(core.parse_timecode("01:02:03.25"), 3723.25)

    def test_parse_rejects_garbage(self):
        for bad in ("", "a:b", "1:2:3:4", "-5"):
            with self.assertRaises(ValueError):
                core.parse_timecode(bad)

    def test_format(self):
        self.assertEqual(core.format_timecode(90), "1:30")
        self.assertEqual(core.format_timecode(3723), "1:02:03")
        self.assertEqual(core.format_timecode(5.25, precision=3), "0:05.250")


BEATS_MD = """# Beats

| id | start | dur | end | anchor word | anchor ts | spoken phrase | type | engine | asset | composition |
|---|---|---|---|---|---|---|---|---|---|---|
| b1 | 0:02 | 3 | 0:05 | alpha | 0:02 | "alpha beta" | overlay | html | null | keyword callout |
| b2 | 12.5 | 2.5 |  | gamma | 12.5 | "gamma" | cta | hyperframes | cta-card | subscribe |
| b3 | 30 |  | 34 | delta | 30 | "delta" |  |  |  | legacy 0.x row |
| bad | oops | 2 |  | x | 0 | "x" | overlay |  |  | broken start |
| b4 | 40 |  |  | x | 40 | "x" | overlay |  |  | no dur or end |
"""


class TestParseBeats(unittest.TestCase):
    def test_rows_and_tolerance(self):
        beats, skipped = core.parse_beats_table(BEATS_MD)
        self.assertEqual([b["id"] for b in beats], ["b1", "b2", "b3"])
        self.assertEqual(beats[0], {"id": "b1", "start": 2.0, "dur": 3.0,
                                    "type": "overlay", "asset": None})
        self.assertEqual(beats[1]["type"], "cta")
        self.assertEqual(beats[1]["asset"], "cta-card")
        # 0.x row missing type/engine/asset: type defaults, dur from end-start
        self.assertEqual(beats[2]["type"], "overlay")
        self.assertEqual(beats[2]["dur"], 4.0)
        self.assertEqual(len(skipped), 2)

    def test_no_table_means_no_beats(self):
        beats, skipped = core.parse_beats_table("just prose, no table")
        self.assertEqual(beats, [])
        self.assertEqual(skipped, [])


class TestResolveOverlays(unittest.TestCase):
    def test_extension_priority_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = Path(tmp)
            (g / "b1.mov").touch()
            (g / "b1.png").touch()  # .mov wins
            (g / "b2.png").touch()
            beats = [{"id": "b2", "start": 9.0, "dur": 1.0},
                     {"id": "b1", "start": 2.0, "dur": 3.0},
                     {"id": "b3", "start": 20.0, "dur": 1.0}]
            found, missing = core.resolve_overlays(beats, g)
            self.assertEqual([o["id"] for o in found], ["b1", "b2"])  # sorted
            self.assertEqual(found[0]["path"], str(g / "b1.mov"))
            self.assertFalse(found[0]["image"])
            self.assertTrue(found[1]["image"])
            self.assertEqual(missing, ["b3"])


def edl_4x10():
    segs = [{"source": "raw/a.mp4", "start": i * 20.0, "end": i * 20.0 + 10.0}
            for i in range(4)]
    return {"source": "raw/a.mp4", "fade_ms": 30, "segments": segs}


def edl_n(n, keep=10.0, gap=10.0):
    """n single-source segments of `keep` seconds, `gap` apart in the source."""
    segs = [{"source": "raw/a.mp4", "start": i * (keep + gap),
             "end": i * (keep + gap) + keep} for i in range(n)]
    return {"source": "raw/a.mp4", "fade_ms": 30, "segments": segs}


class TestPlanSegments(unittest.TestCase):
    def test_target_floor_greedy_split(self):
        # 4x10s, target 25s: accumulate until >=25, cut at next boundary.
        segs = core.plan_segments(edl_4x10(), target_seconds=25.0)
        self.assertEqual([(s["seg_start"], s["seg_end"]) for s in segs],
                         [(0, 3), (3, 4)])
        self.assertEqual(segs[0]["duration"], 30.0)
        self.assertEqual(segs[1]["offset"], 30.0)

    def test_short_timeline_is_one_segment(self):
        # Never reaches the target -> a single render-segment.
        segs = core.plan_segments(edl_4x10(), target_seconds=600.0)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["seg_start"], 0)
        self.assertEqual(segs[0]["seg_end"], 4)

    def test_boundary_snaps_past_overlay_span(self):
        # target 15s would cut after seg1 (boundary at 20s), but an overlay
        # spans 20s, so the cut snaps to the next safe boundary (30s).
        ov = [{"index": 1, "start": 19.0, "dur": 2.0, "image": True,
               "path": "g/x.png", "id": "x"}]
        segs = core.plan_segments(edl_4x10(), ov, target_seconds=15.0)
        self.assertEqual(segs[0]["duration"], 30.0)
        owners = [s for s in segs if s["overlays"]]
        self.assertEqual(len(owners), 1)
        self.assertEqual(owners[0]["overlays"][0]["start"],
                         19.0 - owners[0]["offset"])

    def test_segments_cover_everything_in_order(self):
        segs = core.plan_segments(edl_n(6), target_seconds=25.0)
        self.assertEqual(segs[0]["seg_start"], 0)
        self.assertEqual(segs[-1]["seg_end"], 6)
        for a, b in zip(segs, segs[1:]):
            self.assertEqual(a["seg_end"], b["seg_start"])
            self.assertAlmostEqual(a["offset"] + a["duration"], b["offset"])

    def test_empty_edl(self):
        self.assertEqual(core.plan_segments({"segments": []}), [])

    def test_earlier_boundaries_survive_a_late_edit(self):
        # The stability guarantee: editing a late segment must not move any
        # earlier render-segment boundary (so its cache survives).
        base = edl_n(9)
        before = core.plan_segments(base, target_seconds=25.0)
        edited = edl_n(9)
        edited["segments"][7]["end"] -= 3.0  # trim a late segment
        after = core.plan_segments(edited, target_seconds=25.0)
        # every render-segment that ends before the edited index is identical
        early_before = [(s["seg_start"], s["seg_end"]) for s in before
                        if s["seg_end"] <= 7]
        early_after = [(s["seg_start"], s["seg_end"]) for s in after
                       if s["seg_end"] <= 7]
        self.assertEqual(early_before, early_after)
        self.assertTrue(early_before)  # there is at least one such segment


class TestSegmentIdentity(unittest.TestCase):
    def seg(self, edl, a, b):
        return {"seg_start": a, "seg_end": b, "offset": 0.0,
                "duration": 0.0, "overlays": []}

    def test_id_is_position_independent(self):
        # Same slice content at two different EDL positions -> same id.
        e1 = edl_n(4)
        e2 = edl_n(6)  # segments 2..4 of e2 equal segments 0..2 of a shifted...
        # Build an explicit match: slice (0,2) of e1 vs a slice of e2 whose
        # (source,start,end) tuples are identical.
        s1 = self.seg(e1, 0, 2)
        e3 = {"source": "raw/a.mp4", "fade_ms": 30,
              "segments": [{"source": "x", "start": 5.0, "end": 6.0}]
              + e1["segments"][0:2]}
        s3 = self.seg(e3, 1, 3)  # same two segments as s1, later position
        self.assertEqual(core.segment_id(e1, s1), core.segment_id(e3, s3))

    def test_id_changes_with_content(self):
        e = edl_n(4)
        s = self.seg(e, 0, 2)
        e2 = edl_n(4)
        e2["segments"][1]["end"] += 1.0
        self.assertNotEqual(core.segment_id(e, s),
                            core.segment_id(e2, self.seg(e2, 0, 2)))

    def test_id_distinguishes_overlays_on_identical_slices(self):
        # Two content-identical slices (same source spans) that carry DIFFERENT
        # overlays must get DIFFERENT ids, so neither is deduped onto the other's
        # persisted .ts and each overlay configuration is encoded on its own.
        e = edl_n(4)
        plain = {"seg_start": 0, "seg_end": 2, "offset": 0.0, "duration": 20.0,
                 "overlays": []}
        with_ov = {"seg_start": 0, "seg_end": 2, "offset": 0.0, "duration": 20.0,
                   "overlays": [{"id": "b1", "start": 1.0, "dur": 0.5,
                                 "image": True, "path": "g/b1.png"}]}
        other_ov = {"seg_start": 0, "seg_end": 2, "offset": 0.0, "duration": 20.0,
                    "overlays": [{"id": "b2", "start": 1.0, "dur": 0.5,
                                  "image": True, "path": "g/b2.png"}]}
        self.assertNotEqual(core.segment_id(e, plain), core.segment_id(e, with_ov))
        self.assertNotEqual(core.segment_id(e, with_ov),
                            core.segment_id(e, other_ov))
        # Identical slice + identical overlay layout -> same id (dedup stands).
        same = dict(with_ov, offset=500.0)  # different position, same content
        self.assertEqual(core.segment_id(e, with_ov), core.segment_id(e, same))
        # The overlay FILE digest is not part of the id: re-rendering the graphic
        # keeps the filename stable (input_hash catches the change instead).
        redrawn = {"seg_start": 0, "seg_end": 2, "offset": 0.0, "duration": 20.0,
                   "overlays": [{"id": "b1", "start": 1.0, "dur": 0.5,
                                 "image": True, "path": "g/b1-v2.png"}]}
        self.assertEqual(core.segment_id(e, with_ov), core.segment_id(e, redrawn))

    def test_input_hash_reflects_overlay_file_and_render_key(self):
        e = edl_n(4)
        seg = {"seg_start": 0, "seg_end": 2, "offset": 0.0, "duration": 20.0,
               "overlays": [{"id": "b1", "start": 1.0, "dur": 0.5,
                             "image": True, "path": "g/b1.png"}]}
        rk = {"encoder": "libx264", "ffmpeg": "8.1", "dims": [1920, 1080],
              "encode": ["-c:v", "libx264", "-crf", "18"]}
        sd = {"raw/a.mp4": "size:1:mtime:1"}
        base = core.segment_input_hash(e, seg, rk, sd, {"b1": "digestA"})
        # A re-rendered overlay file (new digest) dirties the segment.
        moved = core.segment_input_hash(e, seg, rk, sd, {"b1": "digestB"})
        self.assertNotEqual(base, moved)
        # An encoder/render-key change dirties the segment.
        rk2 = dict(rk, encoder="h264_videotoolbox")
        self.assertNotEqual(
            base, core.segment_input_hash(e, seg, rk2, sd, {"b1": "digestA"}))
        # A source content change dirties the segment.
        self.assertNotEqual(
            base, core.segment_input_hash(
                e, seg, rk, {"raw/a.mp4": "size:2:mtime:9"}, {"b1": "digestA"}))
        # Same inputs -> stable hash.
        self.assertEqual(
            base, core.segment_input_hash(e, seg, rk, sd, {"b1": "digestA"}))

    def test_input_hash_ignores_timeline_offset(self):
        # The same segment content at a different output offset hashes the same
        # (offset is derived, never hashed) as long as overlay-local starts and
        # slice content are unchanged.
        e = edl_n(4)
        rk = {"encoder": "libx264", "ffmpeg": "8.1", "dims": [10, 10],
              "encode": []}
        sd = {"raw/a.mp4": "d"}
        a = {"seg_start": 0, "seg_end": 2, "offset": 0.0, "duration": 20.0,
             "overlays": []}
        b = {"seg_start": 0, "seg_end": 2, "offset": 999.0, "duration": 20.0,
             "overlays": []}
        self.assertEqual(core.segment_input_hash(e, a, rk, sd, {}),
                         core.segment_input_hash(e, b, rk, sd, {}))


class TestManifestRoundTrip(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "manifest.json"
            m = {"render_key": {"encoder": "libx264"},
                 "segments": [{"id": "seg-abc", "input_hash": "sha256:x"}]}
            core.save_manifest(p, m)
            self.assertEqual(core.load_manifest(p), m)

    def test_load_missing_or_bad_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(core.load_manifest(Path(tmp) / "nope.json"))
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json")
            self.assertIsNone(core.load_manifest(bad))


class TestContentDigest(unittest.TestCase):
    def test_cheap_vs_content_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.bin"
            f.write_bytes(b"hello")
            cheap = core.content_digest(f, cheap=True)
            full = core.content_digest(f, cheap=False)
            self.assertTrue(cheap.startswith("size:5:mtime:"))
            self.assertTrue(full.startswith("sha256:"))
            # content change moves the sha256 digest
            f.write_bytes(b"hello world")
            self.assertNotEqual(full, core.content_digest(f))
            self.assertEqual(core.content_digest(Path(tmp) / "gone"), "missing")


def never_probe(encoder):
    raise AssertionError(f"probe must not be called (got {encoder!r})")


class TestEncoderSelection(unittest.TestCase):
    def test_auto_prefers_videotoolbox_on_macos(self):
        avail = {"libx264", "h264_videotoolbox"}
        self.assertEqual(core.pick_encoder("auto", avail, "Darwin"),
                         "h264_videotoolbox")
        self.assertEqual(core.pick_encoder("auto", avail, "Linux"), "libx264")
        self.assertEqual(core.pick_encoder("auto", {"libx264"}, "Darwin"),
                         "libx264")

    def test_darwin_never_probes(self):
        avail = {"libx264", "h264_videotoolbox"}
        self.assertEqual(core.pick_encoder("auto", avail, "Darwin",
                                           probe=never_probe),
                         "h264_videotoolbox")
        self.assertEqual(core.pick_encoder("auto", {"libx264"}, "Darwin",
                                           probe=never_probe), "libx264")

    def test_explicit_falls_back_when_unavailable(self):
        self.assertEqual(core.pick_encoder("hevc_videotoolbox", {"libx264"},
                                           "Darwin"), "libx264")
        self.assertEqual(core.pick_encoder("libx264", {"libx264"}, "Linux"),
                         "libx264")

    def test_explicit_request_never_probes(self):
        avail = {"libx264", "h264_nvenc"}
        self.assertEqual(core.pick_encoder("h264_nvenc", avail, "Windows",
                                           probe=never_probe), "h264_nvenc")

    def test_windows_ladder_order(self):
        avail = {"libx264", "h264_nvenc", "h264_qsv", "h264_amf"}
        self.assertEqual(core.pick_encoder("auto", avail, "Windows",
                                           probe=lambda e: True),
                         "h264_nvenc")
        self.assertEqual(core.pick_encoder("auto", avail, "Windows",
                                           probe=lambda e: e != "h264_nvenc"),
                         "h264_qsv")
        self.assertEqual(core.pick_encoder(
            "auto", avail, "Windows",
            probe=lambda e: e == "h264_amf"), "h264_amf")
        self.assertEqual(core.pick_encoder("auto", avail, "Windows",
                                           probe=lambda e: False), "libx264")

    def test_linux_ladder_order(self):
        avail = {"libx264", "h264_nvenc", "h264_vaapi"}
        self.assertEqual(core.pick_encoder("auto", avail, "Linux",
                                           probe=lambda e: True),
                         "h264_nvenc")
        self.assertEqual(core.pick_encoder("auto", avail, "Linux",
                                           probe=lambda e: e == "h264_vaapi"),
                         "h264_vaapi")
        self.assertEqual(core.pick_encoder("auto", avail, "Linux",
                                           probe=lambda e: False), "libx264")

    def test_ladder_skips_unlisted_encoders_without_probing(self):
        # amf is not in this build's encoder list, so it must not be probed;
        # nvenc/qsv are listed but their probes fail -> libx264.
        avail = {"libx264", "h264_nvenc", "h264_qsv"}
        probed = []

        def probe(e):
            probed.append(e)
            return False

        self.assertEqual(core.pick_encoder("auto", avail, "Windows",
                                           probe=probe), "libx264")
        self.assertEqual(probed, ["h264_nvenc", "h264_qsv"])

    def test_unknown_system_falls_back(self):
        self.assertEqual(core.pick_encoder("auto", {"libx264"}, "Haiku",
                                           probe=never_probe), "libx264")

    def test_encode_args(self):
        x264 = core.encode_args("libx264", crf=20, height=1080)
        self.assertIn("-crf", x264)
        self.assertIn("20", x264)
        self.assertIn("yuv420p", x264)
        vt = core.encode_args("h264_videotoolbox", height=1080)
        self.assertIn("-b:v", vt)
        self.assertIn("12000k", vt)
        self.assertNotIn("-crf", vt)
        hevc = core.encode_args("hevc_videotoolbox", height=2160)
        self.assertIn("hvc1", hevc)
        self.assertIn("40000k", hevc)

    def test_encode_args_hardware_ladder_encoders(self):
        for enc in ("h264_nvenc", "h264_qsv", "h264_amf"):
            args = core.encode_args(enc, crf=20, height=1080)
            self.assertIn("-b:v", args)
            self.assertIn("12000k", args)
            self.assertNotIn("-crf", args)
            self.assertNotIn("-allow_sw", args)
            # format negotiation is left to ffmpeg for these encoders
            self.assertNotIn("-pix_fmt", args)
        vaapi = core.encode_args("h264_vaapi", height=720)
        self.assertIn("8000k", vaapi)
        self.assertNotIn("-crf", vaapi)
        self.assertNotIn("-pix_fmt", vaapi)  # hardware frames via hwupload

    def test_bitrate_ladder(self):
        self.assertEqual(core.bitrate_for(720), 8000)
        self.assertEqual(core.bitrate_for(540), 5000)
        self.assertEqual(core.bitrate_for(1440), 24000)


class TestEncoderProbe(unittest.TestCase):
    def test_probe_command_shape(self):
        cmd = core.encoder_probe_command("h264_nvenc")
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertIn("lavfi", cmd)
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "1")
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "h264_nvenc")
        self.assertEqual(cmd[-3:], ["-f", "null", "-"])
        self.assertNotIn("-init_hw_device", cmd)
        self.assertNotIn("hwupload", " ".join(cmd))

    def test_probe_command_vaapi_gets_device_and_hwupload(self):
        cmd = core.encoder_probe_command("h264_vaapi")
        self.assertIn("-init_hw_device", cmd)
        self.assertIn("vaapi=va", cmd)
        self.assertIn("-filter_hw_device", cmd)
        self.assertIn("format=nv12,hwupload", cmd)

    def test_probe_cache_short_circuits(self):
        # A cached verdict is returned without running anything (a probe of
        # this fake encoder name would otherwise fail or invoke ffmpeg).
        self.assertTrue(core.probe_encoder("fake_enc", cache={"fake_enc": True}))
        self.assertFalse(core.probe_encoder("fake_enc", cache={"fake_enc": False}))

    def test_probe_records_result_once(self):
        cache = {}
        first = core.probe_encoder("this_encoder_does_not_exist", cache=cache)
        self.assertFalse(first)
        self.assertEqual(cache, {"this_encoder_does_not_exist": False})
        # poison the cache: a second call must not re-run the probe
        cache["this_encoder_does_not_exist"] = True
        self.assertTrue(core.probe_encoder("this_encoder_does_not_exist",
                                           cache=cache))

    @unittest.skipUnless(FFMPEG, "ffmpeg/ffprobe not installed")
    def test_real_probe_passes_for_libx264(self):
        self.assertTrue(core.probe_encoder("libx264", cache={}))


class TestVaapiWiring(unittest.TestCase):
    """The vaapi encode path: device init flags on the command and the
    hwupload tail on the filtergraph, absent for every other encoder."""

    def test_init_flags(self):
        self.assertEqual(core.encoder_init_flags("h264_vaapi"),
                         ["-init_hw_device", "vaapi=va",
                          "-filter_hw_device", "va"])
        for enc in ("libx264", "h264_videotoolbox", "h264_nvenc", None):
            self.assertEqual(core.encoder_init_flags(enc), [])

    def test_build_command_vaapi(self):
        edl = edl_4x10()
        cmd, _ = core.build_command(edl, Path("/proj"), "out.mp4", 1080,
                                    encoder="h264_vaapi")
        self.assertEqual(cmd[1:6], ["-y", "-init_hw_device", "vaapi=va",
                                    "-filter_hw_device", "va"])
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("format=nv12,hwupload[outv]", fc)
        self.assertNotIn("format=yuv420p[outv]", fc)

    def test_build_command_software_unchanged(self):
        edl = edl_4x10()
        cmd, _ = core.build_command(edl, Path("/proj"), "out.mp4", 1080,
                                    encoder="libx264")
        self.assertNotIn("-init_hw_device", cmd)
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertNotIn("hwupload", fc)
        # default (no encoder given) is also unchanged
        cmd2, _ = core.build_command(edl, Path("/proj"), "out.mp4", 1080)
        self.assertNotIn("-init_hw_device", cmd2)

    def test_filter_complex_hwupload_with_overlays(self):
        edl = edl_4x10()
        ovs = [{"index": 1, "start": 5.0, "dur": 2.0, "image": True}]
        fc = core.build_filter_complex(edl, {"raw/a.mp4": 0}, 1080, ovs,
                                       (100, 50), hwupload=True)
        self.assertIn("format=nv12,hwupload[outv]", fc)
        self.assertNotIn("format=yuv420p[outv]", fc)


class TestDiskPreflight(unittest.TestCase):
    def test_estimate_scales_with_duration_and_height(self):
        small = core.estimate_output_bytes(60, 720)
        self.assertGreater(core.estimate_output_bytes(120, 720), small)
        self.assertGreater(core.estimate_output_bytes(60, 2160), small)
        # 60s of 1080p at ~12.2 Mbps is on the order of 90 MB
        self.assertAlmostEqual(core.estimate_output_bytes(60, 1080) / 1e6,
                               91.4, delta=5)

    def test_check_disk(self):
        ok, free = core.check_disk(Path("."), 1)
        self.assertTrue(ok)
        self.assertGreater(free, 0)
        ok, _ = core.check_disk(Path("."), 10 ** 18)
        self.assertFalse(ok)


class TestParseProgress(unittest.TestCase):
    def test_out_time_us_and_state(self):
        info = core.parse_progress("frame=100\nout_time_us=2500000\n"
                                   "progress=continue\n")
        self.assertEqual(info["seconds"], 2.5)
        self.assertEqual(info["state"], "continue")

    def test_out_time_fallback_and_garbage(self):
        info = core.parse_progress("out_time=00:00:01.500000\nprogress=end\n")
        self.assertEqual(info["seconds"], 1.5)
        self.assertEqual(info["state"], "end")
        self.assertEqual(core.parse_progress("not progress output"), {})
        self.assertEqual(core.parse_progress("out_time_us=N/A"), {})


LOUDNORM_STDERR = """\
[Parsed_loudnorm_0 @ 0x600002bb0000]
{
\t"input_i" : "-27.61",
\t"input_tp" : "-14.46",
\t"input_lra" : "0.00",
\t"input_thresh" : "-37.61",
\t"output_i" : "-14.01",
\t"output_tp" : "-1.52",
\t"output_lra" : "0.00",
\t"output_thresh" : "-24.03",
\t"normalization_type" : "linear",
\t"target_offset" : "0.01"
}
"""


class TestLoudnormHelpers(unittest.TestCase):
    def test_parse_stats_block(self):
        stats = render_final.parse_loudnorm_json(
            "noise before\n" + LOUDNORM_STDERR)
        self.assertEqual(stats["input_i"], -27.61)
        self.assertEqual(stats["output_i"], -14.01)
        self.assertEqual(stats["target_offset"], 0.01)
        self.assertEqual(stats["normalization_type"], "linear")

    def test_parse_inf_and_garbage(self):
        stats = render_final.parse_loudnorm_json(
            '{\n"input_i" : "-inf",\n"input_tp" : "-inf"\n}')
        self.assertEqual(stats["input_i"], float("-inf"))
        self.assertIsNone(render_final.parse_loudnorm_json("no json here"))
        self.assertIsNone(render_final.parse_loudnorm_json("{ broken"))

    def test_spec_measurement_form(self):
        spec = render_final.loudnorm_spec(-14.0)
        self.assertIn("loudnorm=I=-14:TP=-1.5:LRA=11", spec)
        self.assertIn("print_format=json", spec)
        self.assertNotIn("measured_I", spec)

    def test_spec_application_form(self):
        measured = {"input_i": -27.61, "input_tp": -14.46, "input_lra": 0.0,
                    "input_thresh": -37.61, "target_offset": 0.01}
        spec = render_final.loudnorm_spec(-14.0, measured)
        self.assertIn("measured_I=-27.61", spec)
        self.assertIn("measured_TP=-14.46", spec)
        self.assertIn("measured_LRA=0.00", spec)
        self.assertIn("measured_thresh=-37.61", spec)
        self.assertIn("offset=0.01", spec)
        self.assertIn("linear=true", spec)


def run_cli(args):
    return subprocess.run([sys.executable, str(SCRIPTS / "render_final.py"),
                           *args], capture_output=True, text=True)


class TestCli(unittest.TestCase):
    def test_missing_edl_exits_2(self):
        r = run_cli(["/nonexistent/edl.json", "-o", "/tmp/f.mp4"])
        self.assertEqual(r.returncode, 2)

    def test_empty_segments_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            edl = Path(tmp) / "edl.json"
            edl.write_text('{"source":"x","segments":[]}')
            r = run_cli([str(edl), "-o", str(Path(tmp) / "f.mp4")])
            self.assertEqual(r.returncode, 2)

    def test_beats_without_graphics_dir_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            edl = Path(tmp) / "edl.json"
            edl.write_text('{"source":"x","segments":[{"source":"x",'
                           '"start":0,"end":1}]}')
            r = run_cli([str(edl), "-o", str(Path(tmp) / "f.mp4"),
                         "--beats", "beats.md"])
            self.assertEqual(r.returncode, 2)

    def test_non_ascii_edl_reads_under_non_utf8_locale(self):
        # A UTF-8 edl.json whose quote text is Czech must parse under an
        # ASCII locale codec (the Windows cp1252 failure class, simulated
        # with LC_ALL=C and UTF-8 mode off) and reach the normal
        # empty-segments error, never a UnicodeDecodeError.
        with tempfile.TemporaryDirectory() as tmp:
            edl = Path(tmp) / "edl.json"
            edl.write_text('{"source":"x","quote":"Čau, uh, světe",'
                           '"segments":[]}', encoding="utf-8")
            env = dict(os.environ, LC_ALL="C", LANG="C",
                       PYTHONCOERCECLOCALE="0")
            r = subprocess.run(
                [sys.executable, "-X", "utf8=0",
                 str(SCRIPTS / "render_final.py"),
                 str(edl), "-o", str(Path(tmp) / "f.mp4")],
                capture_output=True, text=True, env=env)
            self.assertNotIn("UnicodeDecodeError", r.stderr)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("no segments", r.stderr)


@unittest.skipUnless(FFMPEG, "ffmpeg/ffprobe not installed")
class TestEndToEnd(unittest.TestCase):
    """Synthesized-fixture render: a 4s test source, a two-segment EDL, one
    PNG overlay from a beat table, the incremental segment cache (fresh render
    then cache hit), boundary frames."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        proj = Path(cls.tmp.name)
        (proj / "raw").mkdir()
        (proj / "cut").mkdir()
        (proj / "graphics").mkdir()
        # Source: 4s color bars + tone, 320x180, CFR 30. Explicit -t caps on
        # both synthetic lavfi inputs plus -shortest (the runaway lesson).
        r = subprocess.run(
            ["ffmpeg", "-y",
             "-f", "lavfi", "-t", "4", "-i",
             "testsrc2=size=320x180:rate=30",
             "-f", "lavfi", "-t", "4", "-i", "sine=frequency=440:sample_rate=48000",
             "-t", "4", "-shortest",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
             "-pix_fmt", "yuv420p", "-c:a", "aac",
             str(proj / "raw" / "a.mp4")],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        # Overlay: a single red PNG frame for beat b1.
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-t", "1", "-i",
             "color=c=red@0.8:size=120x60", "-frames:v", "1",
             str(proj / "graphics" / "b1.png")],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        edl = {"source": "raw/a.mp4", "fade_ms": 30, "pad_ms": 60,
               "segments": [
                   {"source": "raw/a.mp4", "start": 0.5, "end": 1.5},
                   {"source": "raw/a.mp4", "start": 2.0, "end": 3.0}]}
        (proj / "cut" / "edl.json").write_text(json.dumps(edl))
        (proj / "beats.md").write_text(
            "| id | start | dur | type |\n|---|---|---|---|\n"
            "| b1 | 0.3 | 0.5 | overlay |\n"
            "| b9 | 1.6 | 0.2 | overlay |\n")
        cls.proj = proj

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def render(self, subdir, *extra):
        """Run render_final into its own output dir (so each test's segment
        cache is isolated) and return the parsed summary + the run result."""
        outdir = self.proj / "renders" / subdir
        out = outdir / "final.mp4"
        r = run_cli([str(self.proj / "cut" / "edl.json"), "-o", str(out),
                     "--codec", "libx264", "--crf", "30", *extra])
        return r, out, outdir

    def test_incremental_composited_render_then_cache_hit(self):
        args = ["--beats", str(self.proj / "beats.md"),
                "--graphics-dir", str(self.proj / "graphics"),
                "--parallel", "2", "--segment-target-seconds", "0.5"]
        # First render: a two-segment timeline, both encoded fresh.
        r, out, outdir = self.render("comp", *args,
                                     "--boundary-frames", str(self.proj / "bf"))
        self.assertEqual(r.returncode, 0, r.stderr)
        s = json.loads(r.stdout)
        self.assertEqual(s["segments"], 2)
        self.assertEqual(s["render_segments"], 2)
        self.assertEqual(s["segments_rendered"], 2)
        self.assertEqual(s["segments_cached"], 0)
        self.assertEqual(s["overlays"], 1)
        self.assertEqual(s["overlays_missing"], ["b9"])
        self.assertTrue(out.is_file())
        self.assertAlmostEqual(s["actual_duration_seconds"], 2.0, delta=0.5)
        self.assertEqual(s["boundary_frames"], 2)
        self.assertEqual(s["loudnorm"]["target"], -14.0)
        self.assertTrue(s["loudnorm"]["applied"])
        self.assertIn("render_final:", r.stderr)
        # Segments persisted; the manifest is written; no stray temps.
        segs = sorted((outdir / "segments").glob("seg-*.ts"))
        self.assertEqual(len(segs), 2)
        self.assertTrue((outdir / "segments" / "manifest.json").is_file())
        self.assertEqual(list(outdir.glob("*.ts")), [])       # no temp .ts
        self.assertEqual(list(outdir.glob(".*")), [])          # no dotfile temps

        # Second render, nothing changed: every segment is served from cache.
        r2, out2, _ = self.render("comp", *args)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        s2 = json.loads(r2.stdout)
        self.assertEqual(s2["segments_rendered"], 0)
        self.assertEqual(s2["segments_cached"], 2)
        self.assertAlmostEqual(s2["actual_duration_seconds"], 2.0, delta=0.5)
        self.assertIn("all segments cached", r2.stderr)

    def test_single_segment_plain_render(self):
        # Default 600s target over a 2s timeline: one render-segment, no cache
        # hit, no audio-less handling; plain (no overlays), loudnorm opted out.
        r, out, _ = self.render("plain", "--parallel", "1", "--no-loudnorm")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = json.loads(r.stdout)
        self.assertEqual(s["render_segments"], 1)
        self.assertEqual(s["segments_rendered"], 1)
        self.assertEqual(s["overlays"], 0)
        self.assertIsNone(s["loudnorm"])  # explicit opt-out
        self.assertTrue(out.is_file())

    def test_no_cache_forces_full_rerender(self):
        args = ["--parallel", "2", "--segment-target-seconds", "0.5",
                "--no-loudnorm"]
        r, _, _ = self.render("nocache", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["segments_rendered"], 2)
        # Re-render with --no-cache: nothing served from cache.
        r2, _, _ = self.render("nocache", *args, "--no-cache")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        s2 = json.loads(r2.stdout)
        self.assertEqual(s2["segments_rendered"], 2)
        self.assertEqual(s2["segments_cached"], 0)


class TestMultiSourceCommand(unittest.TestCase):
    """Pure command/filtergraph construction for the mixed-source cases that
    broke concat: different frame sizes, and a source with no audio."""

    def edl_two_sources(self):
        return {
            "source": "raw/cam.mp4", "fade_ms": 30,
            "segments": [
                {"source": "raw/cam.mp4", "start": 0.0, "end": 2.0},
                {"source": "raw/screen.mp4", "start": 0.0, "end": 3.0},
            ],
        }

    def test_target_normalizes_every_segment(self):
        edl = self.edl_two_sources()
        idx = {"raw/cam.mp4": 0, "raw/screen.mp4": 1}
        fc = core.build_filter_complex(edl, idx, 720, target=(1280, 720))
        self.assertEqual(
            fc.count("scale=1280:720:force_original_aspect_ratio=decrease,"
                     "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1"), 2)
        self.assertNotIn("scale=-2:720", fc)

    def test_audioless_source_gets_synthesized_silence(self):
        edl = self.edl_two_sources()
        audio_map = {"raw/cam.mp4": True, "raw/screen.mp4": False}
        cmd, _ = core.build_command(edl, Path("/proj"), "out.mp4", 1080,
                                    target=(1920, 1080), audio_map=audio_map,
                                    encoder="libx264")
        joined = " ".join(cmd)
        self.assertEqual(joined.count("-f lavfi"), 1)
        self.assertIn("anullsrc=channel_layout=stereo:sample_rate=48000",
                      joined)
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertNotIn("[1:a]", fc)          # audio-less source not read
        self.assertIn("[2:a]atrim", fc)        # silence input drives its audio
        self.assertIn("aresample=48000,aformat=channel_layouts=stereo", fc)

    def test_audio_map_none_keeps_real_streams(self):
        edl = self.edl_two_sources()
        cmd, _ = core.build_command(edl, Path("/proj"), "out.mp4", 1080,
                                    encoder="libx264")
        self.assertNotIn("anullsrc", " ".join(cmd))
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("[1:a]atrim", fc)


@unittest.skipUnless(FFMPEG, "ffmpeg/ffprobe not installed")
class TestMixedSourcesEndToEnd(unittest.TestCase):
    """Real renders of the cases that raised 'Input link parameters do not
    match' and 'Stream specifier :a matches no streams': a 16:9 cam with 44.1k
    audio spliced with a 4:3 screen recording that has no audio at all. Both
    the frame-size normalization (video segment pass) and the silence synthesis
    (whole-program audio pass, audio-less source referenced twice) are
    exercised end to end through the segment/concat/mux path."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        proj = Path(cls.tmp.name)
        (proj / "raw").mkdir()
        (proj / "cut").mkdir()
        # cam: 160x90 (16:9), 44.1k audio
        r = subprocess.run(
            ["ffmpeg", "-y",
             "-f", "lavfi", "-t", "6", "-i", "testsrc2=size=160x90:rate=30",
             "-f", "lavfi", "-t", "6",
             "-i", "sine=frequency=440:sample_rate=44100",
             "-t", "6", "-shortest",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
             "-pix_fmt", "yuv420p", "-c:a", "aac",
             str(proj / "raw" / "cam.mp4")],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        # screen: 160x120 (4:3, different aspect), NO audio stream
        r = subprocess.run(
            ["ffmpeg", "-y",
             "-f", "lavfi", "-t", "6", "-i", "testsrc2=size=160x120:rate=30",
             "-t", "6",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
             "-pix_fmt", "yuv420p",
             str(proj / "raw" / "screen.mp4")],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        # cam, then two screen segments (audio-less source, referenced twice)
        edl = {"source": "raw/cam.mp4", "fade_ms": 30, "pad_ms": 60,
               "segments": [
                   {"source": "raw/cam.mp4", "start": 0.5, "end": 2.0},
                   {"source": "raw/screen.mp4", "start": 0.5, "end": 2.0},
                   {"source": "raw/screen.mp4", "start": 3.0, "end": 4.5}]}
        (proj / "cut" / "edl.json").write_text(json.dumps(edl))
        cls.proj = proj

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_mixed_dimensions_and_audioless_render(self):
        out = self.proj / "renders" / "mixed.mp4"
        r = run_cli([str(self.proj / "cut" / "edl.json"), "-o", str(out),
                     "--codec", "libx264", "--crf", "30", "--parallel", "2",
                     "--no-loudnorm", "--height", "120"])
        self.assertEqual(r.returncode, 0, r.stderr)
        summary = json.loads(r.stdout)
        self.assertEqual(summary["segments"], 3)
        self.assertTrue(out.is_file())
        self.assertAlmostEqual(summary["actual_duration_seconds"], 4.5,
                               delta=0.5)
        # every frame is the one normalized target size
        dims = core.probe_dims(self.proj / "raw" / "cam.mp4")
        expected_w = core.even(dims[0] * 120 / dims[1])
        self.assertEqual(core.probe_dims(out), (expected_w, 120))


@unittest.skipUnless(FFMPEG, "ffmpeg/ffprobe not installed")
class TestLoudnormEndToEnd(unittest.TestCase):
    """A real two-pass loudnorm run: a quiet 7s test tone rendered with the
    default -14 LUFS target must measure within about 1 LU of it (measured
    independently with a fresh loudnorm analysis pass)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        proj = Path(cls.tmp.name)
        (proj / "raw").mkdir()
        (proj / "cut").mkdir()
        # 8s tone at 0.1 amplitude (~-26 LUFS): far from target, headroom
        # for the linear gain to land on -14 without hitting the -1.5 TP cap.
        r = subprocess.run(
            ["ffmpeg", "-y",
             "-f", "lavfi", "-t", "8", "-i", "testsrc2=size=320x180:rate=30",
             "-f", "lavfi", "-t", "8", "-i",
             "sine=frequency=440:sample_rate=48000",
             "-t", "8", "-shortest", "-af", "volume=0.1",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
             "-pix_fmt", "yuv420p", "-c:a", "aac",
             str(proj / "raw" / "tone.mp4")],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        edl = {"source": "raw/tone.mp4", "fade_ms": 30, "pad_ms": 60,
               "segments": [
                   {"source": "raw/tone.mp4", "start": 0.5, "end": 7.5}]}
        (proj / "cut" / "edl.json").write_text(json.dumps(edl))
        cls.proj = proj

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def measure(self, path):
        """Independent loudness measurement of a finished file."""
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
             "-af", "loudnorm=print_format=json", "-f", "null", "-"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stats = render_final.parse_loudnorm_json(proc.stderr)
        self.assertIsNotNone(stats)
        return stats

    def test_output_lands_within_one_lu_of_target(self):
        out = self.proj / "renders" / "final.mp4"
        r = run_cli([str(self.proj / "cut" / "edl.json"), "-o", str(out),
                     "--codec", "libx264", "--crf", "30", "--parallel", "1"])
        self.assertEqual(r.returncode, 0, r.stderr)
        summary = json.loads(r.stdout)
        ln = summary["loudnorm"]
        self.assertEqual(ln["target"], -14.0)
        self.assertTrue(ln["applied"])
        self.assertLess(ln["input_i"], -20.0)  # the tone really was quiet
        # the script's own report is close to target...
        self.assertAlmostEqual(ln["output_i"], -14.0, delta=1.0)
        # ...and so is an independent measurement of the file on disk
        measured = self.measure(out)
        self.assertAlmostEqual(measured["input_i"], -14.0, delta=1.0)
        # duration survived the audio re-encode
        self.assertAlmostEqual(summary["actual_duration_seconds"], 7.0,
                               delta=0.5)

    def test_custom_target(self):
        out = self.proj / "renders" / "quiet.mp4"
        r = run_cli([str(self.proj / "cut" / "edl.json"), "-o", str(out),
                     "--codec", "libx264", "--crf", "30", "--parallel", "1",
                     "--loudness-target", "-19"])
        self.assertEqual(r.returncode, 0, r.stderr)
        summary = json.loads(r.stdout)
        self.assertEqual(summary["loudnorm"]["target"], -19.0)
        measured = self.measure(out)
        self.assertAlmostEqual(measured["input_i"], -19.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()
