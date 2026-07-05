# Configure Your Own Manticore Studio

How to go from a fresh install to a working studio. Ten minutes of setup, then the pipeline does the remembering.

The short version: install, then say "talk to Manny". Manny the Manticore (mc-agent) is the studio's director and front door; he notices when the studio is not set up, runs setup with you, turns your ideas into projects, and routes every stage after that. This guide is the same path spelled out, for when you want to understand or drive the pieces yourself.

Note on status: Manticore is shared early. The setup, brain dump, outline, script, and cut stages work today; the graphics and asset lanes are landing (their contracts are fixed). The README's Status section is the source of truth for what runs right now.

## 1. Pick where your studio lives

Manticore is designed to be installed at the root of a dedicated content project: one folder that holds your config, your brand, and every video you make. Global/user-level installs work, but a project root gives you a self-contained, versionable studio.

```
my-studio/                        <- install here, run everything from here
  _bmad/custom/config.toml        <- studio config: the [modules.manticore] table
                                     (mc-setup writes it; config.user.toml overrides)
  manticore/
    brand/                        <- tokens.json, voice-bible.md, blacklist.md,
                                     exemplars/, headshots/
    formats/                      <- your editable format profiles (learnings live here)
    projects/                     <- one folder per video, fully self-contained
      my-first-video/
      another-video/
    engines/                      <- HyperFrames / Remotion workspaces
```

Install:

```bash
npx bmad-method install --custom-source https://github.com/bmad-code-org/bmad-manticore
```

## 2. Run mc-setup

Say "run manticore setup" (or invoke mc-setup). It walks you through everything below and writes the studio config: the `[modules.manticore]` table in `_bmad/custom/config.toml` (personal overrides go in `config.user.toml` next to it). Every other skill resolves that table with the installed `_bmad/scripts/resolve_config.py`; if it is empty, they send you back here. Re-run it any time; it updates rather than overwrites. Each skill also ships its own `customize.toml` defaults, overridable per skill in `_bmad/custom/<skill>.toml`.

What it covers:

- Dependencies: uv (required; runs all pipeline scripts), ffmpeg (with ffprobe), node/npx, git, optionally yt-dlp. It checks, you approve any installs.
- You: name, channel, the links that go in your video descriptions, your measured speaking rate (used for script runtime estimates; measure it from a real transcript, the generic 145 wpm is usually wrong for on-camera educators).
- Your editor: see section 4.
- Your brand: see section 3.
- Your generation tools: see section 5.
- API lanes: which env vars hold your keys. Keys never go in the TOML, only env var names.

## 3. Your brand folder

`{brand-path}` (default `manticore/brand/`) is where your identity lives:

- `tokens.json`: colors, fonts, logo paths, motion timings. Every graphic in every engine reads this file; change it once, everything follows. If you have an existing brand system doc, point mc-setup at it and it fills tokens from that.
- `blacklist.md`: regex patterns for LLM tells and phrases you never say. Ships with a starter set; grows every time you flag something in retro.
- `voice-bible.md`: the rules of how you actually talk, deconstructed from your own published transcripts with cited examples. This is the highest-value asset in the studio; budget a session to build it (the file ships as a build spec that walks you through it).
- `exemplars/`: 3 to 5 of your best published scripts as spoken transcripts. Ground truth for the voice bible and for mc-script.
- `headshots/`: 3 to 6 approved photos of you, used for face-consistent thumbnail generation. Approved photos only.

## 4. Your editor

Manticore's default deliverable is an editable cut in your editor, never a silently baked video (ask for a final render any time and it will happen; it is just not the default). Tell mc-setup what you finish in:

- DaVinci Resolve or Final Cut Pro: an FCPXML timeline of trimmable clips (the first export lane being implemented; see the Status section of the README for what works today). Resolve 21+ users can also set `ograf-editable = true` to receive lower thirds as OGraf packages that stay editable inside Resolve's Inspector.
- Premiere Pro: xmeml/EDL export is on the roadmap; the design gives you the cut plan, the edl.json cut list, and a preview render, which map 1:1 onto manual cuts.
- Descript or anything else: set `timeline-format = "none"`. The cut stage still does its most valuable work (word-level transcript, cut decisions with reasons, preview), you apply the cuts in your tool.

Whatever the editor, motion graphics arrive as ProRes 4444 MOVs with alpha, which everything accepts.

## 5. Your generation tools (the part that stops the forgetting)

If you use CLI tools for image or video generation, register each one as a `[[tools]]` entry with a `headless` invocation and a `notes` field. The notes are the persistent memory: model quirks, what the tool is bad at, how output lands. Skills follow the notes exactly instead of rediscovering the tool every session.

Common examples, all optional: the Grok CLI (Imagine stills and image-to-video with native audio, plus X/Twitter access, covered by a SuperGrok / X Premium+ subscription), the Antigravity CLI `agy` (Gemini image generation on your Google plan quota), and the Codex CLI (stills with near-perfect text rendering for thumbnails and title cards, covered by ChatGPT Plus/Pro). If you already pay for one of these subscriptions, Manticore puts it to work; if not, the metered API lanes below cover the same ground pay-per-use.

```toml
[[modules.manticore.tools]]
name = "grok"
capabilities = ["image", "video", "x"]
headless = 'grok -p "<prompt>" --always-approve'
notes = """
Imagine image and video gen (image-to-video, 720p, native audio). Cinematic
bias. Also the lane for X/Twitter research and posting. Never use for UI or
readable on-screen text; it renders as gibberish.
"""
```

No CLI tools is fine too: the API lanes (xAI for stills/b-roll, Veo for hero shots) are metered pay-per-use and configured with env var names only.

One standing rule regardless of lane: generated footage is for atmosphere and story beats. Anything showing a user interface or text that must read correctly comes from real screen recordings, because AI-generated UI and text render as convincing-at-a-glance gibberish.

## 6. Transcription for the cut stage

The cut stage needs a word-level transcript with verbatim fillers (the "um"s and restarts are exactly what gets cut). The default provider is parakeet-mlx: free, local (Apple Silicon), word timestamps, fillers preserved verbatim, no API key. The model downloads once on first run. Metered API providers (ElevenLabs Scribe, Deepgram) can slot in behind the same `[transcription]` switch if you ever need one.

## 7. Your first video

1. Invoke mc-new: pick a format (start with talking-head), get a project folder.
2. Talk to mc-braindump about the idea until you have said everything you believe about it. Your exact words become the script's raw material.
3. Approve or edit the hook + outline at gate 1.
4. Record however you always record. Drop takes in the project's `raw/` folder (constant frame rate).
5. Review the cut plan at gate 2 ("trailing 'so' at 42:20, keep or cut?"), then open the result in your editor.
6. Approve the graphics beat table at gate 3, let the engines render, and finish in your editor at gate 4.
7. After publishing, give mc-retro one round of notes. It edits the format profile and your brand files so the next video starts smarter.

## 8. Formats

Your `manticore/formats/` copies are yours to edit; each profile decides which stages run and holds a Learnings section that retro appends to. Six ship by default: talking-head, screen-tutorial (bans generated b-roll: real UI only), voiceover-explainer, short (9:16 re-edit of a parent project), livestream-pack (an OBS asset pack, not a video), course-lesson. A new format is a new markdown file.
