![BMad Manticore](docs/assets/manticore-banner.jpg)

# BMad Manticore

[![Version](https://img.shields.io/badge/version-0.0.1-blue)](.claude-plugin/marketplace.json)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-%3E%3D3.11-blue?logo=python&logoColor=white)](https://www.python.org)
[![uv](https://img.shields.io/badge/uv-package%20manager-blueviolet?logo=uv)](https://docs.astral.sh/uv/)
[![Discord](https://img.shields.io/badge/Discord-Join%20Community-7289da?logo=discord&logoColor=white)](https://discord.gg/gk8jAdXWmj)

**From brain dump to a rough cut sitting in your editor, in your own words.**

Talk through your video idea for twenty minutes. Get back a script in your own words, a word-level cut plan for your raw footage, an editable timeline sitting in your editor, brand-themed motion graphics, and a title/thumbnail package. You approve every taste decision along the way.

Manticore is an AI video production pipeline for content creators, packaged as an installable [BMad Method](https://docs.bmad-method.org/) module. It is opinionated but flexible: a wrapper and orchestrator around many great tools (your editor, transcription, generation CLIs, motion graphics engines), with strong defaults and nothing locked in. Every external tool is optional, opt-in gravy; the core pipeline needs only uv, ffmpeg, node, and git.

**100% free and open source.** No paywalls, no gated content.

## What you get per video

- A script woven from your own recorded brain-dump words under a quote-or-cut contract, linted against a blacklist of LLM tells and a 16-rule craft checklist. It sounds like you because it is you.
- A cut plan that itemizes only the calls you might disagree with, each with a timestamp and the quoted words: "trailing 'so' at 42:20, keep or cut?"
- An editable timeline in your own editor as the default deliverable. Want a finished render instead? Ask and the pipeline renders it; it just never bakes your video without you asking.
- Motion graphics as brand-themed alpha overlays (ProRes 4444, works in every NLE), anchored to the exact words you speak.
- Titles, thumbnails, description, and chapters, built around a packaging promise you approved before the script was written.
- A pipeline that gets smarter every video: your post-publish notes edit the pipeline's own files.

## How a video happens

![The Manticore pipeline: brain dump to outline (gate 1) to script to recording, then cut (gate 2), graphics beats (gate 3), graphics and assets, packaging, finishing in your editor (gate 4), and retro, which edits the pipeline's own files so the next video starts smarter](docs/assets/pipeline.svg)

mc-braindump interviews you until your own phrasing starts repeating, capturing your exact words verbatim (or ingest an existing recording or transcript instead). Four approval gates are hard stops: outline, cut plan, graphics beats, final. The pipeline presents its work and waits; nothing proceeds past a gate without your say-so.

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
    brand/      <- tokens.json, voice-bible.md, blacklist.md, exemplars/, headshots/
    formats/    <- your editable format profiles (learnings accumulate here)
    projects/   <- one folder per video, fully self-contained
    engines/    <- HyperFrames / Remotion workspaces
```

Then:

1. Run mc-setup ("run manticore setup"). It checks dependencies, interviews you about your editor, brand, and tools, and writes the studio config everything reads.
2. Run mc-new, pick a format (start with talking-head), and talk to mc-braindump about your idea.
3. Approve the hook and outline at gate 1, record how you always record, and follow the gates from there.

Full walkthrough: [Configure your own Manticore studio](docs/user-guide.md).

## Works with the tools you already have

Manticore orchestrates tools; it does not replace them. Everything below is optional and configured once in the studio config (the `[modules.manticore]` table in `_bmad/custom/config.toml`, maintained by mc-setup). Register any generation CLI you use as a `[[tools]]` entry with a headless invocation and a notes field; the notes are persistent memory, so skills drive the tool correctly every session instead of rediscovering it.

| Tool | What it provides | Cost model |
|---|---|---|
| Your editor (DaVinci Resolve, Final Cut Pro, Premiere Pro, Descript, anything) | Where you finish. Resolve/FCP get an FCPXML timeline; Premiere lanes are planned; `timeline-format = "none"` gives you the cut plan, edl.json, and preview to apply in any tool | You already have it |
| parakeet-mlx | Word-level cutting transcripts with verbatim fillers (the "um"s are exactly what gets cut) | Free, runs locally on Apple Silicon, no API key |
| Grok CLI (xAI) | Imagine stills and image-to-video b-roll clips with native audio, plus X/Twitter research and posting, from the terminal | Covered by a SuperGrok / X Premium+ subscription; metered xAI API (~$0.02 per image, ~$0.05 per video second) as the headless fallback |
| Antigravity CLI `agy` (Google) | Gemini image generation on your plan quota; Veo 3.1 video via the Gemini API | Images subscription-inclusive; video metered or via Flow credits |
| Codex CLI (OpenAI) | Stills with near-perfect text rendering (thumbnails, title cards) via gpt-image | Covered by a ChatGPT Plus/Pro subscription |
| HyperFrames and Remotion | Motion graphics engines for overlay beats, stingers, and karaoke captions | Free (Remotion is free for companies up to 3 people) |
| OGraf + SPX-GC / OBS | Broadcast graphics that stay editable in DaVinci Resolve 21+ and click-to-trigger live in OBS | Free |
| ElevenLabs SFX and Music, or Stable Audio 3 locally | Sound effects, stingers, and music beds with commercial licensing | ElevenLabs paid plans from $6/mo; Stable Audio 3 open weights run free on a Mac |
| yt-dlp | Pulls your back-catalog transcripts to build your voice bible | Free |

One standing rule regardless of lane: generated footage is for atmosphere and story beats. Anything showing a user interface or text that must read correctly comes from real screen recordings, because AI-generated UI renders as convincing-at-a-glance gibberish.

## The pipeline compounds

Taste lives in files, and the files grow with you:

- Your voice bible: how you actually talk, deconstructed from your own published transcripts with cited examples. The highest-value asset in the studio.
- Your blacklist: LLM tells and phrases you never say, enforced by the script linter. It grows every time you flag something.
- Format profile learnings: mc-retro routes every post-publish note to the file that would have prevented it. Voice miss? Voice bible. Structure miss? Format profile. Tool driven wrong? That tool's notes. If you repeatedly override the cut stage in your editor, retro mines the pattern ("always keep pre-demo breaths") into the profile.
- The ratchet only turns one way: blacklist and learnings only grow, and retro will never weaken a gate in response to convenience feedback.

## Formats

Six ship by default: talking-head, screen-tutorial (real UI only, generated b-roll banned), voiceover-explainer, short (a 9:16 re-edit of a parent project), livestream-pack (a branded OBS asset pack, not a video), and course-lesson. A format profile is a markdown file that picks which stages run and carries your accumulated learnings. A new format is a new markdown file, not new code.

## The skills

14 skills, each self-contained: a skill ships its own defaults (`customize.toml`), scripts, and knowledge, and reads only its own folder, the installed BMad core scripts, and your project files.

| Skill | What it does |
|---|---|
| mc-setup | First-run and any-time configuration |
| mc-pipeline | Where is my project, what's next, route to the right stage |
| mc-new | Scaffold a project from a format profile |
| mc-braindump | Interview you; capture your exact words verbatim |
| mc-outline | 3 hooks + one outline + the packaging promise (gate 1) |
| mc-script | Weave the script from your words; lint; craft QA |
| mc-cut | Word-level transcript, cut plan with taste calls (gate 2), edl.json, timeline export |
| mc-beats | The graphics beat table anchored to spoken words (gate 3) |
| mc-graphics | Execute beats in HyperFrames / Remotion; frame-verified alpha overlays |
| mc-ograf | Editable broadcast graphics (DaVinci Resolve 21+ and OBS/SPX-GC) |
| mc-assets | Farm b-roll stills/clips via your configured APIs or CLIs |
| mc-package | Titles, thumbnails, description, chapters |
| mc-stream-pack | A complete branded OBS livestream asset pack |
| mc-retro | Your post-publish notes edit the pipeline's own files, so it compounds |

## Design philosophy

Taste lives in files (your voice bible, format profiles, brand tokens, blacklist). Mechanics live in scripts (run via uv). Skills are thin routers between them. Capable models improve the taste files; smaller models just obey them, so the pipeline gets cheaper over time without getting worse.

## Status

This is a young project, shared early. Honest state as of 2026-07-04:

- Working today: setup and dependency checking, config resolution, project scaffolding, the brain dump / outline / script stages with live lint against your blacklist, and OGraf graphic scaffolding + verification.
- In development (contracts fixed, implementations landing): the cut lane (transcription, cut candidates, FCPXML export), render verification, API asset farming, and direct Resolve import.
- Planned: Premiere export lanes, a free local transcription provider, multitrack recording support, and a research/show-prep skill. See [TODO.md](TODO.md) for the full roadmap.

## Part of the BMad ecosystem

BMad Manticore is an official module in the [BMad Method](https://github.com/bmad-code-org/BMAD-METHOD) family, alongside the core framework, [BMad Builder](https://github.com/bmad-code-org/bmad-builder), and the [module marketplace](https://github.com/bmad-code-org/bmad-plugins-marketplace).

## Community

- [Discord](https://discord.gg/gk8jAdXWmj) — Get help, share what you make, collaborate
- [YouTube](https://youtube.com/@BMadCode) — Tutorials, master class, and more
- [X / Twitter](https://x.com/BMadCode)
- [Website](https://bmadcode.com)
- [GitHub Issues](https://github.com/bmad-code-org/bmad-manticore/issues) — Bug reports and feature requests

## Support BMad

BMad is free for everyone and always will be. Star this repo, [buy me a coffee](https://buymeacoffee.com/bmad), or email <contact@bmadcode.com> for corporate sponsorship.

## License

MIT License — see [LICENSE](LICENSE) for details.

**BMad**, **BMAD-METHOD**, and **BMad Manticore** are trademarks of BMad Code, LLC. The code is MIT licensed; the names and branding are not.

[![Contributors](https://contrib.rocks/image?repo=bmad-code-org/bmad-manticore)](https://github.com/bmad-code-org/bmad-manticore/graphs/contributors)
