![Manny the Manticore perched on a clapperboard rock above the falls at red sunset](assets/manny/banner-02-clifftop-clapperboard.jpg)

# Configure Your Own Manticore Studio

How to go from a fresh install to a working studio. Setup is a real interview now (budget 30 to 60 minutes to do it well); after that, the pipeline does the remembering.

The short version: install, then say "talk to Manny". Manny the Manticore (mc-agent) is the studio's director and front door; he notices when the studio is not set up, runs the onboarding interview with you, turns your ideas into projects, routes footage you already have into footage-first projects, and drives every stage after that. This guide is the same path spelled out, for when you want to understand or drive the pieces yourself.

Note on status: the README's Status section is the source of truth for what is production-proven versus newly built. mc-setup's closing summary also tells you, per configured lane, what is implemented versus planned on your machine.

## 1. Pick where your studio lives

Manticore is designed to be installed at the root of a dedicated content project: one folder that holds your config, your brand, and every video you make. Global/user-level installs work, but a project root gives you a self-contained, versionable studio.

```
my-studio/                        <- install here, run everything from here
  _bmad/custom/config.toml        <- studio config: the [modules.manticore] table
                                     (mc-setup writes it; config.user.toml overrides)
  .env.example                    <- scaffolded by setup if any opted-in lane needs a key
  manticore/
    brand/                        <- tokens.json, production-bible.md, voice-bible.md,
                                     blacklist.md, exemplars/, headshots/
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

## 2. Run mc-setup: the onboarding interview

Say "talk to Manny" and he routes you here, or say "run manticore setup" directly. It walks you through everything below and writes the studio config: the `[modules.manticore]` table in `_bmad/custom/config.toml` (personal overrides go in `config.user.toml` next to it). Every other skill resolves that table with the installed `_bmad/scripts/resolve_config.py`; if it is empty, they send you back here. Re-run it any time; it updates rather than overwrites, and it detects a 0.x studio and runs a short delta interview instead of starting over. Each skill also ships its own `customize.toml` defaults, overridable per skill in `_bmad/custom/<skill>.toml`.

What the interview covers, in order:

- Dependencies and platform: uv (required; runs all pipeline scripts), ffmpeg (with ffprobe), node/npx, git, optionally yt-dlp. It checks, you approve any installs. It also runs a platform gate: the default transcription lane is Apple-Silicon-only, and on other machines it points you at the documented fallbacks (see section 6).
- You: name, channel, the links that go in your video descriptions, your speaking rate (the guided voice-bible build later measures it from a real transcript, which beats the generic 145 wpm every time).
- Video defaults: record resolution, delivery resolution, fps. Offer it a recent recording and it fills the values from ffprobe instead of guessing.
- Live tool: obs, ecamm, or other, which drives the stream-pack lane's deliverable format. Any recurring shows or series you produce get noted for series folders and packaging templates.
- Render consent: see section 3.
- The video style interview: see section 4.
- Your brand build, headshots, and the guided voice bible: see section 5.
- Your editor: see section 7.
- Your generation tools, asset lanes, and audio lanes: see section 8.
- Keys and .env.example: only if you opted into a metered lane, setup confirms the env var name (never the value) and tells you, inside that opt-in branch only, where that vendor issues keys. It then scaffolds a `.env.example` listing exactly the env vars your resolved config references, with a one-line source note per key. Local-first defaults usually mean there are none, and then no file is written.
- The honest runnability report: setup closes by telling you exactly what will happen on your first project with these settings, which configured lanes are implemented versus planned, and which gaps are pending (missing headshots block thumbnails, an unbuilt voice bible, placeholder Production Bible sections, unverified tools, empty asset lanes). The pending list is your highest-value next work.

## 3. Render consent: the render-first default

Manticore renders by default, and setup asks you to confirm it rather than assume it:

- A fast low-res preview render is produced after every cut-plan approval, so every iteration is watchable.
- Once the graphics stage has rendered the overlays, the preview re-renders with them composited, so you iterate on overlays and CTAs visually.
- At the final gate, a final-quality render is offered (delivery resolution and codec per your config).
- The editor timeline export and all assets (edl.json, cutplan.md, overlays) are ALWAYS created alongside, so you can jump into Resolve, Premiere, or any editor at any step without losing work.

Decline it and previews and finals become offers the pipeline makes instead of automatic outputs; the timeline export and assets remain always-on either way.

## 4. The video style interview

This is where 1.0 stops producing sparse text cards: your visual taste is captured up front and seeds the Production Bible (`{brand-path}/production-bible.md`), the styling contract every visual stage reads before authoring anything. It asks:

- A creator to emulate: drop links to videos whose style you want to lean toward. Setup studies them and echoes back what it thinks the takeaway is (fast funny meme cuts? polished charts and dataviz? a particular edit rhythm?), and you confirm before anything lands in the bible. The confirmed takeaways seed every question below as proposed defaults.
- Visual density: high (a graphic beat roughly every 10 to 20 seconds), medium (20 to 45), or low (45 to 90), on a front-loaded pacing curve. Default medium; tutorials and explainers usually want high.
- Preferred image types: SVG/diagrammatic where text must be accurate, generative imagery for what does not exist, real verified imagery first for anything that does. The sourcing hierarchy is real, then generative, then hand-built text card.
- Overlay and popup aesthetic: describe a look, point at reference screenshots or creators to emulate, or supply overlays you have already shipped.
- Animation feel: snappy, smooth, or dramatic, plus entrance/exit conventions, mapped onto your brand tokens' motion values.
- CTA inventory and appetite: which CTAs you run (subscribe, community, support, product, next-video, playlist, site) and how aggressively. mc-beats plans CTA beats from this inventory at research-backed placements, and you approve them at gate 3 like any other graphic.
- Asset libraries you already own (icon sets, b-roll folders, photo archives) and where they live.

The Production Bible evolves after setup: mc-retro routes every visual-style note into it, dated and append-only.

## 5. Your brand folder

`{brand-path}` (default `manticore/brand/`) is where your identity lives, and setup's exit state is filled, never placeholders. Point it at anything that already defines your brand or voice (a website, CSS, design tokens, style guides, past videos) and it mines those sources before asking you anything.

- `tokens.json`: colors, fonts, logo paths, motion timings. Every graphic in every engine reads this file; change it once, everything follows. Filled from your mined brand sources when they exist.
- `production-bible.md`: the visual taste contract from section 4, scaffolded and filled during setup.
- `blacklist.md`: regex patterns for LLM tells and phrases you never say. Ships with a starter set; grows every time you flag something in retro.
- `voice-bible.md`: the rules of how you actually talk. Setup offers a guided build: give it your published YouTube URLs or transcripts (fetched with yt-dlp, with permission) plus any reference creators, and it distills an evidence-cited bible where every rule quotes a verbatim example, measures your real wpm from your own transcript, and keeps your voice separate from reference voices. This is the highest-value asset in the studio.
- `exemplars/`: your best published scripts as spoken transcripts, saved during the voice-bible build (`own/` and `reference/` kept separate).
- `headshots/`: 3 to 6 approved photos of you with varied expressions (neutral, surprised, thinking, excited). Setup classifies and indexes them. When a thumbnail or generated asset needs you in it, the original photo goes straight to your image model with a "use the person in this image" prompt, and any revision re-sends the same original photo with an improved prompt, never a previous generation (chained edits degrade like a photocopy of a photocopy). Approved photos only; thumbnails are blocked until headshots exist, and setup says so loudly.

## 6. Transcription for the cut stage

The cut stage needs a word-level transcript with verbatim fillers (the "um"s and restarts are exactly what gets cut). The default provider is parakeet-mlx: free, local, word timestamps, fillers preserved verbatim, no API key. The model downloads once on first run.

Platform honesty: parakeet-mlx runs only on macOS Apple Silicon. On other machines, setup's dependency check flags it and points at local whisper.cpp or faster-whisper as fallbacks; they normalize fillers away, so cut quality drops, and a supported cross-platform lane is on the roadmap. Metered API providers exist behind the same `[transcription]` switch as explicit opt-in choices only; nothing metered is configured unless you choose it.

## 7. Your editor

Render-first does not lock you out of your editor; the exit ramp is always built. Tell mc-setup what you finish in:

- DaVinci Resolve or Final Cut Pro: an FCPXML timeline of trimmable clips (implemented), exported on every cut approval. Resolve 21+ users can also set `ograf-editable = true` to receive lower thirds as OGraf packages that stay editable inside Resolve's Inspector.
- Premiere Pro: the xmeml export lane has not landed yet, so Premiere users work from the cut plan, edl.json, and the rendered preview/final, which map 1:1 onto manual cuts.
- Descript or anything else: set `timeline-format = "none"`. You get the word-level transcript, cut decisions with reasons, and the renders; you apply the cuts in your tool.

Whatever the editor, motion graphics arrive as ProRes 4444 MOVs with alpha, which everything accepts.

## 8. Your generation tools (CLI-first, metered opt-in)

Asset lanes are CLI-tool-first: a generation CLI backed by a subscription you already pay for is the preferred lane, and metered APIs are an explicit opt-in, never a silent default. Register each CLI you use as a `[[tools]]` entry with a `headless` invocation and a `notes` field. The notes are the persistent memory: model quirks, what the tool is bad at, how output lands. Setup verifies each registered tool end to end (a real tiny invocation whose output file is checked) and records the result.

Common examples, all optional: the Grok CLI (Imagine stills and image-to-video with native audio, plus X/Twitter access, covered by a SuperGrok / X Premium+ subscription), the Antigravity CLI `agy` (Gemini image generation on your Google plan quota), and the Codex CLI (stills with near-perfect text rendering for thumbnails and title cards, covered by ChatGPT Plus/Pro).

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

A lane with no good answer stays empty: mc-assets stops and asks at farming time rather than bill anyone by default.

One standing rule regardless of lane: generated footage is for atmosphere and story beats. Anything showing a user interface or text that must read correctly comes from real screen recordings, because AI-generated UI and text render as convincing-at-a-glance gibberish.

Sound follows the same local-first pattern through the mc-audio service skill: TTS narration and two-host dialogue (Kokoro-82M; stock voices, no cloning, so narration in your own voice still means recording it), instrumental music beds (MusicGen-small), and SFX (AudioLDM2) all run free and local. Setup confirms these lanes and offers to build the engine workspace at `manticore/engines/audio-lab` (a several-GB venv, ~340 MB of voice models now, ~5 GB of model cache on the first music or SFX run; nothing downloads without your go-ahead). Full songs with vocals have no validated local lane yet, and paid audio lanes (ElevenLabs, Gemini TTS) are explicit opt-ins.

## 9. Your first video

Idea-first, the full pipeline:

1. Tell Manny you have an idea (or invoke mc-new): pick a format (start with talking-head), get a project folder.
2. Talk to mc-braindump about the idea until you have said everything you believe about it. Your exact words become the script's raw material. Offer to record the session: with the camera rolling, you read each interview question aloud prefixed with the marker cue (default "question from the interviewer"), and the cut stage segments the recording mechanically, so the braindump becomes usable footage.
3. Approve or edit the hook + outline at gate 1.
4. Record however you always record. Drop takes in the project's `raw/` folder; the cut stage preflights them and remuxes variable frame rate sources automatically.
5. Review the cut plan at gate 2 ("trailing 'so' at 42:20, keep or cut?"). Every approval produces a watchable preview render, and the editor timeline lands next to it.
6. Riff the graphics with Manny before the table exists: he pitches the moments and treatments he sees, you tell him what you were picturing. Then approve the graphics beat table (including the planned CTA beats) at gate 3. The engines render, and the preview re-renders with graphics composited so you see the actual video.
7. At gate 4, take the offered final-quality render, or finish in your editor from the always-exported timeline. Either closes the gate.
8. After publishing, give mc-retro one round of notes. It edits the format profile, your bibles, and your brand files so the next video starts smarter, then offers the post-publish wrap (archive hygiene, promoting evergreen assets).

Footage-first, when the video already exists:

1. Hand Manny the file ("cut this VOD", "make a video from this recording"). mc-new's ingest mode registers the source and writes a post-production stage list that starts at cut.
2. The same gates apply from the cut stage onward: cut plan, beats with CTAs mined from the transcript, graphics, packaging with dual-timeline chapters, the final render offer.

## 10. The teleprompter

mc-prompter is a service skill, not a stage: say "prompt me" or "record with the teleprompter" and it launches a local browser prompter for the recording you were going to do anyway. It comes in three tiers, and each one is optional on top of the one below.

The classic prompter needs nothing extra: no models, no downloads, no workspace. It serves a fullscreen scrolling display with the standard feature set (mirror flips for beam-splitter rigs, adjustable speed and fonts, countdown, timed mode, section jumps), a home page for loading or pasting text, and a phone remote over LAN whose URL carries a per-session token. Inside a pipeline project it prompts `script.md` directly and understands its markers: `[TAKE ...]` lines render dimmed because they were already spoken well on the interview footage, and `[INVENTED]` flags show as subtle badges. Editing from the home page backs up the file before writing, so the prompted text and the pipeline artifact never diverge.

Voice-follow makes the scroll track your voice through the script using local streaming ASR. It needs the prompter-lab workspace (default `manticore/engines/prompter-lab`): a one-time download of about 465 MB of model files plus a small venv, and nothing downloads without your explicit go-ahead. Declining always leaves the classic prompter working. The first enable runs a preflight: pick your microphone, watch the level meter, and read a few words until the tracking check passes. After that, silence or ad-libs hold the scroll and it resumes when you return to the script; clicking any word re-anchors instantly. The microphone is captured on the machine running the server, so a tablet pointed at the page is display-only.

Producer mode is for shows that run on talking points instead of a word-for-word script. You write a rundown, a small markdown file (a starter template lands in `{brand-path}/templates/rundown-template.md` during setup):

```markdown
---
show: "Why local models win"
duration-minutes: 30
cue-density: normal
wrap-minutes: 3
---

