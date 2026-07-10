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
import importlib
import json
import sys
import tempfile
import threading
import time
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


if __name__ == "__main__":
    unittest.main()
