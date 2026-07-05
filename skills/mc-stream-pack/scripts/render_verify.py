#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Verify a rendered graphic before it is called done (phase 4).

Usage:
    uv run {skill-root}/scripts/render_verify.py <rendered-file> [--frames 5] [--checker]

Contract:
    input   a rendered MOV/WebM/mp4
    checks  ffprobe: expected pixel format (ProRes 4444 alpha or VP9 yuva420p),
            resolution, fps, duration vs the beat's dur
            ffmpeg: extract N frames evenly spaced (over a checkerboard for
            alpha files) into a _verify/ folder next to the input for visual
            inspection by the calling skill
    rule    a render is NOT done until frames have been extracted and visually
            checked (the student-kit self-QA loop: edit, lint, preview, draft
            render CRF 28, single-frame verify, final render)

STATUS: stub. Implement in build-order item 2 with the graphics lane.
"""

import sys

sys.exit("render_verify.py is not implemented yet (build-order item 2). See docstring for the contract.")
