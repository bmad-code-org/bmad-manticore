#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for run_prompter.py (mc-prompter Phase A launcher).

Run directly:
    uv run skills/mc-prompter/scripts/tests/test-run_prompter.py

Pure stdlib unittest. Unit-tests the port-probe, health-identity, port
selection, session-file, and command-building helpers by importing them.
Nothing spawns the real server (spawn logic lives only in main(), which
these tests never call); the only sockets used are loopback listeners
opened inside the tests themselves.
"""

import http.server
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent))

import run_prompter as rp  # noqa: E402


def ephemeral_listener():
    """An open loopback TCP listener on an OS-assigned port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


class HealthServer:
    """Tiny loopback HTTP server answering /health with a fixed payload."""

    def __init__(self, payload):
        body = json.dumps(payload).encode("utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/health":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestPortProbe(unittest.TestCase):

    def test_busy_port_is_not_free(self):
        sock, port = ephemeral_listener()
        try:
            self.assertFalse(rp.port_is_free(port))
        finally:
            sock.close()

    def test_released_port_is_free(self):
        sock, port = ephemeral_listener()
        sock.close()
        self.assertTrue(rp.port_is_free(port))


class TestHealthIdentity(unittest.TestCase):

    def test_recognizes_mc_prompter(self):
        server = HealthServer(
            {"app": "mc-prompter", "session": "abcd1234",
             "script": "script.md", "started": "2026-07-09T10:00:00"}
        )
        try:
            info = rp.health_identity(server.port)
            self.assertIsNotNone(info)
            self.assertEqual(info["session"], "abcd1234")
        finally:
            server.stop()

    def test_rejects_other_app(self):
        server = HealthServer({"app": "something-else"})
        try:
            self.assertIsNone(rp.health_identity(server.port))
        finally:
            server.stop()

    def test_none_when_unreachable(self):
        sock, port = ephemeral_listener()
        sock.close()
        self.assertIsNone(rp.health_identity(port, timeout=0.2))


class TestPickPort(unittest.TestCase):

    def test_free_requested_port_is_used(self):
        port = rp.pick_port(
            8770, explicit=False,
            probe=lambda p: True, identify=lambda p: None,
        )
        self.assertEqual(port, 8770)

    def test_auto_increments_past_busy_ports(self):
        port = rp.pick_port(
            8770, explicit=False,
            probe=lambda p: p >= 8772, identify=lambda p: None,
        )
        self.assertEqual(port, 8772)

    def test_explicit_busy_port_raises_conflict(self):
        info = {"app": "mc-prompter", "session": "abcd1234",
                "script": "x.md", "started": "2026-07-09T10:00:00"}
        with self.assertRaises(rp.PortConflict) as ctx:
            rp.pick_port(
                9000, explicit=True,
                probe=lambda p: False, identify=lambda p: info,
            )
        self.assertEqual(ctx.exception.port, 9000)
        self.assertEqual(ctx.exception.info, info)

    def test_scan_limit_exhaustion_raises_runtime_error(self):
        with self.assertRaises(RuntimeError):
            rp.pick_port(
                8770, explicit=False,
                probe=lambda p: False, identify=lambda p: None, limit=3,
            )


class TestSessionFile(unittest.TestCase):

    def test_path_shape(self):
        path = rp.session_file_path(8770)
        self.assertEqual(path.name, "session-8770.json")
        self.assertEqual(path.parent.name, "mc-prompter")
        self.assertTrue(str(path).startswith(tempfile.gettempdir()))

    def test_write_session_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "session-8771.json"
            rp.write_session_file(
                path, port=8771, pid=4242, token="tok123",
                script="/abs/script.md",
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["port"], 8771)
            self.assertEqual(data["pid"], 4242)
            self.assertEqual(data["token"], "tok123")
            self.assertEqual(data["script"], "/abs/script.md")
            self.assertIn("T", data["started"])


class TestBuildServerCmd(unittest.TestCase):

    def test_command_shape(self):
        cmd = rp.build_server_cmd(
            8770, "127.0.0.1", "/abs/script.md", 150,
            "/tmp/mc-prompter/session-8770.json", "tok123",
        )
        self.assertEqual(cmd[:2], ["uv", "run"])
        self.assertIn("--with", cmd)
        self.assertIn("aiohttp==3.12.15", cmd)
        self.assertIn("-m", cmd)
        self.assertIn("server.main", cmd)
        self.assertEqual(cmd[cmd.index("--port") + 1], "8770")
        self.assertEqual(cmd[cmd.index("--host") + 1], "127.0.0.1")
        self.assertEqual(cmd[cmd.index("--script") + 1], "/abs/script.md")
        self.assertEqual(cmd[cmd.index("--owner-wpm") + 1], "150")
        self.assertEqual(cmd[cmd.index("--token") + 1], "tok123")

    def test_empty_script_and_zero_wpm(self):
        cmd = rp.build_server_cmd(8770, "0.0.0.0", "", 0, "s.json", "t")
        self.assertEqual(cmd[cmd.index("--script") + 1], "")
        self.assertEqual(cmd[cmd.index("--owner-wpm") + 1], "0")


class TestSpawnAndTerminate(unittest.TestCase):

    def test_terminate_server_reaps_the_process_group(self):
        child = rp.spawn_server(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            rp.terminate_server(child, timeout=5.0)
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_terminate_server_on_already_dead_child(self):
        child = rp.spawn_server([sys.executable, "-c", "pass"])
        child.wait()
        rp.terminate_server(child, timeout=5.0)
        self.assertIsNotNone(child.poll())

    def test_startup_timeout_allows_cold_uv_cache(self):
        # cold uv caches can take minutes to provision aiohttp; the health
        # wait must not give up after the old 30 s deadline
        self.assertGreaterEqual(rp.STARTUP_TIMEOUT, 300.0)


class TestLocalIp(unittest.TestCase):

    def test_returns_a_string(self):
        ip = rp.local_ip()
        self.assertIsInstance(ip, str)
        self.assertGreaterEqual(ip.count("."), 3)


if __name__ == "__main__":
    unittest.main()
