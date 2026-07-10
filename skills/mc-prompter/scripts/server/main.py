#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp==3.12.15"]
# ///
"""mc-prompter aiohttp server (Phase A prompter + Phase B voice-follow + Phase C producer).

Launched by run_prompter.py as `python -m server.main` with cwd set to the
scripts directory (the PEP 723 header above also allows a direct
`uv run main.py` for debugging). All arguments are explicit; the server does
no config discovery.

Usage:
    python -m server.main --port 8770 --host 127.0.0.1|0.0.0.0
        --script <path or empty> --owner-wpm <int or 0>
        --session-file <path or empty> --token <hex>
        [--models-dir <dir or empty>] [--asr-provider none|nemotron-streaming]
        [--rundown <path or empty>] [--llm-provider none|ollama]
        [--llm-endpoint <url>] [--llm-model <tag>]
        [--cue-density hands-off|minimal|normal|chatty]

ASR (Phase B): when --models-dir is set AND --asr-provider is an implemented
provider (nemotron-streaming), server/asr.py is imported lazily and an
AsrEngine runs on a dedicated worker thread; server/align.py is imported
lazily too and an Aligner is rebuilt from script_ingest.speakable_words(doc)
on every script load or edit. Without --models-dir (or with provider none)
the server is pure tier 1: neither asr nor align nor numpy is ever imported,
and it runs with only aiohttp installed. Provider zipformer-small is a
documented planned lane: selecting it exits 3 at startup, it never pretends
to run.

HTTP routes:
    GET  /            home page (static/home.html)
    GET  /prompt      prompter display (static/prompt.html)
    GET  /remote      phone remote (static/remote.html)
    GET  /overlay     OBS overlay (static/overlay.html)
    GET  /static/*    static assets
    GET  /health      identity JSON, no auth:
                      {"app": "mc-prompter", "version": "0.1.0",
                       "session": "<token first 8 chars>",
                       "script": "<basename or null>", "port": N,
                       "started": "<ISO>"}
    GET  /api/state   {"snapshot": <last state or null>, "doc-version": n,
                       "script": {"path", "title", "word-count"} or null,
                       "config": {"owner-wpm": N or null},
                       "asr": {"available": bool, "provider": str,
                               "ready": bool}}
    GET  /api/source  {"path": <abs or null>, "raw": "<text>",
                       "doc": <script model>, "doc-version": n}
    POST /api/source  {"raw": "<text>", "save": bool} re-ingests, bumps
                      doc-version, broadcasts doc-updated; save copies the
                      current file to <tempdir>/mc-prompter/backups/
                      <basename>.<ISO-compact>.md first, then writes raw to
                      the loaded path. Responds {"doc-version": n,
                      "backup": <path or null>}. Loopback only.
    POST /api/source/load  {"path": "<abs path>"} validates a regular file,
                      loads, ingests, bumps, broadcasts. 400 on a bad path.
                      Loopback only.
    GET  /ws          WebSocket upgrade.

Auth rule: loopback peers are trusted. Non-loopback WS connects must present
the session token as ?token= or are closed with code 4403. Non-loopback
GET /api/state and GET /api/source require the same token (401 otherwise).
Both POST endpoints refuse non-loopback callers outright (403), so a LAN
device can never read or write arbitrary files.

Producer mode (Phase C): --rundown loads a rundown file (parsed lazily via
server/rundown.py). --rundown is an alternative to --script and wins when
both are given: producer state comes from the rundown and the promptable
document is the concatenation of the rundown's SCRIPTED segment bodies
(each under its segment heading), ingested through script_ingest; bullets
segments contribute no words. A bullets-only rundown contributes no doc
at all, so with --script the script text stays on the scroll (identical
to loading the same rundown at runtime over a loaded script). Each scripted segment maps to a global
word-index range so the UI can place the rail against the scroll:
    GET /api/rundown  {"rundown": <parse_rundown result or null>,
                       "segments": [{"id", "word-start", "word-end"}]}
                      (same read auth as /api/state)
/api/state gains "producer": {"active": bool, "live": bool,
"llm": {"provider": str, "model": str, "ok": bool}} where ok reports the
last LLM tick outcome (false until one succeeds).

The producer loop (asyncio task, only when a rundown is loaded) calls
producer.tick() every PRODUCER_TICK_S (1 s) and broadcasts
{"type": "producer", "state": <rail state>} ONLY when the state changed.
The cue engine merges the producer's deterministic cue candidates with the
optional LLM-proposed cue and enforces: one active cue, the per-density
card budget (hands-off: attention only; minimal: 1 card/5 min; normal:
1/2 min; chatty: 1/45 s; attention exempt), release at a VAD pause (from
the Phase B vad events; immediate release when no ASR runs), 15 s
auto-expiry, and dedup by candidate key. Wire frames:
    {"type": "cue", "id": n, "tier": "card"|"attention", "text": str}
    {"type": "cue-clear", "id": n}
Keyword first-pass coverage runs deterministically on every tick WHILE THE
SHOW IS LIVE and not on hold: the last 90 s of FINAL transcript text
(bounded buffer) is compared against each uncovered point's informative
words; at >= 60 percent overlap the point is proposed covered. Off-air
speech can never cover a point: when a producer is active the transcript
buffer itself only collects finals while live and not held (pre-show mic
checks, hold banter, and post-show chat never enter it), and the buffer is
cleared whenever the producer stack is (re)built so speech captured before
a runtime rundown load cannot leak in either. The adaptive LLM tick
(next = max(15 s, 3 * last wall time), skipped while the ASR engine
reports behind, before go-live, while the show is held, and after end)
refines it: coverage proposals at confidence >= 0.7 go through
producer.propose_coverage (uncovered -> covered only, human stays
authoritative).

Voice-follow and the producer rail cooperate on segment handoffs: while
the producer's CURRENT segment is a bullets segment the aligner is fed
NOTHING (bullets contribute no words to the doc, so any anchor motion
during them would be creep into the next scripted segment; ASR broadcasts
and the transcript buffer keep running, the anchor simply holds). When a
point make-current lands on a SCRIPTED segment, the server re-anchors the
aligner to that segment's word-start minus 1 (clamped to -1) and
broadcasts the fresh anchor frame, so creep into future segments is
impossible and recovery after a bullets segment or a backwards jump is
deterministic. make-current is the only segment-transition command on the
wire (the UI's anchor-driven handoff sends it too), so this one hook
covers every transition.

POST /api/rundown/load {"path": ...} adopts a rundown at runtime
(loopback only). While the show is LIVE (and not ended) the load is
refused with 409 so a mid-show reload can never wipe the clock and the
coverage judgments; {"force": true} overrides. Every (re)build resets
llm-ok and clears any active cue from the previous engine.

Phase C WS extensions (from ANY authenticated client; the remote is the
primary user):
    {"type": "show", "cmd": "go-live"|"hold"|"resume"|"end"}
    {"type": "point", "cmd": "covered"|"skip"|"make-current",
     "segment": "<seg id>", "point": <idx, optional for make-current>}
Both answer with an error frame when producer mode is not active; the
resulting rail state is re-broadcast immediately.

Page routes answer 503 text/plain when the static HTML is not built yet, so
the API surface is testable independently of the UI.

WebSocket protocol (JSON text frames; binary frames carry PCM16 audio from
the capture owner, see below):
    hello (first frame, from client):
        {"type": "hello", "role": "prompt"|"remote"|"overlay"|"home",
         "token": "<required from non-loopback, also accepted as ?token=>"}
    welcome (server): {"type": "welcome", "session": "<8 chars>",
        "leader": bool, "doc-version": n, "snapshot": <last or null>}
    cmd (remote/home, relayed to ALL connections, stamped with "from"):
        {"type": "cmd", "cmd": "play"|"pause"|"toggle"|"restart"|
         "speed-delta"|"speed-set"|"jump-section"|"jump-words"|"countdown",
         "value": <optional>}  the server relays without interpreting.
    state (leader prompt page only, cached and fanned out to every OTHER
        connection): {"type": "state", "position": <0..1>, "section": ...,
        "playing": bool, "wpm": N, "mode": "manual"|"timed", "elapsed": s,
        "remaining": <s or null>, "countdown": <s or null>}
    doc-updated (server broadcast): {"type": "doc-updated", "doc-version": n}
        when voice-follow is active it is followed by an anchor reset
        {"type": "anchor", "i": -1, "held": false}: the doc change rebuilt
        the aligner, so any previously broadcast anchor is stale.
    error (server): {"type": "error", "message": "..."}
State frames from non-leaders are ignored. The first prompt-role connection
is leader; on leader disconnect the oldest remaining prompt connection is
promoted and told {"type": "role", "leader": true}.

Phase B WS extensions:
    capture ownership (audio producer role, at most ONE connection):
        {"type": "capture-request"} from a loopback client is answered with
        {"type": "capture-granted"} or {"type": "capture-denied",
        "reason": "owned"}. Non-loopback requests are denied with
        {"reason": "loopback-only"}; when ASR is off (no engine) requests
        are denied with {"reason": "asr-off"} so audio never streams into a
        black hole. Ownership releases on {"type": "capture-release"} or
        disconnect; a request that finds the current owner's socket already
        closed (hard-crashed page awaiting heartbeat reaping) reclaims
        ownership immediately instead of denying.
    binary frames: raw little endian PCM16 mono 16 kHz, any length (the
        browser sends ~3840 bytes per 120 ms). Accepted ONLY from the
        current capture owner and fed to the ASR engine. Frames from a
        non-owner get one error frame, then are silently dropped.
    {"type": "anchor-set", "i": <global word index>} from any client jumps
        the aligner anchor (the human is the authority) and broadcasts the
        new anchor. When voice-follow is not active it gets an error frame.
    server broadcasts to ALL clients:
        {"type": "asr", "kind": "partial"|"final", "segment": n,
         "text": str}                       (tokens are omitted on the wire)
        {"type": "vad", "speaking": bool}   on speaking transitions
        {"type": "anchor", "i": <committed global word index>,
         "held": bool}                      whenever it changes
        {"type": "asr-status", "ready": bool, "behind": bool, "queue": int}
                                            on ready/behind changes

Exit codes: 0 ok, 2 usage, 3 planned asr or llm provider selected,
4 script path, rundown path, or models dir missing/unreadable/unparseable.
"""

