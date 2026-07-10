#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Ollama client for the mc-prompter producer's LLM tick (Phase C).

Pure stdlib (urllib in a worker thread behind asyncio.to_thread), so the
module works identically under both server launch paths (uv run with only
aiohttp, or the prompter-lab workspace venv). Imported lazily by
server/main.py only when --llm-provider ollama is selected; nothing here
runs in tier 1 or tier 2.

Contract:
    OllamaClient(endpoint, model, timeout_s=10.0)
    await client.tick(state_block, status_block, transcript_tail)
        -> dict | None

    tick() POSTs {endpoint}/api/chat with:
        stream      false (one complete JSON response)
        think       false (no reasoning tokens; the tick must stay cheap)
        options     {"temperature": 0, "num_predict": 220}
        keep_alive  "30m" (the model stays resident between ticks)
        format      RESULT_SCHEMA below (Ollama structured outputs)
        messages    stable prefix FIRST: one system message carrying the
                    static producer persona followed by the rundown with
                    per-point covered state (state_block, which changes
                    only when coverage flips). EVERYTHING volatile rides
                    in the single LAST user message: the clock/replan
                    numbers (status_block, changes every tick) and then
                    the rolling transcript tail. The split is the whole
                    point: one volatile byte in the system message would
                    invalidate Ollama's prefix cache and force a full
                    re-prefill of the rundown block on every tick.

    The whole request runs under a hard timeout: asyncio.wait_for bounds
    the awaited tick, the socket-level urllib timeout bounds each socket
    operation, and _post_chat additionally enforces an END-TO-END deadline
    across chunked reads, so even a slow-drip peer (each byte resetting
    the per-operation timeout) cannot pin the worker thread much past
    timeout_s; shutdown therefore never waits long on an in-flight tick.
    ANY failure (timeout, connection refused, HTTP error, malformed
    envelope, non-JSON content, content that is not an object) returns
    None: a failed tick is DROPPED, never queued or retried. The caller
    schedules the next tick adaptively.

