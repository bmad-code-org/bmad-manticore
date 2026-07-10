#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Launch the mc-prompter teleprompter server (Phase A).

Pure stdlib launcher. Probes the port, generates the session token, spawns
the aiohttp server as a child process, confirms it is up, writes the session
file, prints the URLs, and waits on the child (Ctrl-C terminates it).

Usage:
    uv run {skill-root}/scripts/run_prompter.py --script <abs path>
        [--port 8770] [--lan] [--owner-wpm 150] [--no-open]

Behavior:
    port      default 8770. If busy, /health on 127.0.0.1 is queried (1 s
              timeout); when it answers app == "mc-prompter" the running
              session info is printed. Without an explicit --port the
              launcher auto-increments to the next free port; with an
              explicit --port it exits 5 with guidance.
    spawn     `uv run --with aiohttp==3.12.15 python -m server.main <args>`
              with cwd set to this scripts directory so the server package
              resolves. Server args are all explicit: --port --host --script
              --owner-wpm --session-file --token.
    session   token = secrets.token_urlsafe(16). After /health confirms the
              server is up, <tempdir>/mc-prompter/session-<port>.json is
              written: {"port": N, "pid": ..., "token": "...",
              "script": "...", "started": "<ISO>"}. The file is removed
              when the launcher exits.
    --lan     binds 0.0.0.0 and prints the LAN URL (best-effort local IP);
              expect a Windows Firewall consent dialog on Windows. Without
              it the server binds 127.0.0.1 and only localhost URLs print.
    --no-open skip opening the browser at the home page.

Exit codes: 0 ok, 1 server failed to start, 2 usage, 4 script path missing
or unreadable, 5 port conflict with an explicit --port.
"""

import argparse
import contextlib
import datetime
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8770
AIOHTTP_PIN = "aiohttp==3.12.15"
HEALTH_TIMEOUT = 1.0
# Generous cap: a cold uv cache must resolve, download, and build aiohttp
# before the server can even start, which can take minutes on a slow
# network. wait_for_health fails immediately if the child exits.
STARTUP_TIMEOUT = 300.0
PORT_SCAN_LIMIT = 20


class PortConflict(Exception):
    """Requested port is busy and --port was explicit (exit 5)."""

    def __init__(self, port, info=None):
        self.port = port
        self.info = info
        super().__init__(f"port {port} is busy")


def port_is_free(port, host="127.0.0.1"):
    """True when the port can be bound on the given host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def health_identity(port, timeout=HEALTH_TIMEOUT):
    """GET /health on loopback; return its JSON when it is an mc-prompter."""
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("app") == "mc-prompter":
        return data
    return None


def describe_session(info):
    return (
        f"session {info.get('session')} serving "
        f"{info.get('script') or '(no script)'} since {info.get('started')}"
    )


def pick_port(requested, explicit, probe=port_is_free,
              identify=health_identity, limit=PORT_SCAN_LIMIT):
    """Resolve the port to use, auto-incrementing unless explicit.

    Raises PortConflict when the explicitly requested port is busy, and
    RuntimeError when no free port is found within the scan limit.
    """
    port = requested
    for _ in range(limit):
        if probe(port):
            return port
        info = identify(port)
        if explicit:
            raise PortConflict(port, info)
        if info:
            print(
                f"port {port} runs an mc-prompter "
                f"({describe_session(info)}); trying {port + 1}"
            )
        else:
            print(f"port {port} is busy; trying {port + 1}")
        port += 1
    raise RuntimeError(
        f"no free port found in {requested}..{requested + limit - 1}"
    )


def session_file_path(port):
    return Path(tempfile.gettempdir()) / "mc-prompter" / f"session-{port}.json"


