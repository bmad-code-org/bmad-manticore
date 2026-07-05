#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Verify a rendered graphic before it is called done (phase 4).

Usage:
    uv run {skill-root}/scripts/render_verify.py <rendered-file>
        [--pixfmt prores4444|yuva420p] [--expect-dur SECONDS]
        [--expect-fps FPS] [--expect-res WxH] [--frames 5] [--checker]

Contract:
    input   a rendered MOV/WebM/mp4; every expectation arrives as an explicit
            flag from the calling skill (--pixfmt from the delivery target,
            --expect-dur from the beat's dur, --expect-fps and --expect-res
            from the format profile); the script does no config discovery
    checks  ffprobe: pixel format vs --pixfmt (ProRes 4444 alpha or VP9
            yuva420p), resolution vs --expect-res, fps vs --expect-fps,
            duration vs --expect-dur (checks skipped for flags not passed)
            ffmpeg: extract N frames evenly spaced (over a checkerboard for
            alpha files) into a _verify/ folder next to the input for visual
            inspection by the calling skill
    output  structured JSON to stdout: per-check pass/fail plus the extracted
            frame paths
    rule    a render is NOT done until frames have been extracted and visually
            checked (the student-kit self-QA loop: edit, lint, preview, draft
            render CRF 28, single-frame verify, final render)

STATUS: stub. Implement in build-order item 2 with the graphics lane.
"""

import sys

sys.exit("render_verify.py is not implemented yet (build-order item 2). See docstring for the contract.")
