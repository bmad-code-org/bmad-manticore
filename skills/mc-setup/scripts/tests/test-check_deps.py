#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for check_deps.py: --json output shape, exit-code contract, and the
platform/stack decision tree.

PATH-dependent results are not asserted per-dep; the tests pin the shape and
the ok/exit-code relationship, which hold on any machine. The recommend_stack
and classify_gpu decision tables are pure functions tested for every OS/GPU
combination without probing real hardware; no GPU probes are executed beyond
what the script itself does on this machine."""
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "check_deps.py"

spec = importlib.util.spec_from_file_location("check_deps", SCRIPT)
check_deps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_deps)

RECOMMENDED_KEYS = {
    "stack-file", "transcription", "torch-index",
    "encoder-ladder", "svg-rasterizer", "fonts",
}


def run(args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


class TestCheckDeps(unittest.TestCase):
    def test_json_output_shape(self):
        proc = run(["--json"])
        data = json.loads(proc.stdout)
        self.assertIn("ok", data)
        self.assertIsInstance(data["results"], list)
        deps = {r["dep"] for r in data["results"]}
        self.assertIn("uv", deps)
        self.assertIn("ffmpeg", deps)
        for r in data["results"]:
            self.assertEqual({"dep", "required", "found", "detail"}, set(r))

    def test_exit_code_matches_ok(self):
        proc = run(["--json"])
        data = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0 if data["ok"] else 1)

    def test_table_output_runs(self):
        proc = run([])
        self.assertIn("uv", proc.stdout)
        self.assertIn("Recommended stack file:", proc.stdout)
        self.assertIn(proc.returncode, (0, 1))

    def test_platform_gate_row(self):
        proc = run(["--json"])
        data = json.loads(proc.stdout)
        rows = [r for r in data["results"] if r["dep"] == "apple-silicon"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertFalse(row["required"])  # informational; never fails the check
        self.assertIn("parakeet-mlx", row["detail"])
        if not row["found"]:
            self.assertIn("onnx-asr", row["detail"])

    def test_json_platform_shape(self):
        proc = run(["--json"])
        data = json.loads(proc.stdout)
        plat = data["platform"]
        self.assertEqual(
            {"os", "arch", "apple-silicon", "gpu", "gpu-detail", "recommended"},
            set(plat),
        )
        self.assertIn(plat["gpu"], ("apple", "nvidia", "amd", "intel", "none", "unknown"))
        rec = plat["recommended"]
        self.assertEqual(RECOMMENDED_KEYS, set(rec))
        self.assertRegex(rec["stack-file"], r"^references/stack-(macos|windows|linux)\.md$")
        self.assertIsInstance(rec["encoder-ladder"], list)
        self.assertEqual(rec["encoder-ladder"][-1], "libx264")


class TestClassifyGpu(unittest.TestCase):
    def test_empty_is_none(self):
        self.assertEqual(check_deps.classify_gpu([]), "none")

    def test_vendors(self):
        self.assertEqual(check_deps.classify_gpu(["NVIDIA GeForce RTX 4090"]), "nvidia")
        self.assertEqual(check_deps.classify_gpu(["AMD Radeon RX 7900"]), "amd")
        self.assertEqual(check_deps.classify_gpu(["Radeon Pro W6800"]), "amd")
        self.assertEqual(check_deps.classify_gpu(["Intel(R) UHD Graphics 770"]), "intel")

    def test_nvidia_wins_over_igpu(self):
        names = ["Intel(R) UHD Graphics 770", "NVIDIA GeForce RTX 3060"]
        self.assertEqual(check_deps.classify_gpu(names), "nvidia")

    def test_unrecognized_is_unknown(self):
        self.assertEqual(check_deps.classify_gpu(["0x1234"]), "unknown")


class TestRecommendStack(unittest.TestCase):
    def test_macos_apple_silicon(self):
        rec = check_deps.recommend_stack("Darwin", "arm64", "apple")
        self.assertEqual(rec["stack-file"], "references/stack-macos.md")
        self.assertEqual(rec["transcription"], "parakeet-mlx")
        self.assertEqual(rec["encoder-ladder"], ["h264_videotoolbox", "libx264"])
        self.assertIn("MPS", rec["torch-index"])

    def test_intel_mac_gets_onnx_asr_cpu(self):
        rec = check_deps.recommend_stack("Darwin", "x86_64", "unknown")
        self.assertEqual(rec["stack-file"], "references/stack-macos.md")
        self.assertIn("onnx-asr[cpu,hub]", rec["transcription"])

    def test_windows_nvidia(self):
        rec = check_deps.recommend_stack("Windows", "AMD64", "nvidia")
        self.assertEqual(rec["stack-file"], "references/stack-windows.md")
        self.assertIn("onnx-asr[gpu,hub]", rec["transcription"])
        self.assertEqual(rec["torch-index"], check_deps.TORCH_CUDA_INDEX)
        self.assertEqual(rec["encoder-ladder"],
                         ["h264_nvenc", "h264_qsv", "h264_amf", "libx264"])

    def test_windows_cpu_only(self):
        rec = check_deps.recommend_stack("Windows", "AMD64", "none")
        self.assertIn("onnx-asr[cpu,hub]", rec["transcription"])
        self.assertIn("never int8", rec["transcription"])
        self.assertNotIn("download.pytorch.org", rec["torch-index"])
        self.assertEqual(rec["encoder-ladder"], ["libx264"])

    def test_windows_intel_igpu_probes_qsv(self):
        rec = check_deps.recommend_stack("Windows", "AMD64", "intel")
        self.assertEqual(rec["encoder-ladder"], ["h264_qsv", "libx264"])

    def test_windows_amd_probes_amf(self):
        rec = check_deps.recommend_stack("Windows", "AMD64", "amd")
        self.assertEqual(rec["encoder-ladder"], ["h264_amf", "libx264"])

    def test_linux_nvidia(self):
        rec = check_deps.recommend_stack("Linux", "x86_64", "nvidia")
        self.assertEqual(rec["stack-file"], "references/stack-linux.md")
        self.assertIn("onnx-asr[gpu,hub]", rec["transcription"])
        self.assertEqual(rec["encoder-ladder"],
                         ["h264_nvenc", "h264_vaapi", "libx264"])
        self.assertNotIn("download.pytorch.org", rec["torch-index"])

    def test_linux_no_gpu(self):
        rec = check_deps.recommend_stack("Linux", "x86_64", "none")
        self.assertIn("onnx-asr[cpu,hub]", rec["transcription"])
        self.assertEqual(rec["encoder-ladder"], ["h264_vaapi", "libx264"])
        self.assertIn("noto-color-emoji", rec["fonts"])

    def test_unknown_posix_falls_to_linux_stack(self):
        rec = check_deps.recommend_stack("FreeBSD", "amd64", "unknown")
        self.assertEqual(rec["stack-file"], "references/stack-linux.md")


class TestDetectGpu(unittest.TestCase):
    def test_darwin_arm64_is_apple(self):
        vendor, detail = check_deps.detect_gpu("Darwin", "arm64")
        self.assertEqual(vendor, "apple")
        self.assertIn("MPS", detail)

    def test_darwin_intel_is_unknown(self):
        vendor, _ = check_deps.detect_gpu("Darwin", "x86_64")
        self.assertEqual(vendor, "unknown")


if __name__ == "__main__":
    unittest.main()
