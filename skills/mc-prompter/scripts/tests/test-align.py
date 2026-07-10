#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for server/align.py (mc-prompter Phase B voice-follow).

Run directly:
    uv run skills/mc-prompter/scripts/tests/test-align.py

Pure stdlib unittest; no network, no models, no downloads. The fixture files
under fixtures/align/ are REAL partial event streams captured offline from
the nemotron-streaming model (sherpa-onnx 1.13.4, greedy_search, 120 ms
chunks) over synthesized speech; each fixture carries the exact script
snippet it was read against. Tests drive the aligner from those committed
JSON streams and never touch the models.
"""

import json
import sys
import time
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent / "server"))

from align import Aligner, merge_bpe, normalize_word  # noqa: E402
from script_ingest import ingest, speakable_words, take_word_ranges  # noqa: E402

FIXTURES = TESTS_DIR / "fixtures" / "align"


def load_fixture(name):
    fx = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    doc = ingest(fx["script"])
    return fx, speakable_words(doc), take_word_ranges(doc)


def run_fixture(aligner, events):
    """Feed every event; return the list of feed() results."""
    return [aligner.feed(e["tokens"], e["segment"], e["final"])
            for e in events]


def bpe(text):
    """Turn plain words into space-prefixed BPE-ish pieces."""
    return [" " + w for w in text.split()]


def assert_monotonic(testcase, results):
    anchors = [r["anchor"] for r in results]
    for a, b in zip(anchors, anchors[1:]):
        testcase.assertLessEqual(a, b, f"anchor retreated: {anchors}")


class TestBpeMerge(unittest.TestCase):

    def test_space_prefixed_pieces_start_words(self):
        tokens = [" He", "y", " e", "very", "one", ","]
        self.assertEqual(merge_bpe(tokens), ["Hey", "everyone,"])

    def test_first_piece_without_space_starts_a_word(self):
        self.assertEqual(merge_bpe(["We", " are"]), ["We", "are"])

    def test_punctuation_pieces_attach_to_previous_word(self):
        tokens = [" wor", "ks", "h", "op", ".", " We"]
        self.assertEqual(merge_bpe(tokens), ["workshop.", "We"])

    def test_empty_input(self):
        self.assertEqual(merge_bpe([]), [])

    def test_whitespace_only_pieces_dropped(self):
        self.assertEqual(merge_bpe([" ", " a"]), ["a"])


class TestNormalization(unittest.TestCase):

    def test_casefold_and_punctuation(self):
        self.assertEqual(normalize_word("Workshop."), ["workshop"])
        self.assertEqual(normalize_word("quietly,"), ["quietly"])

    def test_hyphen_splits(self):
        self.assertEqual(normalize_word("one-time"), ["one", "time"])
        self.assertEqual(normalize_word("break-even"), ["break", "even"])

    def test_apostrophe_removed_not_split(self):
        self.assertEqual(normalize_word("don't"), ["dont"])

    def test_small_numbers(self):
        self.assertEqual(normalize_word("90"), ["ninety"])
        self.assertEqual(normalize_word("42"), ["forty", "two"])
        self.assertEqual(normalize_word("7"), ["seven"])
        self.assertEqual(normalize_word("15"), ["fifteen"])

    def test_hundreds(self):
        self.assertEqual(normalize_word("300"), ["three", "hundred"])
        self.assertEqual(normalize_word("250"), ["two", "hundred", "fifty"])

    def test_years_read_as_pairs(self):
        self.assertEqual(normalize_word("2026"), ["twenty", "twenty", "six"])
        self.assertEqual(normalize_word("1995"),
                         ["nineteen", "ninety", "five"])

    def test_year_special_cases(self):
        self.assertEqual(normalize_word("2000"), ["two", "thousand"])
        self.assertEqual(normalize_word("2007"), ["two", "thousand", "seven"])
        self.assertEqual(normalize_word("1900"), ["nineteen", "hundred"])
        self.assertEqual(normalize_word("1907"),
                         ["nineteen", "oh", "seven"])

    def test_thousands_separator_comma(self):
        self.assertEqual(normalize_word("1,000"), ["one", "thousand"])

    def test_ordinals(self):
        self.assertEqual(normalize_word("1st"), ["first"])
        self.assertEqual(normalize_word("12th"), ["twelfth"])
        self.assertEqual(normalize_word("42nd"), ["forty", "two"])

    def test_percent(self):
        self.assertEqual(normalize_word("50%"), ["fifty", "percent"])

    def test_symbol_only_word_yields_nothing(self):
        self.assertEqual(normalize_word("..."), [])

    def test_apostrophe_family_stripped(self):
        # ASCII, right/left curly quote, U+02BC modifier letter apostrophe
        # (category Lm: survives isalnum and must be stripped explicitly),
        # and U+201B reversed quote all normalize like a plain apostrophe.
        for apo in ("'", "’", "‘", "ʼ", "‛"):
            self.assertEqual(normalize_word(f"won{apo}t"), ["wont"], repr(apo))
            self.assertEqual(normalize_word(f"don{apo}t"), ["dont"], repr(apo))


class TestAlignerSynthetic(unittest.TestCase):
    """Unit behavior with hand-built token streams (no fixtures)."""

    WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india "
             "juliet kilo lima").split()

    @staticmethod
    def tokens(text):
        """Turn plain words into space-prefixed BPE-ish pieces."""
        return [" " + w for w in text.split()]

    def test_anchor_starts_at_minus_one(self):
        self.assertEqual(Aligner(self.WORDS).anchor, -1)

    def test_provisional_tail_not_committed(self):
        al = Aligner(self.WORDS, k_provisional=4)
        r = al.feed(self.tokens("alpha bravo charlie delta"), 0, False)
        self.assertEqual(r["anchor"], -1)  # all 4 words provisional
        r = al.feed(self.tokens("alpha bravo charlie delta echo"), 0, False)
        self.assertEqual(r["anchor"], 0)  # only alpha committed

    def test_final_commits_whole_hypothesis(self):
        al = Aligner(self.WORDS)
        r = al.feed(self.tokens("alpha bravo charlie delta"), 0, True)
        self.assertEqual(r["anchor"], 3)
        self.assertTrue(r["moved"])

    def test_tail_revision_absorbed(self):
        al = Aligner(self.WORDS, k_provisional=2)
        al.feed(self.tokens("alpha bravo charlie del"), 0, False)
        r = al.feed(self.tokens("alpha bravo charlie delta echo"), 0, False)
        self.assertEqual(r["anchor"], 2)
        r = al.feed(self.tokens("alpha bravo charlie delta echo"), 0, True)
        self.assertEqual(r["anchor"], 4)

    def test_new_segment_resets_tracking_not_anchor(self):
        al = Aligner(self.WORDS)
        al.feed(self.tokens("alpha bravo charlie"), 0, True)
        self.assertEqual(al.anchor, 2)
        r = al.feed(self.tokens("delta echo"), 1, True)
        self.assertEqual(r["anchor"], 4)

    def test_offscript_speech_holds(self):
        al = Aligner(self.WORDS)
        al.feed(self.tokens("alpha bravo"), 0, True)
        r = al.feed(self.tokens("penguin zebra walrus"), 1, True)
        self.assertTrue(r["held"])
        self.assertFalse(r["moved"])
        self.assertEqual(r["anchor"], 1)

    def test_recovers_after_hold(self):
        al = Aligner(self.WORDS)
        al.feed(self.tokens("alpha bravo"), 0, True)
        al.feed(self.tokens("penguin zebra walrus"), 1, True)
        r = al.feed(self.tokens("charlie delta echo"), 2, True)
        self.assertFalse(r["held"])
        self.assertEqual(r["anchor"], 4)

    def test_skip_followed_within_window(self):
        al = Aligner(self.WORDS)
        al.feed(self.tokens("alpha bravo"), 0, True)
        r = al.feed(self.tokens("golf hotel india"), 1, True)
        self.assertEqual(r["anchor"], 8)

    def test_jump_beyond_window_holds(self):
        words = [f"w{i}" for i in range(200)]
        al = Aligner(words, window_base=10, window_mult=2)
        al.feed(self.tokens("w0 w1"), 0, True)
        r = al.feed(self.tokens("w150 w151"), 1, True)
        self.assertTrue(r["held"])
        self.assertEqual(r["anchor"], 1)

    def test_set_anchor_backwards_and_clamped(self):
        al = Aligner(self.WORDS)
        al.feed(self.tokens("alpha bravo charlie delta echo"), 0, True)
        al.set_anchor(1)
        self.assertEqual(al.anchor, 1)
        al.set_anchor(9999)
        self.assertEqual(al.anchor, len(self.WORDS) - 1)
        al.set_anchor(-5)
        self.assertEqual(al.anchor, -1)

    def test_set_anchor_consumes_current_hypothesis(self):
        al = Aligner(self.WORDS)
        al.feed(self.tokens("alpha bravo charlie delta echo"), 0, False)
        al.set_anchor(7)
        # Re-feeding the same hypothesis must not re-match old words.
        r = al.feed(self.tokens("alpha bravo charlie delta echo"), 0, True)
        self.assertEqual(r["anchor"], 7)
        self.assertFalse(r["moved"])
        # New words after the jump match from the new position.
        r = al.feed(self.tokens("india juliet"), 1, True)
        self.assertEqual(r["anchor"], 9)

    def test_number_in_script_matches_spoken_words(self):
        al = Aligner("the answer is 42 exactly".split())
        r = al.feed(self.tokens("the answer is forty two exactly"), 0, True)
        self.assertEqual(r["anchor"], 4)

    def test_spoken_digits_match_worded_script(self):
        al = Aligner("wait ninety days now".split())
        r = al.feed(self.tokens("wait 90 days now"), 0, True)
        self.assertEqual(r["anchor"], 3)


class TestTakeSkipping(unittest.TestCase):
    """Take blocks are free to skip and the anchor never sits inside one.

    Take paragraphs are already-recorded footage, dimmed or hidden in the
    UI; a presenter normally reads straight past them. The take here is 28
    words, far wider than the match window for small pending batches, so
    without take-aware skipping the aligner would stall at it forever.
    """

    INTRO = ("the local rig finally paid for itself after ninety days "
             "running quietly").split()
    TAKE = ("this whole paragraph is already recorded footage from the "
            "interview where our guest explains the memory bandwidth story "
            "in careful and thorough detail for the audience at home").split()
    OUTRO = "so let us get back to the bench and wrap things up".split()

    def build(self, words=None, ranges=None):
        if words is None:
            words = self.INTRO + self.TAKE + self.OUTRO
            ranges = [[len(self.INTRO), len(self.INTRO) + len(self.TAKE)]]
        return Aligner(words, take_ranges=ranges), set(range(*ranges[0]))

    def test_long_take_skipped_in_one_final(self):
        al, take_words = self.build()
        al.feed(bpe(" ".join(self.INTRO)), 0, True)
        self.assertEqual(al.anchor, len(self.INTRO) - 1)
        r = al.feed(bpe(" ".join(self.OUTRO)), 1, True)
        self.assertFalse(r["held"])
        self.assertEqual(r["anchor"],
                         len(self.INTRO) + len(self.TAKE) + len(self.OUTRO) - 1)

    def test_take_longer_than_window_crossed_by_small_batch(self):
        # 3 pending words give a window of 2*3+10 = 16 non-take tokens,
        # much narrower than the 28-word take; the take must not consume
        # window budget.
        al, take_words = self.build()
        al.feed(bpe(" ".join(self.INTRO)), 0, True)
        r = al.feed(bpe("so let us"), 1, True)
        self.assertFalse(r["held"])
        self.assertEqual(r["anchor"], len(self.INTRO) + len(self.TAKE) + 2)

    def test_take_read_aloud_still_tracks(self):
        # If the presenter DOES read the take, substitution matching works
        # as usual; the broadcast anchor snaps to the next visible word.
        al, take_words = self.build()
        al.feed(bpe(" ".join(self.INTRO)), 0, True)
        r = al.feed(bpe(" ".join(self.TAKE)), 1, True)
        self.assertFalse(r["held"])
        self.assertTrue(r["moved"])
        self.assertEqual(r["anchor"], len(self.INTRO) + len(self.TAKE))
        r = al.feed(bpe(" ".join(self.OUTRO)), 2, True)
        self.assertEqual(r["anchor"],
                         len(self.INTRO) + len(self.TAKE) + len(self.OUTRO) - 1)

    def test_anchor_never_inside_take(self):
        for feeds in (
            [self.INTRO, self.OUTRO],
            [self.INTRO, self.TAKE, self.OUTRO],
            [self.INTRO, self.TAKE[:9], self.OUTRO],
        ):
            al, take_words = self.build()
            for seg, chunk in enumerate(feeds):
                # Word-by-word partials plus a final, like a real stream.
                for k in range(1, len(chunk) + 1):
                    r = al.feed(bpe(" ".join(chunk[:k])), seg, False)
                    self.assertNotIn(r["anchor"], take_words)
                r = al.feed(bpe(" ".join(chunk)), seg, True)
                self.assertNotIn(r["anchor"], take_words)

    def test_take_at_end_of_script_falls_back_to_previous_word(self):
        words = self.INTRO + self.TAKE
        ranges = [[len(self.INTRO), len(words)]]
        al, take_words = self.build(words, ranges)
        al.feed(bpe(" ".join(self.INTRO)), 0, True)
        r = al.feed(bpe(" ".join(self.TAKE)), 1, True)
        self.assertNotIn(r["anchor"], take_words)
        self.assertEqual(r["anchor"], len(self.INTRO) - 1)


class TestStopwordGate(unittest.TestCase):
    """Stopword coincidences must not move the anchor (reviewer's repro)."""

    SCRIPT = ("first we review the plan and the budget and the schedule "
              "and then we can go to the demo today").split()

    def test_stopword_dense_adlib_holds_then_recovers(self):
        al = Aligner(self.SCRIPT)
        al.feed(bpe("first we review the plan"), 0, True)
        self.assertEqual(al.anchor, 4)
        # Fully off-script, stopword-dense ad-lib: "and", "the", "we" all
        # appear ahead in the script and "other" fuzzy-matches "the"; the
        # old ratio counted those at full weight and jumped the anchor 9
        # words into unspoken script, after which the resumed read matched
        # later duplicates and compounded the drift.
        r = al.feed(bpe("and also the other thing we did"), 1, True)
        self.assertTrue(r["held"])
        self.assertEqual(r["anchor"], 4)
        # Resuming on script matches the ORIGINAL duplicates, not later
        # ones, because the anchor never drifted.
        r = al.feed(bpe("and the budget and the schedule"), 2, True)
        self.assertFalse(r["held"])
        self.assertEqual(r["anchor"], self.SCRIPT.index("schedule"))

    def test_lone_stopword_on_script_still_advances(self):
        # Small partial commits are often pure stopwords ("and the"); a
        # contiguous match at the window start is on-script continuation
        # and must not hold, or verbatim reads would stutter.
        al = Aligner(self.SCRIPT)
        al.feed(bpe("first we review the plan"), 0, True)
        r = al.feed(bpe("and the"), 1, True)
        self.assertFalse(r["held"])
        self.assertEqual(r["anchor"], 6)

    def test_garbled_content_word_still_advances(self):
        # An ASR garble of an on-script read ("workshopod" for "workshop")
        # has every content word matched, merely fuzzily; that is not an
        # ad-lib and must not hold (the verbatim capture contains exactly
        # this).
        al = Aligner("welcome to the workshop everyone".split())
        al.feed(bpe("welcome to the"), 0, True)
        r = al.feed(bpe("workshopod"), 1, True)
        self.assertFalse(r["held"])
        self.assertEqual(r["anchor"], 3)


class TestPendingClamp(unittest.TestCase):
    """feed() clamps each batch to the last 24 raw words.

    A mid-segment Aligner rebuild (script edit while a segment is in
    flight) makes the next partial commit the whole hypothesis as one
    batch; _match is quadratic in batch size, so an unclamped 300-word
    batch blocks the server's event loop for seconds.
    """

    @staticmethod
    def alpha_words(n):
        syl = [c + v for c in "bcdfghjklmnprst" for v in "aeiou"]
        return [a + b for a in syl for b in syl][:n]

    def test_300_word_batch_bounded_wall_time(self):
        words = self.alpha_words(600)
        al = Aligner(words)
        batch = bpe(" ".join(words[:300]))
        t0 = time.perf_counter()
        al.feed(batch, 0, True)
        elapsed = time.perf_counter() - t0
        # Generous CI-safe bound; unclamped this took seconds.
        self.assertLess(elapsed, 0.2)

    def test_clamp_keeps_recent_speech_matching(self):
        words = self.alpha_words(600)
        al = Aligner(words)
        al.set_anchor(275)
        # 200-word batch: only the last 24 words survive the clamp, and
        # they are exactly the recent speech that continues the anchor.
        r = al.feed(bpe(" ".join(words[100:300])), 0, True)
        self.assertFalse(r["held"])
        self.assertEqual(r["anchor"], 299)


class TestEndOfScript(unittest.TestCase):
    """Script tail plus overflow speech in one batch still anchors the tail."""

    def test_tail_and_overflow_in_same_batch(self):
        al = Aligner("hello world".split())
        r = al.feed(bpe("hello world thanks for watching everyone"), 0, True)
        self.assertFalse(r["held"])
        self.assertEqual(r["anchor"], 1)

    def test_tail_shorter_than_half_the_batch(self):
        al = Aligner("okay folks welcome back".split())
        r = al.feed(bpe("okay folks welcome back thanks so much for "
                        "watching everyone goodbye"), 0, True)
        self.assertFalse(r["held"])
        self.assertEqual(r["anchor"], 3)

    def test_overflow_after_anchored_tail_holds(self):
        al = Aligner("hello world".split())
        al.feed(bpe("hello world"), 0, True)
        r = al.feed(bpe("thanks for watching everyone"), 1, True)
        self.assertTrue(r["held"])
        self.assertEqual(r["anchor"], 1)


class FixtureCase(unittest.TestCase):
    """Shared helpers for the recorded-stream scenarios."""

    def drive(self, name):
        fx, words, ranges = load_fixture(name)
        aligner = Aligner(words, take_ranges=ranges)
        results = run_fixture(aligner, fx["events"])
        assert_monotonic(self, results)
        take_words = {i for a, b in ranges for i in range(a, b)}
        for r in results:
            self.assertNotIn(r["anchor"], take_words,
                             "anchor landed inside a take block")
        return fx, words, results


class TestVerbatimFixture(FixtureCase):

    def test_reaches_near_end_and_monotonic(self):
        fx, words, results = self.drive("verbatim")
        self.assertGreaterEqual(results[-1]["anchor"], len(words) - 3)

    def test_no_holds_on_verbatim_read(self):
        fx, words, results = self.drive("verbatim")
        self.assertEqual(sum(r["held"] for r in results), 0)


class TestAdlibFixture(FixtureCase):

    def test_holds_during_adlib_then_recovers(self):
        fx, words, results = self.drive("adlib")
        held = [r for r in results if r["held"]]
        self.assertGreaterEqual(len(held), 3,
                                "ad-lib produced no holds")
        # The ad-lib starts right after "back." in the script; while held,
        # the anchor must not run away into unspoken script.
        boundary = words.index("back.")
        for r in held:
            self.assertLessEqual(r["anchor"], boundary + 3)
        # After the ad-lib the aligner recovers to (near) the end.
        self.assertGreaterEqual(results[-1]["anchor"], len(words) - 3)


class TestSkipFixture(FixtureCase):

    def test_skip_followed_within_window(self):
        fx, words, results = self.drive("skip")
        # The middle sentence (ending at "key.") was never spoken; the
        # anchor must jump across it and reach near the end anyway.
        skipped_end = words.index("key.")
        self.assertGreater(results[-1]["anchor"], skipped_end)
        self.assertGreaterEqual(results[-1]["anchor"], len(words) - 3)


class TestDigitsFixture(FixtureCase):

    def test_numbers_do_not_stall(self):
        fx, words, results = self.drive("digits")
        anchors = [r["anchor"] for r in results]
        # Anchor moves past "90" (spoken "ninety") and past "2026" (spoken
        # "twenty twenty six") and reaches near the end.
        self.assertGreater(max(anchors), words.index("90"))
        self.assertGreater(max(anchors), words.index("2026"))
        self.assertGreaterEqual(results[-1]["anchor"], len(words) - 3)


class TestTailRevisionFixture(FixtureCase):

    def test_fixture_contains_word_level_tail_revisions(self):
        fx, words, _ = load_fixture("tailrev")
        prev = {}
        revisions = 0
        for e in fx["events"]:
            merged = merge_bpe(e["tokens"])
            before = prev.get(e["segment"], [])
            if merged[:len(before)] != before:
                revisions += 1
            prev[e["segment"]] = merged
        self.assertGreaterEqual(revisions, 1)

    def test_anchor_never_exceeds_then_retreats(self):
        fx, words, results = self.drive("tailrev")
        self.assertGreaterEqual(results[-1]["anchor"], len(words) - 3)


class TestStopwordAdlibFixture(FixtureCase):
    """Captured stream: stopword-dense ad-lib after 'plan' in a script full
    of forward stopword duplicates (the drift reproduction from review)."""

    def test_holds_through_adlib_without_drift(self):
        fx, words, results = self.drive("stopword")
        self.assertGreaterEqual(sum(r["held"] for r in results), 1)
        # While the ad-lib plays (segment 0 carries it), the anchor must
        # not run ahead of the resume point: "budget" and everything after
        # are unspoken until segment 1.
        budget = words.index("budget")
        seg0 = [r for r, e in zip(results, fx["events"])
                if e["segment"] == 0]
        for r in seg0:
            self.assertLess(r["anchor"], budget)
        # The resumed read recovers to the end of the script.
        self.assertGreaterEqual(results[-1]["anchor"], len(words) - 3)


class TestTakeSkipFixture(FixtureCase):
    """Captured stream: a 28-word TAKE paragraph (longer than the window)
    between intro and outro, skipped aloud by the reader."""

    def test_take_crossed_and_never_entered(self):
        fx, words, results = self.drive("takeskip")
        # drive() already asserts the anchor is never inside the take.
        _, _, ranges = load_fixture("takeskip")
        self.assertEqual(len(ranges), 1)
        take_start, take_end = ranges[0]
        self.assertGreater(take_end - take_start, 20)
        anchors = [r["anchor"] for r in results]
        self.assertGreaterEqual(max(anchors), take_end)
        self.assertGreaterEqual(results[-1]["anchor"], len(words) - 3)
        # The outro is followed promptly: no holds after crossing the take.
        crossed = next(i for i, a in enumerate(anchors) if a >= take_end)
        for r in results[crossed:]:
            self.assertFalse(r["held"])


class TestSetAnchorRecovery(FixtureCase):

    def test_backward_jump_then_reread_recovers(self):
        fx, words, _ = load_fixture("verbatim")
        aligner = Aligner(words)
        run_fixture(aligner, fx["events"])
        self.assertGreaterEqual(aligner.anchor, len(words) - 3)
        aligner.set_anchor(5)
        self.assertEqual(aligner.anchor, 5)
        # Re-read the same passage (new segment ids simulate the re-take).
        results = [aligner.feed(e["tokens"], e["segment"] + 100, e["final"])
                   for e in fx["events"]]
        assert_monotonic(self, results)
        self.assertGreaterEqual(results[-1]["anchor"], len(words) - 3)

    def test_manual_jump_at_skip_point(self):
        fx, words, _ = load_fixture("skip")
        aligner = Aligner(words)
        events = fx["events"]
        first_final = next(i for i, e in enumerate(events) if e["final"])
        for e in events[:first_final + 1]:
            aligner.feed(e["tokens"], e["segment"], e["final"])
        # The creator clicks the last word of the skipped sentence.
        target = words.index("key.")
        aligner.set_anchor(target)
        self.assertEqual(aligner.anchor, target)
        results = [aligner.feed(e["tokens"], e["segment"], e["final"])
                   for e in events[first_final + 1:]]
        assert_monotonic(self, results)
        self.assertGreaterEqual(results[-1]["anchor"], len(words) - 3)


class TestFixtureHygiene(unittest.TestCase):
    """The committed fixtures stay CI-safe and schema-correct."""

    NAMES = ("verbatim", "adlib", "skip", "digits", "tailrev",
             "stopword", "takeskip")

    def test_schema_and_size(self):
        for name in self.NAMES:
            path = FIXTURES / f"{name}.json"
            self.assertLess(path.stat().st_size, 100_000)
            fx = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("script", fx)
            self.assertIn("events", fx)
            self.assertIn("notes", fx)
            for e in fx["events"]:
                self.assertEqual(set(e), {"tokens", "segment", "final"})
                self.assertIsInstance(e["tokens"], list)
                self.assertIsInstance(e["segment"], int)
                self.assertIsInstance(e["final"], bool)

    def test_segments_are_monotonic_in_every_stream(self):
        for name in self.NAMES:
            fx = json.loads((FIXTURES / f"{name}.json")
                            .read_text(encoding="utf-8"))
            segs = [e["segment"] for e in fx["events"]]
            self.assertEqual(segs, sorted(segs))


if __name__ == "__main__":
    unittest.main()