import argparse
import asyncio
import collections
import contextlib
import datetime
import hmac
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from aiohttp import WSMsgType, web

try:
    from server import script_ingest
except ImportError:  # direct `uv run main.py` from the server directory
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import script_ingest

APP_NAME = "mc-prompter"
VERSION = "0.1.0"
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "static"
PAGES = {
    "/": "home.html",
    "/prompt": "prompt.html",
    "/remote": "remote.html",
    "/overlay": "overlay.html",
}
WS_ROLES = ("prompt", "remote", "overlay", "home")
LOOPBACK_PEERS = ("127.0.0.1", "::1", "localhost")
ASR_PROVIDERS = ("none", "nemotron-streaming")
ASR_PLANNED_PROVIDERS = ("zipformer-small",)
LLM_PROVIDERS = ("none", "ollama")
DEFAULT_LLM_ENDPOINT = "http://localhost:11434"
DEFAULT_LLM_MODEL = "qwen3:4b"
CUE_DENSITIES = ("hands-off", "minimal", "normal", "chatty")
DEFAULT_CUE_DENSITY = "normal"
# Card-tier budget per density: minimum seconds between cards. None means
# no card is ever shown (hands-off is attention-tier only). The attention
# tier is exempt from the budget by contract.
CUE_DENSITY_BUDGET_S = {
    "hands-off": None,
    "minimal": 300.0,
    "normal": 120.0,
    "chatty": 45.0,
}
CUE_EXPIRY_S = 15.0
PRODUCER_TICK_S = 1.0
LLM_MIN_INTERVAL_S = 15.0
LLM_INTERVAL_MULT = 3.0
LLM_CONFIDENCE_FLOOR = 0.7
# Shutdown grace for a cancelled task stuck on the uninterruptible urllib
# worker; past it the task is abandoned (see stop_producer).
LLM_STOP_GRACE_S = 2.0
TRANSCRIPT_WINDOW_S = 90.0
KEYWORD_OVERLAP_FLOOR = 0.6
# Small function-word set for the keyword first-pass; anything not listed
# and at least 3 characters long counts as informative.
KEYWORD_STOPWORDS = frozenset(
    "the a an and or but of to in on for with is are was were be been being "
    "this that these those it its as at by from we you i he she they them "
    "our your my his her their not no yes so if then than there here what "
    "when where which who how why do does did done can could will would "
    "should about into over under again more most some any all just very "
    "have has had get got one two also because".split()
)
# Keyword-pass text normalization, mirroring align.normalize_word so point
# text and ASR transcript text tokenize the same way (house rules allow
# duplicating the small tables instead of importing align, which needs
# numpy and must never load in tier 1). The apostrophe family collapses
# possessives regardless of the editor's quote style; digit runs expand to
# their spoken words so "4090" in a point matches the ASR's "forty ninety".
KEYWORD_APOSTROPHES = "'’ʼ‘‛"
_NUM_ONES = ("zero one two three four five six seven eight nine ten eleven "
             "twelve thirteen fourteen fifteen sixteen seventeen eighteen "
             "nineteen").split()
_NUM_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty",
             "seventy", "eighty", "ninety")