Result shape (the schema below, enforced by Ollama's format field):
    {"coverage": [{"segment": "g1", "point": 0, "confidence": 0.9}, ...],
     "current-topic": "...",                    (optional)
     "cue": {"text": "...", "reason": "..."} | null,   (optional)
     "pace-note": "..."}                        (optional)
A returned dict always has a list under "coverage" (coerced to [] when the
model omits or mistypes it); everything else is passed through untouched
for the caller to validate per field.

Manual test against a live Ollama (never in CI):
    uv run llm.py --endpoint http://localhost:11434 --model qwen3:4b
prints one tick's parsed result for a canned state block.
"""

import asyncio
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_TIMEOUT_S = 10.0
KEEP_ALIVE = "30m"
NUM_PREDICT = 220

# Ollama structured-output schema (the wire "format" field). Binding shape
# from the Phase C contract; coverage is the only required key.
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "coverage": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "segment": {"type": "string"},
                    "point": {"type": "integer"},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "string"},
                },
                "required": ["segment", "point", "confidence", "evidence"],
            },
        },
        "current-topic": {"type": "string"},
        "cue": {
            "type": ["object", "null"],
            "properties": {
                "text": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
        "pace-note": {"type": "string"},
    },
    "required": ["coverage"],
}

# The producer persona and rules: concise, rule-based, and STATIC, so the
# system message's prefix is byte-identical across ticks (prefix caching).
SYSTEM_PERSONA = (
    "You are a silent broadcast producer watching a live show. "
    "You receive the rundown with per-point covered state; each user "
    "message brings the live clock and replan numbers, then the latest "
    "transcript tail. Rules: judge which UNCOVERED "
    "points the transcript tail's content actually covers; report each as "
    "its segment id, point index, a confidence between 0 and 1, and "
    "evidence: a short VERBATIM quote from the transcript tail (the exact "
    "words the speaker said that cover the point). A claim without a real "
    "quote will be discarded. "
    "A point counts as covered ONLY when the speaker explicitly said the "
    "specific thing the point names; being near the topic, or the show "
    "merely heading that way, is NOT coverage and must be reported with "
    "confidence 0.3 or lower. If the point names a concrete item (an "
    "anecdote, a demo, a number, a name) that the transcript never "
    "mentions, it is not covered. When unsure, use a low confidence: a "
    "missed point costs one reminder card, a wrong covered mark silences "
    "the reminder forever. Only report points that appear in the rundown, "
    "never invent points; optionally suggest at most ONE short cue in "
    "broadcast vocabulary (NEXT, WRAP, STRETCH, time remaining) with a "
    "one-line reason, or null when nothing is needed; keep pace-note to "
    "one short sentence."
)


class OllamaClient:
    """One Ollama /api/chat structured-output client for the producer tick."""

    def __init__(self, endpoint, model, timeout_s=DEFAULT_TIMEOUT_S):
        self.endpoint = str(endpoint).rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def request_payload(self, state_block, status_block, transcript_tail):
        """The exact /api/chat body for one tick (also the test surface).

        Stable prefix first: the system message opens with the static
        persona and carries the rundown/coverage block (state_block, which
        changes only on coverage flips). All volatile content, the
        clock/replan numbers (status_block) and the rolling transcript
        tail, sits together in the final user message so Ollama's prefix
        cache keeps the system message hot across ticks.
        """
        return {
            "model": self.model,
            "stream": False,
            "think": False,
            "keep_alive": KEEP_ALIVE,
            "options": {"temperature": 0, "num_predict": NUM_PREDICT},
            "format": RESULT_SCHEMA,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        SYSTEM_PERSONA
                        + "\n\nRUNDOWN AND COVERAGE:\n"
                        + state_block
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "SHOW CLOCK AND REPLAN:\n"
                        + status_block
                        + "\n\nTRANSCRIPT TAIL:\n"
                        + transcript_tail
                    ),
                },
            ],
        }

    def _post_chat(self, payload):
        """Blocking POST; returns the message content string. Worker thread.

        The urllib timeout bounds each SOCKET OPERATION, not the request:
        a peer dripping one byte per operation would keep resetting it
        forever, and this thread is not cancellable, so it would gate
        process exit. Reading in chunks with an end-to-end deadline check
        between reads bounds the whole call to roughly timeout_s plus one
        socket operation, whatever the peer does.
        """
        deadline = time.monotonic() + self.timeout_s
        request = urllib.request.Request(
            self.endpoint + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
            chunks = []
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("llm tick deadline exceeded")
                # read1, not read: read(n) buffers until n bytes arrive,
                # which would let a dripping peer pin the loop on one call
                chunk = resp.read1(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        envelope = json.loads(b"".join(chunks).decode("utf-8"))
        return envelope["message"]["content"]

    async def tick(self, state_block, status_block, transcript_tail):
        """One producer tick; the parsed result dict, or None on ANY failure.

        The urllib call runs in a worker thread; asyncio.wait_for enforces
        the hard deadline even if the socket stalls between reads (and
        _post_chat's own deadline bounds the thread itself). A tick that
        fails or times out is dropped (None), never queued.
        """
        payload = self.request_payload(
            state_block, status_block, transcript_tail
        )
        try:
            content = await asyncio.wait_for(
                asyncio.to_thread(self._post_chat, payload),
                timeout=self.timeout_s,
            )
            parsed = json.loads(content)
        except asyncio.CancelledError:
            raise
        except Exception:
            # timeout, refused connection, HTTP error, malformed envelope,
            # non-JSON content: all one outcome by contract
            return None
        if not isinstance(parsed, dict):
            return None
        if not isinstance(parsed.get("coverage"), list):
            parsed["coverage"] = []
        return parsed


def main(argv=None):
    """Manual one-tick smoke test against a live Ollama (not run in CI)."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default="http://localhost:11434")
    parser.add_argument("--model", default="qwen3:4b")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    state_block = (
        "SHOW: smoke test\n"
        "SEGMENT g0 'Intro' kind scripted planned 60s\n"
        "SEGMENT g1 'Points' kind bullets planned 240s\n"
        "  point 0 [uncovered]: local models cost nothing per token\n"
        "  point 1 [uncovered]: latency is lower on device\n"
    )
    status_block = (
        "CLOCK: elapsed 60s | remaining 240s | show-state green | LIVE\n"
        "SEGMENT g0: done | replanned 60s spent 60s timing yellow\n"
        "SEGMENT g1: current | replanned 240s spent 0s timing green"
    )
    tail = "so the thing about local models is you never pay per token"
    client = OllamaClient(args.endpoint, args.model, timeout_s=args.timeout)
    result = asyncio.run(client.tick(state_block, status_block, tail))
    if result is None:
        print("tick failed (is Ollama serving and the model pulled?)",
              file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
