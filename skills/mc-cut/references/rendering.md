# Rendering and export

The preview render, the gate-4 final render, and the editor timeline export.
Load this when rendering or exporting. Full flag lists live in each script's
docstring; this file carries the wiring and the decisions.

## Where encoder settings live

The studio config is their single home. Emit the `[render]` and `[video]` keys
first, then `[cut] preview-flags` or `[cut] final-flags` last.

Because the last occurrence wins, restating a key the config owns inside a flags
string silently overrides it, and which value applies then depends on emission
order rather than on any stated rule. So the flags strings are an escape hatch
for what the config does not model, such as `--segment-target-seconds`. Never
put `--height`, `--crf`, or the loudness flags in them.

## The default is NOT to render

Reviewing a cut does not need a rendered file. The virtual timeline plays the
EDL directly — no encode, no wait, frame-exact, video and audio together:

```
uv run {skill-root}/scripts/render_preview.py cut/edl.json -o renders/preview.mp4 --proxy-only
uv run {skill-root}/scripts/edl_to_ffconcat.py cut/edl.json -o cut/preview.ffconcat \
    --source renders/proxy/<stem>-720p-intra.mp4
ffplay -f concat -safe 0 -i cut/preview.ffconcat
```

`--proxy-only` builds the all-intra proxy and stops. Add `--mpv cut/preview.mpv.edl`
to `edl_to_ffconcat.py` for creators who have mpv, which scrubs better.

Measured on a 379-segment 16-minute cut: the timeline resolves in 33ms against
22 minutes for the equivalent render, and was verified pixel-identical (PSNR
inf) to source ground truth at four points including random seeks.

**The all-intra proxy is a correctness precondition, not an optimisation.** The
concat demuxer can only cut on keyframes. Against a long-GOP proxy (8.333s GOP
vs segments averaging 2.51s) every segment starts up to 8.3s early while the
total duration still matches the EDL exactly — a wrong cut that passes every
check except a frame comparison. `edl_to_ffconcat.py` refuses a non-intra
source unless you pass `--allow-long-gop`.

The proxy costs ~10x the disk of a long-GOP one (698MB vs 65MB for 20 minutes
at 720p) and is built once per source, then reused by every later preview and
render.

## Preview file, when you actually need a file

Only when something needs to be a file: a composited preview once overlays
exist, something to upload or share, or the gate-4 final.

```
uv run {skill-root}/scripts/render_preview.py cut/edl.json \
    -o renders/preview.mp4 --boundary-frames cut/boundaries/ \
    plus [cut] preview-flags
```

Defaults to 720p CRF 28 when `[render]` leaves them unset. Never
loudness-normalized. Check `"validated": true` in the summary.

This path re-encodes. A concat stream copy off the intra proxy is ~470x faster
(2.8s vs 22min measured) and is genuinely the same frames, but it emits
duplicate DTS wherever a segment runs only a frame or two, and the file then
fails this script's own decode validation — 125 errors on that cut, and 3 with
every timestamp remedy applied, never zero, from just 2 sub-0.1s segments.
`composite_core.build_streamcopy_command` keeps it, tested, for a caller that
wants a scratch file and accepts the caveat. It is not the review path because
the virtual timeline is faster AND correct.

Composited, once the graphics stage has rendered overlays into `graphics/`, add:

```
--beats beats/beats.md --graphics-dir graphics/
```

Report any `overlays_missing` from the summary; each one is a beat whose overlay
has not landed in `graphics/`.

Beats listed in `titles_in_resolve_only` are Fusion titles from `graphics/titles.json`. They are not missing, but this preview does not show them; tell the creator to review them in Resolve.

## Final render, offered at gate 4

When `[editor] name` is `resolve`, the final render runs in Resolve; see the next section. Every other editor uses `render_final.py`:

```
uv run {skill-root}/scripts/render_final.py cut/edl.json \
    -o renders/final.mp4 --beats beats/beats.md --graphics-dir graphics/ \
    plus [cut] final-flags
```

Wire the config in: `--codec` and `--crf` from `[render]`, `--height` from the
height of `[video]` delivery-resolution, `--loudness-target` from `[render]
loudness-target`, and append `--no-loudnorm` when `[render] loudnorm` is false.

