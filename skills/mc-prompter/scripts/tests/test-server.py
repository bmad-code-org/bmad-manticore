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
import copy
import importlib
import io
import json
import shutil
import sys
import tempfile
import threading
import time
import types
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
    models_dir = None
    asr_provider = "none"

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
            token=TOKEN, owner_wpm=150, static_dir=static_dir,
            models_dir=self.models_dir, asr_provider=self.asr_provider,
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

    async def test_binary_frames_from_non_owner_rejected(self):
        # Phase B semantics: binary audio needs capture ownership; the
        # first stray frame gets an error, the rest are silently dropped.
        ws, _ = await self.hello("prompt")
        await ws.send_bytes(b"\x00\x01\x02")
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "error")
        self.assertIn("capture", frame["message"])
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


# ---------------------------------------------------------------------------
# Phase B: capture ownership, binary routing, ASR/aligner wiring.
# Everything below runs against STUB engine and aligner factories injected
# through the module-level seams; sherpa-onnx is never imported and the
# whole file still needs only aiohttp.
# ---------------------------------------------------------------------------


class StubEngine:
    """Stands in for asr.AsrEngine behind server_main.ENGINE_FACTORY."""

    def __init__(self, models_dir, provider, on_event):
        self.models_dir = models_dir
        self.provider = provider
        self.on_event = on_event
        self.fed = []
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def feed(self, pcm16_bytes):
        self.fed.append(pcm16_bytes)

    @property
    def stats(self):
        return {"queue": len(self.fed), "behind": False, "ready": True}


class StubAligner:
    """Stands in for align.Aligner behind server_main.ALIGNER_FACTORY."""

    def __init__(self, doc):
        self.doc = doc
        self._anchor = -1
        self.feeds = []
        self.set_calls = []

    def feed(self, tokens, segment, final):
        self.feeds.append((list(tokens), segment, final))
        self._anchor += 1
        return {"anchor": self._anchor, "moved": True, "held": False}

    def set_anchor(self, word_index):
        self.set_calls.append(word_index)
        self._anchor = word_index

    @property
    def anchor(self):
        return self._anchor


class AsrServerTestBase(ServerTestBase):
    """Server with ASR enabled and both factories stubbed."""

    models_dir = "/stub-models"  # never touched: the engine is a stub
    asr_provider = "nemotron-streaming"

    async def get_application(self):
        self.engines = []
        self.aligners = []

        def engine_factory(models_dir, provider, on_event):
            engine = StubEngine(models_dir, provider, on_event)
            self.engines.append(engine)
            return engine

        def aligner_factory(doc):
            aligner = StubAligner(doc)
            self.aligners.append(aligner)
            return aligner

        server_main.ENGINE_FACTORY = engine_factory
        server_main.ALIGNER_FACTORY = aligner_factory
        self.addCleanup(self._reset_factories)
        return await super().get_application()

    @staticmethod
    def _reset_factories():
        server_main.ENGINE_FACTORY = None
        server_main.ALIGNER_FACTORY = None

    async def hello(self, role):
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "hello", "role": role})
        welcome = await recv_json(ws)
        return ws, welcome

    async def request_capture(self, ws, retries=40):
        """Send capture-request until granted (a just-closed owner releases
        asynchronously), returning the reply frame."""
        for _ in range(retries):
            await ws.send_json({"type": "capture-request"})
            reply = await recv_json(ws)
            if reply["type"] == "capture-granted":
                return reply
            await asyncio.sleep(0.05)
        return reply


class TestCaptureOwnership(AsrServerTestBase):

    async def test_grant_deny_release(self):
        ws1, _ = await self.hello("prompt")
        ws2, _ = await self.hello("prompt")

        await ws1.send_json({"type": "capture-request"})
        self.assertEqual(await recv_json(ws1), {"type": "capture-granted"})

        await ws2.send_json({"type": "capture-request"})
        self.assertEqual(
            await recv_json(ws2),
            {"type": "capture-denied", "reason": "owned"},
        )

        # re-request by the current owner stays granted (idempotent)
        await ws1.send_json({"type": "capture-request"})
        self.assertEqual(await recv_json(ws1), {"type": "capture-granted"})

        await ws1.send_json({"type": "capture-release"})
        reply = await self.request_capture(ws2)
        self.assertEqual(reply, {"type": "capture-granted"})
        await ws1.close()
        await ws2.close()

    async def test_disconnect_releases_ownership(self):
        ws1, _ = await self.hello("prompt")
        ws2, _ = await self.hello("home")
        await ws1.send_json({"type": "capture-request"})
        self.assertEqual(await recv_json(ws1), {"type": "capture-granted"})
        await ws1.close()
        reply = await self.request_capture(ws2)
        self.assertEqual(reply, {"type": "capture-granted"})
        await ws2.close()

    async def test_stale_owner_is_reclaimed(self):
        # a hard-crashed owner page leaves a closed socket behind; a new
        # loopback capture-request must reclaim immediately instead of
        # being denied until the WS heartbeat reaps the ghost connection
        class _DeadWs:
            closed = True

        ghost = server_main.Client(_DeadWs(), "prompt", False, 999)
        self.state.capture_owner = ghost
        ws, _ = await self.hello("prompt")
        await ws.send_json({"type": "capture-request"})
        self.assertEqual(await recv_json(ws), {"type": "capture-granted"})
        self.assertIsNot(self.state.capture_owner, ghost)
        await ws.close()

    async def test_live_owner_still_denies(self):
        ws1, _ = await self.hello("prompt")
        ws2, _ = await self.hello("prompt")
        await ws1.send_json({"type": "capture-request"})
        self.assertEqual(await recv_json(ws1), {"type": "capture-granted"})
        await ws2.send_json({"type": "capture-request"})
        self.assertEqual(
            await recv_json(ws2),
            {"type": "capture-denied", "reason": "owned"},
        )
        await ws1.close()
        await ws2.close()

    async def test_non_loopback_request_denied(self):
        original = server_main.is_loopback
        server_main.is_loopback = lambda request: False
        self.addCleanup(setattr, server_main, "is_loopback", original)
        ws = await self.client.ws_connect(f"/ws?token={TOKEN}")
        await ws.send_json({"type": "hello", "role": "prompt"})
        self.assertEqual((await recv_json(ws))["type"], "welcome")
        await ws.send_json({"type": "capture-request"})
        self.assertEqual(
            await recv_json(ws),
            {"type": "capture-denied", "reason": "loopback-only"},
        )
        await ws.close()


class TestBinaryRouting(AsrServerTestBase):

    async def test_owner_frames_reach_engine(self):
        ws, _ = await self.hello("prompt")
        await ws.send_json({"type": "capture-request"})
        self.assertEqual(await recv_json(ws), {"type": "capture-granted"})
        await ws.send_bytes(b"\x01\x02\x03\x04")
        await ws.send_bytes(b"\x05\x06")
        # a trailing cmd proves the server has processed both binary frames
        await ws.send_json({"type": "cmd", "cmd": "play"})
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "cmd")
        self.assertEqual(
            self.state.engine.fed, [b"\x01\x02\x03\x04", b"\x05\x06"]
        )
        await ws.close()

    async def test_non_owner_gets_one_error_then_silence(self):
        ws1, _ = await self.hello("prompt")
        ws2, _ = await self.hello("prompt")
        await ws1.send_json({"type": "capture-request"})
        self.assertEqual(await recv_json(ws1), {"type": "capture-granted"})

        await ws2.send_bytes(b"\xaa\xbb")
        await ws2.send_bytes(b"\xcc\xdd")
        await ws2.send_json({"type": "cmd", "cmd": "pause"})
        first = await recv_json(ws2)
        self.assertEqual(first["type"], "error")
        self.assertIn("capture", first["message"])
        second = await recv_json(ws2)
        self.assertEqual(second["type"], "cmd")  # no second error frame
        self.assertEqual(self.state.engine.fed, [])
        await ws1.close()
        await ws2.close()

    async def test_engine_started_and_stopped_with_app(self):
        self.assertEqual(len(self.engines), 1)
        engine = self.engines[0]
        self.assertTrue(engine.started)
        self.assertEqual(engine.provider, "nemotron-streaming")
        self.assertEqual(str(engine.models_dir), "/stub-models")
        # cleanups run after the aiohttp app teardown, so this observes
        # the on_cleanup engine stop
        self.addCleanup(lambda: self.assertTrue(engine.stopped))