def _expand_digits(digits):
    """Digit string -> spoken words (align.py's _expand_number, duplicated).

    0-19 and tens from the tables; 100-999 as "N hundred [rest]"; 1000-9999
    read as digit pairs the way years are spoken ("2026" -> "twenty twenty
    six") with the round/oh special cases; 10000-999999 as "N thousand
    [rest]"; anything larger digit by digit.
    """
    n = int(digits)
    if n < 20:
        return [_NUM_ONES[n]]
    if n < 100:
        tens, ones = divmod(n, 10)
        return [_NUM_TENS[tens]] + ([_NUM_ONES[ones]] if ones else [])
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return ([_NUM_ONES[hundreds], "hundred"]
                + (_expand_digits(str(rest)) if rest else []))
    if n < 10000:
        hi, lo = divmod(n, 100)
        if lo == 0 and hi % 10 == 0:
            return [_NUM_ONES[hi // 10], "thousand"]
        if lo == 0:
            return _expand_digits(str(hi)) + ["hundred"]
        if hi % 10 == 0 and lo < 10:
            return [_NUM_ONES[hi // 10], "thousand"] + _expand_digits(str(lo))
        if lo < 10:
            return _expand_digits(str(hi)) + ["oh"] + _expand_digits(str(lo))
        return _expand_digits(str(hi)) + _expand_digits(str(lo))
    if n < 1_000_000:
        thousands, rest = divmod(n, 1000)
        return (_expand_digits(str(thousands)) + ["thousand"]
                + (_expand_digits(str(rest)) if rest else []))
    return [_NUM_ONES[int(d)] for d in digits]

# Injectable seams for tests (monkeypatchable module globals). When left as
# None the defaults below lazily import server.asr / server.align /
# server.producer / server.llm, so tier 1 and the test suite never touch
# sherpa-onnx or numpy, and producer tests never need a live Ollama.
ENGINE_FACTORY = None  # (models_dir: Path, provider: str, on_event) -> engine
ALIGNER_FACTORY = None  # (doc: dict) -> aligner
PRODUCER_FACTORY = None  # (rundown: dict, cue_density: str) -> producer
LLM_FACTORY = None  # (endpoint: str, model: str) -> client with async tick()


def _default_engine_factory(models_dir, provider, on_event):
    try:
        from server import asr
    except ImportError:  # direct `uv run main.py` from the server directory
        import asr
    return asr.AsrEngine(
        models_dir=models_dir, on_event=on_event, provider=provider
    )


def _default_aligner_factory(doc):
    try:
        from server import align
    except ImportError:
        import align
    # take_word_ranges marks TAKE-block words (pre-recorded footage the
    # reader skips aloud) so the aligner can cross them at no cost.
    return align.Aligner(
        script_ingest.speakable_words(doc),
        take_ranges=script_ingest.take_word_ranges(doc),
    )


def _import_rundown():
    """Import server/rundown.py (pure stdlib) on the rundown-loaded path only."""
    try:
        from server import rundown
    except ImportError:  # direct `uv run main.py` from the server directory
        import rundown
    return rundown


def _default_producer_factory(rundown, cue_density):
    try:
        from server import producer
    except ImportError:
        import producer
    return producer.Producer(rundown, cue_density=cue_density)


def _default_llm_factory(endpoint, model):
    try:
        from server import llm
    except ImportError:
        import llm
    return llm.OllamaClient(endpoint=endpoint, model=model)


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def backup_path(script_path: Path) -> Path:
    """Timestamped backup destination under <tempdir>/mc-prompter/backups.

    The stamp includes microseconds so rapid consecutive saves never
    collide on the same backup filename.
    """
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S%f")
    backups = Path(tempfile.gettempdir()) / "mc-prompter" / "backups"
    return backups / f"{script_path.name}.{stamp}.md"


def save_script(path: Path, raw: str):
    """Backup the current file, then atomically replace it with raw.

    Copies the existing file to the backups directory first, then writes
    raw to a temp file in the same directory, fsyncs, and os.replace()s it
    onto the destination so a crash mid-save can never truncate the
    original. Returns the backup path string, or None when there was no
    existing file to back up. Raises OSError on failure; runs in a worker
    thread via asyncio.to_thread.
    """
    backup = None
    if path.is_file():
        dest = backup_path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        backup = str(dest)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return backup


def build_prompt_doc(rundown):
    """The promptable markdown + per-segment word ranges from a rundown.

    Scripted segments are concatenated, each under a level-2 heading with
    its title, in rundown order; bullets segments contribute no words.
    Returns (raw_markdown, ranges) where ranges is
    [{"id": seg id, "word-start": int, "word-end": int}, ...] in the
    doc's global speakable word-index space ([start, end) half-open), so
    the UI can place the producer rail against the scroll. Word counts are
    additive across segments because each chunk is heading-separated and
    blank-line terminated (script_ingest never merges across them).
    """
    parts = []
    ranges = []
    start = 0
    for seg in rundown.get("segments", []):
        if seg.get("kind") != "scripted":
            continue
        body = (seg.get("body") or "").strip()
        chunk = f"## {seg['title']}\n\n{body}\n"
        count = script_ingest.ingest(chunk)["word-count"]
        parts.append(chunk)
        ranges.append(
            {"id": seg["id"], "word-start": start, "word-end": start + count}
        )
        start += count
    return "\n".join(parts), ranges


def informative_words(text):
    """The lowercase informative-word set of a text (keyword first-pass).

    Normalized the way align.py normalizes, so rundown point text and ASR
    transcript text meet on the same tokens: the apostrophe family
    (ASCII, curly, U+02BC) is stripped first ("market's" and "market’s"
    both become "markets"), then words are alphanumeric runs, and pure
    digit runs expand through the spoken-number table ("4090" -> "forty"
    "ninety", matching what the ASR emits). Stopwords and words shorter
    than 3 characters are dropped, on the expanded forms too.
    """
    lowered = text.lower()
    for ch in KEYWORD_APOSTROPHES:
        lowered = lowered.replace(ch, "")
    out = set()
    for w in re.findall(r"[a-z0-9]+", lowered):
        for token in (_expand_digits(w) if w.isdigit() else (w,)):
            if len(token) >= 3 and token not in KEYWORD_STOPWORDS:
                out.add(token)
    return out


class TranscriptBuffer:
    """Bounded rolling buffer of FINAL transcript text (last ~90 s).

    Fed from the Phase B asr final events; partials never enter (they
    revise). Entries older than window_s, and oldest entries past the
    character cap, are pruned on every add and read, so the buffer stays
    small no matter how long the show runs. now_fn is injectable for
    deterministic tests.
    """

    def __init__(self, window_s=TRANSCRIPT_WINDOW_S, max_chars=8000,
                 now_fn=time.monotonic):
        self.window_s = window_s
        self.max_chars = max_chars
        self.now = now_fn
        self._entries = collections.deque()  # (monotonic stamp, text)
        self._chars = 0

    def add(self, text):
        text = (text or "").strip()
        if not text:
            return
        self._entries.append((self.now(), text))
        self._chars += len(text)
        self._prune()

    def _prune(self):
        cutoff = self.now() - self.window_s
        while self._entries and (
            self._entries[0][0] < cutoff or self._chars > self.max_chars
        ):
            _, dropped = self._entries.popleft()
            self._chars -= len(dropped)

    def text(self):
        """The buffered finals, oldest first, joined with spaces."""
        self._prune()
        return " ".join(text for _, text in self._entries)

    def clear(self):
        """Drop everything (a fresh producer stack must not inherit speech)."""
        self._entries.clear()
        self._chars = 0


class CueEngine:
    """Deterministic cue delivery: the LLM proposes, this engine disposes.

    Enforces the Phase C cue contract: one active cue at a time, the
    per-density card budget (attention tier exempt), dedup by candidate
    key (a key is consumed only when its cue is actually shown), release
    of card-tier cues at a VAD pause (immediate when no ASR runs, i.e.
    gate_on_vad is False), attention-tier cues may interrupt the active
    cue mid-sentence, and every shown cue auto-expires after CUE_EXPIRY_S.
    Density hands-off never shows cards, only attention cues.

    offer()/poll()/on_vad() each return the list of wire frames to
    broadcast ({"type": "cue", ...} / {"type": "cue-clear", ...}) so the
    caller owns all I/O; the engine itself is pure and fake-clock testable
    via now_fn. A card candidate rejected for budget/one-active/pending
    reasons is dropped WITHOUT consuming its key, so the producer's
    re-offered candidates retry on later ticks. A pending card that waits
    longer than CUE_EXPIRY_S for a pause is discarded unshown (stale time
    cues must not surface a minute late), also without consuming its key.
    """

    def __init__(self, density=DEFAULT_CUE_DENSITY, now_fn=time.monotonic,
                 gate_on_vad=False):
        self.density = density
        self.now = now_fn
        self.gate_on_vad = gate_on_vad
        self.speaking = False
        self.active = None  # {"id", "tier", "text", "key", "shown-at"}
        self.pending = None  # card candidate + "queued-at", awaiting a pause
        self.shown_keys = set()
        self.last_card_at = None
        self._next_id = 0

    def _show(self, candidate):
        self._next_id += 1
        self.active = {
            "id": self._next_id,
            "tier": candidate["tier"],
            "text": candidate["text"],
            "key": candidate["key"],
            "shown-at": self.now(),
        }
        self.shown_keys.add(candidate["key"])
        if candidate["tier"] == "card":
            self.last_card_at = self.now()
        return {
            "type": "cue",
            "id": self._next_id,
            "tier": candidate["tier"],
            "text": candidate["text"],
        }

    def _clear_active(self):
        frame = {"type": "cue-clear", "id": self.active["id"]}
        self.active = None
        return frame

    def _card_budget_open(self):
        budget = CUE_DENSITY_BUDGET_S[self.density]
        if budget is None:
            return False
        return (
            self.last_card_at is None
            or self.now() - self.last_card_at >= budget
        )

    def offer(self, candidates):
        """Consider cue candidates in order; returns frames to broadcast.

        Candidates are {"tier": "card"|"attention", "text": str,
        "key": str} dicts (the producer's cue_candidates() shape; the LLM
        cue is adapted to it by the caller).
        """
        frames = self.poll()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            key = candidate.get("key")
            tier = candidate.get("tier")
            text = candidate.get("text")
            if not key or not text or tier not in ("card", "attention"):
                continue
            if key in self.shown_keys:
                continue
            if tier == "attention":
                # exempt from the budget; may interrupt the active cue
                if self.active is not None:
                    frames.append(self._clear_active())
                frames.append(self._show(candidate))
                continue
            # card tier: budget, one-active-cue, single pending slot
            if not self._card_budget_open():
                continue
            if self.active is not None or self.pending is not None:
                continue
            if self.gate_on_vad and self.speaking:
                self.pending = dict(candidate, **{"queued-at": self.now()})
                continue
            frames.append(self._show(candidate))
        return frames

    def poll(self):
        """Advance time-driven transitions: expiry and pending release."""
        frames = []
        now = self.now()
        if (
            self.active is not None
            and now - self.active["shown-at"] >= CUE_EXPIRY_S
        ):
            frames.append(self._clear_active())
        if self.pending is not None:
            if now - self.pending["queued-at"] >= CUE_EXPIRY_S:
                self.pending = None  # stale, discard unshown
            elif self.active is None and not (
                self.gate_on_vad and self.speaking
            ):
                candidate = self.pending
                self.pending = None
                frames.append(self._show(candidate))
        return frames

    def on_vad(self, speaking):
        """Track the VAD state; a pause may release the pending card."""
        self.speaking = bool(speaking)
        return self.poll()


class Client:
    """One WebSocket connection with its declared role."""

    __slots__ = ("ws", "role", "leader", "order")

    def __init__(self, ws, role, leader, order):
        self.ws = ws
        self.role = role
        self.leader = leader
        self.order = order


class AppState:
    """All mutable server state, attached to the aiohttp app.

    Holds the session token, the script path, the ingested doc plus raw text
    and doc-version, the last state snapshot from the leader prompt page,
    the connected WebSocket clients with roles, and owner-wpm.
    """

    def __init__(self, token, owner_wpm=0, static_dir=None,
                 models_dir=None, asr_provider="none",
                 cue_density=DEFAULT_CUE_DENSITY, llm_provider="none",
                 llm_endpoint=DEFAULT_LLM_ENDPOINT,
                 llm_model=DEFAULT_LLM_MODEL):
        self.token = token
        self.owner_wpm = owner_wpm or None
        self.script_path = None
        self.raw = ""
        self.doc = None
        self.doc_version = 0
        self.snapshot = None
        self.started = now_iso()
        self.port = None
        self.static_dir = Path(static_dir) if static_dir else DEFAULT_STATIC_DIR
        self.clients = []
        self._order = 0
        # Phase B ASR state. The engine runs a dedicated worker thread fed
        # binary WS frames from the capture owner; recognition events come
        # back onto the loop via call_soon_threadsafe into asr_events, and a
        # single pump task handles them in order. Nothing blocking runs on
        # the loop.
        self.models_dir = Path(models_dir) if models_dir else None
        self.asr_provider = asr_provider
        self.asr_enabled = self.models_dir is not None and asr_provider != "none"
        self.engine = None
        self.aligner = None
        self.capture_owner = None
        self.asr_ready = False
        self.asr_events = None
        self.asr_pump = None
        self.last_anchor = None
        self.last_status = None
        # Phase C producer state. The producer state machine, the cue
        # engine, and the LLM client are built on startup only when a
        # rundown was loaded (start_producer hook); everything below stays
        # inert in tiers 1 and 2.
        self.cue_density = cue_density
        self.llm_provider = llm_provider
        self.llm_endpoint = llm_endpoint
        self.llm_model = llm_model
        self.llm_ok = False
        self.rundown = None
        self.rundown_path = None
        self.prompt_ranges = []
        self.segment_kinds = {}  # seg id -> "scripted"|"bullets"
        self.producer = None
        self.producer_task = None
        self.cue_engine = None
        self.llm = None
        self.llm_task = None
        self.transcript = TranscriptBuffer()
        self.last_producer_blob = None

    def load_rundown(self, parsed, path=None):
        """Adopt a parsed rundown: producer state + the promptable doc.

        The rundown wins over any --script content by contract: the
        scripted segment bodies become the promptable document (via
        build_prompt_doc + script_ingest) and the per-segment word ranges
        are stored for /api/rundown. A bullets-only rundown yields no
        promptable words; the doc is left untouched then. script_path is
        deliberately NOT pointed at the rundown file, so POST /api/source
        save can never overwrite a rundown with the derived scroll text.
        A frontmatter cue-density overrides the configured one.
        """
        self.rundown = parsed
        self.rundown_path = Path(path) if path else None
        self.segment_kinds = {
            seg["id"]: seg.get("kind")
            for seg in parsed.get("segments", [])
        }
        if parsed.get("cue-density"):
            self.cue_density = parsed["cue-density"]
        raw, ranges = build_prompt_doc(parsed)
        self.prompt_ranges = ranges
        if ranges:
            self.script_path = None
            self.apply_source(raw, script_ingest.ingest(raw))

    def load_script(self, path):
        """Read and ingest a script file; may raise OSError/UnicodeDecodeError."""
        path = Path(path)
        raw = path.read_text(encoding="utf-8-sig")
        self.script_path = path
        self.set_raw(raw)

    def set_raw(self, raw):
        """Re-ingest raw text and bump doc-version (synchronous path).

        A leading U+FEFF (UTF-8 BOM pasted through POST /api/source) is
        stripped so it never defeats the heading parser. The async handlers
        run the ingest in a worker thread and then call apply_source on the
        event loop instead of calling this directly.
        """
        raw = raw.removeprefix("\ufeff")
        self.apply_source(raw, script_ingest.ingest(raw))

    def apply_source(self, raw, doc):
        """Assign an already-ingested doc and rebuild the aligner.

        MUST run on the event loop when the server is live: the ASR event
        pump reads self.aligner/self.last_anchor between awaits, so the
        swap may never interleave with it from a worker thread.
        """
        self.raw = raw
        self.doc = doc
        self.doc_version += 1
        self.rebuild_aligner()

    def rebuild_aligner(self):
        """Build a fresh Aligner from the current doc (voice-follow only).

        Called on every script load or edit and at ASR startup. A doc change
        invalidates the old anchor, so the last-broadcast anchor resets too.
        """
        if not self.asr_enabled or self.doc is None:
            self.aligner = None
            return
        factory = ALIGNER_FACTORY or _default_aligner_factory
        self.aligner = factory(self.doc)
        self.last_anchor = None

    def script_info(self):
        if self.doc is None:
            return None
        return {
            "path": str(self.script_path) if self.script_path else None,
            "title": self.doc["title"],
            "word-count": self.doc["word-count"],
        }

    def add_client(self, ws, role):
        self._order += 1
        leader = role == "prompt" and not any(
            c.role == "prompt" for c in self.clients
        )
        client = Client(ws, role, leader, self._order)
        self.clients.append(client)
        return client

    def remove_client(self, client):
        """Drop a client; return the newly promoted leader, if any."""
        if client in self.clients:
            self.clients.remove(client)
        if self.capture_owner is client:
            self.capture_owner = None
        if client.leader:
            prompts = [c for c in self.clients if c.role == "prompt"]
            if prompts:
                new_leader = min(prompts, key=lambda c: c.order)
                new_leader.leader = True
                return new_leader
        return None

    async def broadcast(self, payload, exclude=None):
        """Fan a JSON payload out to every connected client (best effort)."""
        for client in list(self.clients):
            if client is exclude:
                continue
            with contextlib.suppress(ConnectionError, RuntimeError):
                await client.ws.send_json(payload)


STATE_KEY = web.AppKey("state", AppState)


def is_loopback(request):
    return request.remote is None or request.remote in LOOPBACK_PEERS


def token_ok(request, state):
    provided = request.query.get("token") or ""
    return hmac.compare_digest(provided, state.token)


def make_page_handler(name):
    async def handler(request):
        state = request.app[STATE_KEY]
        page = state.static_dir / name
        if page.is_file():
            return web.FileResponse(page)
        return web.Response(
            status=503,
            text=f"UI not built: static/{name} missing",
            content_type="text/plain",
        )

    return handler


async def static_handler(request):
    state = request.app[STATE_KEY]
    rel = request.match_info["path"]
    root = state.static_dir.resolve()
    target = (root / rel).resolve()
    inside = target == root or str(target).startswith(str(root) + os.sep)
    if not inside or not target.is_file():
        raise web.HTTPNotFound(text="not found")
    return web.FileResponse(target)


async def health_handler(request):
    state = request.app[STATE_KEY]
    return web.json_response(
        {
            "app": APP_NAME,
            "version": VERSION,
            "session": state.token[:8],
            "script": state.script_path.name if state.script_path else None,
            "port": state.port,
            "started": state.started,
        }
    )


def _require_read_auth(request, state):
    """401 response for unauthorized non-loopback reads, else None."""
    if is_loopback(request) or token_ok(request, state):
        return None
    return web.json_response({"error": "token required"}, status=401)


async def api_state_handler(request):
    state = request.app[STATE_KEY]
    denied = _require_read_auth(request, state)
    if denied:
        return denied
    return web.json_response(
        {
            "snapshot": state.snapshot,
            "doc-version": state.doc_version,
            "script": state.script_info(),
            "config": {"owner-wpm": state.owner_wpm},
            "asr": {
                "available": state.asr_enabled,
                "provider": state.asr_provider,
                "ready": state.asr_ready,
            },
            "producer": {
                "active": state.producer is not None,
                "live": bool(
                    state.producer is not None
                    and state.producer.state.get("live")
                ),
                "llm": {
                    "provider": state.llm_provider,
                    "model": state.llm_model,
                    "ok": state.llm_ok,
                },
            },
        }
    )


async def api_rundown_handler(request):
    state = request.app[STATE_KEY]
    denied = _require_read_auth(request, state)
    if denied:
        return denied
    return web.json_response(
        {"rundown": state.rundown, "segments": state.prompt_ranges}
    )


async def api_source_get_handler(request):
    state = request.app[STATE_KEY]
    denied = _require_read_auth(request, state)
    if denied:
        return denied
    return web.json_response(
        {
            "path": str(state.script_path) if state.script_path else None,
            "raw": state.raw,
            "doc": state.doc,
            "doc-version": state.doc_version,
        }
    )


async def _broadcast_doc_updated(state):
    """Fan out doc-updated plus, when voice-follow is live, an anchor reset.

    A doc change rebuilds the aligner (anchor back to -1); without the reset
    frame clients would keep tinting/scrolling to a stale anchor that may
    not even exist in the new doc.
    """
    await state.broadcast(
        {"type": "doc-updated", "doc-version": state.doc_version}
    )
    if state.aligner is not None:
        state.last_anchor = (-1, False)
        await state.broadcast({"type": "anchor", "i": -1, "held": False})


async def api_source_post_handler(request):
    state = request.app[STATE_KEY]
    if not is_loopback(request):
        return web.json_response(
            {"error": "source editing is loopback only"}, status=403
        )
    try:
        body = await request.json()
        raw = body["raw"]
        if not isinstance(raw, str):
            raise TypeError("raw must be a string")
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return web.json_response({"error": f"bad body: {exc}"}, status=400)

    backup = None
    if body.get("save") and state.script_path:
        try:
            backup = await asyncio.to_thread(
                save_script, state.script_path, raw
            )
        except OSError as exc:
            return web.json_response(
                {"error": f"cannot save {state.script_path}: {exc}"},
                status=400,
            )
    raw = raw.removeprefix("\ufeff")
    doc = await asyncio.to_thread(script_ingest.ingest, raw)
    state.apply_source(raw, doc)
    await _broadcast_doc_updated(state)
    return web.json_response(
        {"doc-version": state.doc_version, "backup": backup}
    )


async def api_source_load_handler(request):
    state = request.app[STATE_KEY]
    if not is_loopback(request):
        return web.json_response(
            {"error": "source loading is loopback only"}, status=403
        )
    try:
        body = await request.json()
        path = Path(body["path"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return web.json_response({"error": f"bad body: {exc}"}, status=400)
    if not path.is_file():
        return web.json_response(
            {"error": f"not a readable file: {path}"}, status=400
        )

    def _read_and_ingest(p):
        raw = p.read_text(encoding="utf-8-sig").removeprefix("\ufeff")
        return raw, script_ingest.ingest(raw)

    try:
        # read + ingest off-loop; the doc/aligner swap happens on the loop
        raw, doc = await asyncio.to_thread(_read_and_ingest, path)
    except (OSError, UnicodeDecodeError) as exc:
        return web.json_response(
            {"error": f"cannot read {path}: {exc}"}, status=400
        )
    state.script_path = path
    state.apply_source(raw, doc)
    await _broadcast_doc_updated(state)
    return web.json_response(
        {"doc-version": state.doc_version, "script": state.script_info()}
    )


async def api_rundown_load_handler(request):
    state = request.app[STATE_KEY]
    if not is_loopback(request):
        return web.json_response(
            {"error": "rundown loading is loopback only"}, status=403
        )
    try:
        body = await request.json()
        path = Path(body["path"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return web.json_response({"error": f"bad body: {exc}"}, status=400)
    # A reload while LIVE would rebuild the producer from scratch: the show
    # clock, every coverage judgment, and the replan history would be gone
    # with no undo. Refuse (the home page surfaces the message) unless the
    # caller explicitly forces it. Ended or pre-show producers reload fine.
    if (
        state.producer is not None
        and state.producer.state.get("live")
        and not body.get("force")
    ):
        return web.json_response(
            {
                "error": (
                    "show is live; end the show before loading a rundown, "
                    'or pass "force": true to discard the running show'
                )
            },
            status=409,
        )
    if not path.is_file():
        return web.json_response(
            {"error": f"not a readable file: {path}"}, status=400
        )
    rd = _import_rundown()

    def _read_and_parse(p):
        raw = p.read_text(encoding="utf-8-sig").removeprefix("\ufeff")
        return rd.parse_rundown(raw)

    try:
        # read + parse off-loop; the doc/aligner/producer swap on the loop
        parsed = await asyncio.to_thread(_read_and_parse, path)
    except (OSError, UnicodeDecodeError) as exc:
        return web.json_response(
            {"error": f"cannot read {path}: {exc}"}, status=400
        )
    except rd.RundownError as exc:
        return web.json_response(
            {"error": f"rundown parse failed: {exc}"}, status=400
        )
    # An active cue belongs to the outgoing engine; clear it on every
    # client before the swap so no stale card survives the reload.
    if state.cue_engine is not None and state.cue_engine.active is not None:
        await state.broadcast(
            {"type": "cue-clear", "id": state.cue_engine.active["id"]}
        )
    state.load_rundown(parsed, path)
    _restart_producer_stack(state)
    await _broadcast_doc_updated(state)
    await _broadcast_producer_state(state)
    return web.json_response(
        {
            "rundown": state.rundown,
            "segments": state.prompt_ranges,
            "warnings": parsed.get("warnings", []),
        }
    )


async def ws_handler(request):
    state = request.app[STATE_KEY]
    peer_loopback = is_loopback(request)
    trusted = peer_loopback or token_ok(request, state)
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    if not trusted:
        await ws.close(code=4403, message=b"invalid or missing session token")
        return ws

    client = None
    binary_errored = False
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                # Raw PCM16 mono 16 kHz audio, accepted only from the
                # capture owner. Non-owners get exactly one error frame;
                # further binary frames are dropped silently.
                if client is not None and state.capture_owner is client:
                    if state.engine is not None:
                        state.engine.feed(msg.data)
                elif not binary_errored:
                    binary_errored = True
                    await ws.send_json(
                        {
                            "type": "error",
                            "message": (
                                "binary audio requires capture ownership; "
                                "send capture-request first"
                            ),
                        }
                    )
                continue
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                frame = json.loads(msg.data)
                if not isinstance(frame, dict):
                    raise ValueError("frame is not an object")
            except ValueError:
                await ws.send_json(
                    {
                        "type": "error",
                        "message": "malformed frame: expected a JSON object",
                    }
                )
                continue

            if client is None:
                if frame.get("type") != "hello" or frame.get("role") not in WS_ROLES:
                    await ws.send_json(
                        {
                            "type": "error",
                            "message": (
                                "first frame must be hello with role "
                                "prompt|remote|overlay|home"
                            ),
                        }
                    )
                    continue
                client = state.add_client(ws, frame["role"])
                await ws.send_json(
                    {
                        "type": "welcome",
                        "session": state.token[:8],
                        "leader": client.leader,
                        "doc-version": state.doc_version,
                        "snapshot": state.snapshot,
                    }
                )
                continue

            ftype = frame.get("type")
            if ftype == "cmd":
                relayed = dict(frame)
                relayed["from"] = client.role
                await state.broadcast(relayed)
            elif ftype == "state":
                if client.leader and client.role == "prompt":
                    state.snapshot = frame
                    await state.broadcast(frame, exclude=client)
                # state frames from non-leaders are ignored by contract
            elif ftype == "capture-request":
                if not peer_loopback:
                    await ws.send_json(
                        {"type": "capture-denied", "reason": "loopback-only"}
                    )
                elif state.engine is None:
                    # no ASR engine (tier 1): granting would stream audio
                    # into a black hole with no signal to the client
                    await ws.send_json(
                        {"type": "capture-denied", "reason": "asr-off"}
                    )
                elif (
                    state.capture_owner is not None
                    and state.capture_owner is not client
                    and not state.capture_owner.ws.closed
                ):
                    await ws.send_json(
                        {"type": "capture-denied", "reason": "owned"}
                    )
                else:
                    # free, re-requested by the owner, or the owner's socket
                    # is already closed (hard-crashed page): reclaim now
                    # instead of locking capture out until heartbeat reaping
                    state.capture_owner = client
                    await ws.send_json({"type": "capture-granted"})
            elif ftype == "capture-release":
                if state.capture_owner is client:
                    state.capture_owner = None
            elif ftype == "anchor-set":
                index = frame.get("i")
                if not isinstance(index, int) or isinstance(index, bool):
                    await ws.send_json(
                        {
                            "type": "error",
                            "message": "anchor-set requires an integer i",
                        }
                    )
                elif state.aligner is None:
                    await ws.send_json(
                        {
                            "type": "error",
                            "message": "voice-follow is not active",
                        }
                    )
                else:
                    state.aligner.set_anchor(index)
                    payload = {
                        "type": "anchor",
                        "i": state.aligner.anchor,
                        "held": False,
                    }
                    state.last_anchor = (payload["i"], False)
                    await state.broadcast(payload)
            elif ftype == "show":
                await _handle_show_cmd(state, ws, frame)
            elif ftype == "point":
                await _handle_point_cmd(state, ws, frame)
            elif ftype == "hello":
                await ws.send_json(
                    {"type": "error", "message": "already registered"}
                )
            else:
                await ws.send_json(
                    {"type": "error", "message": f"unknown frame type: {ftype}"}
                )
    finally:
        if client is not None:
            promoted = state.remove_client(client)
            if promoted is not None:
                with contextlib.suppress(ConnectionError, RuntimeError):
                    await promoted.ws.send_json(
                        {"type": "role", "leader": True}
                    )
    return ws


async def _broadcast_anchor(state, anchor, held):
    """Broadcast the committed anchor, but only when it actually changed."""
    if (anchor, held) != state.last_anchor:
        state.last_anchor = (anchor, held)
        await state.broadcast({"type": "anchor", "i": anchor, "held": held})


async def _handle_asr_event(state, event):
    """Handle one engine event on the loop: align, then fan out.

    With a producer active, two gates apply here. (1) The transcript
    buffer collects finals ONLY while the show is live and not held, so
    pre-show mic checks, hold banter, and post-show chat can never feed a
    coverage judgment (coverage is sticky; there is no un-cover). (2) The
    aligner is fed NOTHING while the producer's current segment is a
    bullets segment: bullets contribute no words to the doc, so any anchor
    motion during them would be creep into the next scripted segment. The
    anchor simply holds; ASR text still broadcasts. Recovery is the
    make-current re-anchor in _handle_point_cmd.
    """
    kind = event.get("kind")
    if kind in ("partial", "final"):
        await state.broadcast(
            {
                "type": "asr",
                "kind": kind,
                "segment": event.get("segment", 0),
                "text": event.get("text", ""),
            }
        )
        rail = state.producer.state if state.producer is not None else None
        if kind == "final" and (
            rail is None or (rail.get("live") and not rail.get("hold"))
        ):
            # producer coverage judgments read only committed ON-AIR text
            state.transcript.add(event.get("text", ""))
        if state.aligner is not None:
            bullets_current = (
                rail is not None
                and state.segment_kinds.get(rail.get("current")) == "bullets"
            )
            if not bullets_current:
                result = state.aligner.feed(
                    event.get("tokens") or [],
                    event.get("segment", 0),
                    kind == "final",
                )
                await _broadcast_anchor(
                    state, result["anchor"], bool(result.get("held"))
                )
    elif kind == "vad":
        await state.broadcast(
            {"type": "vad", "speaking": bool(event.get("speaking"))}
        )
        if state.cue_engine is not None:
            # a pause may release the pending card-tier cue immediately
            frames = state.cue_engine.on_vad(event.get("speaking"))
            await _broadcast_cue_frames(state, frames)
    elif kind == "status":
        state.asr_ready = bool(event.get("ready"))
        payload = {
            "type": "asr-status",
            "ready": state.asr_ready,
            "behind": bool(event.get("behind")),
            "queue": int(event.get("queue", 0)),
        }
        key = (payload["ready"], payload["behind"])
        if key != state.last_status:
            state.last_status = key
            await state.broadcast(payload)


async def _asr_event_pump(state):
    """Single consumer task: strict event order, one broadcast at a time."""
    while True:
        event = await state.asr_events.get()
        try:
            await _handle_asr_event(state, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"asr event error: {exc}", file=sys.stderr)


async def start_asr(app):
    """on_startup hook: build and start the engine when ASR is enabled.

    The engine factory is the module-level ENGINE_FACTORY seam (tests inject
    a stub here; the default lazily imports server.asr, which needs the
    workspace venv). Events cross from the worker thread onto the loop via
    call_soon_threadsafe into an asyncio.Queue drained by one pump task.
    """
    state = app[STATE_KEY]
    if not state.asr_enabled:
        return
    loop = asyncio.get_running_loop()
    state.asr_events = asyncio.Queue()
    state.asr_pump = asyncio.ensure_future(_asr_event_pump(state))

    def on_event(event, _loop=loop, _queue=state.asr_events):
        _loop.call_soon_threadsafe(_queue.put_nowait, event)

    factory = ENGINE_FACTORY or _default_engine_factory
    state.engine = factory(state.models_dir, state.asr_provider, on_event)
    state.engine.start()
    state.rebuild_aligner()


async def stop_asr(app):
    """on_cleanup hook: stop the pump task and join the engine thread."""
    state = app[STATE_KEY]
    if state.asr_pump is not None:
        state.asr_pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state.asr_pump
        state.asr_pump = None
    if state.engine is not None:
        await asyncio.to_thread(state.engine.stop)


# ---------------------------------------------------------------------------
# Phase C: producer loop, cue delivery, keyword coverage, LLM tick.
# ---------------------------------------------------------------------------


async def _broadcast_cue_frames(state, frames):
    for frame in frames:
        await state.broadcast(frame)


async def _broadcast_producer_state(state, rail=None):
    """Broadcast the rail state, but only when it actually changed.

    The change detector is a canonical-JSON compare, so nested coverage
    flips and replans are caught without trusting the producer to version
    its own dict. rail defaults to a fresh producer.tick().
    """
    if state.producer is None:
        return
    if rail is None:
        rail = state.producer.tick()
    blob = json.dumps(rail, sort_keys=True)
    if blob != state.last_producer_blob:
        state.last_producer_blob = blob
        await state.broadcast({"type": "producer", "state": rail})


async def _handle_show_cmd(state, ws, frame):
    """WS {"type": "show", "cmd": go-live|hold|resume|end} from any client."""
    if state.producer is None:
        await ws.send_json(
            {"type": "error", "message": "producer mode is not active"}
        )
        return
    handlers = {
        "go-live": state.producer.go_live,
        "hold": state.producer.hold,
        "resume": state.producer.resume,
        "end": state.producer.end_show,
    }
    handler = handlers.get(frame.get("cmd"))
    if handler is None:
        await ws.send_json(
            {
                "type": "error",
                "message": "show cmd must be go-live|hold|resume|end",
            }
        )
        return
    handler()
    await _broadcast_producer_state(state)


async def _handle_point_cmd(state, ws, frame):
    """WS {"type": "point", "cmd": covered|skip|make-current, ...} handler.

    The human is the final authority: covered and skip are sticky
    producer-side. "point" is optional for make-current (it jumps the
    segment). Unknown segment/point ids answer with an error frame.

    make-current is the ONLY segment-transition command on the wire (the
    UI's anchor-driven handoff sends it too), so it carries the aligner
    re-anchor: when the target segment is SCRIPTED and voice-follow is
    active, the anchor jumps to the segment's word-start minus 1 (clamped
    to -1) and the fresh anchor frame broadcasts BEFORE the producer
    state, undoing any creep from a preceding bullets segment and making
    stale-anchor handoffs impossible. A bullets target re-anchors nothing
    (feeds are suspended for it; the anchor holds where the last scripted
    segment left it).
    """
    if state.producer is None:
        await ws.send_json(
            {"type": "error", "message": "producer mode is not active"}
        )
        return
    cmd = frame.get("cmd")
    seg = frame.get("segment")
    idx = frame.get("point")
    if cmd not in ("covered", "skip", "make-current"):
        await ws.send_json(
            {
                "type": "error",
                "message": "point cmd must be covered|skip|make-current",
            }
        )
        return
    if not isinstance(seg, str) or not seg:
        await ws.send_json(
            {"type": "error", "message": "point cmd requires a segment id"}
        )
        return
    needs_index = cmd in ("covered", "skip")
    if needs_index and (not isinstance(idx, int) or isinstance(idx, bool)):
        await ws.send_json(
            {"type": "error", "message": f"{cmd} requires an integer point"}
        )
        return
    try:
        if cmd == "covered":
            state.producer.mark_covered(seg, idx)
        elif cmd == "skip":
            state.producer.skip_point(seg, idx)
        else:
            state.producer.make_current(seg)
    except (KeyError, IndexError, ValueError) as exc:
        await ws.send_json(
            {"type": "error", "message": f"point cmd failed: {exc}"}
        )
        return
    if cmd == "make-current" and state.aligner is not None:
        rng = next(
            (r for r in state.prompt_ranges if r["id"] == seg), None
        )
        if rng is not None:  # scripted segment: re-anchor deterministically
            state.aligner.set_anchor(max(-1, rng["word-start"] - 1))
            payload = {
                "type": "anchor",
                "i": state.aligner.anchor,
                "held": False,
            }
            state.last_anchor = (payload["i"], False)
            await state.broadcast(payload)
    await _broadcast_producer_state(state)


def _keyword_coverage_pass(state):
    """Deterministic first-pass coverage from the final-transcript buffer.

    Runs ONLY while the show is live and not on hold: coverage is sticky
    with no un-cover anywhere in the system, so a pre-show rehearsal, a
    mic check, or hold banter must never mark a point covered. (The
    transcript buffer is also fill-gated on the same condition and cleared
    on producer restarts, so this guard is defense in depth.) Every
    uncovered, unskipped point whose informative words appear in the last
    TRANSCRIPT_WINDOW_S of final transcript at KEYWORD_OVERLAP_FLOOR
    (>= 60 percent) or better is proposed covered. propose_coverage is
    monotonic by contract (uncovered -> covered only), so a false hit can
    never un-cover or flicker anything, and human skips stay authoritative.
    """
    rail = state.producer.state
    if not rail.get("live") or rail.get("hold"):
        return
    tail = informative_words(state.transcript.text())
    if not tail:
        return
    for seg in rail.get("segments", []):
        for idx, point in enumerate(seg.get("points", [])):
            if point.get("covered") or point.get("skipped"):
                continue
            info = informative_words(point.get("text", ""))
            if not info:
                continue
            if len(info & tail) / len(info) >= KEYWORD_OVERLAP_FLOOR:
                state.producer.propose_coverage(seg["id"], idx)


async def producer_tick(state):
    """One producer heartbeat: coverage, replan, rail broadcast, cues.

    Called by the 1 s loop and directly by tests (deterministic, no
    sleeps). Keyword coverage runs first so the tick's replan already
    reflects it; the rail broadcast is change-gated; deterministic cue
    candidates then pass through the cue engine.
    """
    if state.producer is None:
        return
    _keyword_coverage_pass(state)
    rail = state.producer.tick()
    await _broadcast_producer_state(state, rail)
    frames = state.cue_engine.offer(state.producer.cue_candidates())
    await _broadcast_cue_frames(state, frames)


async def _producer_loop(state):
    """The 1 s producer heartbeat task (runs only when a rundown loaded)."""
    while True:
        await asyncio.sleep(PRODUCER_TICK_S)
        try:
            await producer_tick(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"producer tick error: {exc}", file=sys.stderr)


def build_rundown_block(state):
    """The STABLE rundown text for the LLM tick's system message.

    Only slow-changing content lives here: the show title, each segment's
    id/title/kind/planned budget, and per-point covered/skipped flags.
    This text changes ONLY when coverage flips, so Ollama's prefix cache
    survives across ticks (the design reason the system message exists).
    Every per-tick number (clock, replan, spent, timing, NEXT) lives in
    build_status_block, which rides in the user message instead. Segment
    and point ids are the wire ids so the model's coverage answers map
    straight onto propose_coverage.
    """
    rail = state.producer.state
    show = state.rundown.get("show") or "(untitled)"
    lines = [f"SHOW: {show}"]
    for seg in rail.get("segments", []):
        lines.append(
            f"SEGMENT {seg.get('id')} '{seg.get('title')}'"
            f" kind {seg.get('kind')} planned {seg.get('planned-s')}s"
        )
        for idx, point in enumerate(seg.get("points", [])):
            if point.get("skipped"):
                status = "skipped"
            elif point.get("covered"):
                status = "covered"
            else:
                status = "uncovered"
            lines.append(f"  point {idx} [{status}]: {point.get('text')}")
    return "\n".join(lines)


def build_status_block(state):
    """The VOLATILE clock/replan text for the LLM tick's user message.

    Everything here changes every tick (elapsed, remaining, per-segment
    replanned/spent/timing, the current-segment marker, NEXT), so it must
    stay OUT of the system message: one volatile byte at the top of the
    prompt would invalidate Ollama's prefix cache and force a full
    re-prefill of the rundown block on every tick.
    """
    rail = state.producer.state
    lines = [
        f"CLOCK: elapsed {rail.get('elapsed-s', 0)}s"
        f" | remaining {rail.get('remaining-s', 0)}s"
        f" | show-state {rail.get('show-state', 'green')}"
        f" | {'LIVE' if rail.get('live') else 'PRE-SHOW'}"
        f"{' (HOLD)' if rail.get('hold') else ''}"
    ]
    for seg in rail.get("segments", []):
        lines.append(
            f"SEGMENT {seg.get('id')}: {seg.get('state')}"
            f" | replanned {seg.get('replanned-s')}s"
            f" spent {seg.get('spent-s')}s timing {seg.get('timing')}"
        )
    nxt = rail.get("next-point")
    if nxt:
        lines.append(
            f"NEXT: segment {nxt.get('segment')} point {nxt.get('idx')}:"
            f" {nxt.get('text')}"
        )
    return "\n".join(lines)


def _evidence_supports(state, seg_id, idx, evidence):
    """Deterministic gate on an LLM coverage claim's evidence quote.

    A claim is credible only when (a) the quote is real: at least 60
    percent of its informative words appear in the bounded final-transcript
    buffer, and (b) the quote is about THIS point: it shares at least one
    informative word with the point text. Measured on qwen3:4b, a
    hallucinated claim ("the graphics card anecdote" reported covered at
    0.9 on a transcript that never mentions it) cannot satisfy both:
    fabricated evidence fails (a), and a real quote lifted from elsewhere
    in the transcript fails (b). The LLM proposes; this code disposes.
    """
    ev = informative_words(evidence or "")
    if not ev:
        return False
    tail = informative_words(state.transcript.text())
    if not tail or len(ev & tail) / len(ev) < KEYWORD_OVERLAP_FLOOR:
        return False
    for seg in state.producer.state.get("segments", []):
        if seg["id"] != seg_id:
            continue
        points = seg.get("points", [])
        if 0 <= idx < len(points):
            point_info = informative_words(points[idx].get("text", ""))
            return bool(point_info & ev)
    return False


async def apply_llm_result(state, result):
    """Apply one LLM tick result: coverage proposals + the optional cue.

    Coverage entries need segment (str), point (int), confidence >=
    LLM_CONFIDENCE_FLOOR, and evidence that passes _evidence_supports;
    everything else is ignored (the model can only ever propose
    uncovered -> covered, the producer enforces the rest). The optional
    cue is offered to the cue engine as an ordinary card candidate keyed
    by its text, so budget/dedup/one-active all apply.
    """
    proposed = False
    for item in result.get("coverage", []):
        if not isinstance(item, dict):
            continue
        seg = item.get("segment")
        idx = item.get("point")
        conf = item.get("confidence")
        if (
            isinstance(seg, str)
            and isinstance(idx, int) and not isinstance(idx, bool)
            and isinstance(conf, (int, float))
            and conf >= LLM_CONFIDENCE_FLOOR
            and _evidence_supports(state, seg, idx, item.get("evidence"))
        ):
            try:
                state.producer.propose_coverage(seg, idx)
                proposed = True
            except (KeyError, IndexError, ValueError):
                pass  # the model named a point that does not exist
    if proposed:
        await _broadcast_producer_state(state)
    cue = result.get("cue")
    if isinstance(cue, dict) and cue.get("text"):
        text = str(cue["text"]).strip()
        if text:
            frames = state.cue_engine.offer(
                [{"tier": "card", "text": text,
                  "key": "llm:" + text.lower()}]
            )
            await _broadcast_cue_frames(state, frames)


def _llm_should_skip(state):
    """True when this LLM tick must be skipped (never queued for later).

    Skips while the ASR engine reports behind (the tick must never starve
    the ASR thread), while the show is held, before go-live, and after
    end_show (live reads false again then), so the model never judges
    off-air speech (deliberate addition to the contract's behind/hold
    list).
    """
    if state.engine is not None and state.engine.stats.get("behind"):
        return True
    rail = state.producer.state
    return bool(rail.get("hold")) or not rail.get("live")


async def _llm_loop(state):
    """Adaptive LLM tick task: next = max(15 s, 3 * last tick wall time)."""
    interval = LLM_MIN_INTERVAL_S
    while True:
        await asyncio.sleep(interval)
        try:
            if _llm_should_skip(state):
                continue
            started = time.monotonic()
            result = await state.llm.tick(
                build_rundown_block(state),
                build_status_block(state),
                state.transcript.text(),
            )
            wall = time.monotonic() - started
            interval = max(LLM_MIN_INTERVAL_S, LLM_INTERVAL_MULT * wall)
            state.llm_ok = result is not None
            if result is not None:
                await apply_llm_result(state, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"llm tick error: {exc}", file=sys.stderr)


async def start_producer(app):
    """on_startup hook: build the producer stack when a rundown is loaded.

    The producer and LLM client come from the module-level seams
    (PRODUCER_FACTORY / LLM_FACTORY; tests inject stubs, the defaults
    lazily import server.producer / server.llm). The cue engine gates
    card release on VAD pauses only when ASR actually runs; without ASR
    cards release immediately by contract.
    """
    state = app[STATE_KEY]
    if state.rundown is None:
        return
    _restart_producer_stack(state)


def _restart_producer_stack(state):
    """(Re)build the producer stack; used at startup and on runtime loads.

    A fresh producer and cue engine are built from the current rundown;
    the heartbeat and LLM tasks are created only when not already running
    (both loops read state.producer each tick, so a swap is safe). The LLM
    lane cannot appear at runtime: a server started without a rundown has
    llm_provider none by the launcher downgrade rule, so a rundown loaded
    through POST /api/rundown/load runs the deterministic rail only.

    The transcript buffer is cleared (speech captured before this stack
    existed must never feed its coverage judgments) and llm_ok drops back
    to False (it reports the last tick outcome of THIS stack, not a stale
    success from the previous one).
    """
    factory = PRODUCER_FACTORY or _default_producer_factory
    state.producer = factory(state.rundown, state.cue_density)
    state.cue_engine = CueEngine(
        density=state.cue_density, gate_on_vad=state.asr_enabled
    )
    state.last_producer_blob = None
    state.transcript.clear()
    state.llm_ok = False
    if state.producer_task is None:
        state.producer_task = asyncio.ensure_future(_producer_loop(state))
    if state.llm_provider == "ollama" and state.llm is None:
        llm_factory = LLM_FACTORY or _default_llm_factory
        state.llm = llm_factory(state.llm_endpoint, state.llm_model)
    if state.llm is not None and state.llm_task is None:
        state.llm_task = asyncio.ensure_future(_llm_loop(state))


async def stop_producer(app):
    """on_cleanup hook: cancel the producer and LLM tasks.

    The wait after cancel is bounded by LLM_STOP_GRACE_S: an LLM tick in
    flight sits on a urllib worker thread that cancellation cannot
    interrupt, and awaiting it unbounded would gate shutdown on the
    remote peer (up to the tick timeout). Past the grace the task is
    abandoned; llm.py bounds the worker itself with an end-to-end
    deadline, so the thread finishes on its own shortly after and cannot
    pin process exit for long.
    """
    state = app[STATE_KEY]
    for attr in ("producer_task", "llm_task"):
        task = getattr(state, attr)
        if task is not None:
            task.cancel()
            # asyncio.wait (not wait_for): on timeout it neither re-cancels
            # nor blocks on the uninterruptible task, it just returns.
            await asyncio.wait({task}, timeout=LLM_STOP_GRACE_S)
            setattr(state, attr, None)


def create_app(state):
    app = web.Application()
    app[STATE_KEY] = state
    app.on_startup.append(start_asr)
    app.on_startup.append(start_producer)
    app.on_cleanup.append(stop_producer)
    app.on_cleanup.append(stop_asr)
    for route, name in PAGES.items():
        app.router.add_get(route, make_page_handler(name))
    app.router.add_get("/static/{path:.+}", static_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/api/state", api_state_handler)
    app.router.add_get("/api/rundown", api_rundown_handler)
    app.router.add_get("/api/source", api_source_get_handler)
    app.router.add_post("/api/source", api_source_post_handler)
    app.router.add_post("/api/source/load", api_source_load_handler)
    app.router.add_post("/api/rundown/load", api_rundown_load_handler)
    app.router.add_get("/ws", ws_handler)
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--script", default="",
                        help="script file path, or empty for no script")
    parser.add_argument("--owner-wpm", type=int, default=0,
                        help="creator wpm from the studio config, 0 = unset")
    parser.add_argument("--session-file", default="",
                        help="session file path (written by the launcher)")
    parser.add_argument("--token", required=True,
                        help="session token generated by the launcher")
    parser.add_argument("--models-dir", default="",
                        help="workspace models directory; empty = ASR off")
    parser.add_argument("--asr-provider", default="none",
                        help="none | nemotron-streaming "
                             "(zipformer-small is planned and exits 3)")
    parser.add_argument("--rundown", default="",
                        help="rundown file path; enables producer mode "
                             "(wins over --script for the prompted text)")
    parser.add_argument("--llm-provider", default="none",
                        help="none | ollama (anything else is a planned "
                             "lane and exits 3)")
    parser.add_argument("--llm-endpoint", default=DEFAULT_LLM_ENDPOINT,
                        help="Ollama endpoint for the producer's LLM tick")
    parser.add_argument("--llm-model", default=DEFAULT_LLM_MODEL,
                        help="Ollama model tag for the producer's LLM tick")
    parser.add_argument("--cue-density", default=DEFAULT_CUE_DENSITY,
                        choices=CUE_DENSITIES,
                        help="cue budget (rundown frontmatter overrides)")
    args = parser.parse_args(argv)

    provider = args.asr_provider
    if provider not in ASR_PROVIDERS:
        planned = " (a planned lane)" if provider in ASR_PLANNED_PROVIDERS else ""
        print(
            f"error: asr-provider {provider!r} is not implemented{planned}; "
            f"implemented providers: {', '.join(ASR_PROVIDERS)}",
            file=sys.stderr,
        )
        return 3
    llm_provider = args.llm_provider
    if llm_provider not in LLM_PROVIDERS:
        print(
            f"error: llm-provider {llm_provider!r} is not implemented "
            f"(a planned lane); implemented providers: "
            f"{', '.join(LLM_PROVIDERS)}",
            file=sys.stderr,
        )
        return 3
    if llm_provider != "none" and not args.rundown:
        print(
            "note: --llm-provider set without --rundown; the LLM tick "
            "runs only in producer mode and is off",
            file=sys.stderr,
        )
        llm_provider = "none"
    models_dir = None
    if args.models_dir and provider != "none":
        models_dir = Path(args.models_dir)
        if not models_dir.is_dir():
            print(
                f"error: models dir not found: {models_dir}", file=sys.stderr
            )
            return 4
    elif provider != "none":
        print(
            "note: --asr-provider set without --models-dir; ASR is off",
            file=sys.stderr,
        )
        provider = "none"

    parsed_rundown = None
    if args.rundown:
        rundown_path = Path(args.rundown)
        if not rundown_path.is_file():
            print(f"error: rundown not found: {rundown_path}",
                  file=sys.stderr)
            return 4
        rd = _import_rundown()
        try:
            text = rundown_path.read_text(encoding="utf-8-sig")
            parsed_rundown = rd.parse_rundown(text)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"error: cannot read {rundown_path}: {exc}",
                  file=sys.stderr)
            return 4
        except rd.RundownError as exc:
            print(f"error: rundown parse failed: {rundown_path}: {exc}",
                  file=sys.stderr)
            return 4
        for warning in parsed_rundown.get("warnings", []):
            print(f"rundown warning: {warning}", file=sys.stderr)
        if args.script:
            print(
                "note: --rundown wins over --script; the rundown's "
                "scripted segments are what gets prompted (a bullets-only "
                "rundown keeps the script on the scroll)",
                file=sys.stderr,
            )

    state = AppState(
        token=args.token,
        owner_wpm=args.owner_wpm,
        models_dir=models_dir,
        asr_provider=provider if models_dir else "none",
        cue_density=args.cue_density,
        llm_provider=llm_provider,
        llm_endpoint=args.llm_endpoint,
        llm_model=args.llm_model,
    )
    state.port = args.port
    # Load the script FIRST, then the rundown: load_rundown replaces the
    # doc only when the rundown contributes scripted words, so a
    # bullets-only rundown keeps the --script text on the scroll. This
    # matches the runtime POST /api/rundown/load behavior exactly (same
    # inputs, same outcome on both paths).
    if args.script:
        path = Path(args.script)
        if not path.is_file():
            print(f"error: script not found: {path}", file=sys.stderr)
            return 4
        try:
            state.load_script(path)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"error: cannot read {path}: {exc}", file=sys.stderr)
            return 4
    if parsed_rundown is not None:
        state.load_rundown(parsed_rundown, rundown_path)

    app = create_app(state)
    asr_note = (
        f"asr {state.asr_provider}" if state.asr_enabled else "asr off (tier 1)"
    )
    if state.rundown is not None:
        llm_note = (
            f"llm {state.llm_model}" if state.llm_provider == "ollama"
            else "llm off (deterministic rail only)"
        )
        producer_note = (
            f", producer on (cue-density {state.cue_density}, {llm_note})"
        )
    else:
        producer_note = ""
    print(
        f"{APP_NAME} {VERSION} serving on http://{args.host}:{args.port}/ "
        f"({asr_note}{producer_note})",
        file=sys.stderr,
    )
    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)
