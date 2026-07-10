#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "sherpa-onnx==1.13.4"]
# ///
"""Streaming ASR engine for mc-prompter (Phase B, voice-follow).

This module is imported LAZILY by server/main.py, only when the server is
launched with --models-dir and an implemented --asr-provider. It normally
runs inside the prompter-lab workspace venv (which pins sherpa-onnx==1.13.4,
numpy); the PEP 723 header above documents the dependencies and allows a
direct `uv run asr.py` sanity import. CI never imports this module.

Contract (binding, see the Phase B contract):
    AsrEngine(models_dir, on_event, provider="nemotron-streaming",
              num_threads=2)
        models_dir  the workspace models directory containing
                    nemotron-streaming/{encoder.int8.onnx, decoder.int8.onnx,
                    joiner.int8.onnx, tokens.txt} and silero_vad.onnx
        on_event    callable taking one dict; called from the WORKER thread.
                    The caller wraps it with loop.call_soon_threadsafe.
        provider    "nemotron-streaming" is the only implemented provider;
                    anything else raises ValueError (planned lanes never
                    pretend to run).
    engine.start()  spawns the worker thread; model load happens on the
                    worker so start() returns immediately. ready flips true
                    (status event) once the recognizer and VAD are built.
                    Restart after stop() is supported: start() begins from
                    fresh state (queue drained, ready/behind/dropped reset)
                    so a leftover stop sentinel or stale pre-stop audio can
                    never reach a new worker.
    engine.stop()   stops and joins the worker.
    engine.alive    True while the worker thread is running.
    engine.feed(pcm16_bytes)  called from the event loop with raw little
                    endian PCM16 mono 16 kHz bytes of any length (the
                    browser sends ~3840 bytes per 120 ms). Bounded queue
                    (maxsize 50); on overflow the OLDEST frame is dropped
                    and behind=True until the queue drains below half.
    engine.stats    {"queue": n, "behind": bool, "ready": bool}

Events emitted through on_event:
    {"kind": "partial", "segment": n, "text": str, "tokens": [...]}
        on every hypothesis change. tokens are BPE pieces; a piece starting
        with a space starts a new word. The tail of the hypothesis may
        revise between partials; the aligner handles that.
    {"kind": "final", "segment": n, "text": str, "tokens": [...]}
        at an endpoint, before the recognizer resets (only when the
        hypothesis is non-empty; endpoints on pure silence are not final
        events). The next partial starts segment n+1 with a fresh
        hypothesis.
    {"kind": "vad", "speaking": bool}
        on speaking/silence transitions (silero VAD, 512-sample windows).
    {"kind": "status", "ready": bool, "behind": bool, "queue": int}
        on ready/behind changes. If model load fails (imports included) or
        the decode loop crashes mid-session, an error line goes to stderr,
        ready flips False, the queue is drained (stats reflect death), a
        final status event is emitted, and the worker exits; the engine
        never fakes readiness.

Pipeline per frame (all on the worker thread): PCM16 LE bytes -> float32 in
[-1, 1); 512-sample windows into the VAD (drained each window so its buffer
never grows); the full chunk into the recognizer stream; decode while ready;
endpoint -> final + reset + segment increment.
"""

import json
import queue
import sys
import threading
from pathlib import Path

PROVIDERS = ("nemotron-streaming",)
SAMPLE_RATE = 16000
VAD_WINDOW = 512
QUEUE_MAX = 50