It bakes the same EDL the creator approved, with graphics composited from the
approved beat table. Finishing in the creator's own editor from the always
exported timeline is an equally supported path; either closes gate 4.

What the script handles without instruction: hardware encode selection with a
one-frame test encode per candidate and an libx264 fallback, a disk preflight,
progress reporting, boundary-frame checks, and two-pass loudnorm when enabled.

Persistent incremental segments are worth knowing about, because they change
what a re-render costs. The timeline is partitioned into content-addressed
segments under `renders/segments/`, and a re-render re-encodes only the segments
whose inputs actually changed. A single tweaked graphic on a long video is a
seconds-long re-render, so re-rendering after a small fix is cheap and there is
no reason to batch changes to avoid it. `--segment-target-seconds` (default 600)
tunes segment size.

## Final render in Resolve

On the Resolve lane the creator finishes in Resolve, so the final is rendered from their timeline there, not from `cut/edl.json`:

```
uv run {skill-root}/scripts/resolve_render.py --project <slug> -o renders/final.mp4 \
    --render-preset "<[editor] resolve-render-preset>" \
    --loudness "<[editor] resolve-loudness>" \
    --fairlight-preset "<[editor] resolve-fairlight-preset>" \
    --report renders/final-render.json
```

It renders the project's current timeline; add `--timeline "<name>"` when the creator names another, and say which timeline was rendered. Pass every config value, empty ones included: an empty preset renders with Resolve's "H.264 Master" and an empty loudness mode skips normalization, and the script states each fallback.

Before running, tell the creator that normalization sets clip gain on every audio item in their timeline. The gains before and after land in the report, and this puts the before values back:

```
uv run {skill-root}/scripts/resolve_render.py --restore-gains renders/final-render.json
```

Resolve's normalization stops raising gain at the mode's peak ceiling, so a dynamic take can land well under target. The script measures the render and fails when a mode with a known target (YouTube -14, EBU R128 -23, ATSC A/85 -24 LUFS) misses by more than 1 LU. The fix is a limiter in the creator's Fairlight preset. When the creator accepts the level as it is, re-run with `--accept-loudness "<their reason>"`; the reason lands in the report.

Exit 1 publishes nothing and leaves any earlier `renders/final.*` in place; report the reason it printed. Exit 4 means Resolve Studio is not reachable: offer `render_final.py` from the EDL instead and say it will not include the creator's Resolve edits. The preset decides the container, so "H.264 Master" and ProRes presets publish `renders/final.mov`; name the file the summary reports. If the script is interrupted, Resolve keeps rendering; stop the job in Resolve's Deliver page.

The fast preview lane is unchanged: previews render with ffmpeg whatever the editor.

## Editor timeline export

Always exported, per `[editor] timeline-format`.

| Value | Command | Notes |
|---|---|---|
| `fcpxml` | `uv run {skill-root}/scripts/edl_to_fcpxml.py cut/edl.json -o cut/rough.fcpxml` | Resolve and Final Cut import it natively. Refuses VFR sources loudly |
| `xmeml`, `edl` | Not yet implemented | Premiere users work from cutplan.md, edl.json and the rendered preview. See TODO.md |
| `none` | Skipped | Descript and manual workflows. The deliverables are cutplan.md, edl.json and renders/preview.mp4 as the cut map |

When `[editor] name` is `resolve`, `{skill-root}/scripts/resolve_import.py` imports `cut/rough.fcpxml` into the creator's running Resolve Studio as a new timeline, adds review markers, and checks every clip against the fcpxml frame by frame. SKILL.md carries the command and exit handling. It drives Resolve's scripting API directly and needs no MCP server.

## Derived artifacts drift silently

The EDL is the single source of truth. The preview, the boundary frames, the
timeline export and cutplan.md are all derived from it, and nothing stops an
older one sitting next to a newer EDL looking current.

The preview writes an `<output>.key` sidecar naming the render identity it was
built from. When that key does not match the current EDL's, the derived set is
stale and all of it regenerates together.
