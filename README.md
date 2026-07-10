![Manny the Manticore, wings spread, diving head-on over a waterfall at red sunset](docs/assets/manny/banner-03-headon-lensflare.jpg)

# BMad Manticore

[![Version](https://img.shields.io/badge/version-1.0.0-blue)](.claude-plugin/marketplace.json)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-%3E%3D3.11-blue?logo=python&logoColor=white)](https://www.python.org)
[![uv](https://img.shields.io/badge/uv-package%20manager-blueviolet?logo=uv)](https://docs.astral.sh/uv/)
[![Discord](https://img.shields.io/badge/Discord-Join%20Community-7289da?logo=discord&logoColor=white)](https://discord.gg/gk8jAdXWmj)

**From brain dump to a rendered, graphics-rich video, in your own words.**

Talk through your video idea for twenty minutes, or hand over footage you already have. Get back a script in your own words, a word-level cut plan for your raw footage, a watchable preview render of every iteration, brand-themed motion graphics with CTAs placed where they work, a title/thumbnail package, and an offered final-quality render at the end. The editor timeline export and all cut assets are always produced alongside, so you can move into your own editor at any step. You approve every taste decision along the way.

Manticore is an AI video production pipeline for content creators, packaged as an installable [BMad Method](https://docs.bmad-method.org/) module. It is opinionated but flexible: a wrapper and orchestrator around many great tools (your editor, transcription, generation CLIs, motion graphics engines), with strong defaults and nothing locked in. Every external tool is optional, opt-in gravy; the core pipeline needs only uv, ffmpeg, node, and git.

**100% free and open source, local-first.** No paywalls, no gated content, and no paid vendor ships in any default. Metered lanes exist only as explicit opt-ins you choose during setup.

## What you get per video

- A script woven from your own recorded brain-dump words under a quote-or-cut contract, linted against a blacklist of LLM tells and a 16-rule craft checklist. It sounds like you because it is you.
- A cut plan that itemizes only the calls you might disagree with, each with a timestamp and the quoted words: "trailing 'so' at 42:20, keep or cut?"
- A render at every step: a fast low-res preview after every cut approval, the same preview re-rendered with graphics composited once the graphics stage has rendered the overlays, and a final-quality render offered at the last gate. The editor timeline export, edl.json, cutplan, and overlays are always written too, so jumping into your editor never loses work.
- Motion graphics planned under creativity mandates and your visual density tier, styled by your Production Bible, delivered as brand-themed alpha overlays (ProRes 4444, works in every NLE), anchored to the exact words you speak.
- CTAs planned like beats: your configured CTA inventory placed at research-backed seams, approved by you in the same gate as every other graphic.
- Titles, thumbnails (A/B pairs for series), description, and chapters, built around a packaging promise you approved before the script was written.
- A pipeline that gets smarter every video: your post-publish notes edit the pipeline's own files.

## How a video happens

![The Manticore pipeline: talk to Manny, brain dump to outline (gate 1) to script to recording, or bring existing footage straight to the cut; then cut with a preview render every iteration (gate 2), graphics beats (gate 3), graphics and assets, packaging, the final render offer or finishing in your editor (gate 4), and retro, which edits the pipeline's own files so the next video starts smarter](docs/assets/pipeline.svg)

Prefer to see it by scenario? Two slide guides ship in docs/ (open them in a browser): [Working with Manny](docs/manny-scenarios.html) covers setup day, an idea-first video, bringing existing footage, the weekly livestream, packaging, and the retro loop; [Manny, under the hood](docs/manny-under-the-hood.html) covers the machinery: the tool and model roster, the spoken-idea-to-script-to-re-record flow, the raw-footage recut flow, the render pipeline, and the graphics engines.

mc-braindump interviews you until your own phrasing starts repeating, capturing your exact words verbatim (optionally with the camera already rolling, so the interview itself becomes usable footage). Four approval gates are hard stops: outline, cut plan, graphics beats, final. The pipeline presents its work and waits; nothing proceeds past a gate without your say-so.

Already have the footage? A livestream VOD, a recorded talk, any recording made outside the pipeline enters through mc-new's footage-first mode: the project skips ideation, registers the source, and starts at the cut stage with the same gates from there.

The cutting is craft-grade: never cut inside a word, 30 to 200 ms padding on cut edges, 30 ms audio fades, every cut recorded with the quoted words and the reason, and cut boundaries self-verified by extracting and inspecting frames.

## Install

Recommended: install at the root of a dedicated content folder (your "studio"). One installation serves many videos; every video is a self-contained folder.

```bash
npx bmad-method install --custom-source https://github.com/bmad-code-org/bmad-manticore
```

```
my-studio/                        <- install here, run everything from here
  _bmad/custom/config.toml        <- studio config ([modules.manticore]; mc-setup writes it)
  manticore/
    brand/      <- tokens.json, production-bible.md, voice-bible.md, blacklist.md,
                   exemplars/, headshots/
    formats/    <- your editable format profiles (learnings accumulate here)
    projects/   <- one folder per video, fully self-contained
    engines/    <- HyperFrames / Remotion workspaces
```

Then say "talk to Manny". Manny the Manticore (mc-agent) is the studio's director and front door: he detects that the studio is not set up yet and walks you through mc-setup's onboarding interview (identity, editor, render consent, video style, brand, headshots, voice bible, tools), turns your first idea into a project, routes existing footage into a footage-first project, and drives every stage from there. You never have to know which skill does what.

Prefer to drive the skills directly? The same path by hand:

1. Run mc-setup ("run manticore setup"). It checks dependencies and platform support, interviews you about your editor, render defaults, visual style, brand, and tools, and writes the studio config everything reads.
2. Run mc-new, pick a format (start with talking-head), and talk to mc-braindump about your idea. Or point mc-new at existing footage and start at the cut.
3. Approve the hook and outline at gate 1, record how you always record, and follow the gates from there.

Full walkthrough: [Configure your own Manticore studio](docs/user-guide.md).

## Supported platforms

| Platform | Status |
|---|---|
| macOS on Apple Silicon | The reference platform. Everything runs, including the default transcription lane (parakeet-mlx, free and local). |
| macOS on Intel, Linux, Windows | The pipeline scripts run (uv, ffmpeg, node), but the default transcription lane does not: parakeet-mlx is Apple-Silicon-only. mc-setup's dependency check flags this and points at local whisper.cpp or faster-whisper as fallbacks (they normalize fillers away, so cut quality drops). A supported cross-platform transcription lane is planned; see [TODO.md](TODO.md). |

One tool-specific note: Ecamm Live (a planned stream-pack delivery target) is macOS-only. OBS lanes work everywhere OBS does.

## Works with the tools you already have

Manticore orchestrates tools; it does not replace them. The defaults are local and free; every paid or metered lane below is opt-in, chosen explicitly during setup and never billed silently. Everything is configured once in the studio config (the `[modules.manticore]` table in `_bmad/custom/config.toml`, maintained by mc-setup). Register any generation CLI you use as a `[[tools]]` entry with a headless invocation and a notes field; the notes are persistent memory, so skills drive the tool correctly every session instead of rediscovering it, and setup verifies each registered tool end to end.

| Tool | What it provides | Cost model |
|---|---|---|
| Your editor (DaVinci Resolve, Final Cut Pro, Premiere Pro, Descript, anything) | Where you can finish any time you want to. Resolve/FCP get an FCPXML timeline; Premiere users work from the cut plan, edl.json, and the rendered preview/final until the xmeml lane lands; `timeline-format = "none"` gives you the cut plan, edl.json, and renders to apply in any tool | You already have it |
| parakeet-mlx | Word-level cutting transcripts with verbatim fillers (the "um"s are exactly what gets cut) | Free, runs locally on Apple Silicon, no API key |
| Kokoro-82M (kokoro-onnx) | TTS narration and two-host dialogue for the mc-audio lane (stock voices, no cloning) | Free, local, faster than realtime on CPU |
| MusicGen-small + AudioLDM2 | Instrumental music beds and SFX, farmed locally by mc-audio | Free, local, ungated models |
| HyperFrames and Remotion | Motion graphics engines for overlay beats, stingers, and karaoke captions | Free (Remotion is free for companies up to 3 people) |
| OGraf + SPX-GC / OBS | Broadcast graphics that stay editable in DaVinci Resolve 21+ and click-to-trigger live in OBS | Free |
| yt-dlp | Pulls your back-catalog transcripts to build your voice bible | Free |
| Grok CLI (xAI), opt-in | Imagine stills and image-to-video b-roll clips with native audio, plus X/Twitter research and posting, from the terminal | Covered by a SuperGrok / X Premium+ subscription; a metered xAI API lane exists only as an explicit opt-in |
| Antigravity CLI `agy` (Google), opt-in | Gemini image generation on your plan quota | Subscription-inclusive |
| Codex CLI (OpenAI), opt-in | Stills with near-perfect text rendering (thumbnails, title cards) via gpt-image | Covered by a ChatGPT Plus/Pro subscription |

Asset lanes ship unset: mc-setup defaults each lane to a CLI you already pay for and have verified, and mc-assets stops and asks rather than bill a metered API you never chose. Audio lanes ship local: mc-audio's TTS, music, and SFX defaults are free local models, and the paid rungs (ElevenLabs, Gemini TTS) are opt-in choices that follow the same pattern; see [TODO.md](TODO.md) for what remains.

One standing rule regardless of lane: generated footage is for atmosphere and story beats. Anything showing a user interface or text that must read correctly comes from real screen recordings, because AI-generated UI renders as convincing-at-a-glance gibberish.

## The pipeline compounds

Taste lives in files, and the files grow with you:

- Your voice bible: how you actually talk, deconstructed from your own published transcripts with cited examples. Setup offers a guided build (yt-dlp fetch, evidence-cited rules, your real measured words per minute).
- Your Production Bible: the visual half of the taste system. Brand usage scope, motion feel, overlay aesthetic, image-type policy, visual density, and CTA configuration, built interactively at setup and read by every visual stage before it authors anything.
- Your blacklist: LLM tells and phrases you never say, enforced by the script linter. It grows every time you flag something.
- Format profile learnings: mc-retro routes every post-publish note to the file that would have prevented it. Voice miss? Voice bible. Visual style miss? Production Bible. Structure miss? Format profile. Tool driven wrong? That tool's notes.
- The ratchet only turns one way: blacklist, bible learnings, and profile learnings only grow, and retro will never weaken a gate in response to convenience feedback.

## Formats

Seven ship by default: talking-head, screen-tutorial (real UI only, generated b-roll banned), voiceover-explainer, short (a 9:16 re-edit of a parent project), livestream-pack (a branded OBS asset pack, not a video), livestream-vod (footage-first post-production of a stream recording), and course-lesson. A format profile is a markdown file that picks which stages run and carries your accumulated learnings. A new format is a new markdown file, not new code.

## The skills

17 skills, each self-contained: a skill ships its own defaults (`customize.toml`), scripts, and knowledge, and reads only its own folder, the installed BMad core scripts, and your project files.

| Skill | What it does |
|---|---|
| mc-agent | Manny the Manticore, the visionary director: the studio front door, onboarding, routing (including footage-first arrivals), and coaching |
| mc-setup | First-run and any-time configuration: the full onboarding interview, brand and bible builds, tool registration, 0.x migration |
| mc-pipeline | Where is my project, what's next, route to the right stage |
| mc-new | Scaffold a project from a format profile, idea-first or footage-first, with series and deadline modes |
| mc-braindump | Interview you; capture your exact words verbatim (camera-rolling optional) |
| mc-outline | 3 hooks + one outline + the packaging promise (gate 1) |
| mc-script | Weave the script from your words; lint; craft QA |
| mc-cut | Word-level transcript, cut plan with taste calls (gate 2), edl.json, preview render every iteration, timeline export, the offered final render |
| mc-beats | The graphics beat table anchored to spoken words, under creativity mandates and your density tier, with a CTA placement pass (gate 3) |
| mc-graphics | Execute beats in HyperFrames / Remotion / HTML / design-prompting; frame-verified alpha overlays |
| mc-ograf | Editable broadcast graphics (DaVinci Resolve 21+ and OBS/SPX-GC) |
| mc-assets | Farm b-roll stills/clips via your registered CLI tools (metered APIs opt-in), under generative-editing safety rules |
| mc-audio | Farm sound, local-first: TTS narration and two-host dialogue (Kokoro-82M), instrumental beds (MusicGen-small), SFX (AudioLDM2); paid lanes opt-in |
| mc-prompter | Browser teleprompter for the record stage and standalone shows: voice-follow scrolling (local streaming ASR) and producer mode (rundown-driven live shows with a timing rail, broadcast cues, and an OBS overlay; local Ollama opt-in) |
| mc-package | Titles, thumbnails (verified at 120px), description, chapters, series A/B pairs, live-event mode |
| mc-stream-pack | A complete branded OBS livestream asset pack |
| mc-retro | Your post-publish notes edit the pipeline's own files, plus the post-publish wrap lane |

## Design philosophy

Taste lives in files (your voice bible, Production Bible, format profiles, brand tokens, blacklist). Mechanics live in scripts (run via uv). Skills are thin routers between them. Capable models improve the taste files; smaller models just obey them, so the pipeline gets cheaper over time without getting worse.

## Status

1.0.0 is the first release shaped by real production use. Honest state as of 2026-07-07:

- Proven in production: the full cut lane (parakeet-mlx word-level transcription validated on real footage, cut candidate detection, edl.json, FCPXML export, preview render with boundary-frame verification), Manny as the front door, setup and dependency checking, config resolution, project scaffolding, the OBS stream pack, and the retro loop.
- New in 1.0, implemented and unit-tested, with the least real-project mileage: the render lane (composited preview and the offered final render), the expanded setup interview (render consent, video style, creator-emulation takeaways, headshots, guided voice bible), the Production Bible, creativity mandates and the CTA system, footage-first ingest and the livestream-vod format, series support, graphics render verification, the graphics toolkit (HTML render, snug framing, design-prompting lane), CLI-registry asset farming, and the mc-audio local sound lanes (validated end to end on Apple Silicon 2026-07-07).
- Newest, implemented and unit-tested since that date: mc-prompter, the browser teleprompter service skill (classic prompter, voice-follow via local streaming ASR, and rundown-driven producer mode with an opt-in local Ollama lane).
- The writing lane (braindump, outline, script) is the core promise and is wired end to end with live blacklist linting; it has had the least real-video exercise of the core stages, so treat your first run through it as a shakedown and feed mc-retro afterward.
- Planned: Premiere (xmeml) and CMX3600 EDL export lanes, per-episode stream packs with the Ecamm target (the named 1.0.x fast-follow), multitrack recording support, local-first TTS/SFX/music lanes, and a research/show-prep skill. See [TODO.md](TODO.md) for the full roadmap.

## Part of the BMad ecosystem

BMad Manticore is an official module in the [BMad Method](https://github.com/bmad-code-org/BMAD-METHOD) family, alongside the core framework, [BMad Builder](https://github.com/bmad-code-org/bmad-builder), and the [module marketplace](https://github.com/bmad-code-org/bmad-plugins-marketplace).

## Community

- [Discord](https://discord.gg/gk8jAdXWmj) for help, sharing what you make, and collaboration
- [YouTube](https://youtube.com/@BMadCode) for tutorials, master class, and more
- [X / Twitter](https://x.com/BMadCode)
- [Website](https://bmadcode.com)
- [GitHub Issues](https://github.com/bmad-code-org/bmad-manticore/issues) for bug reports and feature requests

## Support BMad

BMad is free for everyone and always will be. Star this repo, [buy me a coffee](https://buymeacoffee.com/bmad), or email <contact@bmadcode.com> for corporate sponsorship.

## License

MIT License, see [LICENSE](LICENSE) for details.

**BMad**, **BMAD-METHOD**, and **BMad Manticore** are trademarks of BMad Code, LLC. The code is MIT licensed; the names and branding are not.

[![Contributors](https://contrib.rocks/image?repo=bmad-code-org/bmad-manticore)](https://github.com/bmad-code-org/bmad-manticore/graphs/contributors)
