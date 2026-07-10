# mc-prompter: teleprompter and AI producer, implementation plan

Working document for the feat-prompter branch. Not intended to merge; it guides the build and gets deleted (or distilled into skill references) before the PR. Written 2026-07-09 from a recon pass over the module and external research on real-time local ASR, teleprompter prior art, and live cueing UX. Revised the same day after an adversarial three-lens review (technical feasibility, module conventions, product); the review's fixes are folded in throughout and marked where they changed a decision.

## Decisions record (2026-07-09, BMad)

- LLM runtime for producer mode: Ollama is the default provider of a new `[llm]` config lane, with the standard provider-ladder pattern (local-first, other rungs opt-in later).
- Scope: all three tiers built on this one branch, in phases, with the classic teleprompter working end to end first.
- Skill shape: one service skill, `mc-prompter`, following the mc-audio pattern (no stage, no gate, no project.json state). The `record` stage stays creator-owned.
- Cue channels: visual-only in v1. The kokoro spoken tier is designed here but ships as a fast-follow behind a config flag.
- Landing (approved 2026-07-09): stacked PRs. PR 1 = Phase A plus minimal docs and the help row, PR 2 = Phase B, PR 3 = Phases C+D. All developed on this branch.

## Product overview: three tiers

Tier 1 is a classic teleprompter with the full standard feature set, no AI, no model downloads. Tier 2 is voice-follow: the scroll tracks the speaker through a known script using streaming local ASR plus a deterministic alignment algorithm, no LLM. Tier 3 is producer mode: a rundown file (duration, ordered talking points, intro, wrap), a rolling transcript, and a small local LLM that keeps the speaker on track with rate-limited visual cues.

Each tier is progressive enhancement over the one below it. Tier 1 works on any machine with only the module installed. Tier 2 requires the prompter-lab workspace (ASR models, consent-gated download). Tier 3 additionally requires a running Ollama.

Everything chosen is cross-platform (Windows, macOS, Linux): browser UI, browser mic capture, sherpa-onnx, silero VAD, kokoro-onnx, Ollama. This is the module's first fully cross-platform lane, and the sherpa-onnx dependency incidentally opens a path to the cross-platform transcription lane already on TODO.md.

## Architecture

### Skill shape

New folder `skills/mc-prompter/`:

```
skills/mc-prompter/
  SKILL.md              service skill: what it does, how to launch, tier gating
  customize.toml        [workflow] block + prompter defaults
  references/
    cueing.md           the cue design contract: escalation ladder, budget, vocabulary,
                        replan rules, coverage semantics, headphones/AEC dependency
    rundown-spec.md     the rundown.md file format specification (time math included)
  scripts/
    ensure_workspace.py prompter-lab builder (mirrors mc-audio's, consent-gated)
    run_prompter.py     stdlib launcher: validates workspace, port probe, launches server
    server/             the application package (both launch paths run python -m server.main)
      __init__.py
      main.py           aiohttp app: HTTP + WebSocket + static UI; lazy tier-2/3 imports
      asr.py            sherpa-onnx streaming recognizer + silero VAD (workspace only)
      align.py          script-follow alignment engine (pure stdlib)
      producer.py       rundown state machine, replanner, cue engine, Ollama client
      rundown.py        rundown.md parser (pure stdlib)
      script_ingest.py  script.md / markdown / plain text ingestion (pure stdlib)
      static/           vanilla HTML/JS/CSS, no build step
    tests/              self-running test-*.py files (convention below) + fixtures/
```

Conventions honored: SKILL.md frontmatter with exactly `name` and `description`; scripts invoked only via `uv run` with PEP 723 headers; scripts take explicit resolved arguments and do no config discovery; the skill reads only its own folder, `_bmad/scripts/`, and project files; nothing user-specific ships in the module; config keys kebab-case.

### The prompter-lab workspace

Heavy dependencies live in a persistent venv at `{engines-path}/prompter-lab`, exactly like audio-lab:

