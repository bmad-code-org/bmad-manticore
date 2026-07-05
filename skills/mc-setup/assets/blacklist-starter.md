# Script Blacklist: LLM Tells and Slop

Starter blacklist shipped with Manticore; mc-setup copies it to `{brand-path}/blacklist.md`, which is the live copy the lint enforces. Enforced by the pipeline's `lint_script.py` on every script before it is presented. The lint parses every fenced `regex` block in this file; one pattern per line, `#` lines are comments, matching is case-insensitive per line of the script.

mc-retro appends new patterns to the live copy when the creator flags a tell in a published or drafted script. The live copy only grows; prune it by hand if a pattern bites legitimate speech.

## Regex patterns

```regex
# punctuation tells
—
–
# openers and throat-clearing
\bin today'?s video\b
\bwithout further ado\b
\bwelcome back to (the|my) channel\b
\blet'?s (dive|jump) (right )?in(to)?\b
# banned words
\bdelve\b
\bdive deep\b
\bdeep dive\b
\bgame.?chang(er|ing)\b
\bunleash\b
\bunlock (the )?(power|potential|secrets?)\b
\brevolutioniz\w*
\bseamless(ly)?\b
\bleverag(e|ing)\b
\brobust\b
\belevate\b
\bembark\b
\bsupercharg\w*
\bactionable\b
\bharness (the )?power\b
\bhidden gem\b
# banned phrases and constructions
\bin the world of\b
\bin the (ever.)?(changing|evolving) landscape\b
\bit'?s important to note\b
\bat the end of the day\b
\blet that sink in\b
\bbut here'?s the (thing|kicker|catch)\b
\btake (it|this|your \w+) to the next level\b
\bimagine (a world|if you could)\b
\bwhether you'?re a\b
\bnot (just|only) \w[^.,;]{0,40}, but\b
\bthe (best|craziest|wildest) part\?
\bgone are the days\b
\bsay goodbye to\b
```

## Manual QA (not regexable, checked by mc-script's craft pass)

- Fake-casual specificity: invented details like "Tuesday", coffee props, weather set-dressing. Real details or none.
- Sentences that do not trace to a braindump phrase and are not flagged as invented (quote-or-cut violations).
- Symmetric triads ("fast, cheap, and reliable") where the creator's natural speech would use one or two items.
- Rhetorical questions the viewer did not ask.
- Every paragraph opening with the same construction.
