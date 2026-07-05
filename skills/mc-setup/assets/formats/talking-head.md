---
format: talking-head
stages: [new, braindump, outline, script, record, cut, beats, graphics, assets, package, final, retro]
engine_overlays: hyperframes
engine_stingers: remotion
generated_broll: allowed
---

# Format: talking-head

The default for main channel videos. The creator on camera, graphics are overlay beats composited over A-roll in the creator's editor.

## Style philosophy

- The A-roll carries the video; graphics support the spoken word, never compete with it.
- Overlay beats ride word anchors from the edited transcript. A graphic that is not tied to a spoken phrase does not exist.
- Brand system throughout: overlays are alpha (no canvas color ever baked in); text and emphasis colors follow `{brand-path}/tokens.json` and its colorRules notes.
- Density target: a graphic beat roughly every 20 to 45 seconds; long uninterrupted stretches are fine when the material carries itself.

## Engine defaults

- Overlays: HyperFrames, registry blocks first, ProRes 4444 alpha export.
- Stinger/transition: the Remotion brand stinger from `{engines-path}/remotion/`.
- Generated b-roll: allowed for atmosphere and story beats only, never for UI or text that must be accurate.

## Templates

- None yet. First finished video donates its best compositions back here as named templates.

## Learnings

(mc-retro appends here; newest first, ISO dated.)
