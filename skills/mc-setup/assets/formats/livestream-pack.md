---
format: livestream-pack
stages: [new, stream-pack, final, retro]
engine_overlays: ograf
engine_stingers: remotion
generated_broll: banned
---

# Format: livestream-pack

Not a video. One run of mc-stream-pack producing a complete OBS asset pack from brand tokens. The final gate is the creator loading the pack into OBS and approving the look live.

## Pack contents

- Static scenes as self-contained local HTML (starting-soon with countdown, BRB, ending, full overlay). OBS browser sources render local HTML transparent by default; no server.
- Scenes are reactive via the `window.obsstudio` JS API (countdown resets on scene activation, lower thirds re-trigger entrance on visibility) with a plain-browser fallback.
- Stinger transition: one Remotion comp rendered twice (VP9 yuva420p WebM for OBS, ProRes 4444 MOV for Resolve), 1 to 2 seconds.
- Lower thirds and topic cards as OGraf (via the mc-ograf skill), standalone-capable and SPX-GC-compatible for click-to-trigger later.

## Verification, not vibes

- Playwright screenshots of every scene at 1920x1080 over a checkerboard to prove alpha and layout.
- ffprobe on the stinger for the right pixel formats.

## Out of scope in v1

- NodeCG (over-engineered for an asset pack).
- Alert-event plumbing (StreamElements' free custom-widget slot accepts our HTML if alerts are ever wanted).

## Learnings

(mc-retro appends here; newest first, ISO dated.)
