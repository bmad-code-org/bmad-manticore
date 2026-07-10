# The rundown format

This is the binding specification for the rundown file that drives mc-prompter's producer mode. The parser at `scripts/server/rundown.py` implements exactly this spec; if the parser and this document disagree, that is a bug. A starter template ships into the studio through mc-setup's assets, so any skill can draft a rundown as a project file without reading this skill's folder.

## What a rundown is

A rundown is one markdown file describing a timed show: total duration, an ordered list of segments with optional per-segment budgets, and a protected wrap. Segments carry either full scripted text (prompted like any script) or bullet points (talking points the producer tracks for coverage). For pipeline projects the file lives at `{projects-path}/<slug>/rundown.md`; standalone shows can pass any path.

## Example

```markdown
---
show: "Why local models win"
duration-minutes: 30
cue-density: normal        # hands-off | minimal | normal | chatty
wrap-minutes: 3
---

## Intro (3 min)

Full scripted intro text, prompted normally.

## Point 1: The cost argument (5 min)

- cloud bills compound, local is capex
- the 4090 anecdote

## Point 2: Latency (5 min)

- round trips add up
- the demo

## Wrap (3 min)

Scripted wrap text.
```

## Frontmatter

The file starts with a `---` delimited block of flat `key: value` lines. Values may be quoted; an unquoted value may carry a trailing `# comment`, which is stripped. Nesting is not supported.

| Key | Required | Type | Meaning |
| --- | --- | --- | --- |
| show | no | string | Show title. Defaults to an empty string. |
| duration-minutes | yes | positive int | Total show length. The hard constraint all time math reconciles against. |
| cue-density | no | enum | One of `hands-off`, `minimal`, `normal`, `chatty`. Overrides the config value: most specific wins. Any other value is a hard error. |
| wrap-minutes | no | positive int | When present, the LAST segment is the wrap and its budget is protected (see time math). Exceeding duration-minutes is a hard error; equaling it loads, but every other segment reconciles to 0 seconds and a warning names them. |

Unknown keys are ignored with a warning, so typos surface at load instead of silently doing nothing. Missing frontmatter, a missing or non-integer `duration-minutes`, and an unterminated block are hard errors.

## Segments

Segments split on level-2 `## ` headings. Deeper headings (`###` and below) are body content. Non-blank content before the first `## ` heading is a hard error with its line number: the parser never guesses which segment stray text belongs to. A rundown with no segments is a hard error.

Segment ids are `g0`, `g1`, ... in document order. The heading text minus any time suffix is the segment title; an empty title is a hard error.

## Time suffixes

A heading may end with a time budget in parentheses. Exactly two forms are accepted:

- `## Intro (3 min)` gives the segment 3 minutes
- `## Intro (3m)` is the compact equivalent

Anything else in a trailing paren group that looks like a time is a hard, line-numbered error. Reject, do not guess: hand-written and model-drafted rundowns produce creative variants on day one, and a silently misread budget is worse than a load error.

| Trailing group | Result |
| --- | --- |
| `(3 min)` | accepted, 3 minutes |
| `(12m)` | accepted, 12 minutes |
| `(3 minutes)` | error: unrecognized time suffix |
| `(3:00)` | error: unrecognized time suffix |
| `(90s)` | error: unrecognized time suffix |
| `(5)` | error: unrecognized time suffix |
| `(0 min)` | error: budget must be positive |
| `(demo)` | not a time; stays in the title |
| `(part 2)` | not a time; stays in the title |

Looks like a time means: a bare number, a `digits:digits` clock form, digits glued to letters (`90s`, `5min`), or a number appearing alongside a time-unit word (`min`, `mins`, `minute`, `minutes`, `s`, `sec`, `second`, `seconds`, `h`, `hr`, `hour`, `hours` and plurals). A trailing paren group with no digits, or with digits but none of those shapes, is title text. Only the trailing paren group is examined; parentheses elsewhere in the title are never touched.

A heading with no suffix is unbudgeted and gets a share of the remaining time (see time math).

## Segment kinds

