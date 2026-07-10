#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Voice-follow alignment engine for mc-prompter (Phase B). Pure stdlib.

Consumes the streaming-transducer partial hypothesis stream (BPE token lists
per segment, as emitted by server/asr.py) and maintains a monotonic committed
anchor into the script's speakable words (the same global word indexing the
UI renders as data-i spans; see script_ingest.speakable_words()).

    aligner = Aligner(speakable_words(doc),
                      take_ranges=take_word_ranges(doc))
    result = aligner.feed(tokens, segment, final)
    # result == {"anchor": int, "moved": bool, "held": bool}
    aligner.set_anchor(word_index)   # human override, backwards allowed
    aligner.anchor                   # committed global word index, -1 initially

take_ranges is the list of [start, end) global word-index pairs for the
script's take blocks (script_ingest.take_word_ranges, same indexing as
speakable_words). Take paragraphs are already-recorded footage: they are
dimmed or hidden in the UI and a presenter normally skips them aloud, so the
aligner treats their words as free to skip (see Matching below).

Input contract (matches the observed nemotron-streaming behavior recorded in
the Phase B contract and the committed fixtures):

    feed() receives the FULL hypothesis token list for the current segment on
    every partial. Hypotheses grow incrementally but the tail revises at the
    word level (BPE pieces append into the previous word, e.g. "workshop"
    becoming "workshopod"), so the last k_provisional merged words are treated
    as provisional and never advance the committed anchor. On final=True the
    whole hypothesis commits. A new segment id resets hypothesis tracking
    (endpoint reset cleared the recognizer state) but never rewinds the
    committed anchor. Timestamps are not used.

Matching:

    BPE pieces merge to words (a piece starting with a space, or the first
    piece, starts a new word). Both script words and hypothesis words are
    normalized: casefold, punctuation stripped, hyphen/slash split, and a
    small documented number/ordinal table expands digit forms to spoken words
    on BOTH sides, so script "2026" matches spoken "twenty twenty six" and a
    recognizer that emits digits still matches a written-out script.

    Newly committed hypothesis words (pending) are matched against a script
    window starting just past the committed anchor, window = window_mult *
    len(pending) + window_base expanded NON-TAKE tokens; take-block tokens
    inside that span ride along for free, so a take paragraph of any length
    never consumes window budget and the words after it stay reachable. A
    word-level Levenshtein DP picks the window prefix end with minimum
    distance. Per-word substitution cost: exact 0, prefix or difflib-fuzzy
    0.5, else 1. Insertion of an unmatched spoken word costs 1; deletion of
    a script window word costs 0.25, except take-block words which delete
    at cost 0 (the presenter is expected to skip them; if they DO read the
    take aloud, substitution matching inside the take still works and the
    anchor tracks through it). Deletions must be cheap: at cost 1, aligning
    the pending words after N skipped script words (cost N) always loses to
    matching nothing (cost len(pending)), so skips and recognizer word
    drops would stall the anchor forever. At 0.25 a deliberate skip is
    followed within the window while spurious far matches stay unprofitable
    (a fuzzy match only pays for itself within about two words of
    deletion). The anchor advances to the window position of the last
    pending word that aligned at cost <= 0.5. The committed anchor word is
    never a take-block word: if the last match lands inside a take (the
    take was read aloud, or a word fuzzy-matched into it), the broadcast
    anchor advances to the next non-take word so the UI always has a
    visible span to scroll to (falling back to the previous non-take word
    when the take runs to the end of the script).

    Hold rule (the gate): off-script speech must not move the anchor. A
    match only counts toward the gate if the spoken token is informative:
    length >= 4, or not in the small stopword set below. Conversational
    ad-libs are stopword-dense and scripts are full of forward stopword
    duplicates, so stopword coincidences ("and", "the", "we") must not be
    evidence of progress. When the pending batch contains informative
    tokens, moving requires (a) at least one informative token matched
    EXACTLY (cost 0), and (b) informative-matches / (informative-pending +
    interior-deletions) >= hold_threshold, where interior-deletions counts
    unmatched non-take window words strictly between the first and last
    matched position. Charging interior deletions makes scattered
    coincidence matches fail (they straddle unmatched script) while a
    genuine skip still passes (its matches are contiguous, the skipped
    words sit BEFORE the first match). A batch of only stopwords (common in
    small partial commits: "and the") falls back to the same ratio over all
    pending words, so verbatim reading still advances promptly. When the
    gate fails, the anchor does not move and held=True is returned. Held
    pending words are consumed, so the window never fills with off-script
    speech and the next on-script words re-match from the committed anchor.

    End of script: when the window is truncated by the document end and the
    gate fails, the batch may be the script tail plus overflow speech
    committed together in one final ("...thanks for watching" continuing
    past the last script word). Overflow past the end cannot match
    anything, so the gate is retried over only the pending words up to the
    last matched one; if that passes, the tail anchors instead of holding
    forever.

    feed() clamps each batch to the last 24 RAW words before normalization.
    Pending is normally 1-5 words, but a mid-segment Aligner rebuild
    (script edit while a segment is in flight) resets _consumed and the
    next partial commits the whole hypothesis so far as one batch; _match
    is O(len(pending) * window) with window proportional to pending, i.e.
    quadratic, and a 300-word batch would block the asyncio event loop for
    seconds. Older words are stale context anyway: the window matcher only
    needs recent speech. The clamp is on raw words (before number expansion
    multiplies tokens) so the DP input is bounded no matter what the words
    expand to.

    set_anchor() jumps anywhere, backwards included (the human is the
    authority), and consumes the current hypothesis so already-spoken words
    are not re-matched against the new position.