## Intro (3 min)

Full scripted intro text, prompted normally.

## Point 1: The cost argument (5 min)

- cloud bills compound, local is capex
- the anecdote that proves it

## Wrap (3 min)

Scripted wrap text.
```

Segments with prose prompt like a script; segments with only bullets become tracked talking points. Time budgets are optional, `wrap-minutes` protects your closing segment, and the home page shows the reconciled plan (with any warnings) before you go live.

Running a show: hit GO LIVE on the prompt page or the phone remote to start the show clock. A rail shows elapsed time, the current segment with its remaining time in green, yellow, or red, and your next uncovered point; when you run long, the remaining time is replanned across what is left rather than just turning red. Cues speak broadcast in two tiers: quiet cards ("30 seconds", "STRETCH", "DROP: point 4, or 90s each") appear at your configured density, while "WRAP" and the overtime clock ("2:30 OVER") flash as high-contrast attention cues that ignore the density budget. Hold freezes the clock during technical trouble. The remote is your override authority: tap any point to mark it covered or skip it, jump between segments with make current, and the producer never un-marks anything you decided.

For OBS, add `/overlay` as a browser source: it is transparent and renders only the rail, the cue cards, and small connection and voice-tracking badges, so your live audience sees a clean frame while you see the producer.

What requires Ollama: only the coverage judgments, where a small local model (default `qwen3:4b`) reads the rolling transcript and proposes which points you have covered. Everything else in producer mode, the rail, the replanning, and every time cue, is deterministic code and works with no LLM at all; without Ollama you mark points covered from the remote yourself. Nothing metered, nothing cloud: the `[llm]` lane is local-first like every other lane.

## 11. Formats

Your `manticore/formats/` copies are yours to edit; each profile decides which stages run, carries structured density and beat-type frontmatter, and holds a Learnings section that retro appends to. Seven ship by default: talking-head, screen-tutorial (bans generated b-roll: real UI only), voiceover-explainer (narration is creator-recorded until the TTS lane lands), short (9:16 re-edit of a parent project), livestream-pack (an OBS asset pack, not a video), livestream-vod (footage-first post-production of a stream recording), course-lesson. A new format is a new markdown file.
