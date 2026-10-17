# Engine: HyperFrames

Default engine for everything the pipeline renders as motion: per-video overlay beats, stingers, karaoke captions, and footage-facing effects. Apache 2.0, fully local, free. `npx hyperframes render` drives HTML/CSS/GSAP frame-by-frame in headless Chrome and exports overlay-only ProRes 4444 MOV with alpha. Everything the pipeline uses is the local CLI and render path; the hosted conveniences (HeyGen cloud-render credits, Claude Design, Figma import, AWS Lambda / Cloud Run) are never a dependency, and no HeyGen account is required.

## The skills are the source of truth

HyperFrames is pre-1.0 and moves fast, so anything transcribed here ships stale. Load HyperFrames' own Agent Skills instead, which teach the current authoring patterns (the `data-*` attributes, GSAP timeline registration, the component vocabulary, every effect) and refresh themselves.

- mc-setup installs them (`npx skills add heygen-com/hyperframes --all` for the whole catalog, or `npx hyperframes skills update` for the maintained core set), so the capability surface is known from the beats stage onward. mc-graphics installs them on first use if setup was skipped.
- `npx hyperframes init` refreshes the core set plus whatever is already installed; `npx hyperframes skills check` reports drift and `npx hyperframes skills update` applies it. Refresh, never blindly expand.
- When a beat needs something, read the installed skills rather than this file, and fetch https://hyperframes.heygen.com/llms.txt for the always-current capability index when they do not cover it.

The engine WORKSPACE still initializes lazily on the first graphics run (below); only the lightweight skill knowledge lands at setup.

## What it can do

All local and free. These are categories to reach for, not the live list (the skills and the index are canonical):

- Registry catalog (about 400 blocks and components): code animations, WebGL shader transitions, caption styles including karaoke, lower thirds, social cards, data viz and maps, VFX, and 3D device showcases with live HTML screens. Theme every block through tokens.json.
- Footage-facing media effects apply to the video and stills, not only to overlays: color grading with presets, project-local `.cube` LUTs, vignette, grain, blur, and pixelate via a `data-color-grading` attribute; background removal to a transparent overlay; HTML-in-Canvas to run WebGL shaders and 3D geometry over the DOM. These open real pipeline moves without leaving the local renderer: propose a few graded looks over the actual footage and let the creator pick, or cut a subject off its background.
- Delivery reach: ProRes 4444 MOV, VP9 alpha WebM, MP4, GIF, and PNG sequences; HDR10 (BT.2020 PQ or HLG, 10-bit H.265) when the sources are HDR; 4K via the Chrome device scale factor.

## Setup

- The engine workspace lives at the creator's `{engines-path}/hyperframes/`, initialized on the first graphics run: `npm install hyperframes@latest`, plus the skills above. Never carry a version number in module docs. Record the resolved engine version in the workspace package.json and upgrade deliberately rather than floating mid-project.
- Search the catalog for every beat before authoring: `npx hyperframes catalog --query "<the beat in plain words>"` ranks the whole registry locally, with nothing installed and no account. Pull what fits with `npx hyperframes add <name>`. Theme every block through `{brand-path}/tokens.json`.
- Studio is the local timeline editor with two-way HTML sync (drag a clip and the code updates; edit the code and Studio reloads). Open it with `npx hyperframes preview`. Use it for timing nudges after mc-graphics gets close; the `hyperframes-studio` skill covers timeline layout and safe zones.
- Audio placed inside a composition mixes through the `hyperframes-audio` skill: fades, gain, a music bed carved under the voice, and a `truepeak` limiter on the root master bus. mc-audio still sources the sound; hyperframes-audio mixes it once it is in the composition. A format profile's final integrated-loudness pass still runs after the render.

## Jobs

- Per-video overlay beats: the default lane, delivered as ProRes 4444 alpha MOVs.
- Brand stinger and transitions: ONE composition rendered twice, VP9 yuva420p WebM for OBS and ProRes 4444 MOV for the editor lane. Keep it 1 to 2 seconds; transparent WebM renders slowly.
- Shorts karaoke captions: word-level highlight driven by the transcript's word timestamps, built from a registry caption block themed through tokens.json.
- Footage-facing effects: color-graded looks and background removal on the source video and stills, when a beat or the Production Bible calls for them.

## Rules

- Favor the catalog and the installed skills before authoring from scratch; a hand-built comp is the last resort, not the first.
- Footage-facing effects use the local render (`data-color-grading`, background removal, HTML-in-Canvas), never a hosted API or credits.
- Brand-themed blocks live in the engine workspace and are reused across projects; per-video comps live in each project's `graphics/` folder.
- Dual-render targets share one source of truth; never maintain two stinger comps.
- WebM VP9 alpha is for browser and OBS consumption only. Editors ignore its alpha channel and render transparent areas black, so the editor lane always takes the ProRes 4444 MOV.
- Overlay roots stay transparent. Alpha renders keep any background on the composition root (the element with `data-composition-id`), so a background there renders as an opaque full-frame overlay. Give a stage ground only to comps meant to cover the frame, and pass `--allow-opaque` to render_verify.py for those.
- Manticore's gates replace HyperFrames' own review step. Its skills open Studio and ask "render now, or what changes?" before a final render. Opening Studio is fine, but do not stop on that question: the gate approvals in project.json decide what renders.
- A render is not done until `{skill-root}/scripts/render_verify.py` passes and its extracted frames have been visually checked.
