#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for server/llm.py (mc-prompter Phase C Ollama client).

Run directly:
    uv run skills/mc-prompter/scripts/tests/test-llm.py

Pure stdlib unittest. The OllamaClient is exercised against a local
http.server stub speaking the /api/chat protocol on an ephemeral loopback
port; every request body is captured and asserted against the Phase C
contract (think false, stream false, structured-output format schema,
options, keep_alive, stable-prefix message order). No Ollama, no models,
no network beyond the in-process stub.
"""

import asyncio
import http.server
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from server import llm  # noqa: E402

STATE_BLOCK = (
    "SHOW: test\n"
    "SEGMENT g1 'Points' kind bullets planned 290s\n"
    "  point 0 [uncovered]: local models cost nothing per token\n"
)
STATUS_BLOCK = (
    "CLOCK: elapsed 10s | remaining 290s | show-state green | LIVE\n"
    "SEGMENT g1: current | replanned 290s spent 10s timing green"
)
TAIL = "so you never pay per token with a local model"
GOOD_RESULT = {
    "coverage": [{"segment": "g1", "point": 0, "confidence": 0.92}],
    "current-topic": "cost",
    "cue": None,
    "pace-note": "on pace",
}


class ChatStub:
    """Loopback /api/chat stub: captures requests, serves a canned reply.

    reply_body may be bytes (sent verbatim) or a dict (JSON-encoded).
    status and delay_s shape the error/timeout scenarios.
    """

    def __init__(self, reply_body, status=200, delay_s=0.0):
        if isinstance(reply_body, dict):
            reply_body = json.dumps(reply_body).encode("utf-8")
        self.requests = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                stub.requests.append(
                    {
                        "path": self.path,
                        "content-type": self.headers.get("Content-Type"),
                        "body": json.loads(raw.decode("utf-8")),
                    }
                )
                if delay_s:
                    time.sleep(delay_s)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply_body)))
                self.end_headers()
                self.wfile.write(reply_body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.endpoint = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def envelope(result):
    """A well-formed Ollama /api/chat non-streaming response envelope."""
    return {
        "model": "stub-model",
        "message": {"role": "assistant", "content": json.dumps(result)},
        "done": True,
    }


def run_tick(client, state_block=STATE_BLOCK, status_block=STATUS_BLOCK,
             tail=TAIL):
    return asyncio.run(client.tick(state_block, status_block, tail))


class TestRequestShape(unittest.TestCase):
    """The captured /api/chat request carries the contract's exact knobs."""

    @classmethod
    def setUpClass(cls):
        cls.stub = ChatStub(envelope(GOOD_RESULT))
        cls.client = llm.OllamaClient(cls.stub.endpoint, "qwen3:4b")
        cls.result = run_tick(cls.client)

    @classmethod
    def tearDownClass(cls):
        cls.stub.stop()

    def request(self):
        self.assertEqual(len(self.stub.requests), 1)
        return self.stub.requests[0]

    def test_posts_api_chat_as_json(self):
        req = self.request()
        self.assertEqual(req["path"], "/api/chat")
        self.assertEqual(req["content-type"], "application/json")

    def test_model_stream_think(self):
        body = self.request()["body"]
        self.assertEqual(body["model"], "qwen3:4b")
        self.assertIs(body["stream"], False)
        self.assertIs(body["think"], False)

    def test_options_and_keep_alive(self):
        body = self.request()["body"]
        self.assertEqual(
            body["options"], {"temperature": 0, "num_predict": 220}
        )
        self.assertEqual(body["keep_alive"], "30m")

    def test_format_is_the_result_schema(self):
        body = self.request()["body"]
        self.assertEqual(body["format"], llm.RESULT_SCHEMA)
        self.assertEqual(body["format"]["required"], ["coverage"])
        coverage_item = body["format"]["properties"]["coverage"]["items"]
        self.assertEqual(
            coverage_item["required"],
            ["segment", "point", "confidence", "evidence"],
        )

    def test_stable_prefix_message_order(self):
        # system message first: static persona, then the rundown/coverage
        # block (changes only on coverage flips); ALL volatile content,
        # the clock/replan status block and the rolling transcript tail,
        # rides in the LAST (user) message, so Ollama prefix caching
        # skips the static part on every tick.
        messages = self.request()["body"]["messages"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "system")
        self.assertTrue(
            messages[0]["content"].startswith(llm.SYSTEM_PERSONA)
        )
        self.assertIn(STATE_BLOCK, messages[0]["content"])
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn(STATUS_BLOCK, messages[-1]["content"])
        self.assertIn(TAIL, messages[-1]["content"])
        # the status block must precede the tail inside the user message
        self.assertLess(
            messages[-1]["content"].index(STATUS_BLOCK),
            messages[-1]["content"].index(TAIL),
        )
        # nothing volatile may leak into the system message: one changed
        # byte there would defeat the prefix cache
        self.assertNotIn(STATUS_BLOCK, messages[0]["content"])
        self.assertNotIn("elapsed", messages[0]["content"])
        self.assertNotIn("spent", messages[0]["content"])
        self.assertNotIn(TAIL, messages[0]["content"])

    def test_valid_response_parses(self):
        self.assertEqual(self.result, GOOD_RESULT)


