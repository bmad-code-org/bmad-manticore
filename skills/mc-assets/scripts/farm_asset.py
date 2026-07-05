#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Generate stills and clips via provider APIs; log cost to a manifest (phase 5).

Usage:
    uv run {skill-root}/scripts/farm_asset.py --kind image|video --prompt "..." \
        --out-dir <resolved {projects-path}/<slug>/assets/> \
        [--provider xai-api|veo-api] [--seconds 8] [--ref path/to/reference.png]

    The calling skill resolves --out-dir from the studio config; this script does
    no config discovery of its own. Provider names match [assets] in the config.

Contract:
    providers  xai-api (xAI API, default): image $0.02, video $0.05/s, 1-15s, 720p,
               24fps, native audio; submit/poll/download REST
               veo-api (Gemini API, escalation): when realism must not wobble
    output     file lands in --out-dir, and <out-dir>/manifest.json
               gets a row: {file, prompt, provider, model, cost, date}
    env        XAI_API_KEY, GEMINI_API_KEY (from .env)
    rule       generated footage never depicts UI or text that must be accurate

STATUS: stub. Implement in build-order item 3 (needs xAI key from console.x.ai;
API billing is separate from the grok CLI subscription).
"""

import sys

sys.exit("farm_asset.py is not implemented yet (build-order item 3). See docstring for the contract.")
