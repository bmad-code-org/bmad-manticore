#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "sherpa-onnx==1.13.4"]
# ///
"""Replay harness: feed a WAV through AsrEngine (+ Aligner) like the WS path.

Dev tool, never run in CI (it needs the downloaded models). It exercises the
exact server code path the browser audio takes: PCM16 bytes in 120 ms chunks
into AsrEngine.feed(), engine events out through on_event, tokens through
Aligner.feed(). Use it to measure end-to-end capture-to-anchor latency per
platform and to capture real partial-event streams as committed fixtures for
test-align.py.

Usage (from the scripts directory, inside the workspace venv):
    <workspace>/.venv/bin/python -m server.replay --wav <path> \
        --models-dir <workspace>/models [--script <script.md>] \
        [--provider nemotron-streaming] [--fast] [--dump-events <out.json>]

    A `uv run replay.py ...` from the server directory also works (uv
    provisions sherpa-onnx and numpy from the header above).

Arguments:
    --wav          16 kHz mono PCM16 WAV (recipe: `say -o x.aiff "<text>"`
                   then `ffmpeg -y -i x.aiff -ar 16000 -ac 1 -sample_fmt s16
                   x.wav`)
    --models-dir   workspace models dir (nemotron-streaming/ + silero_vad.onnx)
    --script       optional script file; when given, an Aligner is built from
                   script_ingest.speakable_words(ingest(text)) and every
                   partial/final is fed through it, printing anchor moves
    --provider     asr provider (default nemotron-streaming)
    --fast         feed as fast as the engine accepts instead of pacing one
                   chunk per 120 ms (real-time pacing is the default because
                   the latency numbers are only honest when paced)
    --dump-events  write the raw partial-event stream as JSON in the
                   test-align fixture shape:
                   {"script": "<raw script text or empty>",
                    "events": [{"tokens": [...], "segment": n,
                                "final": bool}, ...],
                    "notes": ""}

Output: one line per event (vad transitions, partials, finals, status,
anchor moves), then latency stats: for every anchor change, wall time from
the moment the most recent audio chunk was fed to the moment the anchor
updated (chunk-to-anchor; add ~60 ms mean capture framing for mic numbers).

Exit codes: 0 ok, 2 usage, 4 missing wav/models/script, 5 engine failed
(model load failure, worker death, or never ready within the timeout).
"""

import argparse
import json
import queue
import statistics
import sys
import time
import wave
from pathlib import Path

try:
    from server.asr import AsrEngine
except ImportError:  # direct `uv run replay.py` from the server directory
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from asr import AsrEngine

CHUNK_MS = 120
SAMPLE_RATE = 16000
CHUNK_BYTES = int(SAMPLE_RATE * CHUNK_MS / 1000) * 2  # PCM16 mono
READY_TIMEOUT_S = 120.0
FLUSH_SILENCE_S = 2.0


def read_wav(path):
    """Return raw PCM16 bytes; the file must be 16 kHz mono 16-bit."""
    with wave.open(str(path), "rb") as wav:
        if (
            wav.getframerate() != SAMPLE_RATE
            or wav.getnchannels() != 1
            or wav.getsampwidth() != 2
        ):
            raise ValueError(
                f"{path}: need 16 kHz mono PCM16, got "
                f"{wav.getframerate()} Hz, {wav.getnchannels()} ch, "
                f"{wav.getsampwidth() * 8} bit"
            )
        return wav.readframes(wav.getnframes())


def build_aligner(script_path):
    """Aligner over speakable words, exactly as main.py builds it."""
    try:
        from server import align, script_ingest
    except ImportError:
        import align
        import script_ingest
    raw = Path(script_path).read_text(encoding="utf-8-sig")
    doc = script_ingest.ingest(raw)
    return align.Aligner(script_ingest.speakable_words(doc)), raw