- A segment whose body contains any non-bullet prose is `scripted`. Its body is what gets prompted (ingested via script_ingest by the server); its points list is empty, even if the body also contains bullets.
- A segment whose body contains only `- ` bullets, or nothing at all, is `bullets`. Each top-level bullet becomes a point the producer tracks for coverage.

Only `- ` bullets count as list items; `*` bullets are treated as prose and make the segment scripted.

Bullet edge rules, all deterministic:

- Nesting: an indented `- ` sub-bullet joins its parent point. The sub-bullet text is appended to the preceding top-level point after `; `, so `- main` followed by an indented `- detail` yields one coverage point, `main; detail`. Sub-bullets never become independent points. An indented bullet with no preceding top-level bullet is prose and forces the segment scripted.
- Continuation lines: each point must sit on one line. A non-bullet line under a bullet (the shape a hard-wrapping editor produces) is prose, so the whole segment becomes `scripted` and its points are not tracked.
- Warning on the mix: whenever a segment classified `scripted` contains at least one `- ` bullet line, the parser records a warning naming the segment and stating that its points are not tracked. Loading is not blocked; the warning surfaces on the home page so a hard-wrapped bullet cannot silently kill coverage.

## Time math

All budgets resolve to whole seconds (`planned-s`). The rules, in order:

1. `duration-s = duration-minutes * 60`. This number always wins.
2. When `wrap-minutes` is set, the last segment is the wrap and `planned-s = wrap-minutes * 60`. If the wrap heading also carries a suffix and it differs, a warning is recorded and wrap-minutes wins. The wrap budget is protected: reconciliation never scales it.
3. Explicit suffixes on other segments become their budgets.
4. Unbudgeted segments split the remaining time (`duration-s` minus the wrap minus the explicit budgets) evenly. Rounding uses whole seconds; spare seconds from the division go to the earliest unbudgeted segments so the totals add up exactly. If this split leaves any segment with `planned-s` 0 (the wrap reserve and explicit budgets consume the whole show, e.g. wrap-minutes equal to duration-minutes), a warning names the zeroed segments: a plan where a segment has no time never loads silently.
5. If the explicit budgets exceed the available time, duration-minutes wins: the non-wrap explicit budgets are scaled proportionally to fit (largest-remainder rounding, so the reconciled plan sums exactly) and a warning is recorded. If unbudgeted segments exist in this case they get `planned-s` 0 and a second warning names them.
6. If every segment is budgeted and time is left over, the budgets are kept and a warning notes the unallocated seconds. The show simply has slack; the live replanner will stretch into it.

Warnings never block loading. The home page shows the reconciled plan, warnings included, before the show starts.

## Parse result

`parse_rundown(text)` returns:

```json
{
  "show": "Why local models win",
  "duration-s": 1800,
  "cue-density": "normal",
  "wrap-s": 180,
  "warnings": [],
  "segments": [
    {"id": "g0", "title": "Intro", "kind": "scripted", "planned-s": 180,
     "body": "Full scripted intro text, prompted normally.",
     "points": []},
    {"id": "g1", "title": "Point 1: The cost argument", "kind": "bullets",
     "planned-s": 300,
     "body": "- cloud bills compound, local is capex\n- the 4090 anecdote",
     "points": [{"text": "cloud bills compound, local is capex", "covered": false},
                {"text": "the 4090 anecdote", "covered": false}]}
  ]
}
```

`cue-density` is null when the frontmatter omits it; `wrap-s` is null when there is no wrap. `body` is the raw body text with outer blank lines stripped, for both kinds.

## Errors

Hard errors raise `RundownError(line, message)` with the 1-based source line. The hard error set: missing or unterminated frontmatter, malformed frontmatter lines, missing or invalid `duration-minutes`, invalid `cue-density`, invalid `wrap-minutes` or a wrap exceeding the duration, an unrecognized time suffix, a non-positive segment budget, an empty segment title, content before the first heading, and a rundown with no segments.

## CLI

```
uv run {skill-root}/scripts/server/rundown.py <file>
```

Prints the parse result as JSON. Exit codes: 0 ok, 1 parse error (the error with its line number goes to stderr), 2 file missing or unreadable.
