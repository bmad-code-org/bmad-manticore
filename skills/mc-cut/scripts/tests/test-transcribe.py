#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for transcribe.py: the deterministic, model-free parts: the
token->word mapping (indexing, gap computation, clamping, rounding), the
platform provider resolution, the onnx-asr token adapter (SentencePiece
boundary markers, end derivation, score-to-confidence mapping), the fixed
window chunk planner, the overlap merge, and the provider-switch exit codes.

The real transcription requires the parakeet-mlx or onnx-asr model and a media
file; it is exercised by running the script directly, not unit-tested here.
These tests import only stdlib and the script's pure helpers, so they run
without either model dependency, no model download, and no network."""
import contextlib
import importlib.util
import io
import json
import math
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "transcribe.py"

spec = importlib.util.spec_from_file_location("transcribe", SCRIPT)
transcribe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transcribe)


def run(args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True)


class TestGroupSubwords(unittest.TestCase):
    def test_leading_space_starts_a_word_and_conf_is_min(self):
        # The validated run's first words, as real parakeet subword tokens:
        # a leading space marks a new word; continuations/punctuation attach.
        tokens = [
            {"text": " Al", "start": 1.28, "end": 1.52, "confidence": 0.9079},
            {"text": "rig", "start": 1.52, "end": 1.68, "confidence": 1.0},
            {"text": "ht", "start": 1.68, "end": 1.68, "confidence": 1.0},
            {"text": ",", "start": 1.68, "end": 1.76, "confidence": 0.9564},
            {"text": " I", "start": 1.76, "end": 1.84, "confidence": 0.9994},
            {"text": "'", "start": 1.84, "end": 1.92, "confidence": 0.9998},
            {"text": "m", "start": 1.92, "end": 2.0, "confidence": 0.9999},
        ]
        groups = transcribe.group_subwords(tokens)
        self.assertEqual([g[0] for g in groups], ["Alright,", "I'm"])
        # span = first token start .. last token end
        self.assertEqual(groups[0][1], 1.28)
        self.assertEqual(groups[0][2], 1.76)
        # confidence is the minimum over the subwords
        self.assertEqual(groups[0][3], 0.9079)
        self.assertEqual(groups[1][3], 0.9994)

    def test_first_token_starts_a_word_even_without_leading_space(self):
        groups = transcribe.group_subwords(
            [{"text": "hi", "start": 0.0, "end": 0.5, "confidence": 1.0}])
        self.assertEqual([g[0] for g in groups], ["hi"])


class TestNormalizeWords(unittest.TestCase):
    def test_basic_mapping_matches_pinned_schema(self):
        tokens = [
            {"text": " Al", "start": 1.28, "end": 1.52, "confidence": 0.9079},
            {"text": "rig", "start": 1.52, "end": 1.68, "confidence": 1.0},
            {"text": "ht", "start": 1.68, "end": 1.68, "confidence": 1.0},
            {"text": ",", "start": 1.68, "end": 1.76, "confidence": 0.9564},
            {"text": " I", "start": 1.76, "end": 1.84, "confidence": 0.9994},
            {"text": "'", "start": 1.84, "end": 1.92, "confidence": 0.9998},
            {"text": "m", "start": 1.92, "end": 2.0, "confidence": 0.9999},
            {"text": " doing", "start": 2.0, "end": 2.24, "confidence": 1.0},
        ]
        words = transcribe.normalize_words(tokens, duration=10.0)
        self.assertEqual(words[0], {
            "word": "Alright,", "start": 1.28, "end": 1.76, "confidence": 0.9079,
            "i": 0, "gap_before": 1.28, "gap_after": 0.0,
        })
        self.assertEqual([w["word"] for w in words], ["Alright,", "I'm", "doing"])
        self.assertEqual([w["i"] for w in words], [0, 1, 2])

    def test_first_word_gap_before_is_its_start(self):
        tokens = [{"text": "hi", "start": 2.5, "end": 3.0, "confidence": 1.0}]
        words = transcribe.normalize_words(tokens, duration=5.0)
        self.assertEqual(words[0]["gap_before"], 2.5)

    def test_last_word_gap_after_is_duration_minus_end(self):
        tokens = [
            {"text": " a", "start": 0.0, "end": 1.0, "confidence": 1.0},
            {"text": " b", "start": 1.5, "end": 2.0, "confidence": 1.0},
        ]
        words = transcribe.normalize_words(tokens, duration=2.19)
        self.assertEqual(words[-1]["gap_after"], 0.19)

    def test_interior_gaps(self):
        tokens = [
            {"text": " a", "start": 0.0, "end": 1.0, "confidence": 1.0},
            {"text": " b", "start": 1.5, "end": 2.0, "confidence": 1.0},
            {"text": " c", "start": 2.0, "end": 2.5, "confidence": 1.0},
        ]
        words = transcribe.normalize_words(tokens, duration=3.0)
        self.assertEqual(words[0]["gap_after"], 0.5)
        self.assertEqual(words[1]["gap_before"], 0.5)
        self.assertEqual(words[1]["gap_after"], 0.0)

    def test_negative_gaps_are_clamped(self):
        # Overlapping timestamps must never produce a negative gap.
        tokens = [
            {"text": " a", "start": 0.0, "end": 2.0, "confidence": 1.0},
            {"text": " b", "start": 1.5, "end": 3.0, "confidence": 1.0},
        ]
        words = transcribe.normalize_words(tokens, duration=2.5)
        self.assertEqual(words[0]["gap_after"], 0.0)
        self.assertEqual(words[1]["gap_before"], 0.0)
        # duration < last end: gap_after clamps to 0.0
        self.assertEqual(words[1]["gap_after"], 0.0)

    def test_rounding(self):
        tokens = [{"text": " x", "start": 1.23456, "end": 1.78912,
                   "confidence": 0.907891}]
        words = transcribe.normalize_words(tokens, duration=5.0)
        self.assertEqual(words[0]["start"], 1.23)
        self.assertEqual(words[0]["end"], 1.79)
        self.assertEqual(words[0]["confidence"], 0.9079)

    def test_accepts_objects_not_just_dicts(self):
        class Tok:
            def __init__(self, text, start, end, confidence):
                self.text, self.start, self.end, self.confidence = (
                    text, start, end, confidence)
        tokens = [Tok(" hey", 0.0, 0.5, 1.0)]
        words = transcribe.normalize_words(tokens, duration=1.0)
        self.assertEqual(words[0]["word"], "hey")
        self.assertEqual(words[0]["gap_after"], 0.5)


class TestDefaultProvider(unittest.TestCase):
    def test_apple_silicon_gets_parakeet_mlx(self):
        self.assertEqual(
            transcribe.default_provider("darwin", "arm64"), "parakeet-mlx")

    def test_intel_mac_gets_onnx(self):
        self.assertEqual(
            transcribe.default_provider("darwin", "x86_64"), "onnx-asr")

    def test_windows_gets_onnx(self):
        self.assertEqual(
            transcribe.default_provider("win32", "AMD64"), "onnx-asr")

    def test_linux_gets_onnx(self):
        self.assertEqual(
            transcribe.default_provider("linux", "x86_64"), "onnx-asr")
        self.assertEqual(
            transcribe.default_provider("linux", "aarch64"), "onnx-asr")


class TestScoreToConfidence(unittest.TestCase):
    def test_none_is_no_signal_one(self):
        self.assertEqual(transcribe._score_to_confidence(None), 1.0)

    def test_logprobs_are_exponentiated(self):
        self.assertAlmostEqual(
            transcribe._score_to_confidence(-0.1), math.exp(-0.1))
        self.assertEqual(transcribe._score_to_confidence(0.0), 1.0)

    def test_probabilities_pass_through_and_clamp(self):
        self.assertEqual(transcribe._score_to_confidence(0.97), 0.97)
        self.assertEqual(transcribe._score_to_confidence(1.7), 1.0)


class TestOnnxTokensToParakeet(unittest.TestCase):
    def test_boundary_marker_becomes_leading_space(self):
        tokens = transcribe.onnx_tokens_to_parakeet(
            ["▁Al", "rig", "ht", ",", "▁I"],
            [1.28, 1.52, 1.68, 1.68, 1.76],
        )
        self.assertEqual([t["text"] for t in tokens],
                         [" Al", "rig", "ht", ",", " I"])

    def test_start_only_timestamps_derive_ends(self):
        tokens = transcribe.onnx_tokens_to_parakeet(
            ["▁a", "b", "▁c"], [0.0, 0.4, 0.4])
        # end = next start when the next token CONTINUES the same word
        self.assertEqual(tokens[0]["end"], 0.4)
        # end = start + one 80 ms frame when the next start does not advance
        self.assertAlmostEqual(tokens[1]["end"], 0.48)
        # last token: start + one frame
        self.assertAlmostEqual(tokens[2]["end"], 0.48)

    def test_end_before_a_new_word_is_capped_not_stretched(self):
        # The next token opens a NEW word close by: the derived end still
        # reaches its onset (the cap only engages past a few frames).
        near = transcribe.onnx_tokens_to_parakeet(["▁a", "▁b"], [0.0, 0.1])
        self.assertAlmostEqual(near[0]["end"], 0.1)
        # But when the new word's onset sits past the cap, the end must NOT
        # be dragged to it: parakeet emits no tokens during silence, and
        # stretching the end across the pause would zero the gap data.
        far = transcribe.onnx_tokens_to_parakeet(["▁a", "▁b"], [0.0, 3.0])
        self.assertAlmostEqual(
            far[0]["end"],
            transcribe.WORD_END_CAP_FRAMES * transcribe.FRAME_S)

    def test_multi_second_pause_survives_into_gap_after(self):
        # Regression: start-only timestamps around a real 3.1 s pause
        # ('point.' then silence then 'Next'). The pause must land in
        # gap_after, not be absorbed into the word before it.
        tokens = transcribe.onnx_tokens_to_parakeet(
            ["▁point", ".", "▁Next"], [9.5, 9.9, 13.0])
        words = transcribe.normalize_words(tokens, duration=15.0)
        self.assertEqual([w["word"] for w in words], ["point.", "Next"])
        self.assertLessEqual(
            words[0]["end"],
            9.9 + transcribe.WORD_END_CAP_FRAMES * transcribe.FRAME_S)
        self.assertGreaterEqual(words[0]["gap_after"], 2.8)
        self.assertGreaterEqual(words[1]["gap_before"], 2.8)

    def test_pair_timestamps_are_used_directly(self):
        tokens = transcribe.onnx_tokens_to_parakeet(
            ["▁hi"], [(0.16, 0.56)])
        self.assertEqual(tokens[0]["start"], 0.16)
        self.assertEqual(tokens[0]["end"], 0.56)

    def test_logprobs_map_to_confidence_and_absence_degrades_to_one(self):
        with_scores = transcribe.onnx_tokens_to_parakeet(
            ["▁a", "b"], [0.0, 0.4], logprobs=[-0.1, 0.0])
        self.assertAlmostEqual(with_scores[0]["confidence"], math.exp(-0.1))
        self.assertEqual(with_scores[1]["confidence"], 1.0)
        without = transcribe.onnx_tokens_to_parakeet(
            ["▁a", "b"], [0.0, 0.4])
        self.assertEqual([t["confidence"] for t in without], [1.0, 1.0])

    def test_bare_marker_token_carries_boundary_to_next_token(self):
        tokens = transcribe.onnx_tokens_to_parakeet(
            ["▁a", "▁", "b"], [0.0, 0.4, 0.48])
        self.assertEqual([t["text"] for t in tokens], [" a", " b"])
        self.assertEqual(tokens[1]["start"], 0.48)

    def test_clamp_caps_end_times(self):
        tokens = transcribe.onnx_tokens_to_parakeet(
            ["▁a"], [19.96], clamp=20.0)
        self.assertEqual(tokens[0]["end"], 20.0)

    def test_count_mismatch_raises(self):
        with self.assertRaises(ValueError):
            transcribe.onnx_tokens_to_parakeet(["▁a", "b"], [0.0])

    def test_parity_with_parakeet_lane_words(self):
        # The same subwords the parakeet fixture uses, as SentencePiece
        # tokens with (start, end) pairs, must normalize to the identical
        # pinned word schema (byte-compatible words.json across lanes).
        tokens = transcribe.onnx_tokens_to_parakeet(
            ["▁Al", "rig", "ht", ",", "▁I", "'", "m", "▁doing"],
            [(1.28, 1.52), (1.52, 1.68), (1.68, 1.68), (1.68, 1.76),
             (1.76, 1.84), (1.84, 1.92), (1.92, 2.0), (2.0, 2.24)],
        )
        words = transcribe.normalize_words(tokens, duration=10.0)
        self.assertEqual(words[0], {
            "word": "Alright,", "start": 1.28, "end": 1.76, "confidence": 1.0,
            "i": 0, "gap_before": 1.28, "gap_after": 0.0,
        })
        self.assertEqual([w["word"] for w in words], ["Alright,", "I'm", "doing"])


class TestPlanChunks(unittest.TestCase):
    def test_short_audio_is_a_single_chunk(self):
        self.assertEqual(transcribe.plan_chunks(12.5, window=20.0, overlap=2.0),
                         [(0.0, 12.5)])
        self.assertEqual(transcribe.plan_chunks(20.0, window=20.0, overlap=2.0),
                         [(0.0, 20.0)])

    def test_long_audio_steps_by_window_minus_overlap(self):
        chunks = transcribe.plan_chunks(40.0, window=20.0, overlap=2.0)
        self.assertEqual(chunks, [(0.0, 20.0), (18.0, 20.0), (36.0, 4.0)])
        # full coverage: last chunk reaches the duration
        self.assertEqual(chunks[-1][0] + chunks[-1][1], 40.0)
        # adjacent chunks share exactly the overlap
        self.assertEqual(chunks[0][0] + chunks[0][1] - chunks[1][0], 2.0)

    def test_zero_duration_is_a_single_empty_chunk(self):
        self.assertEqual(transcribe.plan_chunks(0.0), [(0.0, 0.0)])

    def test_bad_window_or_overlap_raises(self):
        with self.assertRaises(ValueError):
            transcribe.plan_chunks(10.0, window=0.0)
        with self.assertRaises(ValueError):
            transcribe.plan_chunks(10.0, window=20.0, overlap=20.0)


class TestOffsetTokens(unittest.TestCase):
    def test_offsets_start_and_end_only(self):
        out = transcribe.offset_tokens(
            [{"text": " a", "start": 1.0, "end": 1.5, "confidence": 0.9}], 18.0)
        self.assertEqual(out, [{"text": " a", "start": 19.0, "end": 19.5,
                                "confidence": 0.9}])


class TestMergeChunkTokens(unittest.TestCase):
    def _tok(self, text, start, end):
        return {"text": text, "start": start, "end": end, "confidence": 1.0}

    def test_single_chunk_passes_through(self):
        tokens = [self._tok(" a", 0.0, 0.5), self._tok(" b", 1.0, 1.5)]
        self.assertEqual(
            transcribe.merge_chunk_tokens([(0.0, 20.0, tokens)]), tokens)

    def test_overlap_cuts_at_midpoint_without_duplication(self):
        # overlap region 18..20, cut at 19: the word at 18.5 belongs to the
        # first chunk, the shared boundary word (both chunks heard it, with
        # slightly different times) belongs to the second.
        first = [
            self._tok(" early", 5.0, 5.5),
            self._tok(" late", 18.5, 18.9),
            self._tok(" edge", 19.5, 19.9),   # first chunk's take, dropped
        ]
        second = [
            self._tok(" edge", 19.45, 19.9),  # second chunk's take, kept
            self._tok(" after", 21.0, 21.5),
        ]
        merged = transcribe.merge_chunk_tokens(
            [(0.0, 20.0, first), (18.0, 38.0, second)])
        self.assertEqual([t["text"] for t in merged],
                         [" early", " late", " edge", " after"])
        self.assertEqual(merged[2]["start"], 19.45)

    def test_word_starting_exactly_at_cut_goes_to_the_later_chunk(self):
        first = [self._tok(" at", 19.0, 19.4)]
        second = [self._tok(" at", 19.0, 19.4)]
        merged = transcribe.merge_chunk_tokens(
            [(0.0, 20.0, first), (18.0, 38.0, second)])
        self.assertEqual([t["text"] for t in merged], [" at"])

    def test_multi_token_words_stay_whole(self):
        # A word's subword continuations travel with its first token even
        # when the continuations fall past the cut.
        first = [
            self._tok(" spl", 18.8, 19.0),
            self._tok("it", 19.0, 19.2),
        ]
        second = [self._tok(" next", 20.5, 21.0)]
        merged = transcribe.merge_chunk_tokens(
            [(0.0, 20.0, first), (18.0, 38.0, second)])
        self.assertEqual([t["text"] for t in merged], [" spl", "it", " next"])

    def test_straddling_estimates_do_not_duplicate(self):
        # The two chunks time the same boundary word independently; when the
        # estimates straddle the cut (18.9 < 19 <= 19.05) the base rule keeps
        # BOTH takes. The seam pass must keep exactly one.
        first = [
            self._tok(" before", 17.0, 17.4),
            self._tok(" edge", 18.9, 19.3),   # < cut: kept by chunk 1
        ]
        second = [
            self._tok(" edge", 19.05, 19.45),  # >= cut: kept by chunk 2
            self._tok(" after", 21.0, 21.5),
        ]
        merged = transcribe.merge_chunk_tokens(
            [(0.0, 20.0, first), (18.0, 38.0, second)])
        self.assertEqual([t["text"] for t in merged],
                         [" before", " edge", " after"])
        # the takes' mean midpoint (19.175) is past the cut, nearer the
        # second chunk's window center, so its take wins
        self.assertEqual(merged[1]["start"], 19.05)

    def test_crosswise_estimates_do_not_drop(self):
        # Crosswise takes: chunk 1 heard the word at 19.1 (>= cut, dropped),
        # chunk 2 heard it at 18.9 (< cut, dropped). The base rule loses the
        # word entirely; the seam pass must restore exactly one take.
        first = [
            self._tok(" before", 17.0, 17.4),
            self._tok(" edge", 19.1, 19.5),   # >= cut: dropped by chunk 1
        ]
        second = [
            self._tok(" edge", 18.9, 19.3),   # < cut: dropped by chunk 2
            self._tok(" after", 21.0, 21.5),
        ]
        merged = transcribe.merge_chunk_tokens(
            [(0.0, 20.0, first), (18.0, 38.0, second)])
        self.assertEqual([t["text"] for t in merged],
                         [" before", " edge", " after"])
        # mean midpoint of the takes (19.2) is past the cut, nearer the
        # second chunk's center, so chunk 2's take is restored
        self.assertEqual(merged[1]["start"], 18.9)

    def test_seam_repair_survives_multi_token_words(self):
        # A duplicated word made of several subword tokens deduplicates as a
        # whole run, never token by token.
        first = [self._tok(" spl", 18.8, 19.0), self._tok("it", 19.0, 19.2)]
        second = [self._tok(" spl", 19.0, 19.2), self._tok("it", 19.2, 19.4)]
        merged = transcribe.merge_chunk_tokens(
            [(0.0, 20.0, first), (18.0, 38.0, second)])
        self.assertEqual([t["text"] for t in merged], [" spl", "it"])

    def test_repeated_words_far_apart_are_not_deduplicated(self):
        # Identical text alone never merges words; the takes' time spans must
        # overlap. Two real occurrences of "the" survive.
        first = [self._tok(" the", 18.2, 18.5)]
        second = [self._tok(" the", 19.4, 19.7)]
        merged = transcribe.merge_chunk_tokens(
            [(0.0, 20.0, first), (18.0, 38.0, second)])
        self.assertEqual([t["text"] for t in merged], [" the", " the"])

    def test_words_come_out_sorted_by_start(self):
        first = [self._tok(" b", 10.0, 10.5)]
        second = [self._tok(" a", 19.0, 19.5)]
        # second chunk's word starts inside the first chunk's exclusive zone
        # is impossible by construction; this asserts the stable sort on the
        # kept words.
        merged = transcribe.merge_chunk_tokens(
            [(0.0, 20.0, first), (18.0, 38.0, second)])
        self.assertEqual([t["text"] for t in merged], [" b", " a"])
        self.assertLess(merged[0]["start"], merged[1]["start"])


class TestOutputEncoding(unittest.TestCase):
    def test_words_json_is_written_utf8_never_the_locale_codec(self):
        # The onnx-asr lane exists for the 25 European parakeet languages;
        # a Czech transcript must land as UTF-8 bytes even where the locale
        # codec is cp1252 (Windows), so the write must pass encoding="utf-8"
        # explicitly and downstream utf-8 readers must get the text back.
        tokens = [
            {"text": " Čau,", "start": 0.0, "end": 0.4, "confidence": 1.0},
            {"text": " světe", "start": 0.5, "end": 0.9, "confidence": 1.0},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "take.mp4"
            media.write_bytes(b"\x00")
            out = Path(tmp) / "words.json"
            with mock.patch.object(
                    transcribe, "transcribe_parakeet",
                    return_value=("Čau, světe", tokens, 1.0)), \
                 mock.patch.object(Path, "write_text", autospec=True,
                                   side_effect=Path.write_text) as writes, \
                 contextlib.redirect_stdout(io.StringIO()):
                rc = transcribe.main([str(media), "-o", str(out),
                                      "--provider", "parakeet-mlx"])
            self.assertEqual(rc, 0)
            encodings = [c.kwargs.get("encoding")
                         for c in writes.call_args_list]
            self.assertEqual(encodings, ["utf-8"])
            payload = json.loads(out.read_bytes().decode("utf-8"))
            self.assertEqual(payload["words"][0]["word"], "Čau,")
            self.assertEqual(payload["text"], "Čau, světe")


class TestCudaFallbackWarning(unittest.TestCase):
    def test_nvidia_box_without_cuda_ep_warns_loudly(self):
        msg = transcribe.cuda_fallback_warning(
            ["CPUExecutionProvider"], nvidia_smi_present=True)
        self.assertIsNotNone(msg)
        self.assertIn("CUDAExecutionProvider", msg)
        self.assertIn('onnx-asr[gpu,hub]', msg)
        # the escalation must go through the python command so the script's
        # cpu extra is skipped (onnxruntime + onnxruntime-gpu never co-install)
        self.assertIn("python", msg)

    def test_nvidia_box_with_cuda_ep_is_silent(self):
        self.assertIsNone(transcribe.cuda_fallback_warning(
            ["CUDAExecutionProvider", "CPUExecutionProvider"],
            nvidia_smi_present=True))

    def test_no_nvidia_gpu_is_silent(self):
        self.assertIsNone(transcribe.cuda_fallback_warning(
            ["CPUExecutionProvider"], nvidia_smi_present=False))
        self.assertIsNone(transcribe.cuda_fallback_warning(
            None, nvidia_smi_present=False))

    def test_no_provider_list_on_an_nvidia_box_warns(self):
        self.assertIsNotNone(transcribe.cuda_fallback_warning(
            None, nvidia_smi_present=True))


class TestProviderSwitch(unittest.TestCase):
    def test_unimplemented_provider_exits_3(self):
        r = run(["some.mp4", "-o", "out.json", "--provider", "deepgram-nova3"])
        self.assertEqual(r.returncode, 3)
        self.assertIn("not implemented", r.stderr.lower())

    def test_missing_output_arg_is_usage_error(self):
        r = run(["some.mp4"])
        self.assertEqual(r.returncode, 2)

    def test_onnx_asr_is_an_implemented_provider(self):
        # provider accepted; the missing media is caught first (exit 2),
        # so no model dependency is touched.
        r = run(["missing.mp4", "-o", "out.json", "--provider", "onnx-asr"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("media not found", r.stderr)

    def test_auto_resolves_and_reports_a_platform_lane(self):
        r = run(["missing.mp4", "-o", "out.json", "--provider", "auto"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("provider resolved:", r.stderr)

    def test_help_exits_zero(self):
        r = run(["--help"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("--provider", r.stdout)


if __name__ == "__main__":
    unittest.main()