class AsrEngine:
    """Dedicated-thread streaming recognizer with a bounded drop-oldest queue."""

    def __init__(self, models_dir, on_event, provider="nemotron-streaming",
                 num_threads=2):
        if provider not in PROVIDERS:
            raise ValueError(
                f"asr provider {provider!r} is not implemented; "
                f"implemented: {', '.join(PROVIDERS)}"
            )
        self.models_dir = Path(models_dir)
        self.on_event = on_event
        self.provider = provider
        self.num_threads = num_threads
        self._queue = queue.Queue(maxsize=QUEUE_MAX)
        self._behind = False
        self._ready = False
        self._dropped = 0
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        """Spawn the worker thread (idempotent). Model load happens there.

        Every start begins from fresh state: a prior stop() leaves its None
        sentinel (and possibly stale pre-stop audio) in the queue, which a
        restarted worker must never inherit or it would decode old audio and
        then die on the sentinel while reporting ready.
        """
        if self._thread is not None:
            return
        self._drain_queue()
        self._ready = False
        self._behind = False
        self._dropped = 0
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="mc-prompter-asr", daemon=True
        )
        self._thread.start()

    def stop(self):
        """Signal the worker and join it (safe to call more than once)."""
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def feed(self, pcm16_bytes):
        """Enqueue a PCM16 frame; drop the oldest frame on overflow."""
        try:
            self._queue.put_nowait(pcm16_bytes)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(pcm16_bytes)
            except queue.Full:
                pass
            self._dropped += 1
            if not self._behind:
                self._behind = True
                self._emit_status()

    @property
    def stats(self):
        return {
            "queue": self._queue.qsize(),
            "behind": self._behind,
            "ready": self._ready,
        }

    @property
    def alive(self):
        """True while the worker thread is running."""
        return self._thread is not None and self._thread.is_alive()

    def _drain_queue(self):
        """Empty the audio queue (start reset and worker-death cleanup)."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _emit(self, event):
        try:
            self.on_event(event)
        except Exception as exc:  # a sink bug must never kill the worker
            print(f"asr: on_event raised: {exc}", file=sys.stderr)

    def _emit_status(self):
        self._emit(
            {
                "kind": "status",
                "ready": self._ready,
                "behind": self._behind,
                "queue": self._queue.qsize(),
            }
        )

    def _build(self, sherpa_onnx):
        """Construct the recognizer and VAD (worker thread only)."""
        model_dir = self.models_dir / self.provider
        recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(model_dir / "tokens.txt"),
            encoder=str(model_dir / "encoder.int8.onnx"),
            decoder=str(model_dir / "decoder.int8.onnx"),
            joiner=str(model_dir / "joiner.int8.onnx"),
            num_threads=self.num_threads,
            sample_rate=SAMPLE_RATE,
            feature_dim=80,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=2.4,
            rule2_min_trailing_silence=1.2,
            rule3_min_utterance_length=300,
            decoding_method="greedy_search",
            model_type="nemo_transducer",
        )
        vad_cfg = sherpa_onnx.VadModelConfig()
        vad_cfg.silero_vad.model = str(self.models_dir / "silero_vad.onnx")
        vad_cfg.silero_vad.threshold = 0.5
        vad_cfg.silero_vad.min_silence_duration = 0.4
        vad_cfg.sample_rate = SAMPLE_RATE
        vad = sherpa_onnx.VoiceActivityDetector(
            vad_cfg, buffer_size_in_seconds=30
        )
        return recognizer, vad

    def _load_modules(self):
        """Import the heavy dependencies (worker thread only; test seam)."""
        import numpy
        import sherpa_onnx
        return numpy, sherpa_onnx

    def _run(self):
        # Imports sit inside the guard: a half-installed venv (numpy or
        # sherpa-onnx missing) must fail with a ready=False status, not a
        # silent thread death via the default excepthook.
        try:
            np, sherpa_onnx = self._load_modules()
            recognizer, vad = self._build(sherpa_onnx)
        except Exception as exc:
            print(f"asr: model load failed: {exc}", file=sys.stderr)
            self._ready = False
            self._drain_queue()
            self._emit_status()
            return
        stream = recognizer.create_stream()
        self._ready = True
        self._emit_status()
        try:
            self._decode_loop(np, recognizer, vad, stream)
        except Exception as exc:
            # A mid-session crash (onnxruntime error, bad frame) must never
            # leave ready=True on a dead worker: flag it, empty the dead
            # queue so stats reflect death, tell the clients, exit.
            print(f"asr: decode loop crashed: {exc}", file=sys.stderr)
            self._ready = False
            self._drain_queue()
            self._emit_status()

    def _decode_loop(self, np, recognizer, vad, stream):
        segment = 0
        prev_text = ""
        speaking = False
        vad_buf = np.zeros(0, dtype=np.float32)

        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            if self._behind and self._queue.qsize() < QUEUE_MAX // 2:
                self._behind = False
                self._emit_status()

            data = item
            if len(data) % 2:
                data = data[:-1]
            if not data:
                continue
            samples = (
                np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
            )

            # VAD: fixed 512-sample windows; drain so its buffer stays flat.
            vad_buf = np.concatenate([vad_buf, samples])
            while vad_buf.size >= VAD_WINDOW:
                vad.accept_waveform(vad_buf[:VAD_WINDOW])
                vad_buf = vad_buf[VAD_WINDOW:]
                while not vad.empty():
                    vad.pop()
            now_speaking = bool(vad.is_speech_detected())
            if now_speaking != speaking:
                speaking = now_speaking
                self._emit({"kind": "vad", "speaking": speaking})

            # Recognizer: any chunk length is fine.
            stream.accept_waveform(SAMPLE_RATE, samples)
            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)
            result = json.loads(recognizer.get_result_as_json_string(stream))
            text = result.get("text", "")
            tokens = result.get("tokens", [])
            if text != prev_text:
                self._emit(
                    {
                        "kind": "partial",
                        "segment": segment,
                        "text": text,
                        "tokens": tokens,
                    }
                )
                prev_text = text
            if recognizer.is_endpoint(stream):
                if text:
                    self._emit(
                        {
                            "kind": "final",
                            "segment": segment,
                            "text": text,
                            "tokens": tokens,
                        }
                    )
                recognizer.reset(stream)
                segment += 1
                prev_text = ""
