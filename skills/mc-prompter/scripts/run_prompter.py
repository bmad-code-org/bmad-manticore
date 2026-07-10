#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Launch the mc-prompter teleprompter server.

Pure stdlib launcher. Probes the port, generates the session token, spawns
the aiohttp server as a child process, confirms it is up, writes the session
file, prints the URLs, and waits on the child (Ctrl-C terminates it).

Usage:
    uv run {skill-root}/scripts/run_prompter.py --script <abs path>
        [--port 8770] [--lan] [--owner-wpm 150] [--no-open]
        [--workspace <abs path>] [--asr-provider nemotron-streaming|none]
        [--rundown <abs path>] [--llm-provider none|ollama]
        [--llm-endpoint <url>] [--llm-model <tag>]
        [--cue-density hands-off|minimal|normal|chatty]

Behavior:
    port      default 8770. If busy, /health on 127.0.0.1 is queried (1 s
              timeout); when it answers app == "mc-prompter" the running
              session info is printed. Without an explicit --port the
              launcher auto-increments to the next free port; with an
              explicit --port it exits 5 with guidance.
    spawn     `uv run --with aiohttp==3.12.15 python -m server.main <args>`
              with cwd set to this scripts directory so the server package
              resolves. Server args are all explicit: --port --host --script
              --owner-wpm --session-file --token --asr-provider, plus
              --models-dir on the workspace path.
    workspace when --workspace points at a ready prompter-lab (built by
              ensure_workspace.py; venv present, model files present at
              sane sizes; checked inline, nothing is shelled out), the
              server is spawned with the workspace venv interpreter
              (.venv/bin/python on POSIX, .venv\\Scripts\\python.exe on
              Windows) from the same cwd, and --models-dir plus
              --asr-provider are passed so voice-follow is available.
              Without --workspace, or when it is not ready, the launcher
              falls back to the uv-run spawn with --asr-provider none
              (tier 1, classic prompter) and says so.
    session   token = secrets.token_urlsafe(16). After /health confirms the
              server is up, <tempdir>/mc-prompter/session-<port>.json is
              written: {"port": N, "pid": ..., "token": "...",
              "script": "...", "started": "<ISO>"}. The file is removed
              when the launcher exits.
    --lan     binds 0.0.0.0 and prints the LAN URL (best-effort local IP);
              expect a Windows Firewall consent dialog on Windows. Without
              it the server binds 127.0.0.1 and only localhost URLs print.
    --no-open skip opening the browser at the home page.
    rundown   --rundown enables producer mode (Phase C): the file's
              existence is checked here (exit 4) and the path is passed
              through; the server parses it. --rundown is an alternative
              to --script (either may be given; with both, the server
              prompts the rundown's scripted segments, and a bullets-only
              rundown keeps the script on the scroll). The llm and
              cue-density flags are pure pass-throughs; --llm-provider
              accepts only none|ollama and anything else fails fast with
              exit 3 (planned lane, nothing is spawned).

Exit codes: 0 ok, 1 server failed to start, 2 usage (including neither
--script nor --rundown), 3 planned asr or llm provider selected (fails
fast, nothing is spawned), 4 script or rundown path missing or unreadable,
5 port conflict with an explicit --port.
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
DEFAULT_ASR_PROVIDER = "nemotron-streaming"
ASR_PROVIDERS = ("nemotron-streaming", "zipformer-small", "none")
# Planned lanes stay valid argparse choices so the launcher (not argparse)
# owns the message, but selecting one fails fast with exit 3 before any
# spawn: nothing unvalidated pretends to run.
ASR_PLANNED_PROVIDERS = ("zipformer-small",)
# LLM lane (Phase C producer mode): only ollama is implemented; any other
# non-none value is treated as a planned lane and fails fast with exit 3
# (validated here, not by argparse, so the message owns the exit code).
LLM_PROVIDERS = ("none", "ollama")
DEFAULT_LLM_ENDPOINT = "http://localhost:11434"
DEFAULT_LLM_MODEL = "qwen3:4b"
CUE_DENSITIES = ("hands-off", "minimal", "normal", "chatty")
DEFAULT_CUE_DENSITY = "normal"
# Lightweight readiness floors, mirroring ensure_workspace.py's layout
# check (presence and size only; no subprocess, no imports).
WORKSPACE_MODEL_MIN_SIZES = {
    "models/nemotron-streaming/encoder.int8.onnx": 500_000_000,
    "models/nemotron-streaming/decoder.int8.onnx": 5_000_000,
    "models/nemotron-streaming/joiner.int8.onnx": 1_000_000,
    "models/nemotron-streaming/tokens.txt": 4_000,
    "models/silero_vad.onnx": 500_000,
}


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


