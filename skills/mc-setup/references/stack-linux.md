# Linux default stack

Selected when check_deps.py reports os Linux. The GPU verdict (nvidia, amd, intel, none, unknown) splits the transcription extra and the encoder ladder. Research basis: platform and capabilities audit, 2026-07-21.

## Default stack

| Concern | Default |
|---|---|
| Transcription | onnx-asr, gpu,hub extras if CUDA is detected, else cpu,hub; parakeet-tdt-0.6b-v3 ONNX weights. NeMo is the documented advanced alternative (heavy, not a uv-run default); ROCm is best-effort via onnxruntime-rocm |
| TTS | kokoro-onnx (identical everywhere; manylinux_2_28 wheels, glibc 2.28 or newer) |
| Music / SFX | MusicGen / AudioLDM2, CUDA if present else CPU; plain PyPI torch already bundles CUDA on Linux |
| Final render hardware encode | probe h264_nvenc, then h264_vaapi, then libx264; distro ffmpeg builds vary, always probe |
| Alpha master | ProRes 4444 (prores_ks); probe with `ffmpeg -encoders`, never assume |
| Alpha live deliverable | WebM VP9 alpha for OBS (identical everywhere) |
| Live tool | OBS (vMix, Wirecast, and Ecamm all skip Linux) |
| SVG rasterizer | rsvg-convert with the FONTCONFIG_FILE shim |
| Fonts and emoji | fontconfig works natively; install the noto-color-emoji package for emoji graphics |
| Screen recording | OBS with the PipeWire screen-capture source (Wayland portals); X11-style grabs black-screen on Wayland |

## Torch index

Plain PyPI. Linux torch wheels bundle CUDA already; no custom index. ROCm builds are Linux-only and opt-in.

## Linux notes

- Playwright needs system libraries; `playwright install-deps` automates this on Ubuntu and Debian only. On other distros install the equivalent packages manually.
- Minimal or headless machines need the noto-color-emoji package or Chromium renders emoji as tofu.
- Screen capture on Wayland must go through the PipeWire portal source in OBS; legacy X11 grabs produce black frames.

## DaVinci Resolve notes

The Linux free edition cannot decode or encode H.264 or H.265 and has no AAC at all. The FCPXML timeline imports fine, but mp4/AAC media is undecodable there; either transcode sources to ProRes or DNxHR first, or use Resolve Studio.

The free edition only executes scripts launched from inside the app (Console or Workspace > Scripts); the external scripting API is Studio-only through Resolve 21. To run the scripted import on free Resolve, copy the pipeline's resolve_import.py into the Fusion Scripts folder and launch it from Workspace > Scripts:

`~/.local/share/DaVinciResolve/Fusion/Scripts/`

Studio users who want conversational post-import timeline surgery can opt into a community Resolve MCP server; Manticore itself keeps doing media import and timeline construction through the FCPXML lane and never requires an MCP server.