class TestAsrBroadcasts(AsrServerTestBase):

    async def test_partial_broadcast_and_anchor(self):
        ws, _ = await self.hello("prompt")
        self.state.engine.on_event(
            {
                "kind": "partial",
                "segment": 0,
                "text": "hey everyone",
                "tokens": [" hey", " every", "one"],
            }
        )
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "asr")
        self.assertEqual(frame["kind"], "partial")
        self.assertEqual(frame["segment"], 0)
        self.assertEqual(frame["text"], "hey everyone")
        self.assertNotIn("tokens", frame)  # tokens are omitted on the wire

        anchor = await recv_json(ws)
        self.assertEqual(anchor, {"type": "anchor", "i": 0, "held": False})
        self.assertEqual(
            self.state.aligner.feeds,
            [([" hey", " every", "one"], 0, False)],
        )
        await ws.close()

    async def test_final_feeds_aligner_with_final_true(self):
        ws, _ = await self.hello("prompt")
        self.state.engine.on_event(
            {"kind": "final", "segment": 2, "text": "done", "tokens": [" done"]}
        )
        frame = await recv_json(ws)
        self.assertEqual(frame["kind"], "final")
        await recv_json(ws)  # anchor frame
        self.assertEqual(self.state.aligner.feeds, [([" done"], 2, True)])
        await ws.close()

    async def test_vad_broadcast(self):
        ws, _ = await self.hello("overlay")
        self.state.engine.on_event({"kind": "vad", "speaking": True})
        self.assertEqual(
            await recv_json(ws), {"type": "vad", "speaking": True}
        )
        await ws.close()

    async def test_asr_status_broadcast_and_dedupe(self):
        ws, _ = await self.hello("prompt")
        status = {"kind": "status", "ready": True, "behind": False, "queue": 3}
        self.state.engine.on_event(status)
        frame = await recv_json(ws)
        self.assertEqual(
            frame,
            {"type": "asr-status", "ready": True, "behind": False, "queue": 3},
        )
        # an identical (ready, behind) status is not re-broadcast; the cmd
        # that follows it is the next frame the client sees
        self.state.engine.on_event(dict(status, queue=5))
        await ws.send_json({"type": "cmd", "cmd": "play"})
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "cmd")
        await ws.close()

    async def test_api_state_asr_block(self):
        resp = await self.client.get("/api/state")
        data = await resp.json()
        self.assertEqual(
            data["asr"],
            {
                "available": True,
                "provider": "nemotron-streaming",
                "ready": False,
            },
        )
        ws, _ = await self.hello("home")
        self.state.engine.on_event(
            {"kind": "status", "ready": True, "behind": False, "queue": 0}
        )
        await recv_json(ws)  # wait for the broadcast so the pump has run
        resp = await self.client.get("/api/state")
        data = await resp.json()
        self.assertTrue(data["asr"]["ready"])
        await ws.close()

    async def test_doc_edit_rebuilds_aligner(self):
        before = self.state.aligner
        self.assertIsNotNone(before)
        resp = await self.client.post(
            "/api/source", json={"raw": "# New\n\nOther words.\n", "save": False}
        )
        self.assertEqual(resp.status, 200)
        self.assertIsNot(self.state.aligner, before)

    async def test_doc_edit_broadcasts_anchor_reset(self):
        # the rebuild invalidates any previously broadcast anchor, so the
        # doc-updated frame is followed by an explicit anchor reset
        ws, _ = await self.hello("prompt")
        resp = await self.client.post(
            "/api/source", json={"raw": "# New\n\nOther words.\n", "save": False}
        )
        self.assertEqual(resp.status, 200)
        frame = await recv_json(ws)
        self.assertEqual(frame, {"type": "doc-updated", "doc-version": 2})
        frame = await recv_json(ws)
        self.assertEqual(frame, {"type": "anchor", "i": -1, "held": False})
        self.assertEqual(self.state.last_anchor, (-1, False))
        await ws.close()

    async def test_doc_load_broadcasts_anchor_reset(self):
        ws, _ = await self.hello("prompt")
        other = self.tmpdir / "other.md"
        other.write_text("# Other\n\nDifferent text.\n", encoding="utf-8")
        resp = await self.client.post(
            "/api/source/load", json={"path": str(other)}
        )
        self.assertEqual(resp.status, 200)
        frame = await recv_json(ws)
        self.assertEqual(frame, {"type": "doc-updated", "doc-version": 2})
        frame = await recv_json(ws)
        self.assertEqual(frame, {"type": "anchor", "i": -1, "held": False})
        await ws.close()


class TestAnchorSet(AsrServerTestBase):

    async def test_anchor_set_relays_and_broadcasts(self):
        ws1, _ = await self.hello("prompt")
        ws2, _ = await self.hello("remote")
        await ws2.send_json({"type": "anchor-set", "i": 42})
        expected = {"type": "anchor", "i": 42, "held": False}
        self.assertEqual(await recv_json(ws1), expected)
        self.assertEqual(await recv_json(ws2), expected)
        self.assertEqual(self.state.aligner.set_calls, [42])
        await ws1.close()
        await ws2.close()

    async def test_anchor_set_requires_integer(self):
        ws, _ = await self.hello("prompt")
        await ws.send_json({"type": "anchor-set", "i": "ten"})
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "error")
        self.assertIn("integer", frame["message"])
        await ws.close()


class TestTier1Regression(ServerTestBase):
    """ASR off (no --models-dir): the Phase A surface is untouched and no
    heavy module is ever imported. The whole test file, including the stub
    suites above, must pass with only aiohttp installed."""

    async def test_api_state_asr_block_off(self):
        resp = await self.client.get("/api/state")
        data = await resp.json()
        self.assertEqual(
            data["asr"],
            {"available": False, "provider": "none", "ready": False},
        )

    async def test_no_asr_modules_imported(self):
        self.assertNotIn("server.asr", sys.modules)
        self.assertNotIn("server.align", sys.modules)
        self.assertNotIn("sherpa_onnx", sys.modules)
        self.assertNotIn("numpy", sys.modules)
        self.assertIsNone(self.state.engine)
        self.assertIsNone(self.state.aligner)

    async def test_capture_request_denied_asr_off(self):
        # tier 1 has no engine: granting capture would stream audio into a
        # black hole, so the request is denied with an actionable reason
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "hello", "role": "prompt"})
        await recv_json(ws)
        await ws.send_json({"type": "capture-request"})
        self.assertEqual(
            await recv_json(ws),
            {"type": "capture-denied", "reason": "asr-off"},
        )
        await ws.close()

    async def test_anchor_set_errors_when_voice_follow_off(self):
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "hello", "role": "prompt"})
        await recv_json(ws)
        await ws.send_json({"type": "anchor-set", "i": 3})
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "error")
        self.assertIn("voice-follow", frame["message"])
        await ws.close()