def venv_python_path(workspace, platform=sys.platform):
    """The workspace venv interpreter path, resolved portably."""
    workspace = Path(workspace)
    if platform == "win32":
        return workspace / ".venv" / "Scripts" / "python.exe"
    return workspace / ".venv" / "bin" / "python"


def workspace_ready(workspace):
    """True when the prompter-lab layout looks complete.

    Inline re-implementation of ensure_workspace.py --check semantics at
    the file level (presence and size floors); deliberately no subprocess
    and no venv import check, so launch stays instant.
    """
    workspace = Path(workspace)
    if not venv_python_path(workspace).exists():
        return False
    for rel, min_size in WORKSPACE_MODEL_MIN_SIZES.items():
        path = workspace / rel
        if not path.is_file() or path.stat().st_size < min_size:
            return False
    return True


def spawn_plan(workspace, asr_provider):
    """Resolve the launch mode: (workspace-or-None, effective provider).

    A ready workspace launches through its venv with the requested
    provider (default nemotron-streaming). No workspace, or a not-ready
    one, falls back to the tier-1 uv-run spawn with provider "none".
    """
    if workspace is not None and workspace_ready(workspace):
        return workspace, (asr_provider or DEFAULT_ASR_PROVIDER)
    return None, "none"


def downgrade_notice(requested_ws, requested_provider, workspace):
    """Lines explaining a tier-1 fallback; [] when nothing was downgraded.

    An explicitly requested non-none provider that spawn_plan dropped to
    "none" is always named, so the creator is never silently overridden.
    """
    if workspace is not None:
        return []
    explicit = requested_provider not in (None, "none")
    lines = []
    if requested_ws is not None:
        lines.append(f"workspace not ready: {requested_ws}")
        if explicit:
            lines.append(
                f"  requested --asr-provider {requested_provider} is "
                "disabled (downgraded to none)"
            )
        lines.append(
            "  launching the classic prompter (voice-follow off). "
            "bootstrap the workspace with:"
        )
        lines.append(
            f"  uv run {SCRIPTS_DIR / 'ensure_workspace.py'} "
            f"--workspace {requested_ws}"
        )
    elif explicit:
        lines.append(
            f"note: --asr-provider {requested_provider} needs --workspace; "
            "launching the classic prompter (voice-follow off, provider "
            "downgraded to none)"
        )
    return lines


def voice_follow_line(workspace, asr_provider):
    """The launch banner's voice-follow line, honest about provider none."""
    if workspace is not None and asr_provider != "none":
        return (
            f"  voice-follow: available ({asr_provider}, "
            f"workspace {workspace})"
        )
    return "  voice-follow: off (classic prompter)"


