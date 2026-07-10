#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp==3.12.15"]
# ///
"""mc-prompter aiohttp server (Phase A classic teleprompter + Phase B voice-follow).

Launched by run_prompter.py as `python -m server.main` with cwd set to the
scripts directory (the PEP 723 header above also allows a direct
`uv run main.py` for debugging). All arguments are explicit; the server does
no config discovery.

Usage:
    python -m server.main --port 8770 --host 127.0.0.1|0.0.0.0
        --script <path or empty> --owner-wpm <int or 0>
        --session-file <path or empty> --token <hex>
        [--models-dir <dir or empty>] [--asr-provider none|nemotron-streaming]

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

Exit codes: 0 ok, 2 usage, 3 planned asr provider selected,
4 script path or models dir missing or unreadable.
"""

import argparse
import asyncio
import contextlib
import datetime
import hmac
import json
import os
import shutil
import sys
import tempfile
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

# Injectable seams for tests (monkeypatchable module globals). When left as
# None the defaults below lazily import server.asr / server.align, so tier 1
# and the test suite never touch sherpa-onnx or numpy.
ENGINE_FACTORY = None  # (models_dir: Path, provider: str, on_event) -> engine
ALIGNER_FACTORY = None  # (doc: dict) -> aligner


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
                 models_dir=None, asr_provider="none"):
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
        }
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
    """Handle one engine event on the loop: align, then fan out."""
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
        if state.aligner is not None:
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


def create_app(state):
    app = web.Application()
    app[STATE_KEY] = state
    app.on_startup.append(start_asr)
    app.on_cleanup.append(stop_asr)
    for route, name in PAGES.items():
        app.router.add_get(route, make_page_handler(name))
    app.router.add_get("/static/{path:.+}", static_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/api/state", api_state_handler)
    app.router.add_get("/api/source", api_source_get_handler)
    app.router.add_post("/api/source", api_source_post_handler)
    app.router.add_post("/api/source/load", api_source_load_handler)
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

    state = AppState(
        token=args.token,
        owner_wpm=args.owner_wpm,
        models_dir=models_dir,
        asr_provider=provider if models_dir else "none",
    )
    state.port = args.port
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

    app = create_app(state)
    asr_note = (
        f"asr {state.asr_provider}" if state.asr_enabled else "asr off (tier 1)"
    )
    print(
        f"{APP_NAME} {VERSION} serving on http://{args.host}:{args.port}/ "
        f"({asr_note})",
        file=sys.stderr,
    )
    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)