```
<workspace>/.venv/     aiohttp (same pin as the PEP 723 header), numpy,
                       sherpa-onnx (pinned), soundfile
<workspace>/models/    ASR models (below); later the kokoro pair
<workspace>/out/       session artifacts (take logs, session transcripts, script backups)
```

`ensure_workspace.py --check` exits 0/4 like mc-audio's; the build asks consent before any download. Tier 1 does not require the workspace at all: `run_prompter.py` launches the server with ASR disabled when the workspace is absent, using `uv run` with aiohttp as a PEP 723 dependency. When the workspace exists, the launcher runs the venv interpreter instead, resolved portably (`.venv/bin/python` on POSIX, `.venv\Scripts\python.exe` on Windows).

Two-launch-path discipline (review finding):

- Both paths execute the server identically as `python -m server.main` from the scripts directory, so intra-package imports resolve the same way in both. A test executes main.py under a bare env with only aiohttp and asserts the tier-1 routes come up.
- main.py imports asr.py (and anything touching numpy/soundfile) lazily, only inside the workspace-present branch. align.py, rundown.py, and script_ingest.py stay pure stdlib.
- The aiohttp version is pinned identically in the PEP 723 header and the workspace venv, bumped together.

Port handling (review finding): default port 8770 (mc-ograf's ephemeral verifier uses 8771). On launch, probe the port; if occupied, query `/health` (which reports session and script identity), then offer kill-and-replace or auto-increment to the next free port. The chosen port is printed and written to a session file the skill reads back. `--port` overrides. The server binds 127.0.0.1 by default; `--lan` opts into 0.0.0.0 for the phone remote and tablet displays, and the docs note the Windows Firewall consent dialog this triggers. The home page shows the LAN URL only when it is actually reachable.

ASR models downloaded into `models/`:

- Primary: sherpa-onnx export of nvidia nemotron-speech-streaming-en-0.6b, int8 (the streaming sibling of the parakeet family; NVIDIA Open Model License, commercial use permitted). Chunk setting 560 ms as the default latency point.
- Fallback for low-end hardware: a small streaming zipformer English export (Apache-2.0), selectable via config.
- VAD: silero VAD via sherpa-onnx's built-in VoiceActivityDetector (one dependency covers both).

### Server and UI

One aiohttp process serving four pages plus a WebSocket:

- `/` home: pick a source (project script.md, rundown.md, pasted/loaded text), configure, preflight, launch. Shows the reconciled rundown plan before a show starts.
- `/prompt` the prompter display: fullscreen scroll surface, all tier-1 features, the producer rail when tier 3 is active.
- `/remote` phone-as-remote over LAN: play/pause, speed, jump to marker, next/prev section, and in producer mode the point-list override controls. Control only, no mic. The remote URL embeds a per-session token and is presented as a QR code on the home page, so a random LAN device cannot drive the prompter mid-show.
- `/overlay` OBS browser source: transparent background, renders only the ambient rail and cue cards for live shows.

Concurrency model (review finding, binding): sherpa-onnx decode is CPU-bound and blocking, so it never runs on the event loop. A dedicated ASR thread consumes a bounded queue of PCM frames and publishes recognition events back via `call_soon_threadsafe`. Ollama ticks run as background tasks with hard timeouts and never hold shared state across an await. WebSocket fan-out never awaits decode or LLM work. The server smoke test asserts `/remote` command round-trip latency stays low while the replay harness saturates the ASR path.

Audio capture and ownership (review findings, binding):

- Mic capture happens on the server machine. getUserMedia requires a secure context, which http over LAN is not, so a tablet pointed at `/prompt` is display-only. The WS protocol separates the display role from the audio-producer role: the server grants a capture token to exactly one localhost connection; frames without the current token are rejected and the UI names the mic owner. Mic-on-remote-device would require shipping TLS and is explicitly deferred.
- getUserMedia constraints request `echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1`; browser speech processing measurably degrades ASR input. The preflight screen reads back `track.getSettings()` and surfaces what was actually applied, since browsers may ignore constraints. When the kokoro spoken tier ships, headphones-only output is what keeps AEC unnecessary; cueing.md records that dependency.
- The AudioContext is created with `{ sampleRate: 16000 }` so the browser resamples; if the browser refuses the rate, a small resampler in the worklet handles the conversion (naive decimation from 44.1 kHz aliases into the speech band). The preflight screen verifies `context.sampleRate`. The worklet ships ~120 ms PCM16 mono frames over the WebSocket.
- Backpressure has a policy at both ends: the browser checks `ws.bufferedAmount` and drops frames past a threshold; the server frame queue is bounded and drops oldest on overflow while raising an "ASR behind real-time" state on the rail. The replay harness asserts queue-depth behavior.

State flows back to all connected pages over the same WebSocket (scroll position, VAD state, transcript tail, cue events), so the remote and overlay stay in sync with the prompter display.

UI is vanilla HTML/JS/CSS with no build step. Display settings (mirror flips, font, colors, margins, eyeline position) persist in localStorage per device, with the `[prompter]` config values as defaults; a beam-splitter rig and the operator's browser keep independent settings without reconfiguration each launch.

### Tier 1: the standard feature checklist

Scroll and timing:

- Smooth continuous scroll, speed as WPM with live +/- adjustment (keyboard, wheel, remote)
- Timed mode: give total duration, speed is continuously re-derived (remaining words over remaining time, recomputed on resume and after any jump), with the timer display showing drift from plan
- Pause/resume (spacebar), jump forward/back, jump to marker, restart
- Countdown before scroll starts
- Elapsed and remaining time, estimated read time from word count at the creator's measured wpm (`[owner] wpm` from the studio config when available)

Display:

- Mirror flip horizontal, vertical, and both (beam-splitter rigs)
- Font family/size, text and background colors, margins, line height
- Adjustable eyeline/cue marker (position, style)
- Fullscreen; works on a second monitor or a tablet pointed at the same URL (display-only on remote devices, see audio ownership above)
- Per-device settings persistence (localStorage), config defaults underneath

Script handling:

- Markdown and plain text; project `script.md` ingestion (below)
- Inline bracket notes render dimmed and are never matched by voice-follow
- Named markers/sections for jumping
- Edit-in-place from the home page between takes; edits write back to the source file with a timestamped backup copied to the workspace `out/` first, so the prompted text and the pipeline artifact never silently diverge

Remote:

- Keyboard shortcuts throughout; bluetooth presenters and USB foot pedals work as keyboard emulators for free
- `/remote` phone page over LAN, session-token URL via QR code

### Script ingestion (pipeline tie-in)

`script.md` from a project is directly consumable: plain spoken prose. Ingestion handles the two inline marker types:

- `[INVENTED]` flags render as a subtle badge, toggleable off
- `[TAKE <source-id> <start>s-<end>s]` lines render dimmed with a "have it already" badge, since those lines were already spoken well in the interview footage and may not need re-recording; a toggle hides them entirely

The prompter takes a path argument; the skill resolves it from the project when launched inside the pipeline flow ("record with the teleprompter") or accepts any file standalone.

### Tier 2: voice-follow alignment engine

Deterministic, no LLM. The prior art (bounded-window Levenshtein prefix matching, the PromptSmart hold-and-re-anchor behavior) consumed Web-Speech-style utterance partials; a streaming transducer behaves differently, so the contract is adapted for sherpa-onnx output (review finding, binding):

- Input contract: the engine consumes token deltas since the last partial, not whole hypotheses. The last K tokens (K around 3 to 5) of the hypothesis are held provisional because beam search can revise the tail between partials; the anchor commit lags the hypothesis head by K tokens and absorbs revisions. On endpoint detection (`is_final`), the anchor hard-commits and tail-tracking state resets for the next segment. BPE pieces merge to words during normalization before matching.
- Normalize both script tokens and ASR tokens: lowercase, strip punctuation, expand common number/abbreviation forms at index-build time (a small normalization table; "2026" also indexes as "twenty twenty six")
- Maintain a monotonic anchor (last committed script token). On each delta batch, take a lookahead window from the anchor (window size proportional to utterance length plus a constant, on the order of 2x + 10 tokens) and find the window prefix minimizing Levenshtein distance to the pending recognized words; the best prefix end becomes the provisional anchor
- Silence (VAD) produces no partials, so the scroll holds; ad-libs fail to match and the anchor holds until speech re-matches within the window
- Escape hatches: click/tap any word to re-anchor, arrow keys nudge the anchor, and a paragraph-skip gesture jumps the window when the creator deliberately skips content
- The scroll controller eases toward the anchor position rather than jumping; the anchor leads the eyeline by the measured end-to-end latency times the current speaking rate, so the eyeline sits where the speaker actually is, not where ASR last confirmed
- Match state is visible: matched text subtly tinted behind the eyeline, so trust in the tracker is inspectable

Latency: the honest budget is end-to-end and includes terms the naive sum misses: capture framing (~120 ms) + ASR chunk emission (560 ms configured) + decode compute (hardware-dependent, grows under OBS load) + alignment (<10 ms) + scroll easing (a deliberate time constant). Realistic eyeline-follows-voice latency is 1 to 2 s depending on hardware. The replay harness measures capture-timestamp-to-anchor-update wall time on each target platform, and the measured number feeds the eyeline lead default. No fixed latency claim ships in docs.

Preflight (review finding, this is the try-once-never-again defense): before any take, the preflight screen enumerates input devices with a picker persisted per machine, shows a live level meter, reports the applied audio constraints and sample rate, and runs a 10-second "read this sentence" tracking test that demonstrates the match-tint following before a real take starts. If recognition confidence is garbage, it fails loudly and names the device in use.

The engine is a pure-stdlib module. Its fixture suite is built from recorded real partial sequences (the actual partial/final event stream captured from the model over the replay WAV), not hand-written final transcripts, covering: verbatim read, ad-lib excursion and return, skipped paragraph, number/abbreviation mismatch, repeated-phrase script traps, and tail-revision events.

### Tier 3: producer mode

Inputs: a rundown file, the rolling transcript from the same ASR stream, and the show clock.

Show clock semantics (review finding, binding): the plan's arithmetic never keys off server or page start. An explicit GO LIVE control (on `/prompt` and `/remote`) starts the show clock after any pre-roll, and a plan-hold control freezes elapsed time and the state machine during BRB or technical trouble while VAD and the transcript keep running so context is not lost. Both are fixture-tested scenarios.

The producer is two cooperating parts:

- A deterministic state machine (code, not LLM). It tracks elapsed time against per-segment budgets, and it re-plans rather than merely flagging lateness: on every tick, remaining show time is redistributed across uncovered segments proportionally to their original budgets, with wrap-minutes protected as a hard reserve. Green/yellow/red state is always computed against the current re-plan, never the original rundown. When redistribution would push any segment below a feasibility floor, the state machine emits a card-tier CUT suggestion ("DROP: point 4, or 90s each"). This replan behavior is the core producer value (a countdown that only turns red is a nag, not a producer) and is specified in cueing.md as part of the binding contract.
- An LLM tick (Ollama): scheduled adaptively, and at VAD pause events, it receives a compact state block (rundown with per-point coverage, the current re-plan, elapsed vs plan, the last ~60 seconds of transcript) and returns structured JSON: proposed coverage transitions, current-topic guess, an optional suggested cue with tier and text, and a one-line reason. Cheap keyword/fuzzy matching runs continuously between ticks as a first-pass coverage signal the LLM confirms or overrides.

Coverage semantics (review finding, binding): coverage is sticky and monotonic in the state machine; the LLM may only propose uncovered-to-covered transitions, never reversions, so the rail cannot flicker. "Next" is defined as the first uncovered point in rundown order, which stays well-defined when the creator covers points out of order. The human is the final authority: `/remote` gains producer controls, a tappable point list with mark-covered, skip, and make-current, so one tap mid-show rescues any model misjudgment.

LLM tick budget (review finding, binding): the tick must never starve the ASR thread. Concretely:

- Requests use Ollama structured outputs (`format` with a JSON schema), `think: false`, temperature 0, a hard `num_predict` cap, and `keep_alive` so the model stays resident
- The prompt keeps a stable prefix (system + rundown first, rolling transcript last) so Ollama prefix caching skips reprocessing
- Cadence is adaptive: the next tick is scheduled at `max(15 s, 3x last tick wall time)`, and ticks are skipped entirely while the ASR queue depth signals CPU pressure
- Default model is `qwen3:4b` where Ollama reports GPU/Metal offload; on CPU-only machines the producer startup check recommends and falls back to a sub-2B tag (`qwen3:1.7b`). Every request carries a hard timeout; a timed-out tick is dropped, not queued

The cue engine (code) is the final authority on delivery: it applies the density setting, the one-active-cue rule, per-interval budget, and tier gating. The LLM proposes; the state machine disposes. A wrong suggestion costs nothing because rate limiting, tiering, and coverage stickiness are deterministic.

Visual cue surface (v1, from the cueing research):

- Ambient tier, always on: a rail showing current point, next point, and a green/yellow/red segment-time state computed against the re-plan (Toastmasters vocabulary), plus overall show progress. No motion, no reading required
- Card tier: a single quiet card ("NEXT: pricing demo", "STRETCH: 4 min left, 1 point to go", "DROP: point 4, or 90s each"), released at pauses, auto-expiring, never stacked
- Attention tier: the card flashes/enlarges for time-critical states ("WRAP", "2:00 OVER"), the one tier allowed to appear mid-sentence
- Vocabulary is the broadcast lexicon (standby, wrap, stretch, hard wrap, time remaining); `references/cueing.md` is the binding contract

Free-talk support: a rundown with no script body per point is exactly the "5 ideas in this order plus intro and wrap" show. The intro and wrap can carry full scripted text (prompted via tier 1/2) while the middle segments run producer-only. The segment handoff is explicit UI behavior, not hand-waved: when a scripted segment ends (anchor reaches section end, or manual next-section), `/prompt` switches to a large-type rail view showing the current bullet set; entering the next scripted segment switches back to the scroll surface.

Spoken tier (designed now, shipped later behind `spoken-cues = false`): short formulaic kokoro phrases only ("thirty seconds", "wrap"), synthesized by a persistent kokoro instance (~150 to 300 ms for a short cue on CPU), released only at pauses, hard requirement that output routes to headphones (this is also what keeps browser AEC unnecessary). Never speech-over-speech except a true emergency tier. The mix-minus principle from IFB practice: the speaker must never hear their own voice back.

### The rundown artifact

New file format, specified in `references/rundown-spec.md`. The starter template ships through mc-setup's `assets/` into the studio like tokens and format profiles, so Manny and any skill can read it as a project file without crossing skill-folder boundaries; mc-prompter owns the spec and does any template-based drafting, and Manny routes to it (review finding).

```markdown
---
show: "Why local models win"
duration-minutes: 30
cue-density: normal        # hands-off | minimal | normal | chatty
wrap-minutes: 3
---

## Intro (3 min)

Full scripted intro text here, prompted normally.

## Point 1: The cost argument (5 min)

- cloud bills compound, local is capex
- the 4090 anecdote

## Point 2: Latency (5 min)
...

## Wrap (3 min)

Scripted wrap text.
```

Time math (review finding, in the spec): per-segment minutes are optional; unbudgeted segments split the remaining time evenly. If explicit minutes exceed duration-minutes, duration-minutes wins and the parser warns at load; the home page shows the reconciled plan before the show starts. The parser accepts exactly `(N min)` and `(Nm)` heading suffixes and rejects anything else with a line-numbered error, because hand-written and Manny-drafted rundowns will produce creative variants on day one. Frontmatter `cue-density` overrides the config value (most specific wins).

Segments with prose bodies prompt as script; segments with only bullets run producer-only. For pipeline projects the file lives at `{projects-path}/<slug>/rundown.md`; standalone shows pass any path. This artifact also fills the episode-plan gap the livestream formats already reference (the 1.0.x per-episode stream-pack fast-follow needs the same file), and the planned mc-research skill becomes its natural upstream.

### Config: new studio sub-tables

Two new tables in `[modules.manticore]`, seeded from mc-setup's `[defaults]`:

```toml
[defaults.prompter]
workspace = "prompter-lab"       # resolved {engines-path}/{prompter.workspace}
asr-provider = "nemotron-streaming"   # nemotron-streaming (default) | zipformer-small | none
cue-density = "normal"           # hands-off | minimal | normal | chatty
spoken-cues = false              # kokoro tier, fast-follow
port = 8770

[defaults.llm]
provider = "ollama"              # the only implemented rung; others planned, opt-in
model = "qwen3:4b"               # any ollama tag; producer falls back to a sub-2B tag on CPU-only
endpoint = "http://localhost:11434"
api-key-env = ""                 # stays empty for local lanes, pattern-consistent
```

mc-setup changes:

- Add both tables to `[defaults]`, a short optional interview step (offer the teleprompter, ask about producer mode and Ollama only if wanted), and both table names to the step 8 write list
- Migration (review finding, important): do NOT add these tables to the step 1a 0.x classifier list; that list defines what makes a studio 0.x, and adding 1.1 tables to it would mislabel every current 1.0.x studio as 0.x and run the full migration flow on it. Instead add a separate backfill rule alongside it: a config that has the 1.0 tables but is missing `[prompter]` or `[llm]` is a current studio predating the teleprompter; backfill both tables surgically from `[defaults]` and offer the optional prompter interview
- check_deps.py gains an optional `ollama` row (producer mode only) with install pointers
- PIPELINE.md's conventions line mentions the new sub-tables

The `[llm]` lane follows the enforcement pattern: the producer accepts only `provider = "ollama"` and exits 3 for anything else, so no planned lane ever pretends to work.

## Module integration checklist

- `skills/module-help.csv`: one new row with the full 13-column schema (module, skill, display-name, menu-code, description, action, args, phase, preceded-by, followed-by, required, output-location, outputs); phase `anytime`, preceded-by and followed-by left empty per the mc-audio/mc-ograf service-skill precedent
- `.claude-plugin/marketplace.json`: add `./skills/mc-prompter` to the skills array (version bump at release, not in this PR)
- `skills/mc-pipeline/PIPELINE.md`: record stage row gains a sentence noting mc-prompter as the optional tool for the creator-owned record stage; no stage table change, no gate change
- mc-script SKILL.md step 6: the "now record" handoff names the teleprompter option (naming only, no cross-skill read)
- mc-agent (Manny) capabilities: route "teleprompter", "prompt me", "run my show", "producer mode" to mc-prompter; for rundown drafting Manny routes to mc-prompter or works from the studio-installed template, never reads mc-prompter's folder
- docs/user-guide.md: new section; README skill table row; TODO.md: add the spoken-cue fast-follow and the sherpa-onnx transcription-lane opportunity

## Phasing

- Phase A, classic teleprompter: workspace-less launch path, aiohttp server with the concurrency skeleton, `/prompt` with the full tier-1 checklist, `/remote` with session token, script.md ingestion with marker handling, home page, settings persistence, port handling. Verifiable end to end with zero models
- Phase B, voice-follow: ensure_workspace.py, browser audio path with the constraint/ownership/backpressure rules, sherpa-onnx + VAD integration on the ASR thread, align.py with the transducer contract and its recorded-partials fixture suite, preflight screen with device picker and tracking test, tracking UX (hold, re-anchor, click-to-anchor, match tinting, eyeline lead)
- Phase C, producer: rundown parser + spec, producer state machine with replanner and cue engine plus time-warped fixtures (running long, running short, out-of-order coverage, GO LIVE / hold), Ollama tick with the budget rules, ambient rail + cards on `/prompt` and `/overlay`, `/remote` producer controls, `[prompter]`/`[llm]` config plumbing, mc-setup + check_deps integration
- Phase D, finish: docs, help catalog row, marketplace entry, Manny routing, take-log hook, cross-platform smoke instructions, changelog

Landing (approved 2026-07-09): develop everything on this branch, land as stacked PRs: PR 1 = Phase A plus minimal docs and the help row (a complete, useful teleprompter on its own), PR 2 = Phase B, PR 3 = Phases C+D. Each is independently green and valuable, and review feedback on the foundations arrives before the producer is built on top of them.

## Testing and verification

Test convention (review finding, matches the quality gate CI): the CI discovers `skills/*/scripts/tests/test-*.py` (hyphenated) and runs each file directly via `uv run`; there is no pytest. Every test file carries its own PEP 723 header, uses stdlib unittest with `unittest.main()`, and runs with no models, no network, no downloads.

- `test-align.py`: the transducer-contract fixtures (recorded partial sequences committed as small JSON fixtures; the five failure-mode cases plus tail-revision events)
- `test-producer.py`: replanner and cue engine under time-warped scenarios (running long with replan and CUT suggestion, running short with STRETCH, point skipped, out-of-order coverage, GO LIVE and hold semantics, budget/ladder enforcement, coverage stickiness)
- `test-rundown.py`: parser, time-math reconciliation, heading-suffix rejection with line numbers
- `test-script_ingest.py`: marker handling, bracket-note exclusion
- `test-server.py`: aiohttp smoke via its own PEP 723 aiohttp dependency; routes up under the bare tier-1 env; WS state fan-out; remote-latency-under-ASR-load assertion lives here but auto-skips (exit 0 with a message) when the workspace is absent, so CI never needs models
- One small recorded fixture WAV (a few seconds, 16 kHz mono) committed under `scripts/tests/fixtures/`; nothing generates audio in-test
- The ASR replay harness (feed the fixture WAV through the same code path the WebSocket uses, measure capture-to-anchor wall time) is a documented manual check per platform, not a CI assertion; CI has no workspace and wall-clock assertions are flaky by construction
- Manual test matrix documented in the skill: macOS (reference), Windows, Linux; beam-splitter mirror check; phone remote; OBS overlay; device-picker preflight

## Risks and mitigations

- Nemotron streaming model quality/latency on low-end CPUs: zipformer-small fallback behind `asr-provider`, and tier 1 works with `none`
- ASR token timestamps are start-only in sherpa-onnx: alignment keys on token text order, not timestamps, so this costs nothing
- CPU contention between ASR, the LLM tick, and OBS on one machine: the tick budget rules above (adaptive cadence, pressure-skip, small-model fallback, timeouts) plus the bounded-queue drop policy keep voice-follow latency from compounding; the rail surfaces "ASR behind real-time" instead of silently lagging
- Ollama absent or model not pulled: producer mode degrades to the deterministic rail (timing and replan cues still work, coverage judgments off); the UI says exactly what is missing
- Repeated phrases in scripts confusing alignment: bounded window plus monotonic anchor limits damage; fixture-tested
- Browser mic pitfalls (wrong device, processing constraints ignored, wrong sample rate): the preflight screen with device picker, level meter, applied-settings readback, and the 10-second tracking test catches all of these before the first real take
- Scope creep: the kokoro spoken tier and the mc-cut take-log consumption are explicitly out of v1

## Explicitly out of scope (future hooks)

- Spoken kokoro cue tier (designed, config key ships false; fast-follow)
- mc-cut consuming the take log (`out/take-log.json`, script positions and timestamps per take) to pre-anchor cut plans
- Chat/vision inputs to the producer (reading live chat is a natural producer input later)
- Cloud LLM rungs for `[llm]`, paid ASR rungs
- TLS for mic capture on remote devices (tablet-as-mic)
- Cross-platform batch transcription lane via sherpa-onnx parakeet-tdt offline export (separate TODO item this branch makes cheaper)