class Replay:
    """Drains engine events on the main thread, mirroring main.py's pump."""

    def __init__(self, aligner=None):
        self.events = queue.Queue()
        self.aligner = aligner
        self.captured = []
        self.latencies_ms = []
        self.last_feed_time = None
        self.last_anchor = None
        self.ready = False
        # Any status event with ready=False is fatal in the replay setting:
        # before readiness it is a model-load failure, after it a decode
        # crash. Either way the worker is exiting; waiting out READY_TIMEOUT
        # or spinning on the dead queue would just stall for minutes.
        self.failed = False

    def on_event(self, event):
        # Called from the engine worker thread; the queue crosses back to
        # the main thread (stand-in for call_soon_threadsafe in main.py).
        self.events.put((time.perf_counter(), event))

    def drain(self):
        while True:
            try:
                arrived, event = self.events.get_nowait()
            except queue.Empty:
                return
            self.handle(arrived, event)

    def handle(self, arrived, event):
        kind = event.get("kind")
        if kind == "status":
            self.ready = bool(event.get("ready"))
            if not self.ready:
                self.failed = True
            print(
                f"status ready={event.get('ready')} "
                f"behind={event.get('behind')} queue={event.get('queue')}"
            )
        elif kind == "vad":
            print(f"vad speaking={event.get('speaking')}")
        elif kind in ("partial", "final"):
            self.captured.append(
                {
                    "tokens": event.get("tokens", []),
                    "segment": event.get("segment", 0),
                    "final": kind == "final",
                }
            )
            text = event.get("text", "")
            print(f"{kind} seg={event.get('segment')} text={text[:70]!r}")
            if self.aligner is not None:
                result = self.aligner.feed(
                    event.get("tokens") or [],
                    event.get("segment", 0),
                    kind == "final",
                )
                anchor = result["anchor"]
                if anchor != self.last_anchor:
                    self.last_anchor = anchor
                    if self.last_feed_time is not None:
                        # clamp: an event queued just before last_feed_time
                        # was updated must not record a negative latency
                        ms = max(0.0, (arrived - self.last_feed_time) * 1000)
                        self.latencies_ms.append(ms)
                        print(
                            f"  anchor -> {anchor} held={result.get('held')} "
                            f"(+{ms:.0f} ms after last chunk)"
                        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wav", required=True)
    parser.add_argument("--models-dir", required=True)
    parser.add_argument("--script", default="")
    parser.add_argument("--provider", default="nemotron-streaming")
    parser.add_argument("--fast", action="store_true",
                        help="no real-time pacing (latency stats meaningless)")
    parser.add_argument("--dump-events", default="",
                        help="write the raw event stream as a fixture JSON")
    args = parser.parse_args(argv)

    wav_path = Path(args.wav)
    models_dir = Path(args.models_dir)
    if not wav_path.is_file():
        print(f"error: wav not found: {wav_path}", file=sys.stderr)
        return 4
    if not models_dir.is_dir():
        print(f"error: models dir not found: {models_dir}", file=sys.stderr)
        return 4

    aligner = None
    script_raw = ""
    if args.script:
        if not Path(args.script).is_file():
            print(f"error: script not found: {args.script}", file=sys.stderr)
            return 4
        aligner, script_raw = build_aligner(args.script)

    try:
        pcm = read_wav(wav_path)
    except (ValueError, wave.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    replay = Replay(aligner=aligner)
    engine = AsrEngine(
        models_dir=models_dir, on_event=replay.on_event, provider=args.provider
    )
    engine.start()
    try:
        deadline = time.perf_counter() + READY_TIMEOUT_S
        while not replay.ready:
            replay.drain()
            if replay.ready:
                break
            # fail fast: a ready=False status or a dead worker means the
            # model load already failed; waiting out READY_TIMEOUT_S would
            # stall two minutes on a failure known at t+1 s
            if replay.failed or not engine.alive:
                replay.drain()
                if replay.ready:
                    break
                print("error: engine failed to load (see the asr error "
                      "above)", file=sys.stderr)
                return 5
            if time.perf_counter() > deadline:
                print("error: engine never became ready", file=sys.stderr)
                return 5
            time.sleep(0.05)

        silence = b"\x00" * int(SAMPLE_RATE * FLUSH_SILENCE_S) * 2
        data = pcm + silence
        t0 = time.perf_counter()
        for start in range(0, len(data), CHUNK_BYTES):
            if replay.failed or not engine.alive:
                break
            chunk = data[start:start + CHUNK_BYTES]
            replay.last_feed_time = time.perf_counter()
            engine.feed(chunk)
            if not args.fast:
                # pace to one chunk per CHUNK_MS of audio, like a live mic
                target = t0 + (start + len(chunk)) / 2 / SAMPLE_RATE
                delay = target - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
            # drain AFTER the pacing sleep so events that arrived during it
            # are timed against this chunk's feed time, never a newer one
            replay.drain()

        # let the worker finish the queued tail, then drain the last events;
        # a dead or crashed worker never empties its queue, so stop spinning
        while engine.stats["queue"] > 0 and engine.alive and not replay.failed:
            replay.drain()
            time.sleep(0.05)
        time.sleep(0.5)
        replay.drain()
        if replay.failed:
            print("error: engine failed mid-replay (see the asr error "
                  "above)", file=sys.stderr)
            return 5
    finally:
        engine.stop()
    replay.drain()

    audio_s = len(pcm) / 2 / SAMPLE_RATE
    wall_s = time.perf_counter() - t0
    print(
        f"\n{audio_s:.1f} s audio replayed in {wall_s:.1f} s wall, "
        f"{len(replay.captured)} partial/final events"
    )
    if replay.latencies_ms:
        lat = replay.latencies_ms
        print(
            f"chunk-to-anchor latency over {len(lat)} anchor moves: "
            f"min {min(lat):.0f} ms, median {statistics.median(lat):.0f} ms, "
            f"max {max(lat):.0f} ms"
        )
        if args.fast:
            print("(fed with --fast; latency numbers are not meaningful)")

    if args.dump_events:
        fixture = {"script": script_raw, "events": replay.captured, "notes": ""}
        Path(args.dump_events).write_text(
            json.dumps(fixture, indent=1), encoding="utf-8"
        )
        print(f"event stream written to {args.dump_events}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