class TestResultHandling(unittest.TestCase):

    def tick_against(self, reply_body, status=200):
        stub = ChatStub(reply_body, status=status)
        try:
            client = llm.OllamaClient(stub.endpoint, "m")
            return run_tick(client)
        finally:
            stub.stop()

    def test_coverage_coerced_to_list_when_missing(self):
        result = self.tick_against(envelope({"pace-note": "fine"}))
        self.assertEqual(result["coverage"], [])
        self.assertEqual(result["pace-note"], "fine")

    def test_coverage_coerced_to_list_when_mistyped(self):
        result = self.tick_against(envelope({"coverage": "g1"}))
        self.assertEqual(result["coverage"], [])

    def test_malformed_content_json_returns_none(self):
        env = envelope(GOOD_RESULT)
        env["message"]["content"] = "{not json"
        self.assertIsNone(self.tick_against(env))

    def test_non_object_content_returns_none(self):
        env = envelope(GOOD_RESULT)
        env["message"]["content"] = json.dumps(["a", "list"])
        self.assertIsNone(self.tick_against(env))

    def test_malformed_envelope_returns_none(self):
        self.assertIsNone(self.tick_against(b"{broken envelope"))

    def test_missing_message_key_returns_none(self):
        self.assertIsNone(self.tick_against({"done": True}))

    def test_http_error_returns_none(self):
        self.assertIsNone(
            self.tick_against({"error": "model not found"}, status=404)
        )

    def test_connection_refused_returns_none(self):
        # bind then close a socket so the port is known-dead
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        client = llm.OllamaClient(f"http://127.0.0.1:{port}", "m",
                                  timeout_s=2.0)
        self.assertIsNone(run_tick(client))


class TestTimeout(unittest.TestCase):

    def test_slow_server_times_out_to_none(self):
        # the stub sleeps well past the client deadline: the tick must
        # return None near the deadline, never wait for the reply
        stub = ChatStub(envelope(GOOD_RESULT), delay_s=1.5)
        try:
            client = llm.OllamaClient(stub.endpoint, "m", timeout_s=0.3)
            started = time.monotonic()
            result = run_tick(client)
            elapsed = time.monotonic() - started
            self.assertIsNone(result)
            self.assertLess(elapsed, 1.2)
        finally:
            stub.stop()

    def test_slow_drip_response_bounded_end_to_end(self):
        # a peer dripping bytes forever resets urllib's per-socket-op
        # timeout on every read; _post_chat's own deadline must bound the
        # WORKER THREAD itself (this is what keeps shutdown from hanging
        # on an in-flight tick against a wedged Ollama)
        class DripHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "10000000")
                self.end_headers()
                try:
                    for _ in range(200):
                        self.wfile.write(b"x" * 10)
                        self.wfile.flush()
                        time.sleep(0.05)
                except OSError:
                    pass  # client hung up: expected

            def log_message(self, *args):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), DripHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            client = llm.OllamaClient(
                f"http://127.0.0.1:{httpd.server_address[1]}", "m",
                timeout_s=0.3,
            )
            payload = client.request_payload(STATE_BLOCK, STATUS_BLOCK, TAIL)
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                client._post_chat(payload)
            # roughly timeout_s plus one socket operation, never the full
            # drip duration (~10 s here)
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestClientBasics(unittest.TestCase):

    def test_endpoint_trailing_slash_normalized(self):
        client = llm.OllamaClient("http://localhost:11434/", "m")
        self.assertEqual(client.endpoint, "http://localhost:11434")

    def test_payload_is_json_serializable(self):
        client = llm.OllamaClient("http://localhost:11434", "m")
        payload = client.request_payload(STATE_BLOCK, STATUS_BLOCK, TAIL)
        json.dumps(payload)  # must not raise

    def test_default_timeout(self):
        client = llm.OllamaClient("http://localhost:11434", "m")
        self.assertEqual(client.timeout_s, llm.DEFAULT_TIMEOUT_S)


if __name__ == "__main__":
    unittest.main()