def write_session_file(path, port, pid, token, script):
    """Write the session JSON the skill reads back; returns the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "port": port,
        "pid": pid,
        "token": token,
        "script": str(script),
        "started": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def build_server_cmd(port, host, script, owner_wpm, session_file, token):
    """The child process command; runs with cwd = the scripts directory."""
    # Phase B seam: when the prompter-lab workspace exists, swap the
    # `uv run --with` prefix for `<workspace>/.venv python -m server.main`
    # (POSIX .venv/bin/python, Windows .venv\Scripts\python.exe) with the
    # same cwd and the same explicit args. TODO(phase-b): implement the
    # workspace-present branch here; do not add config discovery.
    return [
        "uv", "run", "--with", AIOHTTP_PIN, "python", "-m", "server.main",
        "--port", str(port),
        "--host", host,
        "--script", str(script) if script else "",
        "--owner-wpm", str(owner_wpm or 0),
        "--session-file", str(session_file),
        "--token", token,
    ]


def spawn_server(cmd):
    """Spawn the server command in its own process group.

    The child is a `uv run` wrapper whose grandchild is the actual python
    server; isolating the tree in its own group lets terminate_server
    signal all of it, so no orphan keeps the port after shutdown.
    """
    if sys.platform == "win32":
        return subprocess.Popen(
            cmd, cwd=SCRIPTS_DIR,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(cmd, cwd=SCRIPTS_DIR, start_new_session=True)


def terminate_server(child, timeout=10.0):
    """Terminate the whole server process tree and reap the child.

    POSIX: SIGTERM the process group, escalate to SIGKILL after timeout.
    Windows: terminate()/kill() the child (spawned with its own process
    group; uv forwards termination on Windows).
    """
    if child.poll() is not None:
        return
    if sys.platform == "win32":
        child.terminate()
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        return
    try:
        pgid = os.getpgid(child.pid)
    except ProcessLookupError:
        child.wait()
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
        child.wait()


def local_ip():
    """Best-effort LAN IP via the UDP connect trick (no packets sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def wait_for_health(port, child, timeout=STARTUP_TIMEOUT):
    """Poll /health until the child answers as mc-prompter or dies."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return None
        info = health_identity(port)
        if info:
            return info
        time.sleep(0.25)
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--script", required=True,
                        help="path to the script file to prompt")
    parser.add_argument("--port", type=int, default=None,
                        help=f"port (default {DEFAULT_PORT}, "
                             "auto-increments when busy)")
    parser.add_argument("--lan", action="store_true",
                        help="bind 0.0.0.0 and print the LAN remote URL")
    parser.add_argument("--owner-wpm", type=int, default=0,
                        help="creator wpm for read-time estimates, 0 = unset")
    parser.add_argument("--no-open", action="store_true",
                        help="do not open the browser at the home page")
    args = parser.parse_args(argv)

    script = Path(args.script).expanduser().resolve()
    if not script.is_file():
        print(f"error: script not found: {script}", file=sys.stderr)
        return 4
    try:
        script.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"error: cannot read {script}: {exc}", file=sys.stderr)
        return 4

    explicit = args.port is not None
    requested = args.port if explicit else DEFAULT_PORT
    try:
        port = pick_port(requested, explicit)
    except PortConflict as exc:
        print(f"error: port {exc.port} is already in use.", file=sys.stderr)
        if exc.info:
            print(
                f"  an mc-prompter is running there: "
                f"{describe_session(exc.info)}",
                file=sys.stderr,
            )
        print(
            "  pick another --port, or omit --port to auto-select one.",
            file=sys.stderr,
        )
        return 5
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    token = secrets.token_urlsafe(16)
    session_file = session_file_path(port)
    cmd = build_server_cmd(port, host, script, args.owner_wpm,
                           session_file, token)

    child = spawn_server(cmd)
    try:
        info = wait_for_health(port, child)
        if info is None:
            print("error: server failed to start", file=sys.stderr)
            terminate_server(child)
            return 1

        write_session_file(session_file, port, child.pid, token, script)

        base_url = f"http://127.0.0.1:{port}"
        local_url = f"{base_url}/?token={token}"
        print(f"mc-prompter is up (session {token[:8]})")
        print(f"  home:    {local_url}")
        print(f"  prompt:  {base_url}/prompt")
        print(f"  remote:  http://127.0.0.1:{port}/remote?token={token}")
        if args.lan:
            ip = local_ip()
            print(f"  LAN remote: http://{ip}:{port}/remote?token={token}")
            print("  note: on Windows the first --lan launch triggers a "
                  "Windows Firewall consent dialog; allow it for the LAN "
                  "remote to reach the server.")
        print(f"  session file: {session_file}", flush=True)

        if not args.no_open:
            webbrowser.open(local_url)

        def _on_terminate(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, _on_terminate)
        try:
            child.wait()
        except KeyboardInterrupt:
            terminate_server(child)
            return 0
        return 0 if child.returncode == 0 else 1
    finally:
        # Never leave a stale session file advertising a dead pid/token.
        with contextlib.suppress(OSError):
            session_file.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
