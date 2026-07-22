# Windows default stack

Selected when check_deps.py reports os Windows. The GPU verdict (nvidia, intel, amd, none, unknown) splits the stack below. Research basis: platform and capabilities audit, 2026-07-21.

## Default stack (NVIDIA GPU)

| Concern | Default |
|---|---|
| Transcription | onnx-asr with the gpu,hub extras (onnxruntime CUDA execution provider), parakeet-tdt-0.6b-v3 ONNX weights; no WSL2 required |
| TTS | kokoro-onnx (identical everywhere; espeakng-loader wheels bundle espeak-ng.dll and data) |
| Music / SFX | MusicGen / AudioLDM2 on CUDA; torch from `--index-url https://download.pytorch.org/whl/cu126` (CUDA wheels add roughly 2.5 to 3 GB; the workspace consent message must say so) |
| Final render hardware encode | probe h264_nvenc, then h264_qsv, then h264_amf, then libx264; each probe is a 1-frame test encode, not just an encoder listing |
| Alpha master | ProRes 4444 (prores_ks); probe with `ffmpeg -encoders`, never assume |
| Alpha live deliverable | WebM VP9 alpha for OBS; see the vMix and Wirecast notes below |
| Live tool | OBS default; vMix and Wirecast are opt-in |
| SVG rasterizer | html_to_png.py (Chromium) or resvg; not rsvg-convert (awkward on Windows, never renders color emoji) |
| Fonts | data-URI @font-face inlining; Chromium uses DirectWrite on Windows, so the fontconfig shim is a no-op |
| Screen recording | OBS; Game Bar captures a single app only, never the Desktop or Explorer |

## CPU-only variant

Same as above except:

- Transcription: onnx-asr with the cpu,hub extras at fp32. The 0.6B model is CPU-viable; never default to int8, which has a reported missing-words defect.
- Torch: plain PyPI (CPU wheels); no custom index.
- Music is usable but slow on CPU; AudioLDM2 at 100 steps is painful on CPU. Set expectations before farming.
- Render encode: libx264, with h264_qsv probed on Intel iGPUs and h264_amf on AMD.

## Alpha assets for vMix and Wirecast

- vMix rejects MP4 stingers; it wants MOV or, preferably, PNG sequences for stinger transitions. Deliver baked alpha as a PNG sequence or ProRes 4444 MOV when the live tool is vMix.
- Wirecast accepts ProRes 4444 MOV directly; the standard alpha master already satisfies it.
- OBS takes the WebM VP9 alpha deliverable and HTML browser sources; that stays the default.

## Windows notes

- Keep `{engines-path}` short. Deep Node engine workspaces (HyperFrames) can trip MAX_PATH; also enable LongPathsEnabled in the registry.
- Install the gyan.dev full ffmpeg build; it carries libx264, prores_ks, libvpx-vp9, and drawtext with freetype/fontconfig.
- Screen recording goes through OBS, not Game Bar (single-app only, no Desktop or Explorer capture).
- Registered CLI tools installed via npm run through `.cmd` shims; the pipeline resolves them with PATH lookup before launching. Headless templates use POSIX quoting.

## DaVinci Resolve notes

The free edition only executes scripts launched from inside the app (Console or Workspace > Scripts); the external scripting API is Studio-only through Resolve 21. To run the scripted import on free Resolve, copy the pipeline's resolve_import.py into the Fusion Scripts folder and launch it from Workspace > Scripts:

`%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\`

Studio users who want conversational post-import timeline surgery can opt into a community Resolve MCP server; Manticore itself keeps doing media import and timeline construction through the FCPXML lane and never requires an MCP server.
