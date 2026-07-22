# macOS default stack

Selected when check_deps.py reports os Darwin. Apple Silicon is the module's reference platform; everything here is the shipped default behavior, confirmed with the creator during the mc-setup interview. Research basis: platform and capabilities audit, 2026-07-21.

## Default stack (Apple Silicon)

| Concern | Default |
|---|---|
| Transcription | parakeet-mlx (parakeet-tdt-0.6b-v3), free, local, verbatim fillers, word timestamps |
| TTS | kokoro-onnx (CPU realtime) |
| Music / SFX | MusicGen / AudioLDM2 on MPS (plain PyPI torch ships MPS wheels) |
| Final render hardware encode | h264_videotoolbox / hevc_videotoolbox, libx264 fallback |
| Alpha master | ProRes 4444 (prores_ks); probe with `ffmpeg -encoders`, never assume |
| Alpha live deliverable | WebM VP9 alpha for OBS browser and stinger use |
| Live tool | OBS default; Ecamm Live is the interviewed macOS-only option |
| SVG rasterizer | rsvg-convert or html_to_png.py (Chromium) |
| Fonts | system fonts via CoreText; data-URI @font-face inlining also works |
| Screen recording | Cmd-Shift-5 or OBS |

## Intel Mac variant

parakeet-mlx requires Apple Silicon (MLX runs on Metal only). On an Intel Mac the transcription lane is onnx-asr with the cpu,hub extras, running the same parakeet-tdt-0.6b-v3 weights as an ONNX conversion, so verbatim fillers and word timestamps carry over. Everything else above still applies, including videotoolbox encode.

## Torch index

Plain PyPI. macOS wheels include MPS support; no custom index is ever needed.

## DaVinci Resolve notes

The free edition only executes scripts launched from inside the app (Console or Workspace > Scripts); the external scripting API is Studio-only through Resolve 21. To run the scripted import on free Resolve, copy the pipeline's resolve_import.py into the Fusion Scripts folder and launch it from Workspace > Scripts:

`~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/`

Studio users who want conversational post-import timeline surgery can opt into a community Resolve MCP server; Manticore itself keeps doing media import and timeline construction through the FCPXML lane and never requires an MCP server.
