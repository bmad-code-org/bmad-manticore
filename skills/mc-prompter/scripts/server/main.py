#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp==3.12.15"]
# ///
"""mc-prompter aiohttp server (Phase A: classic teleprompter).

Launched by run_prompter.py as `python -m server.main` with cwd set to the
scripts directory (the PEP 723 header above also allows a direct
`uv run main.py` for debugging). All arguments are explicit; the server does
no config discovery.

Usage:
    python -m server.main --port 8770 --host 127.0.0.1|0.0.0.0
        --script <path or empty> --owner-wpm <int or 0>
        --session-file <path or empty> --token <hex>

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
                       "config": {"owner-wpm": N or null}}
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

WebSocket protocol (JSON text frames; binary frames are reserved for the
Phase B audio path and rejected with an error frame for now):
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
    error (server): {"type": "error", "message": "..."}
State frames from non-leaders are ignored. The first prompt-role connection
is leader; on leader disconnect the oldest remaining prompt connection is
promoted and told {"type": "role", "leader": true}.

Exit codes: 0 ok, 2 usage, 4 script path missing or unreadable.
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

    def __init__(self, token, owner_wpm=0, static_dir=None):
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
        # Phase B: ASR thread plugs in here. A dedicated thread will consume
        # a bounded queue of PCM frames (fed by binary WS frames from the
        # capture-token holder) and publish recognition events back onto the
        # loop via call_soon_threadsafe. Nothing blocking runs on the loop.

    def load_script(self, path):
        """Read and ingest a script file; may raise OSError/UnicodeDecodeError."""
        path = Path(path)
        raw = path.read_text(encoding="utf-8-sig")
        self.script_path = path
        self.set_raw(raw)

    def set_raw(self, raw):
        """Re-ingest raw text and bump doc-version.

        A leading U+FEFF (UTF-8 BOM pasted through POST /api/source) is
        stripped so it never defeats the heading parser.
        """
        raw = raw.removeprefix("\ufeff")
        self.raw = raw
        self.doc = script_ingest.ingest(raw)
        self.doc_version += 1

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
    await asyncio.to_thread(state.set_raw, raw)
    await state.broadcast(
        {"type": "doc-updated", "doc-version": state.doc_version}
    )
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
    try:
        await asyncio.to_thread(state.load_script, path)
    except (OSError, UnicodeDecodeError) as exc:
        return web.json_response(
            {"error": f"cannot read {path}: {exc}"}, status=400
        )
    await state.broadcast(
        {"type": "doc-updated", "doc-version": state.doc_version}
    )
    return web.json_response(
        {"doc-version": state.doc_version, "script": state.script_info()}
    )


async def ws_handler(request):
    state = request.app[STATE_KEY]
    trusted = is_loopback(request) or token_ok(request, state)
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    if not trusted:
        await ws.close(code=4403, message=b"invalid or missing session token")
        return ws

    client = None
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                # Phase B: binary frames carry PCM audio from the capture
                # token holder; rejected until the ASR thread exists.
                await ws.send_json(
                    {
                        "type": "error",
                        "message": "binary frames are reserved for Phase B",
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


def create_app(state):
    app = web.Application()
    app[STATE_KEY] = state
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
    args = parser.parse_args(argv)

    state = AppState(token=args.token, owner_wpm=args.owner_wpm)
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
    print(
        f"{APP_NAME} {VERSION} serving on http://{args.host}:{args.port}/",
        file=sys.stderr,
    )
    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)
