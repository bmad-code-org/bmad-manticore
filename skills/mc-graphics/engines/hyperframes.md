# Engine: HyperFrames

Default engine for everything the pipeline renders as motion: per-video overlay beats, stingers, karaoke captions, and footage-facing effects. Apache 2.0, fully local, free. `npx hyperframes render` drives HTML/CSS/GSAP frame-by-frame in headless Chrome and exports overlay-only ProRes 4444 MOV with alpha (their docs recommend exactly this for Resolve workflows). Everything the pipeline uses is the local CLI and render path. The hosted conveniences (HeyGen cloud-render credits, the Studio web app, Claude Design, Figma import, AWS Lambda / Cloud Run) are never a dependency, and no HeyGen account is required.

## The skills are the source of truth (install them, favor them)

HyperFrames is pre-1.0 and moves fast, so this file never transcribes its full capability list or pins a version, which would only ship stale knowledge to every creator. Instead the agent loads HyperFrames' own Agent Skills, which teach the current authoring patterns (the `data-*` attributes, GSAP timeline registration, the component vocabulary, every effect) and refresh themselves:

- Installed at setup (mc-setup step 2b), so the whole capability surface is known from the beats stage onward, not just at graphics time: `npx skills add heygen-com/hyperframes --all --full-depth` loads the whole catalog (or `npx hyperframes skills update` for the maintained core set). Favor them: the installed skills plus the block catalog are the FIRST place to look for any beat, ahead of authoring anything by hand.
- Refreshed on every graphics run, never blindly expanded: `npx hyperframes init` refreshes the core set plus whatever is already installed; `npx hyperframes skills check` reports drift and `npx hyperframes skills update` applies it. If setup was skipped, mc-graphics installs them on first use. The engine WORKSPACE itself still initializes lazily on the first graphics run (below); only the lightweight skill knowledge lands at setup. The harness loads the skills by its own skill resolution; nothing here is specific to one agent.
- The authoritative, always-current capability index is https://hyperframes.heygen.com/llms.txt. When a beat needs something, consult the installed skills and that index rather than this file.

## What it can do (reach for these before building from scratch)

All of the following run locally and free. Names are examples, not the live list (the skills and the index above are canonical); the point is that these categories exist and the agent should reach for them:

- Block catalog (`npx hyperframes add`, 100+ blocks): code animations (typing, diff, morph, 3D extrude, particle assemble), transitions including WebGL shaders (whip pan, glitch, light leak, iris, vortex, burn), caption styles (karaoke, kinetic slam, neon, gradient, texture-mask), lower thirds, social cards (X, TikTok, Instagram, Reddit, Spotify), data viz (bar and line charts, flowcharts, US/world/choropleth maps), VFX (liquid glass, portal, shatter, news ticker, logo outro), and 3D device showcases (GLTF iPhone/MacBook with live HTML screens). Theme every block through tokens.json.
- Footage-facing media effects (these apply to the video and stills, not only to overlays): color grading with presets, project-local `.cube` LUTs, vignette, grain, blur, and pixelate via a `data-color-grading` attribute; background removal to a transparent overlay; HTML-in-Canvas to run WebGL shaders and 3D geometry over the DOM. These open real pipeline moves without leaving the local renderer: propose a few graded looks over the actual footage and let the creator pick, or cut a subject off its background.
- Delivery reach: renders to ProRes 4444 MOV, VP9 alpha WebM, MP4, GIF, and PNG sequences; HDR10 (BT.2020 PQ or HLG, 10-bit H.265) when the sources are HDR; 4K via the Chrome device scale factor.

## Setup

- The engine workspace lives at the creator's `{engines-path}/hyperframes/`, initialized on the first graphics run: install the latest published version at that moment (`npm install hyperframes@latest`) and install the skills (above). Never carry a version number in module docs. Record the resolved engine version in the workspace package.json and upgrade deliberately from there rather than floating mid-project.
- Pull the blocks a beat needs before authoring: `npx hyperframes add`. Theme every block through `{brand-path}/tokens.json`.
- `@hyperframes/studio` is the local timeline GUI with real bidirectional HTML sync (drag a beat in the GUI, the code updates; hand-edit the code, the GUI hot-reloads). Use it for timing nudges after mc-graphics gets close. The hosted Studio preview and Claude Design are optional and never required.

## Why this engine and not Remotion

Remotion is not used. Two reasons, recorded here so the decision is not relitigated per video:

- License. Remotion is free only for companies of up to 3 people; past that it needs a paid Company License (per-seat, or per-render with a monthly minimum). Manticore is a distributed module, so shipping Remotion would hand every creator at a 4+ person company a licensing obligation they did not opt into. HyperFrames is Apache 2.0 with no commercial-use threshold.
- React bought nothing. Remotion's remaining justification was "anything React-stateful", but in a frame-deterministic renderer state IS a function of frame index. Every job it might hold here (the dual-render brand stinger, word-level karaoke captions, plain HTML/SVG comps) is a paused GSAP timeline in HyperFrames, and the beat table drives it identically. One engine means one authoring model and one thing for the agent to know.

Remotion remains the stronger pick for React shops rendering at massive scale. That is not this pipeline.

## Jobs

- Per-video overlay beats: the default lane, delivered as ProRes 4444 alpha MOVs.
- Brand stinger and transitions: ONE composition rendered twice, VP9 yuva420p WebM for OBS and ProRes 4444 MOV for the editor lane. Keep it 1 to 2 seconds (transparent WebM renders slowly). HyperFrames captures each frame as PNG with alpha and encodes through either alpha-capable codec, so the dual target is one comp and two renders.
- Shorts karaoke captions: word-level highlight driven by the transcript's word timestamps, built from a registry caption block themed through tokens.json.
- Footage-facing effects: color-graded looks and background removal on the source video and stills, when a beat or the Production Bible calls for them.

## Rules

- Favor the catalog and the installed skills before authoring from scratch; a hand-built comp is the last resort, not the first.
- Footage-facing effects use the local render (`data-color-grading`, background removal, HTML-in-Canvas), never a hosted API or credits.
- Brand-themed blocks live in the engine workspace and are reused across projects; per-video comps live in each project's `graphics/` folder.
- Dual-render targets share one source of truth; never maintain two stinger comps.
- WebM VP9 alpha is for browser and OBS consumption only. Editors ignore its alpha channel and render transparent areas black, so the editor lane always takes the ProRes 4444 MOV.
- A render is not done until `{skill-root}/scripts/render_verify.py` has extracted frames and they have been visually checked.
