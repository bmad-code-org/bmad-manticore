---
format: screen-tutorial
stages: [new, braindump, outline, script, record, cut, beats, graphics, package, final, retro]
engine_overlays: hyperframes
engine_stingers: remotion
generated_broll: banned
---

# Format: screen-tutorial

Talking head plus screen recording. The teaching happens on screen; the pipeline adds structure and emphasis around real UI.

## Style philosophy

- Real UI only. Generated b-roll is BANNED in this format (UI accuracy rule); that is why the assets stage is absent from the stage list.
- Beat types extend talking-head with: zoom/pan on the screen recording, UI callouts (boxes, arrows, key-press chips), and step counters.
- Callouts use the brand accent color on a subtle border stroke (per tokens.json); never obscure the UI element being discussed, point at it.
- Screen recordings are captured at native resolution, constant frame rate, and cursor visible.

## Engine defaults

- Overlays and callouts: HyperFrames.
- Zoom/pan moves are executed in the creator's editor on the screen-recording clip (keyframed transform), planned as beats with anchor words like any other graphic.

## Templates

- None yet.

## Learnings

(mc-retro appends here; newest first, ISO dated.)
