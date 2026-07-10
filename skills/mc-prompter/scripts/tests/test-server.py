#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp==3.12.15"]
# ///
"""Tests for server/main.py (mc-prompter Phase A).

Run directly:
    uv run skills/mc-prompter/scripts/tests/test-server.py

Stdlib unittest with aiohttp declared in the PEP 723 block; the app runs
in-process via aiohttp's test utilities. No network beyond the in-process
test server, no models, no downloads. Static pages are exercised against a
temp static dir with placeholder HTML (the real UI is built separately);
the missing-UI 503 path is covered explicitly.
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from aiohttp.test_utils import AioHTTPTestCase  # noqa: E402

from server import main as server_main  # noqa: E402

SAMPLE = (
    "# Test Script\n\n"
    "Hello world, this is the opening line.\n\n"
    "## Section One\n\n"
    "More speakable words live here today.\n"
)
TOKEN = "test-token-0123456789abcdef"
PAGE_ROUTES = ("/", "/prompt", "/remote", "/overlay")


async def recv_json(ws, timeout=5.0):
    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    return json.loads(msg.data)


class ServerTestBase(AioHTTPTestCase):

    build_static = True

    async def get_application(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="mc-prompter-test-"))
        self.script_path = self.tmpdir / "script.md"
        self.script_path.write_text(SAMPLE, encoding="utf-8")
        static_dir = self.tmpdir / "static"
        static_dir.mkdir()
        if self.build_static:
            for name in server_main.PAGES.values():
                (static_dir / name).write_text(
                    f"<h1>{name}</h1>", encoding="utf-8"
                )
        self.state = server_main.AppState(
            token=TOKEN, owner_wpm=150, static_dir=static_dir
        )
        self.state.port = 0
        self.state.load_script(self.script_path)
        return server_main.create_app(self.state)


class TestHttpSurface(ServerTestBase):

    async def test_health_shape(self):
        resp = await self.client.get("/health")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["app"], "mc-prompter")
        self.assertEqual(data["version"], "0.1.0")
        self.assertEqual(data["session"], TOKEN[:8])
        self.assertEqual(len(data["session"]), 8)
        self.assertEqual(data["script"], "script.md")
        self.assertIn("port", data)
        self.assertIn("T", data["started"])

    async def test_pages_return_html(self):
        for route in PAGE_ROUTES:
            resp = await self.client.get(route)
            self.assertEqual(resp.status, 200, route)
            self.assertEqual(resp.content_type, "text/html", route)

    async def test_static_asset_served(self):
        resp = await self.client.get("/static/home.html")
        self.assertEqual(resp.status, 200)

    async def test_static_traversal_blocked(self):
        resp = await self.client.get("/static/../script.md")
        self.assertIn(resp.status, (403, 404))

    async def test_api_state_initial(self):
        resp = await self.client.get("/api/state")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIsNone(data["snapshot"])
        self.assertEqual(data["doc-version"], 1)
        self.assertEqual(data["script"]["title"], "Test Script")
        self.assertGreater(data["script"]["word-count"], 0)
        self.assertEqual(data["config"]["owner-wpm"], 150)

    async def test_api_source_roundtrip(self):
        resp = await self.client.get("/api/source")
        data = await resp.json()
        self.assertEqual(data["raw"], SAMPLE)
        self.assertEqual(data["path"], str(self.script_path))
        self.assertEqual(data["doc"]["title"], "Test Script")
        self.assertEqual(data["doc-version"], 1)

        new_raw = "# Edited\n\nNew words to speak.\n"
        resp = await self.client.post(
            "/api/source", json={"raw": new_raw, "save": True}
        )
        self.assertEqual(resp.status, 200)
        posted = await resp.json()
        self.assertEqual(posted["doc-version"], 2)
        backup = Path(posted["backup"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), SAMPLE)
        self.assertEqual(
            self.script_path.read_text(encoding="utf-8"), new_raw
        )
        backup.unlink()

        resp = await self.client.get("/api/source")
        data = await resp.json()
        self.assertEqual(data["raw"], new_raw)
        self.assertEqual(data["doc"]["title"], "Edited")

    async def test_api_source_apply_without_save(self):
        new_raw = "Session only text.\n"
        resp = await self.client.post(
            "/api/source", json={"raw": new_raw, "save": False}
        )
        posted = await resp.json()
        self.assertEqual(posted["doc-version"], 2)
        self.assertIsNone(posted["backup"])
        self.assertEqual(
            self.script_path.read_text(encoding="utf-8"), SAMPLE
        )

    async def test_api_source_bad_body(self):
        resp = await self.client.post("/api/source", json={"save": True})
        self.assertEqual(resp.status, 400)

    async def test_rapid_saves_get_distinct_backups(self):
        resp1 = await self.client.post(
            "/api/source", json={"raw": "# One\n\nFirst.\n", "save": True}
        )
        resp2 = await self.client.post(
            "/api/source", json={"raw": "# Two\n\nSecond.\n", "save": True}
        )
        backup1 = Path((await resp1.json())["backup"])
        backup2 = Path((await resp2.json())["backup"])
        try:
            self.assertNotEqual(backup1, backup2)
            self.assertTrue(backup1.is_file())
            self.assertTrue(backup2.is_file())
            self.assertEqual(backup1.read_text(encoding="utf-8"), SAMPLE)
            self.assertEqual(
                backup2.read_text(encoding="utf-8"), "# One\n\nFirst.\n"
            )
        finally:
            backup1.unlink(missing_ok=True)
            backup2.unlink(missing_ok=True)

    async def test_save_failure_returns_400_not_500(self):
        self.state.script_path = self.tmpdir / "missing-dir" / "script.md"
        resp = await self.client.post(
            "/api/source", json={"raw": "New text.\n", "save": True}
        )
        self.assertEqual(resp.status, 400)
        data = await resp.json()
        self.assertIn("cannot save", data["error"])
        # the failed save must not have applied the edit
        resp = await self.client.get("/api/state")
        self.assertEqual((await resp.json())["doc-version"], 1)

    async def test_api_source_load(self):
        other = self.tmpdir / "other.md"
        other.write_text("# Other\n\nDifferent text.\n", encoding="utf-8")
        resp = await self.client.post(
            "/api/source/load", json={"path": str(other)}
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["doc-version"], 2)
        self.assertEqual(data["script"]["title"], "Other")

    async def test_api_source_load_bad_path(self):
        resp = await self.client.post(
            "/api/source/load", json={"path": str(self.tmpdir / "nope.md")}
        )
        self.assertEqual(resp.status, 400)
        data = await resp.json()
        self.assertIn("not a readable file", data["error"])


class TestMissingUi(ServerTestBase):

    build_static = False

    async def test_pages_answer_503_when_ui_absent(self):
        for route in PAGE_ROUTES:
            resp = await self.client.get(route)
            self.assertEqual(resp.status, 503, route)
            self.assertEqual(resp.content_type, "text/plain", route)
            self.assertIn("UI not built", await resp.text())


class TestWebSocket(ServerTestBase):

    async def hello(self, role):
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "hello", "role": role})
        welcome = await recv_json(ws)
        return ws, welcome

    async def test_welcome_and_leader_election(self):
        ws1, w1 = await self.hello("prompt")
        self.assertEqual(w1["type"], "welcome")
        self.assertTrue(w1["leader"])
        self.assertEqual(w1["session"], TOKEN[:8])
        self.assertEqual(w1["doc-version"], 1)
        self.assertIsNone(w1["snapshot"])

        ws2, w2 = await self.hello("prompt")
        self.assertFalse(w2["leader"])

        await ws1.close()
        role = await recv_json(ws2)
        self.assertEqual(role, {"type": "role", "leader": True})
        await ws2.close()

    async def test_cmd_relay_to_all_with_from_stamp(self):
        ws1, _ = await self.hello("prompt")
        ws2, _ = await self.hello("remote")
        await ws2.send_json({"type": "cmd", "cmd": "play"})
        for ws in (ws1, ws2):
            frame = await recv_json(ws)
            self.assertEqual(frame["type"], "cmd")
            self.assertEqual(frame["cmd"], "play")
            self.assertEqual(frame["from"], "remote")
        await ws1.close()
        await ws2.close()

    async def test_state_cached_and_fanned_out(self):
        ws1, _ = await self.hello("prompt")
        ws2, _ = await self.hello("remote")
        snapshot = {
            "type": "state", "position": 0.25, "section": "s1",
            "playing": True, "wpm": 150, "mode": "manual",
            "elapsed": 12.0, "remaining": 36.0, "countdown": None,
        }
        await ws1.send_json(snapshot)
        fanned = await recv_json(ws2)
        self.assertEqual(fanned, snapshot)

        resp = await self.client.get("/api/state")
        data = await resp.json()
        self.assertEqual(data["snapshot"], snapshot)

        ws3, w3 = await self.hello("overlay")
        self.assertEqual(w3["snapshot"], snapshot)
        await ws1.close()
        await ws2.close()
        await ws3.close()

    async def test_state_from_non_leader_ignored(self):
        ws1, w1 = await self.hello("prompt")
        ws2, w2 = await self.hello("prompt")
        self.assertTrue(w1["leader"])
        self.assertFalse(w2["leader"])
        await ws2.send_json({"type": "state", "position": 0.9})
        # a cmd on the same connection orders after the state frame, so its
        # relay proves the server already processed (and ignored) the state
        await ws2.send_json({"type": "cmd", "cmd": "pause"})
        frame = await recv_json(ws2)
        self.assertEqual(frame["type"], "cmd")
        resp = await self.client.get("/api/state")
        data = await resp.json()
        self.assertIsNone(data["snapshot"])
        await ws1.close()
        await ws2.close()

    async def test_doc_updated_broadcast_on_source_post(self):
        ws, _ = await self.hello("home")
        resp = await self.client.post(
            "/api/source", json={"raw": "New text.\n", "save": False}
        )
        self.assertEqual(resp.status, 200)
        frame = await recv_json(ws)
        self.assertEqual(frame, {"type": "doc-updated", "doc-version": 2})
        await ws.close()

    async def test_malformed_frame_gets_error(self):
        ws, _ = await self.hello("remote")
        await ws.send_str("this is not json")
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "error")
        self.assertIn("malformed", frame["message"])
        await ws.close()

    async def test_first_frame_must_be_hello(self):
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "cmd", "cmd": "play"})
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "error")
        self.assertIn("hello", frame["message"])
        await ws.close()

    async def test_bad_role_rejected(self):
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "hello", "role": "hacker"})
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "error")
        await ws.close()

    async def test_binary_frames_rejected(self):
        ws, _ = await self.hello("prompt")
        await ws.send_bytes(b"\x00\x01\x02")
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "error")
        self.assertIn("Phase B", frame["message"])
        await ws.close()


class TestBomHandling(unittest.TestCase):

    def test_utf8_sig_file_load_parses_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bom.md"
            path.write_text(
                "# Bom Title\n\nWords to speak.\n", encoding="utf-8-sig"
            )
            state = server_main.AppState(token=TOKEN)
            state.load_script(path)
            self.assertEqual(state.doc["title"], "Bom Title")
            self.assertFalse(state.raw.startswith("\ufeff"))

    def test_set_raw_strips_leading_bom(self):
        state = server_main.AppState(token=TOKEN)
        state.set_raw("\ufeff# Pasted Title\n\nText here.\n")
        self.assertEqual(state.doc["title"], "Pasted Title")
        self.assertFalse(state.raw.startswith("\ufeff"))


class TestNoScriptStartup(AioHTTPTestCase):

    async def get_application(self):
        state = server_main.AppState(token=TOKEN)
        state.port = 0
        return server_main.create_app(state)

    async def test_health_and_state_without_script(self):
        resp = await self.client.get("/health")
        data = await resp.json()
        self.assertIsNone(data["script"])
        resp = await self.client.get("/api/state")
        data = await resp.json()
        self.assertIsNone(data["script"])
        self.assertEqual(data["doc-version"], 0)
        self.assertIsNone(data["config"]["owner-wpm"])


if __name__ == "__main__":
    unittest.main()
