#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Check the external dependencies the Manticore pipeline needs.

Usage:
    uv run check_deps.py [--json]

Checks presence on PATH (and Python version), detects the platform (OS,
CPU architecture, GPU vendor via cheap best-effort probes), and reports
the recommended per-platform stack: which stack reference file applies
(references/stack-macos.md, stack-windows.md, or stack-linux.md) and the
platform-specific defaults it implies (transcription lane, torch index,
hardware-encoder ladder, SVG rasterizer, fonts approach).

The platform rows and the stack verdict are informational only; they
never fail the check. Prints a table (or JSON: {"results", "platform",
"ok"}) and exits 0 if all required deps are present, 1 otherwise.
Installs nothing.
"""

import argparse
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

DEPS = [
    # (command, required, why)
    ("uv", True, "runs every pipeline script (installs Python automatically if needed)"),
    ("ffmpeg", True, "frame extraction, re-mux to constant frame rate, preview renders"),
    ("ffprobe", True, "frame-rate and pixel-format verification"),
    ("node", True, "HyperFrames render engine"),
    ("npx", True, "hyperframes CLI and registry blocks"),
    ("git", True, "project history"),
    ("yt-dlp", False, "pulling your published transcripts for the voice bible"),
]

# PCI vendor ids seen in /sys/class/drm on Linux.
PCI_VENDORS = {
    "0x10de": "NVIDIA",
    "0x1002": "AMD",
    "0x1022": "AMD",
    "0x8086": "Intel",
}

TORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu126"


def is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def classify_gpu(names: list[str]) -> str:
    """Map a list of adapter/vendor names to a vendor verdict.

    Empty list means the probe ran and found nothing ("none"); names that
    match no known vendor yield "unknown".
    """
    if not names:
        return "none"
    joined = " ".join(names).lower()
    if "nvidia" in joined:
        return "nvidia"
    if "amd" in joined or "radeon" in joined or "advanced micro devices" in joined:
        return "amd"
    if "intel" in joined:
        return "intel"
    return "unknown"


def gpu_names_linux() -> list[str] | None:
    """Best-effort GPU vendor names from sysfs; None when the probe fails."""
    drm = Path("/sys/class/drm")
    if not drm.is_dir():
        return None
    names: list[str] = []
    for vendor_file in sorted(drm.glob("card[0-9]*/device/vendor")):
        try:
            vid = vendor_file.read_text().strip().lower()
        except OSError:
            continue
        names.append(PCI_VENDORS.get(vid, vid))
    return names


def gpu_names_windows() -> list[str] | None:
    """Best-effort adapter names via PowerShell CIM or legacy wmic; None on failure.

    PowerShell first: wmic is disabled by default on Windows 11 23H2+ and is
    being removed, so it is only a fallback for older installs."""
    commands = [
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
        ["wmic", "path", "win32_VideoController", "get", "name"],
    ]
    for cmd in commands:
        if not shutil.which(cmd[0]):
            continue
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        names = [ln for ln in lines if ln.lower() != "name"]  # wmic header row
        if names:
            return names
    return None


def detect_gpu(os_name: str, arch: str) -> tuple[str, str]:
    """Return (vendor, detail). Vendor: apple/nvidia/amd/intel/none/unknown."""
    if os_name == "Darwin":
        if arch == "arm64":
            return "apple", "Apple Silicon GPU (Metal/MPS)"
        return "unknown", "Intel Mac; GPU vendor not probed (CPU lanes apply)"
    if shutil.which("nvidia-smi"):
        return "nvidia", "nvidia-smi found on PATH"
    if os_name == "Windows":
        names = gpu_names_windows()
    elif os_name == "Linux":
        names = gpu_names_linux()
    else:
        names = None
    if names is None:
        return "unknown", "GPU probe unavailable or failed on this platform"
    if not names:
        return "none", "no GPU adapters detected (best-effort probe)"
    return classify_gpu(names), "; ".join(names)


def recommend_stack(os_name: str, arch: str, gpu: str) -> dict:
    """Pure decision tree: platform facts -> stack file + per-platform defaults.

    Keys are kebab-case. Non-Darwin, non-Windows systems get the Linux stack
    (the closest POSIX shape).
    """
    if os_name == "Darwin":
        return {
            "stack-file": "references/stack-macos.md",
            "transcription": ("parakeet-mlx" if arch == "arm64"
                              else "onnx-asr[cpu,hub] (parakeet-tdt-0.6b-v3 ONNX weights)"),
            "torch-index": "default PyPI (MPS wheels on macOS)",
            "encoder-ladder": ["h264_videotoolbox", "libx264"],
            "svg-rasterizer": "rsvg-convert or html_to_png.py (Chromium)",
            "fonts": "system fonts via CoreText; data-URI @font-face inlining also works",
        }
    if os_name == "Windows":
        if gpu == "nvidia":
            ladder = ["h264_nvenc", "h264_qsv", "h264_amf", "libx264"]
        elif gpu == "intel":
            ladder = ["h264_qsv", "libx264"]
        elif gpu == "amd":
            ladder = ["h264_amf", "libx264"]
        else:
            ladder = ["libx264"]
        return {
            "stack-file": "references/stack-windows.md",
            "transcription": ("onnx-asr[gpu,hub] (onnxruntime CUDA EP)" if gpu == "nvidia"
                              else "onnx-asr[cpu,hub] at fp32 (never int8)"),
            "torch-index": (TORCH_CUDA_INDEX if gpu == "nvidia"
                            else "default PyPI (CPU-only wheels on Windows)"),
            "encoder-ladder": ladder,
            "svg-rasterizer": "html_to_png.py (Chromium) or resvg; not rsvg-convert",
            "fonts": "data-URI @font-face inlining (DirectWrite; fontconfig shim is a no-op)",
        }
    # Linux and any other POSIX.
    ladder = (["h264_nvenc", "h264_vaapi", "libx264"] if gpu == "nvidia"
              else ["h264_vaapi", "libx264"])
    return {
        "stack-file": "references/stack-linux.md",
        "transcription": ("onnx-asr[gpu,hub] (onnxruntime CUDA EP)" if gpu == "nvidia"
                          else "onnx-asr[cpu,hub]"),
        "torch-index": "default PyPI (CUDA-bundled wheels on Linux)",
        "encoder-ladder": ladder,
        "svg-rasterizer": "rsvg-convert with the FONTCONFIG_FILE shim",
        "fonts": "fontconfig; install noto-color-emoji for emoji graphics",
    }


def platform_report() -> dict:
    os_name = platform.system()
    arch = platform.machine()
    gpu, gpu_detail = detect_gpu(os_name, arch)
    return {
        "os": os_name,
        "arch": arch,
        "apple-silicon": is_apple_silicon(),
        "gpu": gpu,
        "gpu-detail": gpu_detail,
        "recommended": recommend_stack(os_name, arch, gpu),
    }


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
    apple_silicon = is_apple_silicon()
    results.append({
        "dep": "apple-silicon",
        "required": False,
        "found": apple_silicon,
        "detail": (
            "default transcription lane (parakeet-mlx) is supported on this machine"
            if apple_silicon
            else "default transcription lane (parakeet-mlx) is Apple-Silicon-only; the "
            "recommended lane on this machine is onnx-asr with the same "
            "parakeet-tdt-0.6b-v3 weights (verbatim fillers and word timestamps "
            "carry over; CPU or CUDA). See the recommended stack below and the "
            "README platform matrix."
        ),
    })
    for cmd, required, why in DEPS:
        path = shutil.which(cmd)
        results.append({"dep": cmd, "required": required, "found": bool(path), "detail": path or why})

    missing_required = [r for r in results if r["required"] and not r["found"]]
    plat = platform_report()

    if args.json:
        print(json.dumps({"results": results, "platform": plat, "ok": not missing_required}, indent=2))
    else:
        for r in results:
            mark = "ok " if r["found"] else ("MISSING " if r["required"] else "missing (optional) ")
            print(f"{mark:22} {r['dep']:14} {r['detail']}")
        rec = plat["recommended"]
        print(f"\nPlatform: {plat['os']} {plat['arch']}, gpu {plat['gpu']} ({plat['gpu-detail']})")
        print(f"Recommended stack file: {rec['stack-file']}")
        print(f"  transcription:  {rec['transcription']}")
        print(f"  torch-index:    {rec['torch-index']}")
        print(f"  encoder-ladder: {' -> '.join(rec['encoder-ladder'])}")
        print(f"  svg-rasterizer: {rec['svg-rasterizer']}")
        print(f"  fonts:          {rec['fonts']}")
        if not apple_silicon:
            print(
                "\nNOTE: this machine cannot run the default transcription lane "
                "(parakeet-mlx is Apple-Silicon-only). The recommended lane here is "
                "onnx-asr with the same parakeet-tdt-0.6b-v3 weights; see the "
                "recommended stack file above and the README platform matrix."
            )
        if missing_required:
            print(f"\n{len(missing_required)} required dependency(ies) missing.")

    raise SystemExit(1 if missing_required else 0)


if __name__ == "__main__":
    main()
