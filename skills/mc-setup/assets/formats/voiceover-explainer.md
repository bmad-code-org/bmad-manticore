---
format: voiceover-explainer
stages: [new, braindump, outline, script, record, cut, beats, graphics, assets, package, final, retro]
engine_overlays: hyperframes
engine_stingers: remotion
generated_broll: allowed
---

# Format: voiceover-explainer

No camera. Recorded VO drives a fully composed visual track: slides, diagrams, farmed clips, motion graphics. Architecture cribbed from digitalsamba's claude-code-video-toolkit (the one system verified in production).

## Style philosophy

- The visual track is wall-to-wall: unlike talking-head, silence on screen is a bug. Every beat covers its span.
- Diagrams over decoration. This format exists to explain; SVG diagrams in the brand system are the primary visual, farmed clips are seasoning.
- Record VO in one sitting where possible; the cut stage tightens gaps and retakes exactly like A-roll.

## Engine defaults

- Diagrams and slides: HyperFrames or plain HTML/SVG comps in Remotion (the creator's call per video; both read `{brand-path}/tokens.json`).
- Farmed stills/clips: grok default, Veo escalation, per `PIPELINE.md` engine policy.

## Templates

- None yet.

## Learnings

(mc-retro appends here; newest first, ISO dated.)
