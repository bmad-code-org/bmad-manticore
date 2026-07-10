# The cueing contract

This is the binding contract for how mc-prompter's producer mode cues a live speaker. The deterministic state machine in `scripts/server/producer.py` and the cue engine wired in `scripts/server/main.py` implement it; the UI renders it. If code and this document disagree, that is a bug. The design principle: the LLM proposes, the state machine disposes. Every rule that protects the speaker (rate limits, tiering, coverage stickiness, the replan) is deterministic code, so a wrong model suggestion costs nothing.

## The escalation ladder

Four tiers, from least to most intrusive. A cue always enters at the lowest tier that can do the job.

| Tier | Surface | Interrupts speech | Shipped |
| --- | --- | --- | --- |
| ambient | the rail: show clock, green/yellow/red state, current segment and its replanned time left, next point, progress dots | never (no motion, glanceable) | yes |
| card | a single quiet card near the eyeline ("NEXT: pricing demo", "STRETCH", "DROP: Point 4, or 90s each") | no: released at a VAD pause | yes |
| attention | the card flashes and enlarges for time-critical states ("WRAP", "2:00 OVER") | yes: the one visual tier allowed mid-sentence | yes |
| spoken | short formulaic synthesized phrases ("thirty seconds", "wrap") | pause-released only, emergency excepted | no: designed, shipped off behind `spoken-cues = false` |

The spoken tier is a fast-follow. When it ships it carries a hard requirement: cue audio routes to headphones only, never to speakers. That is the mix-minus principle from IFB practice (the speaker must never hear their own voice back) and it is also what keeps browser echo cancellation unnecessary for the ASR path. Until then the config key exists and stays false.

## The cue budget

`cue-density` (config `[prompter]`, overridden by rundown frontmatter, most specific wins) sets how often card-tier cues may appear. The attention tier is exempt from the budget: time-critical states always surface.

| Density | Card budget | Intent |
| --- | --- | --- |
| hands-off | no cards at all; time-critical attention cues only | the speaker wants a clock and nothing else |
| minimal | at most 1 card per 5 minutes | rare nudges |
| normal | at most 1 card per 2 minutes | the default producer presence |
| chatty | at most 1 card per 45 seconds | dense guidance for improvised shows |

## Delivery rules

- One active cue: at most one cue is on screen at any moment. Cards never stack; a new candidate waits for the active cue to clear.
- Release at a VAD pause: card-tier cues are held until the speaker pauses (the Phase B vad events). The attention tier may interrupt mid-sentence. When no ASR is running, cues release immediately (there is no pause signal to wait for).
- Auto-expiry: every cue clears itself after 15 seconds if not superseded.
- Dedup by key: every candidate carries a stable `key`. The engine shows a given key once; the producer re-emits candidates statelessly every tick and the key is what stops repeats. The OVER key advances once per whole minute over, so a long overrun re-alerts each minute.
- Quiet states: no cues before GO LIVE, none while the show is on hold, none after end-show.
- The wire frames are `{"type": "cue", "id": n, "tier": "card"|"attention", "text": str}` and `{"type": "cue-clear", "id": n}`.

## Replan rules

These mirror `producer.py` exactly.

- The show clock counts live, non-hold time only. GO LIVE starts it; hold freezes it (VAD and the transcript keep running so context is not lost); resume unfreezes; end-show freezes it permanently.
- On every tick: `remaining = duration-s - elapsed`. The wrap reserve (`wrap-s`, budgeting the last segment when set) is subtracted first and protected: the wrap's replanned budget always equals its planned budget. The rest is distributed across not-done, non-wrap segments proportionally to their ORIGINAL budgets; each not-done segment's replanned budget is its share of that future, and its spent time is consumed against it. An over-running segment therefore goes red precisely because the replan can no longer afford it.
- Green/yellow/red is always computed against the REPLANNED budgets, never the original rundown: green under 80 percent consumed, yellow at 80 to 100, red over. The show-level state applies the same thresholds to elapsed against the total duration, which is the classic speech-timer semantic: yellow means wrap is approaching, red means over.
- Done segments leave the replan: they report their original planned budget and their timing reads spent against plan. That row is history, and history does not rewrite as the plan shifts. Spent time accrues only to a segment whose state is current; a done segment's spent is frozen.
- Pointer on done: after advancing past the last segment there is nothing pending, so the wire state's `current` keeps naming that final done segment. The show clock keeps counting (elapsed and remaining stay live) but no segment accrues spent while the host talks past the end; consumers of the rail state must not assume the `current` id names a segment in the current state.
- Feasibility floor: max(45 seconds, 25 percent of the segment's original budget). When the replan pushes any pending non-wrap segment below its floor during a live show, the producer emits a DROP suggestion naming the lowest-priority pending segment, defined as the last non-wrap pending segment in rundown order, with the even-split alternative: "DROP: <title>, or <n>s each" where n is the distributable future divided across the pending non-wrap segments. A countdown that only turns red is a nag; the replan plus the DROP alternative is what makes this a producer.

## Coverage semantics

- Coverage is sticky and monotonic. Nothing ever un-covers a point.
- `propose_coverage` (the LLM tick and the deterministic keyword first-pass) may only flip an uncovered, unskipped point to covered. Proposals against skipped points, covered points, or unknown ids do nothing.
- The human is the final authority: mark-covered and skip from `/remote` or `/prompt` always win. Skipped points are excluded from the replan's attention and from next.
- Next is the first uncovered, unskipped point in rundown order. It stays well-defined when the speaker covers points out of order.

## Broadcast vocabulary

Deterministic candidates use the broadcast lexicon so a speaker who has worked with a floor manager already knows the words.

| Text | Tier | Key | When |
| --- | --- | --- | --- |
| 30 seconds | card | seg-30:<id> | 30 seconds left in the current segment's replanned budget |
| WRAP | attention | wrap | remaining show time has reached the wrap reserve |
| STRETCH | card | stretch | the show is more than 20 percent ahead of plan (plan credit for finished segments, plus the current segment's spend capped at its budget, exceeds 1.2 times elapsed) |
| <m>:<ss> OVER | attention | over:<m> | past the show duration; the key advances per whole minute over |
| DROP: <title>, or <n>s each | card | drop:<id> | the feasibility floor is breached (see replan rules) |

The LLM tick may additionally propose one short cue per tick in the same vocabulary (a next-point nudge, a stretch suggestion). LLM proposals enter the engine as card-tier candidates and obey every rule above: the budget, one-active-cue, pause release, expiry. The engine, not the model, decides what the speaker sees.
