# Engine: HyperFrames

Default engine for per-video overlay beats, stingers, and karaoke captions. Apache 2.0, fully local, free: `npx hyperframes render` drives HTML/CSS/GSAP frame-by-frame in headless Chrome. Export overlay-only ProRes 4444 MOV with alpha (their docs recommend exactly this for Resolve workflows). The hosted MCP/HeyGen credits are optional convenience; the pipeline never depends on them.

## Why this engine and not Remotion

Remotion was the module's second engine through 0.x and was removed on 2026-07-22. Two reasons, recorded here so the decision is not relitigated per video:

- License. Remotion is free only for companies of up to 3 people; past that it needs a paid Company License (per-seat, or per-render with a monthly minimum). Manticore is a distributed module, so shipping Remotion would hand every creator at a 4+ person company a licensing obligation they did not opt into. HyperFrames is Apache 2.0 with no commercial-use threshold.
- React bought nothing. Remotion's remaining justification was "anything React-stateful", but in a frame-deterministic renderer state IS a function of frame index. Every job Remotion held here (the dual-render brand stinger, word-level karaoke captions, plain HTML/SVG comps) is a paused GSAP timeline in HyperFrames, and the beat table drove both engines identically. One engine means one authoring model and one thing for the agent to know.

Remotion remains the stronger pick for React shops rendering at massive scale. That is not this pipeline.

## Setup

- The engine workspace lives at the creator's `{engines-path}/hyperframes/`, initialized on first graphics run. Install the latest published version at that moment (`npm install hyperframes@latest`); never carry a version number in module docs, which only ships a stale pin to every new creator. Upstream is pre-1.0 and moves fast, so record the version the install actually resolved in the workspace's package.json and upgrade deliberately from there rather than floating mid-project.
- Pull registry blocks before authoring anything: `npx hyperframes add` (50+ blocks: caption styles, lower thirds, transitions, dataviz). Theme every block through `{brand-path}/tokens.json`.
- `@hyperframes/studio` is the timeline GUI; bidirectional sync with the HTML source is real (drag a beat in the GUI, the code updates; hand-edit the code, the GUI hot-reloads). Use it for timing nudges after mc-graphics gets close.

## Jobs

- Per-video overlay beats: the default lane, delivered as ProRes 4444 alpha MOVs.
- Brand stinger and transitions: ONE composition rendered twice, VP9 yuva420p WebM for OBS and ProRes 4444 MOV for the editor lane. Keep it 1 to 2 seconds (transparent WebM renders slowly). HyperFrames captures each frame as PNG with alpha and encodes through either alpha-capable codec, so the dual target is one comp and two renders.
- Shorts karaoke captions: word-level highlight driven by the transcript's word timestamps, built from a registry caption block themed through tokens.json.

## Rules

- Brand-themed blocks live in the engine workspace and are reused across projects; per-video comps live in each project's `graphics/` folder.
- Dual-render targets share one source of truth; never maintain two stinger comps.
- WebM VP9 alpha is for browser and OBS consumption only. Editors ignore its alpha channel and render transparent areas black, so the editor lane always takes the ProRes 4444 MOV.
- A render is not done until `{skill-root}/scripts/render_verify.py` has extracted frames and they have been visually checked.