def build_server_cmd(port, host, script, owner_wpm, session_file, token,
                     workspace=None, asr_provider="none", rundown=None,
                     llm_provider="none",
                     llm_endpoint=DEFAULT_LLM_ENDPOINT,
                     llm_model=DEFAULT_LLM_MODEL,
                     cue_density=DEFAULT_CUE_DENSITY):
    """The child process command; runs with cwd = the scripts directory.

    With a workspace, the command is the workspace venv interpreter running
    `-m server.main` (same cwd, so the server package resolves identically
    on both paths) plus --models-dir and --asr-provider. Without one, it is
    the uv-run spawn with --asr-provider none. The Phase C producer flags
    (--rundown only when given; the llm and cue-density flags always) are
    pure pass-throughs appended on both paths.
    """
    server_args = [
        "--port", str(port),
        "--host", host,
        "--script", str(script) if script else "",
        "--owner-wpm", str(owner_wpm or 0),
        "--session-file", str(session_file),
        "--token", token,
    ]
    if rundown:
        server_args += ["--rundown", str(rundown)]
    server_args += [
        "--llm-provider", llm_provider,
        "--llm-endpoint", llm_endpoint,
        "--llm-model", llm_model,
        "--cue-density", cue_density,
    ]
    if workspace is not None:
        workspace = Path(workspace)
        return [
            str(venv_python_path(workspace)), "-m", "server.main",
            *server_args,
            "--models-dir", str(workspace / "models"),
            "--asr-provider", asr_provider,
        ]
    return [
        "uv", "run", "--with", AIOHTTP_PIN, "python", "-m", "server.main",
        *server_args,
        "--asr-provider", asr_provider,
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
    parser.add_argument("--script", default=None,
                        help="path to the script file to prompt "
                             "(this, --rundown, or both)")
    parser.add_argument("--rundown", default=None,
                        help="rundown file path; enables producer mode "
                             "(alternative to --script)")
    parser.add_argument("--llm-provider", default="none",
                        help="none | ollama for the producer's LLM tick "
                             "(anything else is a planned lane, exit 3)")
    parser.add_argument("--llm-endpoint", default=DEFAULT_LLM_ENDPOINT,
                        help="Ollama endpoint (producer mode)")
    parser.add_argument("--llm-model", default=DEFAULT_LLM_MODEL,
                        help="Ollama model tag (producer mode)")
    parser.add_argument("--cue-density", default=DEFAULT_CUE_DENSITY,
                        choices=CUE_DENSITIES,
                        help="cue budget (rundown frontmatter overrides)")
    parser.add_argument("--port", type=int, default=None,
                        help=f"port (default {DEFAULT_PORT}, "
                             "auto-increments when busy)")
    parser.add_argument("--lan", action="store_true",
                        help="bind 0.0.0.0 and print the LAN remote URL")
    parser.add_argument("--owner-wpm", type=int, default=0,
                        help="creator wpm for read-time estimates, 0 = unset")
    parser.add_argument("--no-open", action="store_true",
                        help="do not open the browser at the home page")
    parser.add_argument("--workspace", default=None,
                        help="prompter-lab workspace path (built by "
                             "ensure_workspace.py); enables voice-follow "
                             "when ready")
    parser.add_argument("--asr-provider", default=None,
                        choices=ASR_PROVIDERS,
                        help="ASR provider for voice-follow (default "
                             f"{DEFAULT_ASR_PROVIDER} when --workspace is "
                             "given; planned lanes fail fast with exit 3)")
    args = parser.parse_args(argv)

    if args.asr_provider in ASR_PLANNED_PROVIDERS:
        print(
            f"error: asr-provider {args.asr_provider!r} is a planned lane "
            "and is not implemented yet; use "
            f"{DEFAULT_ASR_PROVIDER} or none",
            file=sys.stderr,
        )
        return 3
    if args.llm_provider not in LLM_PROVIDERS:
        print(
            f"error: llm-provider {args.llm_provider!r} is a planned lane "
            "and is not implemented yet; use ollama or none",
            file=sys.stderr,
        )
        return 3

    if not args.script and not args.rundown:
        print(
            "error: give --script, --rundown, or both",
            file=sys.stderr,
        )
        return 2
    script = None
    if args.script:
        script = Path(args.script).expanduser().resolve()
        if not script.is_file():
            print(f"error: script not found: {script}", file=sys.stderr)
            return 4
        try:
            script.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"error: cannot read {script}: {exc}", file=sys.stderr)
            return 4
    rundown = None
    if args.rundown:
        rundown = Path(args.rundown).expanduser().resolve()
        if not rundown.is_file():
            print(f"error: rundown not found: {rundown}", file=sys.stderr)
            return 4
        try:
            rundown.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"error: cannot read {rundown}: {exc}", file=sys.stderr)
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

    requested_ws = (Path(args.workspace).expanduser().resolve()
                    if args.workspace else None)
    workspace, asr_provider = spawn_plan(requested_ws, args.asr_provider)
    for line in downgrade_notice(requested_ws, args.asr_provider, workspace):
        print(line)

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    token = secrets.token_urlsafe(16)
    session_file = session_file_path(port)
    cmd = build_server_cmd(port, host, script or "", args.owner_wpm,
                           session_file, token,
                           workspace=workspace, asr_provider=asr_provider,
                           rundown=rundown, llm_provider=args.llm_provider,
                           llm_endpoint=args.llm_endpoint,
                           llm_model=args.llm_model,
                           cue_density=args.cue_density)

    child = spawn_server(cmd)
    try:
        info = wait_for_health(port, child)
        if info is None:
            print("error: server failed to start", file=sys.stderr)
            terminate_server(child)
            return 1

        write_session_file(session_file, port, child.pid, token,
                           script or "")

        base_url = f"http://127.0.0.1:{port}"
        local_url = f"{base_url}/?token={token}"
        print(f"mc-prompter is up (session {token[:8]})")
        print(voice_follow_line(workspace, asr_provider))
        if rundown is not None:
            llm_note = (
                f"llm {args.llm_model}" if args.llm_provider == "ollama"
                else "llm off, deterministic rail only"
            )
            print(f"  producer: on ({rundown.name}, {llm_note})")
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
