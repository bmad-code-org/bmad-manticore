#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Check the external dependencies the Manticore pipeline needs.

Usage:
    uv run check_deps.py [--json]

Checks presence on PATH (and Python version). Prints a table (or JSON) and
exits 0 if all required deps are present, 1 otherwise. Installs nothing.
"""

import argparse
import json
import shutil
import sys

DEPS = [
    # (command, required, why)
    ("uv", True, "runs every pipeline script (installs Python automatically if needed)"),
    ("ffmpeg", True, "frame extraction, re-mux to constant frame rate, preview renders"),
    ("ffprobe", True, "frame-rate and pixel-format verification"),
    ("node", True, "HyperFrames and Remotion render engines"),
    ("npx", True, "hyperframes CLI and registry blocks"),
    ("git", True, "project history"),
    ("yt-dlp", False, "pulling your published transcripts for the voice bible"),
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    results = []
    py_ok = sys.version_info >= (3, 11)
    results.append({
        "dep": "python3.11+",
        "required": False,
        "found": py_ok,
        "detail": f"running {sys.version_info.major}.{sys.version_info.minor}; uv provisions a suitable Python if the system one is old",
    })
    for cmd, required, why in DEPS:
        path = shutil.which(cmd)
        results.append({"dep": cmd, "required": required, "found": bool(path), "detail": path or why})

    missing_required = [r for r in results if r["required"] and not r["found"]]

    if args.json:
        print(json.dumps({"results": results, "ok": not missing_required}, indent=2))
    else:
        for r in results:
            mark = "ok " if r["found"] else ("MISSING " if r["required"] else "missing (optional) ")
            print(f"{mark:22} {r['dep']:14} {r['detail']}")
        if missing_required:
            print(f"\n{len(missing_required)} required dependency(ies) missing.")

    raise SystemExit(1 if missing_required else 0)


if __name__ == "__main__":
    main()
