#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Connect to a running DaVinci Resolve Studio through its scripting API.

Imported by the Resolve-lane scripts in this skill; not run on its own except
as a connection check:

    uv run {skill-root}/scripts/resolve_api.py

Contract:
    connect(timeout)  returns the Resolve object, or raises ResolveUnavailable
                      with a message naming the fix. Checks, in order: the
                      API module is installed, Resolve answers, and the
                      product is Studio (21.1 moved external scripting to
                      Studio; the free edition refuses the connection).
    module_dir(), lib_path()
                      the per-OS install locations, overridden by
                      RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB when set.

Resolve must be running with Preferences > System > General > External
scripting using: Local. Blackmagic's MCP server uses the same API; this module
is the scripted lane and needs no MCP.

Exit codes (connection check only): 0 connected, 4 Resolve unavailable.

STATUS: implemented (pure path logic covered by
scripts/tests/test-resolve_api.py).
"""

import json
import os
import sys
import time
from pathlib import Path

UNAVAILABLE_EXIT = 4

SETUP_HINT = ("Open DaVinci Resolve Studio and set Preferences > System > "
              "General > External scripting using: Local, then retry. "
              "mc-setup checks this.")


class ResolveUnavailable(RuntimeError):
    pass


def module_dir(platform=None, env=None):
    """Folder holding DaVinciResolveScript.py (pure)."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    if env.get("RESOLVE_SCRIPT_API"):
        return str(Path(env["RESOLVE_SCRIPT_API"]) / "Modules")
    if platform.startswith("darwin"):
        return ("/Library/Application Support/Blackmagic Design/"
                "DaVinci Resolve/Developer/Scripting/Modules")
    if platform.startswith(("win", "cygwin")):
        base = env.get("PROGRAMDATA", r"C:\ProgramData")
        return (base + r"\Blackmagic Design\DaVinci Resolve\Support"
                r"\Developer\Scripting\Modules")
    return "/opt/resolve/Developer/Scripting/Modules"


def lib_path(platform=None, env=None):
    """fusionscript library path (pure)."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    if env.get("RESOLVE_SCRIPT_LIB"):
        return env["RESOLVE_SCRIPT_LIB"]
    if platform.startswith("darwin"):
        return ("/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/"
                "Libraries/Fusion/fusionscript.so")
    if platform.startswith(("win", "cygwin")):
        return (r"C:\Program Files\Blackmagic Design\DaVinci Resolve"
                r"\fusionscript.dll")
    return "/opt/resolve/libs/Fusion/fusionscript.so"


def is_studio(product_name):
    """True when the product name is the Studio edition (pure)."""
    return "studio" in str(product_name or "").lower()


def _load_module():
    mdir = module_dir()
    if not Path(mdir, "DaVinciResolveScript.py").is_file():
        raise ResolveUnavailable(
            f"DaVinci Resolve's scripting module is not at {mdir}. Install "
            "DaVinci Resolve Studio, or set RESOLVE_SCRIPT_API to its "
            "Developer/Scripting folder.")
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", lib_path())
    if mdir not in sys.path:
        sys.path.append(mdir)
    try:
        import DaVinciResolveScript as dvr
    except ImportError as e:
        raise ResolveUnavailable(
            f"could not load fusionscript ({os.environ['RESOLVE_SCRIPT_LIB']}): "
            f"{e}") from e
    return dvr


def connect(timeout=10.0):
    """The Resolve object from a running Studio instance."""
    dvr = _load_module()
    deadline = time.monotonic() + timeout
    resolve = None
    while True:
        resolve = dvr.scriptapp("Resolve")
        if resolve or time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    if not resolve:
        raise ResolveUnavailable("DaVinci Resolve did not answer. "
                                 + SETUP_HINT)
    product = resolve.GetProductName()
    if not is_studio(product):
        raise ResolveUnavailable(
            f"connected to {product}, which refuses external scripting. The "
            "Resolve lane needs DaVinci Resolve Studio; the fcpxml timeline "
            "still imports by hand through File > Import > Timeline.")
    return resolve


def main():
    try:
        resolve = connect()
    except ResolveUnavailable as e:
        print(f"resolve_api: {e}", file=sys.stderr)
        return UNAVAILABLE_EXIT
    project = resolve.GetProjectManager().GetCurrentProject()
    print(json.dumps({"ok": True, "product": resolve.GetProductName(),
                      "version": resolve.GetVersionString(),
                      "project": project.GetName() if project else None}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
