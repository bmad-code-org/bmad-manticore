#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for render_preview.py: the deterministic, ffmpeg-free parts: the
timeline math (segment durations, internal boundary times) and the
filter_complex / command construction from a canned EDL, plain and with the
composited-overlay mode (shared with render_final.py via composite_core.py).

The actual render, ffprobe, and boundary-frame extraction are exercised by
the synthesized-fixture integration test in test-render_final.py (same
compositing core) and by running the script against a real source."""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "render_preview.py"

import composite_core as core  # noqa: E402

spec = importlib.util.spec_from_file_location("render_preview", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def canned_edl():
    return {
        "source": "raw/camera-a.mp4",
        "fade_ms": 30,
        "pad_ms": 60,
        "segments": [
            {"source": "raw/camera-a.mp4", "start": 1.28, "end": 9.76},
            {"source": "raw/camera-a.mp4", "start": 14.0, "end": 14.8},
            {"source": "raw/camera-a.mp4", "start": 20.72, "end": 23.28},
        ],
    }


class TestTimelineMath(unittest.TestCase):
    def test_segment_durations(self):
        durs = mod.segment_durations(canned_edl())
        self.assertEqual(len(durs), 3)
        self.assertAlmostEqual(durs[0], 8.48)
        self.assertAlmostEqual(durs[1], 0.8)
        self.assertAlmostEqual(durs[2], 2.56)

    def test_boundary_times_are_internal_cumulative(self):
        # Two internal cuts for three segments: at 8.48 and 9.28.
        times = mod.boundary_times(canned_edl())
        self.assertEqual(len(times), 2)
        self.assertAlmostEqual(times[0], 8.48)
        self.assertAlmostEqual(times[1], 9.28)

    def test_expected_duration_is_sum(self):
        self.assertAlmostEqual(sum(mod.segment_durations(canned_edl())), 11.84)


class TestFilterComplex(unittest.TestCase):
    def build(self):
        edl = canned_edl()
        idx = {"raw/camera-a.mp4": 0}
        return mod.build_filter_complex(edl, idx, 720)

    def test_trim_endpoints_and_scale(self):
        fc = self.build()
        self.assertIn("trim=start=1.28:end=9.76", fc)
        self.assertIn("atrim=start=1.28:end=9.76", fc)
        self.assertIn("scale=-2:720", fc)

    def test_fades_at_every_boundary(self):
        fc = self.build()
        # in-fade at the head of each segment...
        self.assertEqual(fc.count("afade=t=in:st=0:d=0.03"), 3)
        # ...and an out-fade timed to (segment duration - fade) on each.
        self.assertIn("afade=t=out:st=8.45:d=0.03", fc)   # 8.48 - 0.03
        self.assertIn("afade=t=out:st=0.77:d=0.03", fc)   # 0.80 - 0.03

    def test_concat_over_all_segments(self):
        fc = self.build()
        self.assertIn("concat=n=3:v=1:a=1[outv][outa]", fc)

    def test_short_segment_does_not_overlap_fades(self):
        edl = canned_edl()
        edl["segments"] = [{"source": "raw/camera-a.mp4", "start": 0.0, "end": 0.04}]
        fc = mod.build_filter_complex(edl, {"raw/camera-a.mp4": 0}, 720)
        # 40ms clip, 30ms fade -> fade clamped to dur/2 = 20ms.
        self.assertIn("afade=t=in:st=0:d=0.02", fc)


class TestCompositedMode(unittest.TestCase):
    def overlays(self):
        return [
            {"index": 1, "start": 1.0, "dur": 2.5, "image": False,
             "path": "graphics/b1.mov", "id": "b1"},
            {"index": 2, "start": 9.0, "dur": 1.5, "image": True,
             "path": "graphics/b2.png", "id": "b2"},
        ]

    def test_overlay_chain_and_windows(self):
        edl = canned_edl()
        fc = mod.build_filter_complex(edl, {"raw/camera-a.mp4": 0}, 720,
                                      overlays=self.overlays(),
                                      overlay_size=(1280, 720))
        # concat feeds the overlay chain, which ends in [outv]
        self.assertIn("concat=n=3:v=1:a=1[basev][outa]", fc)
        self.assertIn("[1:v]format=rgba,scale=1280:720,"
                      "setpts=PTS-STARTPTS+1/TB[ov0]", fc)
        self.assertIn("overlay=eof_action=pass:enable='between(t,1,3.5)'", fc)
        self.assertIn("overlay=eof_action=pass:enable='between(t,9,10.5)'", fc)
        self.assertIn("[base2]format=yuv420p[outv]", fc)

    def test_no_overlays_keeps_plain_labels(self):
        fc = mod.build_filter_complex(canned_edl(), {"raw/camera-a.mp4": 0}, 720)
        self.assertIn("concat=n=3:v=1:a=1[outv][outa]", fc)
        self.assertNotIn("overlay=", fc)
        self.assertNotIn("format=yuv420p", fc)

    def test_command_caps_looped_image_inputs(self):
        cmd, _ = mod.build_command(canned_edl(), Path("/proj"),
                                   Path("/out/p.mp4"), 720,
                                   overlays=self.overlays(),
                                   overlay_size=(1280, 720))
        joined = " ".join(cmd)
        # video overlay input capped to its beat dur
        self.assertIn("-t 2.5 -i graphics/b1.mov", joined)
        # image overlay looped AND explicitly duration-capped
        self.assertIn("-loop 1 -t 1.5 -i graphics/b2.png", joined)
        self.assertEqual(cmd.count("-i"), 3)

    def test_beats_without_graphics_dir_is_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            edl = Path(tmp) / "edl.json"
            edl.write_text('{"source":"x","segments":[{"source":"x",'
                           '"start":0,"end":1}]}')
            r = run([str(edl), "-o", str(Path(tmp) / "p.mp4"),
                     "--beats", str(Path(tmp) / "beats.md")])
            self.assertEqual(r.returncode, 2)


class TestCommand(unittest.TestCase):
    def test_multi_source_inputs_and_encode_flags(self):
        edl = canned_edl()
        edl["segments"][2]["source"] = "raw/b-roll.mp4"
        cmd, index = mod.build_command(edl, Path("/proj"), Path("/out/p.mp4"), 720)
        self.assertEqual(index, {"raw/camera-a.mp4": 0, "raw/b-roll.mp4": 1})
        self.assertEqual(cmd.count("-i"), 2)
        self.assertIn("libx264", cmd)
        self.assertIn("-crf", cmd)
        self.assertIn("28", cmd)
        self.assertIn("veryfast", cmd)
        self.assertIn("aac", cmd)
        self.assertEqual(cmd[-1], "/out/p.mp4")


def edl_two_sources():
    # A 16:9 cam and a 4:3 screencast, both used in the timeline.
    return {
        "source": "raw/cam.mp4", "fade_ms": 30,
        "segments": [
            {"source": "raw/cam.mp4", "start": 0.0, "end": 2.0},
            {"source": "raw/screen.mp4", "start": 0.0, "end": 3.0},
            {"source": "raw/screen.mp4", "start": 5.0, "end": 6.0},
        ],
    }


class TestMultiSourceFrame(unittest.TestCase):
    """Mixed-size sources must be normalized to one target frame, or the
    concat filter rejects the mismatched inputs (cam 16:9 + screencast 4:3)."""

    def test_target_pads_every_segment_to_one_frame(self):
        edl = edl_two_sources()
        idx = {"raw/cam.mp4": 0, "raw/screen.mp4": 1}
        fc = mod.build_filter_complex(edl, idx, 720, target=(1280, 720))
        # all three segment chains normalize to the SAME frame
        self.assertEqual(
            fc.count("scale=1280:720:force_original_aspect_ratio=decrease,"
                     "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1"), 3)
        # the old height-only scale (which left widths mismatched) is gone
        self.assertNotIn("scale=-2:720", fc)

    def test_single_source_keeps_plain_scale(self):
        # single-source behavior is unchanged: plain scale=-2:height, no pad
        fc = mod.build_filter_complex(canned_edl(), {"raw/camera-a.mp4": 0}, 720)
        self.assertIn("scale=-2:720", fc)
        self.assertNotIn("force_original_aspect_ratio", fc)


class TestAudioNormalization(unittest.TestCase):
    """Every audio chain is resampled to 48k stereo so sources with different
    sample rates or channel layouts concat cleanly; audio-less sources draw
    synthesized silence instead of a missing :a stream."""

    def test_every_chain_ends_in_resample_and_layout(self):
        fc = mod.build_filter_complex(canned_edl(), {"raw/camera-a.mp4": 0}, 720)
        self.assertEqual(
            fc.count("aresample=48000,aformat=channel_layouts=stereo"), 3)

    def test_audioless_source_draws_shared_silence(self):
        edl = edl_two_sources()
        audio_map = {"raw/cam.mp4": True, "raw/screen.mp4": False}
        cmd, _ = mod.build_command(edl, Path("/proj"), Path("/out/p.mp4"), 720,
                                   target=(1280, 720), audio_map=audio_map)
        joined = " ".join(cmd)
        # exactly one synthesized silent input, added after the two real sources
        self.assertEqual(joined.count("-f lavfi"), 1)
        self.assertIn("anullsrc=channel_layout=stereo:sample_rate=48000", joined)
        fc = cmd[cmd.index("-filter_complex") + 1]
        # cam (input 0) keeps its real audio
        self.assertIn("[0:a]atrim=start=0:end=2", fc)
        # screen (input 1) is audio-less: never referenced for audio; both of
        # its segments draw from the shared anullsrc at input index 2
        self.assertNotIn("[1:a]", fc)
        self.assertEqual(fc.count("[2:a]atrim"), 2)

    def test_no_silence_input_when_every_source_has_audio(self):
        edl = edl_two_sources()
        audio_map = {"raw/cam.mp4": True, "raw/screen.mp4": True}
        cmd, _ = mod.build_command(edl, Path("/proj"), Path("/out/p.mp4"), 720,
                                   target=(1280, 720), audio_map=audio_map)
        self.assertNotIn("anullsrc", " ".join(cmd))
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("[1:a]atrim", fc)


def run(args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True)


class TestCli(unittest.TestCase):
    def test_missing_edl_exits_2(self):
        r = run(["/nonexistent/edl.json", "-o", "/tmp/p.mp4"])
        self.assertEqual(r.returncode, 2)

    def test_empty_segments_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            edl = Path(tmp) / "edl.json"
            edl.write_text('{"source":"x","segments":[]}')
            r = run([str(edl), "-o", str(Path(tmp) / "p.mp4")])
            self.assertEqual(r.returncode, 2)


@unittest.skipUnless(FFMPEG, "ffmpeg/ffprobe not installed")
class TestMixedSourcePreviewEndToEnd(unittest.TestCase):
    """The maintainer's primary render-first case: a preview composited from a
    16:9 cam (44.1k audio) and a 4:3 screen recording with NO audio. Mixed
    frame sizes and the missing audio stream both used to hard-fail at concat;
    this renders the actual preview end to end."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        proj = Path(cls.tmp.name)
        (proj / "raw").mkdir()
        (proj / "cut").mkdir()
        # cam: 192x108 (16:9) with 44.1k audio (mismatched rate on purpose)
        r = subprocess.run(
            ["ffmpeg", "-y",
             "-f", "lavfi", "-t", "4", "-i", "testsrc2=size=192x108:rate=30",
             "-f", "lavfi", "-t", "4",
             "-i", "sine=frequency=440:sample_rate=44100",
             "-t", "4", "-shortest",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
             "-pix_fmt", "yuv420p", "-c:a", "aac",
             str(proj / "raw" / "cam.mp4")],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        # screen: 160x120 (4:3, different aspect) with NO audio at all
        r = subprocess.run(
            ["ffmpeg", "-y",
             "-f", "lavfi", "-t", "4", "-i", "testsrc2=size=160x120:rate=30",
             "-t", "4",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
             "-pix_fmt", "yuv420p",
             str(proj / "raw" / "screen.mp4")],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        edl = {"source": "raw/cam.mp4", "fade_ms": 30, "pad_ms": 60,
               "segments": [
                   {"source": "raw/cam.mp4", "start": 0.5, "end": 2.0},
                   {"source": "raw/screen.mp4", "start": 0.5, "end": 2.0},
                   {"source": "raw/screen.mp4", "start": 2.5, "end": 3.5}]}
        (proj / "cut" / "edl.json").write_text(json.dumps(edl))
        cls.proj = proj

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_preview_renders_mixed_dimensions_and_audioless(self):
        out = self.proj / "cut" / "preview.mp4"
        r = run([str(self.proj / "cut" / "edl.json"), "-o", str(out),
                 "--height", "108"])
        self.assertEqual(r.returncode, 0, r.stderr)
        summary = json.loads(r.stdout)
        self.assertEqual(summary["segments"], 3)
        self.assertTrue(out.is_file())
        self.assertAlmostEqual(summary["actual_duration_seconds"], 3.5,
                               delta=0.5)
        # every frame is the one normalized target size, computed from the
        # first source at the requested height
        dims = core.probe_dims(self.proj / "raw" / "cam.mp4")
        expected_w = core.even(dims[0] * 108 / dims[1])
        self.assertEqual(core.probe_dims(out), (expected_w, 108))


def ov(oid, start, dur, path="g/x.mov", image=False):
    return {"id": oid, "start": start, "dur": dur, "path": path,
            "image": image}


class TestPlanOverlayLanes(unittest.TestCase):
    """Lane count must equal max concurrent overlays: that number becomes the
    depth of the final overlay stack, which is the whole optimization. The
    real project went from a 56-deep stack to 2 lanes."""

    def test_no_overlays_is_no_lanes(self):
        self.assertEqual(core.plan_overlay_lanes([]), [])

    def test_sequential_overlays_share_one_lane(self):
        lanes = core.plan_overlay_lanes(
            [ov("a", 0.0, 1.0), ov("b", 1.0, 1.0), ov("c", 5.0, 1.0)])
        self.assertEqual(len(lanes), 1)
        self.assertEqual([o["id"] for o in lanes[0]], ["a", "b", "c"])

    def test_overlapping_overlays_split_into_lanes(self):
        lanes = core.plan_overlay_lanes(
            [ov("a", 0.0, 3.0), ov("b", 1.0, 3.0)])
        self.assertEqual(len(lanes), 2)

    def test_lane_count_equals_max_concurrency(self):
        # Three at once in the middle, one at a time either side.
        lanes = core.plan_overlay_lanes([
            ov("a", 0.0, 1.0),
            ov("b", 5.0, 4.0), ov("c", 6.0, 4.0), ov("d", 7.0, 4.0),
            ov("e", 20.0, 1.0)])
        self.assertEqual(len(lanes), 3)

    def test_fifty_six_sequential_overlays_still_collapse(self):
        # The real shape: many overlays, almost none concurrent.
        many = [ov(f"b{i:02d}", i * 10.0, 4.0) for i in range(56)]
        self.assertEqual(len(core.plan_overlay_lanes(many)), 1)

    def test_every_overlay_lands_in_exactly_one_lane(self):
        overlays = [ov(f"b{i}", i * 1.5, 2.0) for i in range(12)]
        lanes = core.plan_overlay_lanes(overlays)
        placed = [o["id"] for lane in lanes for o in lane]
        self.assertEqual(sorted(placed), sorted(o["id"] for o in overlays))
        self.assertEqual(len(placed), len(set(placed)))

    def test_within_a_lane_overlays_never_overlap(self):
        overlays = [ov(f"b{i}", i * 1.5, 2.0) for i in range(12)]
        for lane in core.plan_overlay_lanes(overlays):
            for a, b in zip(lane, lane[1:]):
                self.assertLessEqual(a["start"] + a["dur"], b["start"] + 1e-6)

    def test_touching_overlays_can_share_a_lane(self):
        lanes = core.plan_overlay_lanes(
            [ov("a", 0.0, 2.0), ov("b", 2.0, 2.0)])
        self.assertEqual(len(lanes), 1)


class TestLaneCommands(unittest.TestCase):
    def test_lane_filter_alternates_gaps_and_overlays(self):
        inputs, graph = core.build_lane_filter(
            [ov("a", 1.0, 2.0), ov("b", 5.0, 1.0)], 10.0, (320, 180), 30)
        # gap, a, gap, b, trailing gap = 5 concat inputs
        self.assertIn("concat=n=5", graph)
        self.assertEqual(inputs.count("-i"), 5)

    def test_gaps_are_forced_fully_transparent(self):
        _, graph = core.build_lane_filter(
            [ov("a", 1.0, 2.0)], 5.0, (320, 180), 30)
        # A colour source carries no usable alpha; aa=0 is load-bearing.
        self.assertIn("colorchannelmixer=aa=0", graph)

    def test_overlay_starting_at_zero_has_no_leading_gap(self):
        _, graph = core.build_lane_filter(
            [ov("a", 0.0, 2.0)], 4.0, (320, 180), 30)
        self.assertIn("concat=n=2", graph)  # overlay + trailing gap only

    def test_lane_filling_the_timeline_has_no_gaps(self):
        _, graph = core.build_lane_filter(
            [ov("a", 0.0, 4.0)], 4.0, (320, 180), 30)
        self.assertIn("concat=n=1", graph)

    def test_png_overlays_are_looped_for_their_duration(self):
        inputs, _ = core.build_lane_filter(
            [ov("a", 0.0, 2.0, "g/x.png", image=True)], 4.0, (320, 180), 30)
        self.assertIn("-loop", inputs)

    def test_empty_lane_yields_no_command(self):
        self.assertEqual(
            core.build_lane_command([], 10.0, (320, 180), 30, "o.mov"), [])

    def test_lane_codec_default_is_alpha_capable(self):
        cmd = core.build_lane_command(
            [ov("a", 0.0, 2.0)], 4.0, (320, 180), 30, "o.mov")
        self.assertIn("qtrle", cmd)
        self.assertIn("argb", cmd)

    def test_lane_codec_is_selectable(self):
        cmd = core.build_lane_command(
            [ov("a", 0.0, 2.0)], 4.0, (320, 180), 30, "o.mov",
            codec="prores")
        self.assertIn("prores_ks", cmd)

    def test_composite_depth_is_the_lane_count_not_the_overlay_count(self):
        cmd = core.build_lane_composite_command("base.mp4",
                                                ["l1.mov", "l2.mov"], "o.mp4")
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertEqual(graph.count("overlay="), 2)

    def test_composite_carries_the_base_audio(self):
        cmd = core.build_lane_composite_command("base.mp4", ["l1.mov"],
                                                "o.mp4")
        self.assertIn("0:a?", cmd)

    def test_composite_with_no_lanes_is_a_passthrough_graph(self):
        cmd = core.build_lane_composite_command("base.mp4", [], "o.mp4")
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertEqual(graph.count("overlay="), 0)


class TestProxies(unittest.TestCase):
    def test_proxy_path_is_named_by_stem_height_and_family(self):
        p = core.proxy_path("/p/renders/proxy", "raw/cam.mp4", 720)
        self.assertEqual(p.name, "cam-720p-intra.mp4")

    def test_long_gop_proxy_gets_a_distinct_path(self):
        """An all-intra proxy must never collide with a long-GOP one: the
        stream-copy lane is silently wrong against long-GOP."""
        intra = core.proxy_path("/p", "raw/cam.mp4", 720, intra=True)
        longgop = core.proxy_path("/p", "raw/cam.mp4", 720, intra=False)
        self.assertNotEqual(intra.name, longgop.name)

    def test_proxy_command_scales_to_the_height(self):
        cmd = core.build_proxy_command("in.mp4", "out.mp4", 720)
        self.assertIn("scale=-2:720", cmd)

    def test_proxy_command_is_all_intra_by_default(self):
        """-g 1 alone is not enough: x264 scenecut detection would still
        insert its own keyframes and keyint_min governs the minimum."""
        cmd = core.build_proxy_command("in.mp4", "out.mp4", 720)
        for flag, val in (("-g", "1"), ("-keyint_min", "1"),
                          ("-sc_threshold", "0")):
            self.assertIn(flag, cmd)
            self.assertEqual(cmd[cmd.index(flag) + 1], val)

    def test_proxy_command_can_opt_out_of_intra(self):
        cmd = core.build_proxy_command("in.mp4", "out.mp4", 720, intra=False)
        self.assertNotIn("-sc_threshold", cmd)

    def test_streamcopy_command_never_encodes(self):
        cmd = core.build_streamcopy_command("t.ffconcat", "o.mp4")
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertNotIn("-filter_complex", cmd)
        self.assertIn("concat", cmd)

    def test_a_proxy_from_an_older_recipe_is_stale(self):
        """A sidecar with no recipe line predates the intra lane. Reusing it
        would feed the stream-copy lane a long-GOP proxy: right duration,
        wrong frames."""
        import tempfile
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as d:
            src = _P(d) / "s.mp4"; src.write_bytes(b"x" * 64)
            proxy = _P(d) / "p.mp4"; proxy.write_bytes(b"y" * 64)
            # old-format sidecar: digest only, no recipe
            proxy.with_name("p.mp4.src").write_text(
                core.content_digest(src, cheap=True) + "\n")
            self.assertFalse(core.proxy_is_fresh(proxy, src))
            core.write_proxy_sidecar(proxy, src)
            self.assertTrue(core.proxy_is_fresh(proxy, src))

    def test_missing_proxy_is_not_fresh(self):
        self.assertFalse(core.proxy_is_fresh("/nope/p.mp4", "/nope/s.mp4"))

    def test_proxy_freshness_tracks_the_source_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            proxy = Path(tmp) / "src-720p.mp4"
            src.write_bytes(b"original")
            proxy.write_bytes(b"proxy")
            core.write_proxy_sidecar(proxy, src)
            self.assertTrue(core.proxy_is_fresh(proxy, src))
            # A re-recorded take with the same filename must invalidate it.
            src.write_bytes(b"re-recorded, different content entirely")
            self.assertFalse(core.proxy_is_fresh(proxy, src))

    def test_proxied_edl_swaps_sources_and_keeps_every_timecode(self):
        edl = {"source": "raw/cam.mp4", "fade_ms": 30, "segments": [
            {"source": "raw/cam.mp4", "start": 1.0, "end": 2.5},
            {"source": "raw/screen.mp4", "start": 0.0, "end": 3.0}]}
        out = core.proxied_edl(edl, {"raw/cam.mp4": "renders/proxy/cam.mp4"})
        self.assertEqual(out["segments"][0]["source"],
                         "renders/proxy/cam.mp4")
        self.assertEqual(out["segments"][1]["source"], "raw/screen.mp4")
        # A proxy is the same footage at a smaller size: times are untouched.
        for a, b in zip(edl["segments"], out["segments"]):
            self.assertEqual((a["start"], a["end"]), (b["start"], b["end"]))

    def test_proxied_edl_does_not_mutate_the_original(self):
        edl = {"segments": [{"source": "raw/cam.mp4", "start": 0, "end": 1}]}
        core.proxied_edl(edl, {"raw/cam.mp4": "proxy/cam.mp4"})
        self.assertEqual(edl["segments"][0]["source"], "raw/cam.mp4")


@unittest.skipUnless(FFMPEG, "ffmpeg/ffprobe not installed")
class TestLanePreviewEndToEnd(unittest.TestCase):
    """The composited preview through the lane path: overlapping overlays
    must still land at the right times on the edited timeline."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        proj = Path(cls.tmp.name)
        for d in ("raw", "cut", "graphics", "beats", "renders"):
            (proj / d).mkdir()
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-t", "12", "-i",
             "testsrc2=size=320x180:rate=30", "-f", "lavfi", "-t", "12",
             "-i", "sine=frequency=440:sample_rate=48000", "-shortest",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
             "-pix_fmt", "yuv420p", "-c:a", "aac",
             str(proj / "raw" / "cam.mp4")], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        for bid in ("b01", "b02", "b03", "b04"):
            r = subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-t", "2", "-i",
                 "color=c=red:s=320x180:r=30,format=rgba", "-c:v", "qtrle",
                 "-pix_fmt", "argb", str(proj / "graphics" / f"{bid}.mov")],
                capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
        # b01/b02 overlap, b03/b04 overlap: 2 lanes, with a clean gap at 4.5.
        (proj / "beats" / "beats.md").write_text(
            "| id | start | dur | end | type |\n|---|---|---|---|---|\n"
            "| b01 | 0.5 | 2.0 | 2.5 | overlay |\n"
            "| b02 | 2.0 | 2.0 | 4.0 | overlay |\n"
            "| b03 | 5.0 | 2.0 | 7.0 | overlay |\n"
            "| b04 | 5.5 | 2.0 | 7.5 | overlay |\n")
        (proj / "cut" / "edl.json").write_text(json.dumps(
            {"source": "raw/cam.mp4", "fade_ms": 30, "pad_ms": 60,
             "segments": [
                 {"source": "raw/cam.mp4", "start": 0.0, "end": 5.0},
                 {"source": "raw/cam.mp4", "start": 6.0, "end": 11.0}]}))
        cls.proj = proj
        cls.out = proj / "renders" / "preview.mp4"
        r = subprocess.run(
            [sys.executable, str(SCRIPT), str(proj / "cut" / "edl.json"),
             "-o", str(cls.out), "--height", "180",
             "--beats", str(proj / "beats" / "beats.md"),
             "--graphics-dir", str(proj / "graphics")],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        cls.summary = json.loads(r.stdout)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _centre_pixel(self, t):
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(self.out),
             "-frames:v", "1", "-vf",
             "crop=40:40:(iw-40)/2:(ih-40)/2,scale=1:1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True)
        return tuple(r.stdout[:3])

    def _is_red(self, rgb):
        return rgb[0] > 150 and rgb[1] < 90 and rgb[2] < 90

    def test_four_overlays_pack_into_two_lanes(self):
        self.assertEqual(self.summary["overlays"], 4)
        self.assertEqual(self.summary["overlay_lanes"], 2)

    def test_output_validates(self):
        self.assertTrue(self.summary["validated"])
        ok, problems = core.validate_render(self.out)
        self.assertTrue(ok, problems)

    def test_overlay_is_on_screen_inside_its_window(self):
        self.assertTrue(self._is_red(self._centre_pixel(1.0)))
        self.assertTrue(self._is_red(self._centre_pixel(3.0)))
        self.assertTrue(self._is_red(self._centre_pixel(6.0)))

    def test_overlay_is_absent_in_the_gap_between_beats(self):
        self.assertFalse(self._is_red(self._centre_pixel(4.5)))

    def test_overlay_is_absent_after_the_last_beat(self):
        self.assertFalse(self._is_red(self._centre_pixel(8.0)))

    def test_duration_matches_the_edl(self):
        self.assertAlmostEqual(self.summary["actual_duration_seconds"], 10.0,
                               delta=0.2)

    def test_a_proxy_was_built_and_is_reused(self):
        self.assertEqual(self.summary["proxied_sources"], 1)
        self.assertTrue((self.proj / "renders" / "proxy" /
                         "cam-180p-intra.mp4").is_file())

    def test_audio_survives_the_lane_composite(self):
        self.assertTrue(core.probe_has_audio(self.out))


class TestRenderKey(unittest.TestCase):
    """Content identity: two renders share a key exactly when they would
    produce the same bytes."""

    EDL = {"source": "raw/a.mp4", "fade_ms": 30,
           "segments": [{"source": "raw/a.mp4", "start": 0.0, "end": 1.0}]}

    def key(self, edl=None, params=None, overlays=None):
        return core.render_key(edl or self.EDL, params or {"height": 720},
                               overlays or {}, ffmpeg="8.1.2")

    def test_identical_inputs_give_identical_keys(self):
        self.assertEqual(self.key(), self.key())

    def test_key_ignores_dict_ordering(self):
        a = core.render_key(self.EDL, {"height": 720, "mode": "preview"},
                            {"b": "1", "a": "2"}, ffmpeg="8.1.2")
        b = core.render_key(self.EDL, {"mode": "preview", "height": 720},
                            {"a": "2", "b": "1"}, ffmpeg="8.1.2")
        self.assertEqual(a, b)

    def test_changed_edl_changes_the_key(self):
        other = json.loads(json.dumps(self.EDL))
        other["segments"][0]["end"] = 2.0
        self.assertNotEqual(self.key(), self.key(edl=other))

    def test_changed_params_change_the_key(self):
        self.assertNotEqual(self.key(), self.key(params={"height": 540}))

    def test_rerendered_overlay_changes_the_key(self):
        self.assertNotEqual(self.key(overlays={"b01": "sha256:aaa"}),
                            self.key(overlays={"b01": "sha256:bbb"}))

    def test_ffmpeg_bump_changes_the_key(self):
        a = core.render_key(self.EDL, {}, {}, ffmpeg="8.1.2")
        b = core.render_key(self.EDL, {}, {}, ffmpeg="9.0.0")
        self.assertNotEqual(a, b)


class TestTempRenderPath(unittest.TestCase):
    def test_temp_path_is_beside_the_output(self):
        tmp = core.temp_render_path(Path("/p/renders/preview.mp4"), "abc123")
        self.assertEqual(tmp.parent, Path("/p/renders"))
        self.assertTrue(tmp.name.endswith(".mp4"))

    def test_temp_path_is_not_the_output(self):
        out = Path("/p/renders/preview.mp4")
        self.assertNotEqual(core.temp_render_path(out, "abc123"), out)

    def test_temp_path_carries_the_pid(self):
        # Two concurrent renders of the SAME edl must not share a temp path,
        # or the interleaved-write corruption just moves one level down.
        import os
        tmp = core.temp_render_path(Path("/p/renders/preview.mp4"), "abc123")
        self.assertIn(str(os.getpid()), tmp.name)


class TestCurrentRenderKey(unittest.TestCase):
    def test_unchanged_edl_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "edl.json"
            edl = {"segments": [{"source": "a.mp4", "start": 0, "end": 1}]}
            p.write_text(json.dumps(edl))
            key = core.render_key(edl, {}, {}, ffmpeg="8.1.2")
            self.assertEqual(
                core.current_render_key(p, {}, {}, ffmpeg="8.1.2"), key)

    def test_changed_edl_does_not_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "edl.json"
            edl = {"segments": [{"source": "a.mp4", "start": 0, "end": 1}]}
            p.write_text(json.dumps(edl))
            key = core.render_key(edl, {}, {}, ffmpeg="8.1.2")
            edl["segments"][0]["end"] = 5
            p.write_text(json.dumps(edl))
            self.assertNotEqual(
                core.current_render_key(p, {}, {}, ffmpeg="8.1.2"), key)

    def test_unreadable_edl_returns_none_not_a_false_supersede(self):
        # An unreadable EDL is a different problem; treating it as a
        # supersede would throw away a good render.
        self.assertIsNone(core.current_render_key("/nope/edl.json"))


@unittest.skipUnless(FFMPEG, "ffmpeg/ffprobe not installed")
class TestValidateRender(unittest.TestCase):
    """The check that was missing when a corrupt preview shipped: a truncated
    mp4 still reports a plausible container duration, so only a full decode
    pass can see it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.good = cls.dir / "good.mp4"
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-t", "3", "-i",
             "testsrc2=size=160x90:rate=30", "-c:v", "libx264",
             "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p",
             str(cls.good)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_good_file_validates(self):
        ok, problems = core.validate_render(self.good, 3.0)
        self.assertTrue(ok, problems)

    def test_missing_file_fails(self):
        ok, problems = core.validate_render(self.dir / "nope.mp4")
        self.assertFalse(ok)
        self.assertIn("not written", problems[0])

    def test_empty_file_fails(self):
        empty = self.dir / "empty.mp4"
        empty.write_bytes(b"")
        ok, problems = core.validate_render(empty)
        self.assertFalse(ok)
        self.assertIn("empty", problems[0])

    def test_truncated_file_fails_decode(self):
        # The real corruption shape: a partially-written mp4.
        bad = self.dir / "truncated.mp4"
        bad.write_bytes(self.good.read_bytes()[: int(
            self.good.stat().st_size * 0.4)])
        ok, problems = core.validate_render(bad)
        self.assertFalse(ok)

    def test_garbage_bytes_fail(self):
        bad = self.dir / "garbage.mp4"
        bad.write_bytes(b"\x00\x01\x02" * 5000)
        ok, _ = core.validate_render(bad)
        self.assertFalse(ok)

    def test_duration_mismatch_is_reported(self):
        ok, problems = core.validate_render(self.good, 30.0)
        self.assertFalse(ok)
        self.assertTrue(any("duration" in p for p in problems))

    def test_duration_within_tolerance_passes(self):
        ok, _ = core.validate_render(self.good, 3.3, tolerance=0.5)
        self.assertTrue(ok)


class TestPublishRender(unittest.TestCase):
    def test_publish_moves_atomically_and_writes_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "part.mp4"
            dst = Path(tmp) / "preview.mp4"
            src.write_bytes(b"payload")
            core.publish_render(src, dst, "deadbeef")
            self.assertFalse(src.exists())
            self.assertEqual(dst.read_bytes(), b"payload")
            self.assertEqual(
                (Path(tmp) / "preview.mp4.key").read_text().strip(),
                "deadbeef")

    def test_publish_overwrites_an_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "part.mp4"
            dst = Path(tmp) / "preview.mp4"
            dst.write_bytes(b"old")
            src.write_bytes(b"new")
            core.publish_render(src, dst, "k")
            self.assertEqual(dst.read_bytes(), b"new")

    def test_discard_removes_the_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "part.mp4"
            p.write_bytes(b"x")
            core.discard_render(p)
            self.assertFalse(p.exists())

    def test_discard_tolerates_a_missing_file(self):
        core.discard_render("/nope/never/part.mp4")  # must not raise


@unittest.skipUnless(FFMPEG, "ffmpeg/ffprobe not installed")
class TestSupersedeEndToEnd(unittest.TestCase):
    """The 2026-07-24 corruption, reproduced as a test: a render whose EDL
    changed underneath it must discard its output, not publish over the
    newer one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name)
        (self.proj / "raw").mkdir()
        (self.proj / "cut").mkdir()
        (self.proj / "renders").mkdir()
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-t", "5", "-i",
             "testsrc2=size=160x90:rate=30", "-f", "lavfi", "-t", "5",
             "-i", "sine=frequency=440:sample_rate=48000", "-shortest",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
             "-pix_fmt", "yuv420p", "-c:a", "aac",
             str(self.proj / "raw" / "cam.mp4")],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        self.edl_path = self.proj / "cut" / "edl.json"
        self.out = self.proj / "renders" / "preview.mp4"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_edl(self, end):
        # Atomic, because a render already in flight may be reading this
        # file: a plain write truncates first and a concurrent reader gets a
        # torn EDL. Any writer of edl.json owes readers the same.
        core.write_json_atomic(self.edl_path, {
            "source": "raw/cam.mp4", "fade_ms": 30, "pad_ms": 60,
            "segments": [{"source": "raw/cam.mp4", "start": 0.0,
                          "end": end}]})

    def _render(self):
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.edl_path), "-o",
             str(self.out), "--height", "90"],
            capture_output=True, text=True)

    def test_normal_render_publishes_and_validates(self):
        self._write_edl(2.0)
        r = self._render()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.out.is_file())
        summary = json.loads(r.stdout)
        self.assertTrue(summary["validated"])
        ok, problems = core.validate_render(self.out)
        self.assertTrue(ok, problems)

    def test_published_output_records_its_key(self):
        self._write_edl(2.0)
        self._render()
        keyfile = self.out.with_name(self.out.name + ".key")
        self.assertTrue(keyfile.is_file())
        self.assertEqual(keyfile.read_text().strip(),
                         json.loads(self._render().stdout)["render_key"])

    def test_no_temp_files_survive_a_successful_render(self):
        self._write_edl(2.0)
        self._render()
        leftovers = [p for p in (self.proj / "renders").iterdir()
                     if ".part" in p.name]
        self.assertEqual(leftovers, [])

    def test_concurrent_renders_never_produce_a_corrupt_output(self):
        """The bug report's acceptance test, literally.

        Two renders of DIFFERENT EDLs racing to the same output path. That
        is what produced an unplayable preview ("Invalid NAL unit size",
        "missing picture in access unit") on the real project. Whichever
        wins is not the assertion; the assertion is that the file left
        behind always decodes cleanly."""
        self._write_edl(4.5)
        first = subprocess.Popen(
            [sys.executable, str(SCRIPT), str(self.edl_path), "-o",
             str(self.out), "--height", "90"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # Change the EDL underneath the running render, then start a second
        # render against the new one, both aimed at the same output.
        self._write_edl(1.5)
        second = subprocess.Popen(
            [sys.executable, str(SCRIPT), str(self.edl_path), "-o",
             str(self.out), "--height", "90"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        first.communicate()
        second.communicate()

        self.assertTrue(self.out.is_file())
        ok, problems = core.validate_render(self.out)
        self.assertTrue(ok, f"published a corrupt render: {problems}")
        # And nothing half-written is left lying around.
        leftovers = [p for p in (self.proj / "renders").iterdir()
                     if ".part" in p.name]
        self.assertEqual(leftovers, [])

    def test_a_stale_render_is_reported_as_superseded(self):
        # A render whose EDL changes before it publishes must refuse to
        # publish, and say why, rather than clobber the newer output.
        self._write_edl(2.0)
        edl = json.loads(self.edl_path.read_text())
        params = {"height": 90, "mode": "preview", "beats": False}
        stale_key = core.render_key(edl, params, {}, core.ffmpeg_version())
        self._write_edl(3.0)
        live_key = core.current_render_key(self.edl_path, params, {},
                                           core.ffmpeg_version())
        self.assertIsNotNone(live_key)
        self.assertNotEqual(stale_key, live_key)

    def test_a_failed_render_leaves_the_previous_output_intact(self):
        self._write_edl(2.0)
        self.assertEqual(self._render().returncode, 0)
        good = self.out.read_bytes()
        # An EDL referencing a source that does not exist: the render fails
        # and must not touch the published file.
        self.edl_path.write_text(json.dumps({
            "source": "raw/gone.mp4", "fade_ms": 30, "pad_ms": 60,
            "segments": [{"source": "raw/gone.mp4", "start": 0.0,
                          "end": 1.0}]}))
        self.assertEqual(self._render().returncode, 1)
        self.assertEqual(self.out.read_bytes(), good)


class TestFusionTitleBeats(unittest.TestCase):
    """Title beats live in Resolve only (graphics/titles.json): no baked file,
    and never reported missing."""

    def test_title_beats_are_not_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = Path(tmp)
            (g / "b01.mov").write_bytes(b"")
            (g / "titles.json").write_text(json.dumps([{"id": "b02", "text": "x"}]))
            beats = [{"id": i, "start": n, "dur": 1.0}
                     for n, i in enumerate(["b01", "b02", "b03"])]
            found, missing = core.resolve_overlays(beats, g)
            self.assertEqual([o["id"] for o in found], ["b01"])
            self.assertEqual(missing, ["b03"])
            self.assertEqual(core.title_ids(g), {"b02"})

    def test_unreadable_titles_file_counts_as_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "titles.json").write_text("{not json")
            self.assertEqual(core.title_ids(tmp), set())


if __name__ == "__main__":
    unittest.main()