No I/O, no threads, no imports beyond stdlib. server/main.py owns the wiring
(ASR events in, anchor broadcasts out).
"""

import difflib

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven",
         "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
         "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty",
         7: "seventy", 8: "eighty", 9: "ninety"}
# Small ordinal table: the common single-word ordinals. Larger ordinals fall
# back to cardinal expansion of the digits ("42nd" -> "forty two"), close
# enough for the fuzzy per-word cost.
_ORDINALS = {"1st": "first", "2nd": "second", "3rd": "third", "4th": "fourth",
             "5th": "fifth", "6th": "sixth", "7th": "seventh",
             "8th": "eighth", "9th": "ninth", "10th": "tenth",
             "11th": "eleventh", "12th": "twelfth"}

_FUZZY_RATIO = 0.75
_FUZZY_COST = 0.5
_DELETE_COST = 0.25  # script window word skipped; see the module docstring
_TAKE_DELETE_COST = 0.0  # take-block words are free to skip

# Apostrophe-family characters stripped inside words so "don't", "don’t" and
# "donʼt" all normalize to "dont". U+02BC (modifier letter apostrophe) is
# Unicode category Lm and would otherwise survive isalnum() as part of the
# token; the curly quotes would split the word instead.
_APOSTROPHES = "'’ʼ‘‛"

# Uninformative tokens for the hold gate: a match on one of these (or any
# token shorter than 4 characters that IS in this set) is a coincidence, not
# evidence the presenter is on script. Deliberately tiny and limited to
# common function words of length <= 3 (length >= 4 already counts as
# informative regardless of this set).
_STOPWORDS = frozenset(
    "a an and are as at be but by do for he i in is it me my no not of on "
    "or so the to us was we you".split())

# feed() batch clamp, in raw (pre-normalization) words; see the module
# docstring ("feed() clamps each batch...").
_PENDING_CAP = 24


def _informative(token):
    """True when a match on this token counts toward the hold gate."""
    return len(token) >= 4 or token not in _STOPWORDS


def merge_bpe(tokens):
    """Merge BPE pieces into words.

    A piece starting with a space, or the first piece, starts a new word;
    every other piece (including bare punctuation pieces like "," and ".")
    appends to the current word. Empty results are dropped.
    """
    words = []
    for piece in tokens:
        if piece.startswith(" ") or not words:
            words.append(piece.strip())
        else:
            words[-1] += piece.strip()
    return [w for w in words if w]


def _expand_number(digits):
    """Expand a digit string to spoken words (the documented small table).

    0-19 and tens from the ones/tens tables; 100-999 as "N hundred [rest]";
    1000-9999 read as digit pairs the way years are spoken ("2026" ->
    "twenty twenty six", "1995" -> "nineteen ninety five"), with the special
    cases "2000" -> "two thousand", "1900" -> "nineteen hundred", "2007" ->
    "two thousand seven", "1907" -> "nineteen oh seven"; 10000-999999 as
    "N thousand [rest]"; anything larger digit by digit.
    """
    n = int(digits)
    if n < 20:
        return [_ONES[n]]
    if n < 100:
        tens, ones = divmod(n, 10)
        return [_TENS[tens]] + ([_ONES[ones]] if ones else [])
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return [_ONES[hundreds], "hundred"] + (_expand_number(str(rest)) if rest else [])
    if n < 10000:
        hi, lo = divmod(n, 100)
        if lo == 0 and hi % 10 == 0:
            return [_ONES[hi // 10], "thousand"]
        if lo == 0:
            return _expand_number(str(hi)) + ["hundred"]
        if hi % 10 == 0 and lo < 10:
            return [_ONES[hi // 10], "thousand"] + _expand_number(str(lo))
        if lo < 10:
            return _expand_number(str(hi)) + ["oh"] + _expand_number(str(lo))
        return _expand_number(str(hi)) + _expand_number(str(lo))
    if n < 1_000_000:
        thousands, rest = divmod(n, 1000)
        return (_expand_number(str(thousands)) + ["thousand"]
                + (_expand_number(str(rest)) if rest else []))
    return [_ONES[int(d)] for d in digits]


def normalize_word(word):
    """Normalize one raw word to a list of match tokens (possibly empty).

    Casefold; "%" becomes the word "percent"; the apostrophe family
    (_APOSTROPHES: ASCII, curly, and U+02BC modifier letter) is removed so
    "don't", "don’t", "donʼt" and "dont" all match; commas inside digit
    runs are removed; every
    other non-alphanumeric character splits the word (hyphens, slashes,
    trailing punctuation). Digit pieces expand through the number table;
    digit+ordinal-suffix pieces go through the ordinal table; mixed pieces
    split at digit/letter boundaries.
    """
    w = word.casefold().replace("%", " percent ")
    for ch in _APOSTROPHES:
        w = w.replace(ch, "")
    # Drop commas used as thousands separators before they split the number.
    w = "".join(ch for i, ch in enumerate(w)
                if not (ch == "," and 0 < i < len(w) - 1
                        and w[i - 1].isdigit() and w[i + 1].isdigit()))
    pieces = []
    buf = ""
    for ch in w:
        if ch.isalnum():
            buf += ch
        elif buf:
            pieces.append(buf)
            buf = ""
    if buf:
        pieces.append(buf)

    out = []
    for piece in pieces:
        if piece in _ORDINALS:
            out.append(_ORDINALS[piece])
            continue
        if piece.isdigit():
            out.extend(_expand_number(piece))
            continue
        if piece.isalpha():
            out.append(piece)
            continue
        # Mixed digits and letters: split at boundaries ("90s", "42nd").
        if piece[:-2].isdigit() and piece[-2:] in ("st", "nd", "rd", "th"):
            out.extend(_expand_number(piece[:-2]))
            continue
        run = ""
        for ch in piece:
            if run and ch.isdigit() != run[-1].isdigit():
                out.extend(_expand_number(run) if run.isdigit() else [run])
                run = ""
            run += ch
        if run:
            out.extend(_expand_number(run) if run.isdigit() else [run])
    return out


def _word_cost(a, b):
    """Per-word similarity cost: exact 0, prefix/fuzzy 0.5, else 1."""
    if a == b:
        return 0.0
    if len(a) >= 3 and len(b) >= 3 and (a.startswith(b) or b.startswith(a)):
        return _FUZZY_COST
    if difflib.SequenceMatcher(None, a, b).ratio() >= _FUZZY_RATIO:
        return _FUZZY_COST
    return 1.0


def _match(pending, window, take_flags=None):
    """Word-level Levenshtein of pending against the best window prefix.

    take_flags marks window positions that belong to take blocks; those
    delete at _TAKE_DELETE_COST (0) so a skipped take costs nothing to
    cross. Returns (matches, best_distance) where matches is a list of
    (i, j, c) triples: pending[i] aligned to window[j] at cost c <= 0.5 on
    the minimum-cost path to the best prefix end (ties break to the
    shortest prefix; a substitution tie beats a free take deletion, so a
    take read aloud still registers its matches).
    """
    m, n = len(pending), len(window)
    if take_flags is None:
        take_flags = [False] * n
    dele = [_TAKE_DELETE_COST if t else _DELETE_COST for t in take_flags]
    dist = [[0.0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        dist[i][0] = float(i)
    for j in range(1, n + 1):
        dist[0][j] = dist[0][j - 1] + dele[j - 1]
    cost = [[0.0] * n for _ in range(m)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            c = _word_cost(pending[i - 1], window[j - 1])
            cost[i - 1][j - 1] = c
            dist[i][j] = min(dist[i - 1][j - 1] + c,
                             dist[i - 1][j] + 1.0,
                             dist[i][j - 1] + dele[j - 1])
    best_j = min(range(n + 1), key=lambda j: (dist[m][j], j))
    matches = []
    i, j = m, best_j
    while i > 0 and j > 0:
        c = cost[i - 1][j - 1]
        if dist[i][j] == dist[i - 1][j - 1] + c:
            if c <= _FUZZY_COST:
                matches.append((i - 1, j - 1, c))
            i, j = i - 1, j - 1
        elif dist[i][j] == dist[i][j - 1] + dele[j - 1]:
            j -= 1
        else:
            i -= 1
    matches.reverse()
    return matches, dist[m][best_j]


class Aligner:
    """Monotonic committed-anchor aligner over the script's speakable words."""

    def __init__(self, words, k_provisional=4, window_base=10, window_mult=2,
                 hold_threshold=0.5, take_ranges=None):
        self.k_provisional = k_provisional
        self.window_base = window_base
        self.window_mult = window_mult
        self.hold_threshold = hold_threshold
        # Take-block words (script_ingest.take_word_ranges, [start, end)
        # pairs in the same global word indexing as `words`).
        self._take_words = set()
        for a, b in (take_ranges or []):
            self._take_words.update(range(max(0, a), min(b, len(words))))
        # Expanded script: flat normalized tokens, each carrying its source
        # global word index (number expansion makes this one-to-many).
        self._exp = []
        self._word_last_exp = []
        last = -1
        for i, word in enumerate(words):
            for tok in normalize_word(word):
                self._exp.append((tok, i))
                last = len(self._exp) - 1
            self._word_last_exp.append(last)
        self._exp_take = [i in self._take_words for _, i in self._exp]
        self._n_words = len(words)
        self._exp_anchor = -1
        self._anchor_word = -1
        self._segment = None
        self._consumed = 0
        self._last_len = 0

    @property
    def anchor(self):
        """Committed global word index, -1 before any match."""
        return self._anchor_word

    def set_anchor(self, word_index):
        """Jump the anchor anywhere (backwards allowed) and clear tracking.

        The current hypothesis is consumed so already-spoken words are not
        re-matched against the new position; the next feed matches only
        newly committed words.
        """
        word_index = max(-1, min(int(word_index), self._n_words - 1))
        if word_index < 0:
            self._exp_anchor = -1
            self._anchor_word = -1
        else:
            self._exp_anchor = self._word_last_exp[word_index]
            self._anchor_word = word_index
        # Consume everything already heard in the current segment so the
        # next feed matches only words spoken after the jump.
        self._consumed = self._last_len

    def feed(self, tokens, segment, final):
        """Consume one partial or final hypothesis for a segment.

        tokens is the full BPE token list for the segment's current
        hypothesis. Returns {"anchor": int, "moved": bool, "held": bool}.
        """
        words = merge_bpe(tokens)
        if segment != self._segment:
            self._segment = segment
            self._consumed = 0
        self._last_len = len(words)
        if final:
            commit = len(words)
        else:
            commit = max(self._consumed, len(words) - self.k_provisional)
        raw_pending = words[self._consumed:commit]
        self._consumed = max(self._consumed, commit)
        # Clamp to the last _PENDING_CAP raw words: a mid-segment Aligner
        # rebuild (script edit) resets _consumed and would otherwise commit
        # the whole hypothesis as one batch, and _match is quadratic in the
        # batch size (a 300-word batch blocks the event loop for seconds).
        # Raw words, not normalized tokens, so number expansion cannot
        # reinflate the bound; the dropped words are stale context the
        # window matcher does not need.
        raw_pending = raw_pending[-_PENDING_CAP:]

        pending = []
        for w in raw_pending:
            pending.extend(normalize_word(w))

        moved = False
        held = False
        if pending:
            start = self._exp_anchor + 1
            width = self.window_mult * len(pending) + self.window_base
            end, truncated = self._window_end(start, width)
            window_toks = [tok for tok, _ in self._exp[start:end]]
            take_flags = self._exp_take[start:end]
            matches, _ = _match(pending, window_toks, take_flags)
            ok = self._gate_passes(pending, matches, take_flags)
            if not ok and truncated and matches:
                # Window truncated by document end: the batch may be the
                # script tail plus overflow speech. Overflow past the end
                # cannot match anything, so retry the gate over only the
                # pending words up to the last matched one.
                last_i = max(i for i, _, _ in matches)
                ok = self._gate_passes(pending[:last_i + 1], matches,
                                       take_flags)
            if not ok:
                held = True
            elif matches:
                new_exp = start + max(j for _, j, _ in matches)
                if new_exp > self._exp_anchor:
                    self._exp_anchor = new_exp
                    word = self._exp[new_exp][1]
                    # Never broadcast a take-block word as the anchor: the
                    # UI dims or hides takes, so snap to the next visible
                    # word (max() keeps the anchor monotonic when the
                    # fallback resolves backwards at end of script).
                    self._anchor_word = max(self._anchor_word,
                                            self._visible_word(word))
                    moved = True
        return {"anchor": self._anchor_word, "moved": moved, "held": held}

    def _window_end(self, start, width):
        """Window end so [start:end) holds `width` non-take tokens.

        Take-block tokens ride along for free (they delete at cost 0 in
        _match), so a take span of any length never consumes window budget
        and the script after it stays reachable. Returns (end, truncated)
        where truncated means the document ended before the budget filled.
        """
        n = len(self._exp)
        end = start
        budget = width
        while end < n and budget > 0:
            if not self._exp_take[end]:
                budget -= 1
            end += 1
        return end, budget > 0

    def _gate_passes(self, pending, matches, take_flags):
        """The hold gate: is this batch evidence of on-script progress?

        Only informative matches count (see _informative); when the batch
        has informative tokens, at least one must match exactly. Unmatched
        non-take window words strictly between the first and last matched
        position (interior deletions) are charged against the ratio, so
        scattered stopword coincidences fail while a genuine contiguous
        skip (deletions before the first match) passes. See the module
        docstring, Hold rule.
        """
        if not matches:
            return False
        inf_idx = {k for k, tok in enumerate(pending) if _informative(tok)}
        if inf_idx:
            relevant = [m for m in matches if m[0] in inf_idx]
            # Require an exact informative match, UNLESS every informative
            # pending token matched: a batch whose content words all match,
            # merely fuzzily, is an ASR garble of an on-script read (the
            # recorded streams produce e.g. "workshopod" for "workshop"),
            # not an ad-lib; off-script speech carries unmatched content
            # words and fails here or on the ratio below.
            if (not any(c == 0.0 for _, _, c in relevant)
                    and {m[0] for m in relevant} != inf_idx):
                return False
            num = len(relevant)
            denom = len(inf_idx)
        else:
            num = len(matches)
            denom = len(pending)
        matched_j = {j for _, j, _ in matches}
        interior = sum(1 for j in range(min(matched_j) + 1, max(matched_j))
                       if j not in matched_j and not take_flags[j])
        return num / (denom + interior) >= self.hold_threshold

    def _visible_word(self, word):
        """Snap a take-block word to the nearest visible (non-take) word.

        Prefers the next non-take word (the one the presenter reads next,
        and the one the UI can scroll to); falls back to the previous one
        when the take runs to the end of the script. Returns -1 only when
        the whole script is take blocks.
        """
        if word not in self._take_words:
            return word
        w = word + 1
        while w < self._n_words and w in self._take_words:
            w += 1
        if w < self._n_words:
            return w
        w = word - 1
        while w >= 0 and w in self._take_words:
            w -= 1
        return w