class TestAsrEngineWorkerGuards(unittest.TestCase):
    """AsrEngine failure guards, exercised with fakes (no numpy, no sherpa).

    server/asr.py imports only stdlib at module import time; the heavy
    imports live inside the worker's guarded _load_modules. The module is
    popped from sys.modules after each test so TestTier1Regression's
    no-heavy-imports assertion stays meaningful regardless of test order.
    """

    def setUp(self):
        self.asr = importlib.import_module("server.asr")
        self.addCleanup(sys.modules.pop, "server.asr", None)

    @staticmethod
    def _wait_for(predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_load_failure_emits_ready_false_and_drains(self):
        # a half-installed venv (numpy/sherpa missing) or a bad model dir
        # must produce a ready=False status, never a silent thread death
        events = []
        release = threading.Event()

        class Exploding(self.asr.AsrEngine):
            def _load_modules(self):
                release.wait(5)
                raise RuntimeError("no numpy here")

        engine = Exploding("/nowhere", on_event=events.append)
        engine.start()
        engine.feed(b"\x01\x02")
        release.set()
        self.assertTrue(self._wait_for(lambda: not engine.alive))
        statuses = [e for e in events if e.get("kind") == "status"]
        self.assertTrue(statuses)
        self.assertFalse(statuses[-1]["ready"])
        self.assertFalse(engine.stats["ready"])
        self.assertEqual(engine.stats["queue"], 0)  # dead queue drained

    def test_decode_crash_flips_ready_false_and_drains(self):
        # a mid-session crash in the decode loop must not leave ready=True
        # on a dead worker with frames piling into the queue
        events = []

        class _CrashNp:
            float32 = "float32"

            @staticmethod
            def zeros(n, dtype=None):
                return None

            @staticmethod
            def frombuffer(data, dtype=None):
                raise RuntimeError("decode crash")

        class _StubRecognizer:
            def create_stream(self):
                return None

        class Crashing(self.asr.AsrEngine):
            def _load_modules(self):
                return _CrashNp(), None

            def _build(self, sherpa_onnx):
                return _StubRecognizer(), None

        engine = Crashing("/nowhere", on_event=events.append)
        engine.start()
        self.assertTrue(self._wait_for(lambda: engine.stats["ready"]))
        engine.feed(b"\x00\x00")
        self.assertTrue(self._wait_for(lambda: not engine.alive))
        statuses = [e for e in events if e.get("kind") == "status"]
        self.assertGreaterEqual(len(statuses), 2)
        self.assertTrue(statuses[0]["ready"])
        self.assertFalse(statuses[-1]["ready"])
        self.assertFalse(engine.stats["ready"])
        self.assertEqual(engine.stats["queue"], 0)

    def test_start_after_stop_begins_from_fresh_state(self):
        # stop() leaves its None sentinel (and possibly stale audio) in the
        # queue; a restart must not inherit them or stale ready/behind flags
        engine = self.asr.AsrEngine("/nowhere", on_event=lambda e: None)
        engine.stop()  # never started: just plants the sentinel
        engine.feed(b"\xaa\xbb")
        engine._ready = True
        engine._behind = True
        engine._dropped = 7
        engine._run = lambda: None  # instance shadow: no real worker
        engine.start()
        engine._thread.join(timeout=5)
        self.assertFalse(engine.stats["ready"])
        self.assertFalse(engine.stats["behind"])
        self.assertEqual(engine.stats["queue"], 0)
        self.assertEqual(engine._dropped, 0)


class TestProviderGuard(unittest.TestCase):
    """main() planned-lane and models-dir guards (no server is started)."""

    def _run_main(self, argv):
        import contextlib
        import io

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = server_main.main(argv)
        return code, err.getvalue()

    def test_planned_provider_exits_3(self):
        code, err = self._run_main(
            ["--token", "t", "--asr-provider", "zipformer-small",
             "--models-dir", "/tmp"]
        )
        self.assertEqual(code, 3)
        self.assertIn("zipformer-small", err)
        self.assertIn("planned", err)

    def test_unknown_provider_exits_3(self):
        code, err = self._run_main(
            ["--token", "t", "--asr-provider", "bogus"]
        )
        self.assertEqual(code, 3)
        self.assertIn("not implemented", err)

    def test_missing_models_dir_exits_4(self):
        code, err = self._run_main(
            ["--token", "t", "--asr-provider", "nemotron-streaming",
             "--models-dir", "/nonexistent-mc-prompter-models"]
        )
        self.assertEqual(code, 4)
        self.assertIn("models dir not found", err)


# ---------------------------------------------------------------------------
# Phase C: producer mode. Everything below runs against a STUB producer and
# a STUB LLM client injected through the module-level PRODUCER_FACTORY /
# LLM_FACTORY seams: server/producer.py, server/rundown.py, and a live
# Ollama are never needed, and the whole file still runs with only aiohttp.
# ---------------------------------------------------------------------------

# A parsed rundown in the parse_rundown() result shape (two scripted
# segments around one bullets segment).
RUNDOWN = {
    "show": "Test Show",
    "duration-s": 600,
    "cue-density": None,
    "wrap-s": 60,
    "warnings": [],
    "segments": [
        {"id": "g0", "title": "Intro", "kind": "scripted", "planned-s": 120,
         "body": "Welcome to the show everyone. Glad you are here today.",
         "points": []},
        {"id": "g1", "title": "Point 1: Costs", "kind": "bullets",
         "planned-s": 300, "body": "",
         "points": [{"text": "cloud bills compound monthly forever"},
                    {"text": "the graphics card anecdote"}]},
        {"id": "g2", "title": "Wrap", "kind": "scripted", "planned-s": 180,
         "body": "Thanks for watching and goodbye everyone.", "points": []},
    ],
}


class FakeClock:
    """A monotonic-clock stand-in: call it, advance .t by hand."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class StubProducer:
    """Stands in for producer.Producer behind server_main.PRODUCER_FACTORY.

    Implements the full Phase C producer API; every method records itself
    in .calls and mutates a plausible rail-state dict so the server's
    change-gated broadcast has something real to compare.
    """

    def __init__(self, rundown, cue_density="normal"):
        self.rundown = rundown
        self.cue_density = cue_density
        self.calls = []
        self.candidates = []
        self.tick_count = 0
        self._state = {
            "live": False, "hold": False, "elapsed-s": 0,
            "remaining-s": 600, "show-state": "green", "current": "g0",
            "next-point": {"segment": "g1", "idx": 0,
                           "text": "cloud bills compound monthly forever"},
            "segments": [
                {"id": "g0", "title": "Intro", "kind": "scripted",
                 "planned-s": 120, "replanned-s": 120, "spent-s": 0,
                 "state": "current", "timing": "green", "points": []},
                {"id": "g1", "title": "Point 1: Costs", "kind": "bullets",
                 "planned-s": 300, "replanned-s": 300, "spent-s": 0,
                 "state": "pending", "timing": "green",
                 "points": [
                     {"text": "cloud bills compound monthly forever",
                      "covered": False, "skipped": False},
                     {"text": "the graphics card anecdote",
                      "covered": False, "skipped": False}]},
                {"id": "g2", "title": "Wrap", "kind": "scripted",
                 "planned-s": 180, "replanned-s": 180, "spent-s": 0,
                 "state": "pending", "timing": "green", "points": []},
            ],
            "drop": None,
        }

    def _point(self, seg_id, point_idx):
        for seg in self._state["segments"]:
            if seg["id"] == seg_id:
                return seg["points"][point_idx]
        raise KeyError(seg_id)

    def go_live(self):
        self.calls.append(("go-live",))
        self._state["live"] = True

    def hold(self):
        self.calls.append(("hold",))
        self._state["hold"] = True

    def resume(self):
        self.calls.append(("resume",))
        self._state["hold"] = False

    def end_show(self):
        self.calls.append(("end",))
        self._state["live"] = False

    def mark_covered(self, seg_id, point_idx):
        self.calls.append(("covered", seg_id, point_idx))
        self._point(seg_id, point_idx)["covered"] = True

    def skip_point(self, seg_id, point_idx):
        self.calls.append(("skip", seg_id, point_idx))
        self._point(seg_id, point_idx)["skipped"] = True

    def make_current(self, seg_id):
        self.calls.append(("make-current", seg_id))
        self._state["current"] = seg_id

    def propose_coverage(self, seg_id, point_idx):
        self.calls.append(("propose", seg_id, point_idx))
        point = self._point(seg_id, point_idx)
        if not point["covered"] and not point["skipped"]:
            point["covered"] = True

    def tick(self):
        self.tick_count += 1
        return copy.deepcopy(self._state)

    def cue_candidates(self):
        return [dict(c) for c in self.candidates]

    @property
    def state(self):
        return copy.deepcopy(self._state)


class StubLlm:
    """Stands in for llm.OllamaClient behind server_main.LLM_FACTORY."""

    def __init__(self, endpoint, model):
        self.endpoint = endpoint
        self.model = model
        self.ticks = []
        self.result = None

    async def tick(self, state_block, status_block, transcript_tail):
        self.ticks.append((state_block, status_block, transcript_tail))
        return self.result


class ProducerServerTestBase(ServerTestBase):
    """Server with a rundown loaded and the producer/LLM seams stubbed.

    PRODUCER_TICK_S is patched huge so the background loop never ticks by
    itself: tests drive server_main.producer_tick(state) directly and stay
    deterministic.
    """

    llm_provider = "none"

    async def get_application(self):
        self.producers = []
        self.llms = []

        def producer_factory(rundown, cue_density):
            producer = StubProducer(rundown, cue_density)
            self.producers.append(producer)
            return producer

        def llm_factory(endpoint, model):
            client = StubLlm(endpoint, model)
            self.llms.append(client)
            return client

        server_main.PRODUCER_FACTORY = producer_factory
        server_main.LLM_FACTORY = llm_factory
        self._saved_tick_s = server_main.PRODUCER_TICK_S
        server_main.PRODUCER_TICK_S = 3600.0
        self.addCleanup(self._reset_producer_seams)
        app = await super().get_application()
        self.state.llm_provider = self.llm_provider
        self.state.load_rundown(copy.deepcopy(RUNDOWN))
        return app

    def _reset_producer_seams(self):
        server_main.PRODUCER_FACTORY = None
        server_main.LLM_FACTORY = None
        server_main.PRODUCER_TICK_S = self._saved_tick_s

    @property
    def producer(self):
        return self.producers[0]

    async def hello(self, role):
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "hello", "role": role})
        welcome = await recv_json(ws)
        return ws, welcome


class TestRundownLoadAndApi(ProducerServerTestBase):

    async def test_api_rundown_shape(self):
        resp = await self.client.get("/api/rundown")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["rundown"]["show"], "Test Show")
        self.assertEqual(len(data["rundown"]["segments"]), 3)
        ranges = data["segments"]
        self.assertEqual([r["id"] for r in ranges], ["g0", "g2"])
        for entry in ranges:
            self.assertEqual(
                sorted(entry), ["id", "word-end", "word-start"]
            )
        # scripted ranges are contiguous in the doc's word-index space
        self.assertEqual(ranges[0]["word-start"], 0)
        self.assertGreater(ranges[0]["word-end"], 0)
        self.assertEqual(ranges[1]["word-start"], ranges[0]["word-end"])

    async def test_prompt_doc_is_the_scripted_segments(self):
        resp = await self.client.get("/api/source")
        data = await resp.json()
        self.assertIn("## Intro", data["raw"])
        self.assertIn("## Wrap", data["raw"])
        self.assertIn("Welcome to the show", data["raw"])
        # bullets segments contribute no words to the scroll surface
        self.assertNotIn("cloud bills", data["raw"])
        resp = await self.client.get("/api/rundown")
        ranges = (await resp.json())["segments"]
        self.assertEqual(
            data["doc"]["word-count"], ranges[-1]["word-end"]
        )

    async def test_api_state_producer_block(self):
        resp = await self.client.get("/api/state")
        data = await resp.json()
        self.assertEqual(
            data["producer"],
            {
                "active": True,
                "live": False,
                "llm": {"provider": "none", "model": "qwen3:4b",
                        "ok": False},
            },
        )

    async def test_producer_built_from_the_loaded_rundown(self):
        self.assertEqual(len(self.producers), 1)
        self.assertEqual(self.producer.rundown["show"], "Test Show")
        self.assertEqual(self.producer.cue_density, "normal")
        self.assertIsNotNone(self.state.producer_task)
        self.assertIsNone(self.state.llm_task)  # provider none: no tick


class TestShowCommands(ProducerServerTestBase):

    async def test_go_live_hold_resume_end(self):
        ws, _ = await self.hello("remote")
        await ws.send_json({"type": "show", "cmd": "go-live"})
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "producer")
        self.assertTrue(frame["state"]["live"])

        await ws.send_json({"type": "show", "cmd": "hold"})
        frame = await recv_json(ws)
        self.assertTrue(frame["state"]["hold"])

        await ws.send_json({"type": "show", "cmd": "resume"})
        frame = await recv_json(ws)
        self.assertFalse(frame["state"]["hold"])

        await ws.send_json({"type": "show", "cmd": "end"})
        frame = await recv_json(ws)
        self.assertFalse(frame["state"]["live"])
        self.assertEqual(
            self.producer.calls,
            [("go-live",), ("hold",), ("resume",), ("end",)],
        )
        await ws.close()

    async def test_show_commands_broadcast_to_all_clients(self):
        ws1, _ = await self.hello("remote")
        ws2, _ = await self.hello("overlay")
        await ws1.send_json({"type": "show", "cmd": "go-live"})
        for ws in (ws1, ws2):
            frame = await recv_json(ws)
            self.assertEqual(frame["type"], "producer")
            self.assertTrue(frame["state"]["live"])
        await ws1.close()
        await ws2.close()

    async def test_unknown_show_cmd_errors(self):
        ws, _ = await self.hello("remote")
        await ws.send_json({"type": "show", "cmd": "party"})
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "error")
        self.assertIn("go-live", frame["message"])
        self.assertEqual(self.producer.calls, [])
        await ws.close()


class TestPointCommands(ProducerServerTestBase):

    async def test_covered_skip_make_current(self):
        ws, _ = await self.hello("remote")
        await ws.send_json(
            {"type": "point", "cmd": "covered", "segment": "g1", "point": 0}
        )
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "producer")
        points = frame["state"]["segments"][1]["points"]
        self.assertTrue(points[0]["covered"])

        await ws.send_json(
            {"type": "point", "cmd": "skip", "segment": "g1", "point": 1}
        )
        frame = await recv_json(ws)
        points = frame["state"]["segments"][1]["points"]
        self.assertTrue(points[1]["skipped"])

        # point is optional for make-current (it jumps the segment)
        await ws.send_json(
            {"type": "point", "cmd": "make-current", "segment": "g2"}
        )
        frame = await recv_json(ws)
        self.assertEqual(frame["state"]["current"], "g2")
        self.assertEqual(
            self.producer.calls,
            [("covered", "g1", 0), ("skip", "g1", 1),
             ("make-current", "g2")],
        )
        await ws.close()

    async def test_point_cmd_validation(self):
        ws, _ = await self.hello("remote")
        cases = [
            {"type": "point", "cmd": "cover-all", "segment": "g1",
             "point": 0},
            {"type": "point", "cmd": "covered", "point": 0},
            {"type": "point", "cmd": "covered", "segment": "g1",
             "point": "zero"},
            {"type": "point", "cmd": "skip", "segment": "g1"},
        ]
        for frame in cases:
            await ws.send_json(frame)
            reply = await recv_json(ws)
            self.assertEqual(reply["type"], "error", frame)
        self.assertEqual(self.producer.calls, [])
        await ws.close()

    async def test_unknown_segment_answers_error(self):
        ws, _ = await self.hello("remote")
        await ws.send_json(
            {"type": "point", "cmd": "covered", "segment": "g9", "point": 0}
        )
        reply = await recv_json(ws)
        self.assertEqual(reply["type"], "error")
        self.assertIn("failed", reply["message"])
        await ws.close()


class TestProducerBroadcastDedup(ProducerServerTestBase):

    async def test_unchanged_state_broadcasts_once(self):
        ws, _ = await self.hello("prompt")
        await server_main.producer_tick(self.state)
        await server_main.producer_tick(self.state)  # unchanged: silent
        await ws.send_json({"type": "cmd", "cmd": "play"})  # sentinel
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "producer")
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "cmd")

        self.producer._state["elapsed-s"] = 5
        await server_main.producer_tick(self.state)
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "producer")
        self.assertEqual(frame["state"]["elapsed-s"], 5)
        self.assertEqual(self.producer.tick_count, 3)
        await ws.close()

    async def test_deterministic_cues_flow_through_the_engine(self):
        ws, _ = await self.hello("overlay")
        self.producer.candidates = [
            {"tier": "card", "text": "30 seconds", "key": "g0-30s"}
        ]
        await server_main.producer_tick(self.state)
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "producer")
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "cue")
        self.assertEqual(frame["tier"], "card")
        self.assertEqual(frame["text"], "30 seconds")
        self.assertEqual(frame["id"], 1)

        # the same candidate re-offered next tick is deduped by key
        await server_main.producer_tick(self.state)
        await ws.send_json({"type": "cmd", "cmd": "play"})
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "cmd")
        await ws.close()


class TestKeywordCoverage(ProducerServerTestBase):

    async def test_first_pass_proposes_matching_point(self):
        self.producer.go_live()
        self.state.transcript.add(
            "and your cloud bills compound monthly forever if you stay"
        )
        await server_main.producer_tick(self.state)
        self.assertIn(("propose", "g1", 0), self.producer.calls)
        self.assertNotIn(("propose", "g1", 1), self.producer.calls)

    async def test_sixty_percent_overlap_is_enough(self):
        # point 0 has 5 informative words; 3 of them = 60 percent exactly
        self.producer.go_live()
        self.state.transcript.add("the cloud bills compound")
        await server_main.producer_tick(self.state)
        self.assertIn(("propose", "g1", 0), self.producer.calls)

    async def test_below_floor_does_not_propose(self):
        self.producer.go_live()
        self.state.transcript.add("the cloud bills are big")  # 2 of 5
        await server_main.producer_tick(self.state)
        self.assertNotIn(("propose", "g1", 0), self.producer.calls)

    async def test_covered_and_skipped_points_left_alone(self):
        self.producer.go_live()
        self.producer._state["segments"][1]["points"][0]["skipped"] = True
        self.state.transcript.add(
            "cloud bills compound monthly forever"
        )
        await server_main.producer_tick(self.state)
        self.assertEqual(
            [c for c in self.producer.calls if c[0] == "propose"], []
        )

    async def test_pre_show_speech_never_covers(self):
        # coverage is sticky with no un-cover: a rehearsal or mic check
        # before GO LIVE must not silence a reminder for the whole show
        self.state.transcript.add(
            "cloud bills compound monthly forever"
        )
        await server_main.producer_tick(self.state)
        self.assertEqual(
            [c for c in self.producer.calls if c[0] == "propose"], []
        )

    async def test_hold_speech_never_covers(self):
        self.producer.go_live()
        self.producer.hold()
        self.state.transcript.add(
            "cloud bills compound monthly forever"
        )
        await server_main.producer_tick(self.state)
        self.assertEqual(
            [c for c in self.producer.calls if c[0] == "propose"], []
        )

    async def test_smart_quote_point_matches_ascii_transcript(self):
        # a rundown written with editor smart quotes must still meet the
        # ASR's plain-ASCII possessives on the same tokens
        self.producer.go_live()
        self.producer._state["segments"][1]["points"][0]["text"] = (
            "the market’s overnight reaction"
        )
        self.state.transcript.add(
            "and the market's overnight reaction was brutal"
        )
        await server_main.producer_tick(self.state)
        self.assertIn(("propose", "g1", 0), self.producer.calls)

    async def test_numeric_point_matches_spoken_numbers(self):
        # "4090" in the point text; the ASR emits the spoken form
        self.producer.go_live()
        self.producer._state["segments"][1]["points"][0]["text"] = (
            "the 4090 pricing anecdote"
        )
        self.state.transcript.add(
            "so the forty ninety pricing anecdote goes like this"
        )
        await server_main.producer_tick(self.state)
        self.assertIn(("propose", "g1", 0), self.producer.calls)

    async def test_final_asr_text_feeds_the_buffer(self):
        # transcript accumulation is wired off the asr FINAL events only
        self.state.transcript.add("spoken words so far")
        self.assertIn("spoken words", self.state.transcript.text())


class ProducerAsrTestBase(ProducerServerTestBase):
    """Producer AND stub ASR together: the vad/final wiring under test."""

    models_dir = "/stub-models"
    asr_provider = "nemotron-streaming"

    async def get_application(self):
        server_main.ENGINE_FACTORY = (
            lambda models_dir, provider, on_event:
            StubEngine(models_dir, provider, on_event)
        )
        server_main.ALIGNER_FACTORY = lambda doc: StubAligner(doc)
        self.addCleanup(self._reset_asr_seams)
        return await super().get_application()

    @staticmethod
    def _reset_asr_seams():
        server_main.ENGINE_FACTORY = None
        server_main.ALIGNER_FACTORY = None


class TestAsrProducerWiring(ProducerAsrTestBase):

    async def test_final_events_fill_the_transcript_buffer_while_live(self):
        self.producer.go_live()
        ws, _ = await self.hello("prompt")
        self.state.engine.on_event(
            {"kind": "final", "segment": 0, "text": "hello buffer",
             "tokens": [" hello", " buffer"]}
        )
        await recv_json(ws)  # asr frame
        await recv_json(ws)  # anchor frame
        self.assertIn("hello buffer", self.state.transcript.text())
        # partials revise and never enter the coverage buffer
        self.state.engine.on_event(
            {"kind": "partial", "segment": 1, "text": "revising",
             "tokens": [" revising"]}
        )
        await recv_json(ws)  # asr frame
        await recv_json(ws)  # anchor frame
        self.assertNotIn("revising", self.state.transcript.text())
        await ws.close()

    async def test_off_air_finals_never_enter_the_buffer(self):
        # BLOCKER regression: pre-show and hold speech must not reach the
        # coverage buffer at all, so not even the first live tick can see
        # the last 90 s of off-air talk
        ws, _ = await self.hello("prompt")

        async def speak(text):
            self.state.engine.on_event(
                {"kind": "final", "segment": 0, "text": text,
                 "tokens": [" " + text]}
            )
            # drain to the anchor frame (producer frames from earlier
            # ticks may be queued between the asr frame and it)
            while (await recv_json(ws))["type"] != "anchor":
                pass

        await speak("pre show rehearsal of the cloud bills point")
        self.assertEqual(self.state.transcript.text(), "")
        await server_main.producer_tick(self.state)
        self.assertEqual(
            [c for c in self.producer.calls if c[0] == "propose"], []
        )

        self.producer.go_live()
        await server_main.producer_tick(self.state)  # first live tick
        self.assertEqual(
            [c for c in self.producer.calls if c[0] == "propose"], []
        )

        self.producer.hold()
        await speak("hold banter cloud bills compound monthly forever")
        self.assertEqual(self.state.transcript.text(), "")
        self.producer.resume()
        await server_main.producer_tick(self.state)
        self.assertEqual(
            [c for c in self.producer.calls if c[0] == "propose"], []
        )

        # live speech flows normally
        await speak("cloud bills compound monthly forever")
        self.assertIn("compound", self.state.transcript.text())
        await server_main.producer_tick(self.state)
        self.assertIn(("propose", "g1", 0), self.producer.calls)
        await ws.close()

    async def test_aligner_feeds_suspended_while_bullets_current(self):
        # a bullets segment contributes no words to the doc, so any anchor
        # motion during it is creep by definition: the aligner gets NOTHING
        # while the producer's current segment is bullets (asr text still
        # broadcasts and the anchor holds)
        ws, _ = await self.hello("prompt")
        await ws.send_json(
            {"type": "point", "cmd": "make-current", "segment": "g1"}
        )
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "producer")  # no anchor for bullets
        self.assertEqual(frame["state"]["current"], "g1")
        self.assertEqual(self.state.aligner.set_calls, [])

        feeds_before = len(self.state.aligner.feeds)
        for kind in ("partial", "final"):
            self.state.engine.on_event(
                {"kind": kind, "segment": 0, "text": "cloud bills",
                 "tokens": [" cloud", " bills"]}
            )
            frame = await recv_json(ws)
            self.assertEqual(frame["type"], "asr")  # text still broadcasts
        # a sentinel proves the server fully processed both events and no
        # anchor frame followed either of them
        await ws.send_json({"type": "cmd", "cmd": "play"})
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "cmd")
        self.assertEqual(len(self.state.aligner.feeds), feeds_before)
        # the transcript buffer keeps running through bullets (while live)
        self.producer.go_live()
        self.state.engine.on_event(
            {"kind": "final", "segment": 0, "text": "still buffered",
             "tokens": [" still", " buffered"]}
        )
        while (await recv_json(ws))["type"] != "asr":
            pass
        self.assertIn("still buffered", self.state.transcript.text())
        await ws.close()

    async def test_make_current_scripted_reanchors_and_broadcasts(self):
        # entering a SCRIPTED segment re-anchors to word-start - 1 and
        # broadcasts the fresh anchor frame, so creep accrued during a
        # bullets segment (or a stale anchor after a backwards jump) can
        # never leak into the new segment
        ws, _ = await self.hello("prompt")
        g2 = next(r for r in self.state.prompt_ranges if r["id"] == "g2")
        await ws.send_json(
            {"type": "point", "cmd": "make-current", "segment": "g2"}
        )
        frame = await recv_json(ws)
        self.assertEqual(
            frame,
            {"type": "anchor", "i": g2["word-start"] - 1, "held": False},
        )
        producer_frame = await recv_json(ws)
        self.assertEqual(producer_frame["type"], "producer")
        self.assertEqual(producer_frame["state"]["current"], "g2")
        self.assertEqual(
            self.state.aligner.set_calls, [g2["word-start"] - 1]
        )
        self.assertEqual(
            self.state.last_anchor, (g2["word-start"] - 1, False)
        )
        await ws.close()

    async def test_make_current_first_segment_clamps_to_minus_one(self):
        ws, _ = await self.hello("prompt")
        # move away first so make-current g0 changes something
        await ws.send_json(
            {"type": "point", "cmd": "make-current", "segment": "g2"}
        )
        await recv_json(ws)  # anchor frame
        await recv_json(ws)  # producer frame
        await ws.send_json(
            {"type": "point", "cmd": "make-current", "segment": "g0"}
        )
        frame = await recv_json(ws)
        self.assertEqual(frame, {"type": "anchor", "i": -1, "held": False})
        g2 = next(r for r in self.state.prompt_ranges if r["id"] == "g2")
        self.assertEqual(
            self.state.aligner.set_calls, [g2["word-start"] - 1, -1]
        )
        await ws.close()

    async def test_card_cue_waits_for_the_vad_pause(self):
        ws, _ = await self.hello("prompt")
        self.assertTrue(self.state.cue_engine.gate_on_vad)
        self.state.engine.on_event({"kind": "vad", "speaking": True})
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "vad")

        self.producer.candidates = [
            {"tier": "card", "text": "NEXT: anecdote", "key": "next-g1-1"}
        ]
        await server_main.producer_tick(self.state)
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "producer")  # rail state only
        self.assertIsNotNone(self.state.cue_engine.pending)

        # the pause releases the held card immediately
        self.state.engine.on_event({"kind": "vad", "speaking": False})
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "vad")
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "cue")
        self.assertEqual(frame["text"], "NEXT: anecdote")
        await ws.close()


class TestLlmWiring(ProducerServerTestBase):

    llm_provider = "ollama"

    async def test_llm_client_built_and_task_started(self):
        self.assertEqual(len(self.llms), 1)
        self.assertEqual(self.llms[0].endpoint, "http://localhost:11434")
        self.assertEqual(self.llms[0].model, "qwen3:4b")
        self.assertIsNotNone(self.state.llm_task)

    def _speak(self, text):
        """Put final-transcript text in the buffer so evidence can verify."""
        self.state.transcript.add(text)

    async def test_confidence_floor_gates_proposals(self):
        self._speak(
            "cloud bills compound monthly forever and the point about "
            "ninety days was made explicitly"
        )
        good_evidence = "cloud bills compound monthly forever"
        result = {
            "coverage": [
                {"segment": "g1", "point": 0, "confidence": 0.9,
                 "evidence": good_evidence},
                {"segment": "g1", "point": 1, "confidence": 0.69,
                 "evidence": good_evidence},
                {"segment": "g9", "point": 0, "confidence": 0.99,
                 "evidence": good_evidence},
                {"segment": "g1", "point": True, "confidence": 0.99,
                 "evidence": good_evidence},
                "garbage",
            ]
        }
        await server_main.apply_llm_result(self.state, result)
        points = self.producer._state["segments"][1]["points"]
        self.assertTrue(points[0]["covered"])
        self.assertFalse(points[1]["covered"])

    async def test_evidence_gate_rejects_hallucinated_claims(self):
        self._speak("a local rig is a one time cost that keeps paying back")
        result = {
            "coverage": [
                # fabricated quote: those words were never spoken
                {"segment": "g1", "point": 0, "confidence": 0.9,
                 "evidence": "cloud bills compound monthly forever"},
                # real quote, but unrelated to the point it claims to cover
                {"segment": "g1", "point": 1, "confidence": 0.9,
                 "evidence": "a local rig is a one time cost"},
                # no evidence at all
                {"segment": "g1", "point": 0, "confidence": 0.9},
                {"segment": "g1", "point": 0, "confidence": 0.9,
                 "evidence": ""},
            ]
        }
        await server_main.apply_llm_result(self.state, result)
        points = self.producer._state["segments"][1]["points"]
        self.assertFalse(points[0]["covered"])
        self.assertFalse(points[1]["covered"])

    async def test_evidence_gate_accepts_normalized_numeric_points(self):
        # the point names "4090"; the speaker and the model's verbatim
        # quote both carry the spoken form: normalization must let the
        # point-overlap requirement pass
        self.producer._state["segments"][1]["points"][1]["text"] = (
            "the 4090 pricing anecdote"
        )
        self._speak("the forty ninety pricing anecdote was that "
                    "scalpers won that whole launch")
        result = {
            "coverage": [
                {"segment": "g1", "point": 1, "confidence": 0.9,
                 "evidence": "the forty ninety pricing anecdote"},
            ]
        }
        await server_main.apply_llm_result(self.state, result)
        points = self.producer._state["segments"][1]["points"]
        self.assertTrue(points[1]["covered"])

    async def test_llm_cue_goes_through_the_engine(self):
        ws, _ = await self.hello("prompt")
        self._speak("cloud bills compound monthly forever she said")
        result = {
            "coverage": [{"segment": "g1", "point": 0, "confidence": 0.8,
                          "evidence": "cloud bills compound monthly"}],
            "cue": {"text": "NEXT: the anecdote", "reason": "point done"},
        }
        await server_main.apply_llm_result(self.state, result)
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "producer")  # coverage changed
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "cue")
        self.assertEqual(frame["tier"], "card")
        self.assertEqual(frame["text"], "NEXT: the anecdote")
        await ws.close()

    async def test_should_skip_respects_live_hold_end_and_behind(self):
        self.assertTrue(server_main._llm_should_skip(self.state))  # pre-show
        self.producer.go_live()
        self.assertFalse(server_main._llm_should_skip(self.state))
        self.producer.hold()
        self.assertTrue(server_main._llm_should_skip(self.state))
        self.producer.resume()
        self.assertFalse(server_main._llm_should_skip(self.state))
        self.state.engine = types.SimpleNamespace(
            stats={"queue": 40, "behind": True, "ready": True}
        )
        try:
            self.assertTrue(server_main._llm_should_skip(self.state))
        finally:
            self.state.engine = None
        # after end the loop never ticks again: post-show chat is off-air
        self.producer.end_show()
        self.assertTrue(server_main._llm_should_skip(self.state))

    async def test_rundown_block_carries_coverage_and_stays_stable(self):
        self.producer.mark_covered("g1", 0)
        block = server_main.build_rundown_block(self.state)
        self.assertIn("SHOW: Test Show", block)
        self.assertIn("SEGMENT g1", block)
        self.assertIn("planned", block)
        self.assertIn("point 0 [covered]", block)
        self.assertIn("point 1 [uncovered]", block)
        # nothing volatile: the block goes into the system message, where
        # one changed byte per tick would defeat Ollama prefix caching
        for volatile in ("elapsed", "remaining", "spent", "replanned",
                         "timing", "NEXT"):
            self.assertNotIn(volatile, block)
        # per-tick clock changes leave the block byte-identical
        self.producer._state["elapsed-s"] = 99
        self.producer._state["segments"][0]["spent-s"] = 99
        self.assertEqual(block, server_main.build_rundown_block(self.state))

    async def test_status_block_carries_the_volatile_plan(self):
        block = server_main.build_status_block(self.state)
        self.assertIn("CLOCK: elapsed 0s", block)
        self.assertIn("remaining 600s", block)
        self.assertIn("SEGMENT g0: current", block)
        self.assertIn("replanned", block)
        self.assertIn("spent", block)
        self.assertIn("timing", block)
        self.assertIn("NEXT:", block)
        # no rundown structure duplicated here: point text stays stable-side
        self.assertNotIn("point 1", block)


class TestCueEngine(unittest.TestCase):
    """Deterministic cue-engine rules under a fake clock (no server)."""

    def setUp(self):
        self.clock = FakeClock()

    def engine(self, density="normal", gate_on_vad=False):
        return server_main.CueEngine(
            density=density, now_fn=self.clock, gate_on_vad=gate_on_vad
        )

    @staticmethod
    def card(text="NEXT: pricing", key="next-1"):
        return {"tier": "card", "text": text, "key": key}

    @staticmethod
    def attention(text="WRAP", key="wrap"):
        return {"tier": "attention", "text": text, "key": key}

    def test_card_released_immediately_without_asr(self):
        engine = self.engine()
        frames = engine.offer([self.card()])
        self.assertEqual(
            frames,
            [{"type": "cue", "id": 1, "tier": "card",
              "text": "NEXT: pricing"}],
        )

    def test_density_budget_blocks_early_second_card(self):
        engine = self.engine(density="normal")  # 1 card / 120 s
        engine.offer([self.card(key="a")])
        self.clock.t += 60
        # card a expired (cue-clear may surface) but the budget still
        # blocks card b: no new cue is shown
        frames = engine.offer([self.card(key="b")])
        self.assertEqual([f for f in frames if f["type"] == "cue"], [])
        self.clock.t += 65  # 125 s since the first card
        frames = engine.offer([self.card(text="b text", key="b")])
        self.assertEqual([f["type"] for f in frames], ["cue"])
        self.assertEqual(frames[0]["text"], "b text")

    def test_hands_off_never_shows_cards(self):
        engine = self.engine(density="hands-off")
        self.assertEqual(engine.offer([self.card()]), [])
        frames = engine.offer([self.attention()])
        self.assertEqual([f["type"] for f in frames], ["cue"])
        self.assertEqual(frames[0]["tier"], "attention")

    def test_chatty_budget_is_45s(self):
        engine = self.engine(density="chatty")
        engine.offer([self.card(key="a")])
        self.clock.t += 40
        frames = engine.offer([self.card(key="b")])
        self.assertEqual([f for f in frames if f["type"] == "cue"], [])
        self.clock.t += 6
        frames = engine.offer([self.card(key="b")])
        self.assertEqual([f["type"] for f in frames], ["cue"])

    def test_one_active_cue_blocks_cards(self):
        engine = self.engine(density="chatty")
        engine.offer([self.attention(key="w1")])  # active, not a card
        self.assertEqual(engine.offer([self.card()]), [])

    def test_attention_interrupts_active_cue(self):
        engine = self.engine()
        engine.offer([self.card()])
        frames = engine.offer([self.attention()])
        self.assertEqual(
            [f["type"] for f in frames], ["cue-clear", "cue"]
        )
        self.assertEqual(frames[0]["id"], 1)
        self.assertEqual(frames[1]["tier"], "attention")

    def test_attention_exempt_from_budget(self):
        engine = self.engine(density="minimal")
        engine.offer([self.card(key="a")])
        self.clock.t += 16  # active card expired, budget still closed
        engine.poll()
        frames = engine.offer([self.attention()])
        self.assertEqual([f["type"] for f in frames], ["cue"])

    def test_active_cue_expires_after_15s(self):
        engine = self.engine()
        engine.offer([self.card()])
        self.clock.t += 14
        self.assertEqual(engine.poll(), [])
        self.clock.t += 1
        self.assertEqual(engine.poll(), [{"type": "cue-clear", "id": 1}])
        self.assertIsNone(engine.active)

    def test_dedup_by_key_survives_expiry(self):
        engine = self.engine()
        engine.offer([self.card()])
        self.clock.t += 200  # expired long ago, budget open again
        engine.poll()
        self.assertEqual(engine.offer([self.card()]), [])  # same key

    def test_vad_gate_holds_card_until_pause(self):
        engine = self.engine(gate_on_vad=True)
        engine.on_vad(True)
        self.assertEqual(engine.offer([self.card()]), [])
        self.assertIsNotNone(engine.pending)
        frames = engine.on_vad(False)
        self.assertEqual([f["type"] for f in frames], ["cue"])
        self.assertIsNone(engine.pending)

    def test_stale_pending_card_is_discarded_unshown(self):
        engine = self.engine(gate_on_vad=True)
        engine.on_vad(True)
        engine.offer([self.card()])
        self.clock.t += 16
        self.assertEqual(engine.on_vad(False), [])
        self.assertIsNone(engine.pending)
        # the key was never consumed: the candidate may retry later
        frames = engine.offer([self.card()])
        self.assertEqual([f["type"] for f in frames], ["cue"])

    def test_attention_shows_mid_speech(self):
        engine = self.engine(gate_on_vad=True)
        engine.on_vad(True)
        frames = engine.offer([self.attention()])
        self.assertEqual([f["type"] for f in frames], ["cue"])

    def test_rejected_candidates_do_not_consume_their_key(self):
        engine = self.engine(density="normal")
        engine.offer([self.card(key="a")])
        self.clock.t += 30
        engine.offer([self.card(key="b")])  # budget-blocked
        self.clock.t += 95  # budget open again (125 s since card a)
        engine.poll()
        frames = engine.offer([self.card(key="b")])
        self.assertEqual([f["type"] for f in frames], ["cue"])


class TestTranscriptBuffer(unittest.TestCase):

    def test_window_pruning(self):
        clock = FakeClock()
        buf = server_main.TranscriptBuffer(window_s=90.0, now_fn=clock)
        buf.add("first words")
        clock.t += 60
        buf.add("middle words")
        clock.t += 60
        buf.add("latest words")
        text = buf.text()
        self.assertNotIn("first", text)  # 120 s old, outside the window
        self.assertIn("middle", text)
        self.assertIn("latest", text)

    def test_char_cap_drops_oldest(self):
        clock = FakeClock()
        buf = server_main.TranscriptBuffer(
            window_s=90.0, max_chars=30, now_fn=clock
        )
        buf.add("aaaaaaaaaaaaaaaaaaaa")  # 20 chars
        buf.add("bbbbbbbbbbbbbbbbbbbb")  # 40 total: a-entry must go
        text = buf.text()
        self.assertNotIn("a", text)
        self.assertIn("b", text)

    def test_blank_finals_ignored(self):
        buf = server_main.TranscriptBuffer(now_fn=FakeClock())
        buf.add("")
        buf.add("   ")
        self.assertEqual(buf.text(), "")


class TestBuildPromptDoc(unittest.TestCase):

    def test_scripted_ranges_are_contiguous_and_additive(self):
        raw, ranges = server_main.build_prompt_doc(RUNDOWN)
        self.assertEqual([r["id"] for r in ranges], ["g0", "g2"])
        self.assertEqual(ranges[0]["word-start"], 0)
        self.assertEqual(ranges[1]["word-start"], ranges[0]["word-end"])
        from server import script_ingest
        doc = script_ingest.ingest(raw)
        self.assertEqual(doc["word-count"], ranges[-1]["word-end"])

    def test_bullets_only_rundown_yields_nothing(self):
        rundown = {"segments": [RUNDOWN["segments"][1]]}
        raw, ranges = server_main.build_prompt_doc(rundown)
        self.assertEqual(raw, "")
        self.assertEqual(ranges, [])

    def test_informative_words_filtering(self):
        words = server_main.informative_words(
            "The 4090 is a graphics card, and it wins!"
        )
        # digit runs expand to spoken words, matching what the ASR emits
        self.assertEqual(
            words, {"forty", "ninety", "graphics", "card", "wins"}
        )

    def test_informative_words_apostrophe_family_collapses(self):
        # ASCII, curly, and U+02BC apostrophes tokenize identically, so a
        # smart-quote rundown meets the ASR's plain possessives
        for text in ("the market's move", "the market’s move",
                     "the marketʼs move"):
            self.assertEqual(
                server_main.informative_words(text),
                {"markets", "move"},
                text,
            )

    def test_informative_words_digits_match_their_spoken_form(self):
        self.assertEqual(
            server_main.informative_words("the 2026 roadmap"),
            server_main.informative_words("the twenty twenty six roadmap"),
        )
        self.assertEqual(
            server_main.informative_words("pay 25 dollars"),
            {"pay", "twenty", "five", "dollars"},
        )


class TestLoadRundown(unittest.TestCase):

    def test_frontmatter_density_overrides_config(self):
        state = server_main.AppState(token=TOKEN, cue_density="minimal")
        rundown = dict(copy.deepcopy(RUNDOWN), **{"cue-density": "chatty"})
        state.load_rundown(rundown)
        self.assertEqual(state.cue_density, "chatty")

    def test_no_frontmatter_density_keeps_config(self):
        state = server_main.AppState(token=TOKEN, cue_density="minimal")
        state.load_rundown(copy.deepcopy(RUNDOWN))
        self.assertEqual(state.cue_density, "minimal")

    def test_doc_built_and_script_path_cleared(self):
        state = server_main.AppState(token=TOKEN)
        state.script_path = Path("/tmp/somewhere.md")
        state.load_rundown(copy.deepcopy(RUNDOWN))
        self.assertIsNone(state.script_path)  # save can never clobber
        self.assertGreater(state.doc["word-count"], 0)
        self.assertEqual(len(state.prompt_ranges), 2)

    def test_bullets_only_rundown_leaves_doc_untouched(self):
        state = server_main.AppState(token=TOKEN)
        state.set_raw("# Kept\n\nExisting words.\n")
        rundown = {"show": "x", "segments": [RUNDOWN["segments"][1]]}
        state.load_rundown(rundown)
        self.assertEqual(state.doc["title"], "Kept")
        self.assertEqual(state.prompt_ranges, [])


class TestProducerOffRegression(ServerTestBase):
    """No rundown loaded: tiers 1/2 see no producer surface at all."""

    async def test_api_rundown_is_null(self):
        resp = await self.client.get("/api/rundown")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIsNone(data["rundown"])
        self.assertEqual(data["segments"], [])

    async def test_api_state_producer_inactive(self):
        resp = await self.client.get("/api/state")
        data = await resp.json()
        self.assertEqual(data["producer"]["active"], False)
        self.assertEqual(data["producer"]["live"], False)
        self.assertEqual(data["producer"]["llm"]["provider"], "none")
        self.assertEqual(data["producer"]["llm"]["ok"], False)

    async def test_show_and_point_commands_answer_error(self):
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "hello", "role": "remote"})
        await recv_json(ws)
        await ws.send_json({"type": "show", "cmd": "go-live"})
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "error")
        self.assertIn("producer mode is not active", frame["message"])
        await ws.send_json(
            {"type": "point", "cmd": "covered", "segment": "g1", "point": 0}
        )
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "error")
        await ws.close()

    async def test_no_producer_frames_ever_broadcast(self):
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "hello", "role": "prompt"})
        await recv_json(ws)
        await ws.send_json({"type": "cmd", "cmd": "play"})
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "cmd")  # nothing producer-shaped
        self.assertIsNone(self.state.producer)
        self.assertIsNone(self.state.producer_task)
        await ws.close()

    async def test_no_producer_modules_imported(self):
        self.assertNotIn("server.producer", sys.modules)
        self.assertNotIn("server.llm", sys.modules)
        self.assertNotIn("server.rundown", sys.modules)


class TestProducerMainGuards(unittest.TestCase):
    """main() Phase C argument guards (no server is started)."""

    def _run_main(self, argv):
        import contextlib

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = server_main.main(argv)
        return code, err.getvalue()

    def test_unknown_llm_provider_exits_3(self):
        code, err = self._run_main(
            ["--token", "t", "--llm-provider", "openai"]
        )
        self.assertEqual(code, 3)
        self.assertIn("openai", err)
        self.assertIn("planned lane", err)

    def test_missing_rundown_exits_4(self):
        code, err = self._run_main(
            ["--token", "t", "--rundown",
             "/nonexistent-mc-prompter-rundown.md"]
        )
        self.assertEqual(code, 4)
        self.assertIn("rundown not found", err)

    def test_rundown_parse_error_exits_4(self):
        class FakeRundownError(Exception):
            pass

        def parse_rundown(text):
            raise FakeRundownError("line 7: bad time suffix")

        fake = types.SimpleNamespace(
            parse_rundown=parse_rundown, RundownError=FakeRundownError
        )
        original = server_main._import_rundown
        server_main._import_rundown = lambda: fake
        self.addCleanup(
            setattr, server_main, "_import_rundown", original
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rundown.md"
            path.write_text("## Broken (banana min)\n", encoding="utf-8")
            code, err = self._run_main(
                ["--token", "t", "--rundown", str(path)]
            )
        self.assertEqual(code, 4)
        self.assertIn("rundown parse failed", err)
        self.assertIn("line 7", err)

    def test_bad_cue_density_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            with contextlib_redirect_stderr_null():
                server_main.main(
                    ["--token", "t", "--cue-density", "frantic"]
                )
        self.assertEqual(ctx.exception.code, 2)


RUNTIME_RUNDOWN_MD = """---
show: "Runtime Load Show"
duration-minutes: 10
wrap-minutes: 2
---

## Intro (2 min)

Scripted intro prose for the runtime load test.

## Middle point (4 min)

- first talking point here
- second talking point here

## Wrap (2 min)

Scripted wrap prose.
"""


class TestRuntimeRundownLoad(ServerTestBase):
    """POST /api/rundown/load adopts a rundown on a running server.

    The server starts with NO rundown (script only), so this exercises the
    home-page runtime load path: real parse via server/rundown.py, producer
    stack built through the injected factory, /api/rundown and /api/state
    reflecting the change, and a second load replacing the producer.
    """

    async def get_application(self):
        self.producers = []

        def producer_factory(rundown, cue_density):
            producer = StubProducer(rundown, cue_density)
            self.producers.append(producer)
            return producer

        server_main.PRODUCER_FACTORY = producer_factory
        self._saved_tick_s = server_main.PRODUCER_TICK_S
        server_main.PRODUCER_TICK_S = 3600.0

        def _cleanup():
            server_main.PRODUCER_FACTORY = None
            server_main.PRODUCER_TICK_S = self._saved_tick_s

        self.addCleanup(_cleanup)
        return await super().get_application()

    def _write_rundown(self, text=RUNTIME_RUNDOWN_MD):
        tmp = tempfile.mkdtemp(prefix="mc-prompter-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = Path(tmp) / "show.md"
        path.write_text(text, encoding="utf-8")
        return path

    async def test_runtime_load_builds_producer_stack(self):
        resp = await self.client.post(
            "/api/rundown/load", json={"path": str(self._write_rundown())}
        )
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["rundown"]["show"], "Runtime Load Show")
        self.assertIsInstance(body["warnings"], list)
        self.assertEqual(len(self.producers), 1)

        api = await (await self.client.get("/api/rundown")).json()
        self.assertIsNotNone(api["rundown"])
        state = await (await self.client.get("/api/state")).json()
        self.assertTrue(state["producer"]["active"])

    async def test_second_load_replaces_the_producer(self):
        for _ in range(2):
            resp = await self.client.post(
                "/api/rundown/load",
                json={"path": str(self._write_rundown())},
            )
            self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.producers), 2)

    async def test_load_while_live_is_409_and_leaves_the_show_alone(self):
        path = str(self._write_rundown())
        resp = await self.client.post("/api/rundown/load", json={"path": path})
        self.assertEqual(resp.status, 200)
        producer = self.producers[0]
        producer.go_live()

        resp = await self.client.post("/api/rundown/load", json={"path": path})
        self.assertEqual(resp.status, 409)
        body = await resp.json()
        self.assertIn("live", body["error"])
        # the running show is untouched: same producer object, still live
        self.assertEqual(len(self.producers), 1)
        self.assertIs(self.state.producer, producer)
        self.assertTrue(self.state.producer.state["live"])

    async def test_force_load_replaces_a_live_show(self):
        path = str(self._write_rundown())
        await self.client.post("/api/rundown/load", json={"path": path})
        self.producers[0].go_live()
        resp = await self.client.post(
            "/api/rundown/load", json={"path": path, "force": True}
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.producers), 2)
        self.assertFalse(self.state.producer.state["live"])

    async def test_reload_resets_llm_ok_and_clears_transcript(self):
        path = str(self._write_rundown())
        await self.client.post("/api/rundown/load", json={"path": path})
        self.state.llm_ok = True
        self.state.transcript.add("speech from the previous stack")
        resp = await self.client.post("/api/rundown/load", json={"path": path})
        self.assertEqual(resp.status, 200)
        self.assertFalse(self.state.llm_ok)
        self.assertEqual(self.state.transcript.text(), "")

    async def test_reload_clears_the_active_cue_on_every_client(self):
        path = str(self._write_rundown())
        await self.client.post("/api/rundown/load", json={"path": path})
        shown = self.state.cue_engine.offer(
            [{"tier": "card", "text": "30 seconds", "key": "seg-30:g0"}]
        )
        self.assertEqual([f["type"] for f in shown], ["cue"])
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "hello", "role": "prompt"})
        await recv_json(ws)  # welcome
        resp = await self.client.post("/api/rundown/load", json={"path": path})
        self.assertEqual(resp.status, 200)
        frame = await recv_json(ws)
        self.assertEqual(frame, {"type": "cue-clear", "id": shown[0]["id"]})
        frame = await recv_json(ws)
        self.assertEqual(frame["type"], "doc-updated")
        await ws.close()

    async def test_missing_file_is_400(self):
        resp = await self.client.post(
            "/api/rundown/load",
            json={"path": "/nonexistent-mc-prompter-runtime.md"},
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(len(self.producers), 0)

    async def test_parse_error_is_400_and_state_untouched(self):
        bad = RUNTIME_RUNDOWN_MD.replace("(2 min)", "(2 minutes)", 1)
        resp = await self.client.post(
            "/api/rundown/load", json={"path": str(self._write_rundown(bad))}
        )
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertIn("rundown parse failed", body["error"])
        api = await (await self.client.get("/api/rundown")).json()
        self.assertIsNone(api["rundown"])
        self.assertEqual(len(self.producers), 0)


class TestStopProducerGrace(unittest.TestCase):
    """stop_producer must never wait longer than LLM_STOP_GRACE_S on an
    LLM task pinned to the uninterruptible urllib worker thread."""

    def test_shutdown_bounded_when_llm_task_ignores_cancel(self):
        async def scenario():
            state = server_main.AppState(token=TOKEN)
            app = {server_main.STATE_KEY: state}
            started = asyncio.Event()

            async def stubborn():
                started.set()
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    # simulate the in-flight urllib worker: the first
                    # cancel cannot interrupt it (a later cancel from the
                    # loop teardown still ends the task)
                    await asyncio.sleep(3600)

            state.llm_task = asyncio.ensure_future(stubborn())
            await started.wait()
            started_at = time.monotonic()
            await server_main.stop_producer(app)
            self.assertIsNone(state.llm_task)
            return time.monotonic() - started_at

        saved = server_main.LLM_STOP_GRACE_S
        server_main.LLM_STOP_GRACE_S = 0.2
        try:
            elapsed = asyncio.run(scenario())
        finally:
            server_main.LLM_STOP_GRACE_S = saved
        self.assertLess(elapsed, 1.0)


class TestStartupScriptPlusBulletsRundown(unittest.TestCase):
    """--script plus a bullets-only rundown keeps the script on the scroll.

    Startup must match the runtime POST /api/rundown/load behavior (same
    inputs, same outcome): load_rundown leaves the doc untouched when the
    rundown contributes no scripted words, so the script loaded first
    stays promptable while the producer rail runs from the rundown.
    web.run_app is stubbed to capture the app; no server starts.
    """

    BULLETS_ONLY = (
        "---\n"
        'show: "Bullets Only"\n'
        "duration-minutes: 10\n"
        "---\n\n"
        "## Points (10 min)\n\n"
        "- first talking point here\n"
        "- second talking point here\n"
    )

    def test_script_kept_when_rundown_contributes_no_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "talk.md"
            script.write_text(SAMPLE, encoding="utf-8")
            rundown = Path(tmp) / "outline.md"
            rundown.write_text(self.BULLETS_ONLY, encoding="utf-8")
            captured = {}
            original = server_main.web.run_app
            server_main.web.run_app = (
                lambda app, **kwargs: captured.setdefault("app", app)
            )
            try:
                with contextlib_redirect_stderr_null():
                    code = server_main.main(
                        ["--token", "t", "--script", str(script),
                         "--rundown", str(rundown)]
                    )
            finally:
                server_main.web.run_app = original
            self.assertEqual(code, 0)
            state = captured["app"][server_main.STATE_KEY]
            self.assertEqual(state.doc["title"], "Test Script")
            self.assertIn("Hello world", state.raw)
            self.assertEqual(state.script_path, script)
            self.assertIsNotNone(state.rundown)  # producer rail still on
            self.assertEqual(state.prompt_ranges, [])


class contextlib_redirect_stderr_null:
    """Tiny stderr silencer for the argparse usage-error test."""

    def __enter__(self):
        import contextlib

        self._cm = contextlib.redirect_stderr(io.StringIO())
        self._cm.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


if __name__ == "__main__":
    unittest.main()
