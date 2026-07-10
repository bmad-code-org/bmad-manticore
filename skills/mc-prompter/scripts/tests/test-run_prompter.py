#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for run_prompter.py (mc-prompter launcher).

Run directly:
    uv run skills/mc-prompter/scripts/tests/test-run_prompter.py

Pure stdlib unittest. Unit-tests the port-probe, health-identity, port
selection, session-file, command-building, workspace-readiness, and
launch-message helpers by importing them. Nothing spawns the real server:
main() is called only on the planned-provider path, which fails fast with
exit 3 before script validation or any spawn. The only sockets used are
loopback listeners opened inside the tests themselves. Ready-looking
workspaces are fabricated with sparse files truncated to the size floors;
no models, no downloads.
"""

import contextlib
import http.server
import io
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


def fabricate_workspace(root):
    """A ready-looking prompter-lab: venv python + models at size floors."""
    ws = Path(root)
    py = rp.venv_python_path(ws)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_bytes(b"")
    for rel, min_size in rp.WORKSPACE_MODEL_MIN_SIZES.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.truncate(min_size)
    return ws


class TestWorkspaceReadiness(unittest.TestCase):

    def test_venv_python_path_posix(self):
        p = rp.venv_python_path("/ws", platform="darwin")
        self.assertEqual(p, Path("/ws/.venv/bin/python"))

    def test_venv_python_path_windows(self):
        p = rp.venv_python_path("/ws", platform="win32")
        self.assertEqual(p, Path("/ws/.venv/Scripts/python.exe"))

    def test_empty_workspace_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(rp.workspace_ready(tmp))

    def test_fabricated_workspace_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            fabricate_workspace(tmp)
            self.assertTrue(rp.workspace_ready(tmp))

    def test_truncated_model_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = fabricate_workspace(tmp)
            short = ws / "models" / "nemotron-streaming" / "encoder.int8.onnx"
            with open(short, "wb") as f:
                f.truncate(1000)
            self.assertFalse(rp.workspace_ready(ws))

    def test_missing_venv_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = fabricate_workspace(tmp)
            rp.venv_python_path(ws).unlink()
            self.assertFalse(rp.workspace_ready(ws))


class TestSpawnPlan(unittest.TestCase):

    def test_no_workspace_falls_back_to_none(self):
        self.assertEqual(rp.spawn_plan(None, None), (None, "none"))

    def test_no_workspace_ignores_a_requested_provider(self):
        self.assertEqual(
            rp.spawn_plan(None, "nemotron-streaming"), (None, "none"))

    def test_not_ready_workspace_falls_back_to_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            self.assertEqual(rp.spawn_plan(ws, None), (None, "none"))

    def test_ready_workspace_defaults_the_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = fabricate_workspace(tmp)
            self.assertEqual(
                rp.spawn_plan(ws, None), (ws, "nemotron-streaming"))

    def test_ready_workspace_passes_the_provider_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = fabricate_workspace(tmp)
            self.assertEqual(rp.spawn_plan(ws, "none"), (ws, "none"))


class TestPlannedProviderFailsFast(unittest.TestCase):

    def test_zipformer_small_exits_3_before_any_spawn(self):
        # the nonexistent script proves the planned-lane check runs first:
        # main() returns 3 before script validation, port probing, or spawn
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = rp.main(
                ["--script", "/definitely/not/there.md",
                 "--asr-provider", "zipformer-small"]
            )
        self.assertEqual(rc, 3)
        self.assertIn("zipformer-small", err.getvalue())
        self.assertIn("planned", err.getvalue())


class TestDowngradeNotice(unittest.TestCase):

    def test_ready_workspace_produces_no_notice(self):
        ws = Path("/ws")
        self.assertEqual(
            rp.downgrade_notice(ws, "nemotron-streaming", ws), [])

    def test_not_ready_workspace_names_the_dropped_provider(self):
        text = "\n".join(
            rp.downgrade_notice(Path("/ws"), "nemotron-streaming", None))
        self.assertIn("workspace not ready", text)
        self.assertIn("nemotron-streaming", text)
        self.assertIn("ensure_workspace.py", text)

    def test_not_ready_workspace_default_provider_no_provider_line(self):
        text = "\n".join(rp.downgrade_notice(Path("/ws"), None, None))
        self.assertIn("workspace not ready", text)
        self.assertNotIn("--asr-provider", text)

    def test_explicit_provider_without_workspace_is_called_out(self):
        lines = rp.downgrade_notice(None, "nemotron-streaming", None)
        self.assertEqual(len(lines), 1)
        self.assertIn("nemotron-streaming", lines[0])
        self.assertIn("--workspace", lines[0])

    def test_tier1_defaults_are_silent(self):
        self.assertEqual(rp.downgrade_notice(None, None, None), [])
        self.assertEqual(rp.downgrade_notice(None, "none", None), [])


class TestVoiceFollowLine(unittest.TestCase):

    def test_available_with_a_real_provider(self):
        line = rp.voice_follow_line(Path("/ws"), "nemotron-streaming")
        self.assertIn("available", line)
        self.assertIn("nemotron-streaming", line)

    def test_off_when_provider_none_even_with_a_workspace(self):
        # a creator who chose --asr-provider none must not read "available"
        line = rp.voice_follow_line(Path("/ws"), "none")
        self.assertIn("off", line)
        self.assertNotIn("available", line)

    def test_off_without_a_workspace(self):
        self.assertIn("off", rp.voice_follow_line(None, "none"))


class TestBuildServerCmdWorkspace(unittest.TestCase):

    def test_workspace_command_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cmd = rp.build_server_cmd(
                8770, "127.0.0.1", "/abs/script.md", 150,
                "/tmp/mc-prompter/session-8770.json", "tok123",
                workspace=ws, asr_provider="nemotron-streaming",
            )
            self.assertEqual(cmd[0], str(rp.venv_python_path(ws)))
            self.assertEqual(cmd[1:3], ["-m", "server.main"])
            self.assertNotIn("uv", cmd)
            self.assertEqual(
                cmd[cmd.index("--models-dir") + 1], str(ws / "models"))
            self.assertEqual(
                cmd[cmd.index("--asr-provider") + 1], "nemotron-streaming")
            self.assertEqual(cmd[cmd.index("--port") + 1], "8770")
            self.assertEqual(cmd[cmd.index("--token") + 1], "tok123")

    def test_fallback_command_carries_provider_none(self):
        cmd = rp.build_server_cmd(
            8770, "127.0.0.1", "/abs/script.md", 150, "s.json", "t",
        )
        self.assertEqual(cmd[:2], ["uv", "run"])
        self.assertNotIn("--models-dir", cmd)
        self.assertEqual(cmd[cmd.index("--asr-provider") + 1], "none")


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
