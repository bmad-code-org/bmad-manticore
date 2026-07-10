#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Rundown parser for mc-prompter (Phase C producer mode). Pure stdlib.

Usage:
    uv run {skill-root}/scripts/server/rundown.py <file>

Implements the binding format in references/rundown-spec.md. A rundown is a
markdown file with YAML-ish frontmatter (a flat key: value block, no nesting)
followed by "## " segment headings:

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

Contract:
    parse_rundown(text) -> dict, raising RundownError(line, message) on hard
    errors. Result shape:

        {"show": str, "duration-s": int, "cue-density": str|None,
         "wrap-s": int|None, "warnings": [str],
         "segments": [{"id": "g0", "title": str,
                       "kind": "scripted"|"bullets", "planned-s": int,
                       "body": str,
                       "points": [{"text": str, "covered": false}]}]}

Frontmatter:
    show              optional string (default ""), may be quoted
    duration-minutes  required positive int
    cue-density       optional, one of hands-off|minimal|normal|chatty;
                      overrides the config value (most specific wins)
    wrap-minutes      optional positive int; when present the LAST segment
                      is the wrap and its budget is protected
    Unknown keys produce a warning. Inline "# ..." comments are stripped
    outside quoted values.

Segments:
    Split on level-2 "## " headings. Non-blank content before the first
    heading is a line-numbered error. Heading time suffix: exactly "(N min)"
    or "(Nm)" as the trailing paren group. Any other trailing paren group
    that looks like a time (bare number, digits:digits, digits glued to a
    unit like "90s", or a time-unit word next to a number) is a
    line-numbered error: reject, never guess. Trailing parens that do not
    look like a time (e.g. "(demo)", "(part 2)") stay in the title. No
    suffix = unbudgeted.

    Kind: a body with any non-bullet prose is "scripted" (the body gets
    prompted via script_ingest); a body with only "- " bullets (or nothing)
    is "bullets" and each top-level bullet becomes a point. Indented "- "
    sub-bullets join their parent point ("; " separated); an indented
    bullet with no parent is prose. A continuation line under a bullet
    (a hard-wrapped point) is prose too, so it forces scripted. Scripted
    segments have an empty points list; when a scripted segment contains
    any "- " bullet line a warning notes its points are not tracked.

Time math (see rundown-spec.md for the full rules):
    Unbudgeted segments split the remaining time evenly (largest-remainder
    rounding, earliest segments take the spare seconds). If explicit budgets
    exceed duration-minutes, duration-minutes wins: non-wrap explicit
    budgets are scaled proportionally to fit and a warning is recorded. The
    wrap budget is never scaled. Every segment ends up with planned-s;
    any segment reconciled to planned-s 0 records a warning.

Exit codes: 0 ok, 1 parse error (RundownError), 2 file missing/unreadable.
Pure stdlib; importable by server/main.py via `import rundown` from the
scripts/server directory.
"""

import argparse
import json
import re
import sys
from pathlib import Path

CUE_DENSITIES = ("hands-off", "minimal", "normal", "chatty")

HEADING_RE = re.compile(r"^##(?!#)\s+(.*?)\s*$")
TRAILING_PAREN_RE = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")
ACCEPT_MIN_RE = re.compile(r"^(\d+) min$")
ACCEPT_M_RE = re.compile(r"^(\d+)m$")
BARE_NUMBER_RE = re.compile(r"^\d+(\.\d+)?$")
CLOCK_RE = re.compile(r"^\d+:\d+$")
GLUED_UNIT_RE = re.compile(r"^\d+(\.\d+)?[a-z]+$")
BULLET_RE = re.compile(r"^\s*-\s+(.*\S)\s*$")
TOP_BULLET_RE = re.compile(r"^-\s+(.*\S)\s*$")
SUB_BULLET_RE = re.compile(r"^\s+-\s+(.*\S)\s*$")
UNIT_WORDS = frozenset((
    "m", "min", "mins", "minute", "minutes",
    "s", "sec", "secs", "second", "seconds",
    "h", "hr", "hrs", "hour", "hours",
))


class RundownError(Exception):
    """Hard parse error with a 1-based source line number."""

    def __init__(self, line, message):
        self.line = line
        self.message = message
        super().__init__(f"line {line}: {message}")


def _strip_comment(value):
    """Strip an inline comment from an unquoted frontmatter value."""
    if value.startswith("#"):
        return ""
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def _parse_frontmatter(lines):
    """Parse the frontmatter block; return (fields, body_start_index).

    fields maps key -> (value_string, line_number). body_start_index is the
    0-based index of the first line after the closing delimiter. Raises
    RundownError when the frontmatter is missing or unterminated.
    """
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        raise RundownError(i + 1, "missing frontmatter (expected --- block "
                                  "with duration-minutes)")
    fields = {}
    j = i + 1
    while j < len(lines):
        line = lines[j]
        if line.strip() == "---":
            return fields, j + 1
        if line.strip() and ":" in line:
            key, _, raw = line.partition(":")
            key = key.strip()
            value = raw.strip()
            if value.startswith('"') and value.endswith('"') and len(value) >= 2:
                value = value[1:-1]
            elif value.startswith("'") and value.endswith("'") and len(value) >= 2:
                value = value[1:-1]
            else:
                value = _strip_comment(value)
            fields[key] = (value, j + 1)
        elif line.strip():
            raise RundownError(j + 1, f"malformed frontmatter line: "
                                      f"{line.strip()!r}")
        j += 1
    raise RundownError(i + 1, "unterminated frontmatter (no closing ---)")


def _positive_int(value, line, key):
    try:
        n = int(value)
    except ValueError:
        raise RundownError(line, f"{key} must be an integer, got {value!r}")
    if n <= 0:
        raise RundownError(line, f"{key} must be positive, got {n}")
    return n


def _looks_like_time(content):
    """True when a paren group reads as a time expression (reject cases)."""
    c = content.strip().casefold()
    if not any(ch.isdigit() for ch in c):
        return False
    if BARE_NUMBER_RE.match(c) or CLOCK_RE.match(c) or GLUED_UNIT_RE.match(c):
        return True
    tokens = c.split()
    return any(t in UNIT_WORDS for t in tokens)


def _parse_heading(text, line):
    """Split a heading into (title, planned_minutes_or_None).

    Accepts exactly "(N min)" or "(Nm)" as the trailing paren group. Any
    other trailing paren group that looks like a time is a hard error.
    """
    if not text.strip():
        raise RundownError(line, "segment heading has no title")
    m = TRAILING_PAREN_RE.match(text)
    if not m:
        return text, None
    title, content = m.group(1).strip(), m.group(2).strip()
    am = ACCEPT_MIN_RE.match(content) or ACCEPT_M_RE.match(content)
    if am:
        minutes = int(am.group(1))
        if minutes <= 0:
            raise RundownError(line, f"segment budget must be positive: "
                                     f"({content})")
        if not title:
            raise RundownError(line, "segment heading has no title")
        return title, minutes
    if _looks_like_time(content):
        raise RundownError(
            line,
            f"unrecognized time suffix ({content}): use exactly (N min) "
            f"or (Nm)")
    return text, None


def _segment_kind(body_lines):
    """Classify a body: any non-bullet prose is scripted, else bullets.

    Top-level "- " bullets become points. An indented "- " sub-bullet
    joins its parent point (appended after "; "); an indented bullet with
    no parent is prose. Any other non-blank line (including a
    hard-wrapped bullet's continuation line) is prose and makes the
    segment scripted with an empty points list.
    """
    points = []
    for line in body_lines:
        if not line.strip():
            continue
        tm = TOP_BULLET_RE.match(line)
        if tm:
            points.append(tm.group(1).strip())
            continue
        sm = SUB_BULLET_RE.match(line)
        if sm and points:
            points[-1] += "; " + sm.group(1).strip()
            continue
        return "scripted", []
    return "bullets", points


def _apportion(weights, total):
    """Split integer total proportionally to weights, exactly.

    Largest-remainder method; ties break to the earliest index. Zero or
    empty weight sums fall back to an even split.
    """
    n = len(weights)
    if n == 0:
        return []
    wsum = sum(weights)
    if wsum <= 0:
        base, rem = divmod(total, n)
        return [base + (1 if i < rem else 0) for i in range(n)]
    shares = [w * total / wsum for w in weights]
    floors = [int(s) for s in shares]
    leftover = total - sum(floors)
    order = sorted(range(n), key=lambda i: (floors[i] - shares[i], i))
    for i in order[:leftover]:
        floors[i] += 1
    return floors


def _even_split(total, n):
    base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def parse_rundown(text):
    """Parse rundown text; return the rundown dict (shape in the docstring).

    Raises RundownError(line, message) on hard errors; recoverable issues
    (budget reconciliation, unknown keys) land in result["warnings"].
    """
    lines = text.splitlines()
    fields, body_start = _parse_frontmatter(lines)
    warnings = []

    known = {"show", "duration-minutes", "cue-density", "wrap-minutes"}
    for key, (_, line) in fields.items():
        if key not in known:
            warnings.append(f"line {line}: unknown frontmatter key "
                            f"{key!r} ignored")

    show = fields.get("show", ("", 0))[0]

    if "duration-minutes" not in fields:
        raise RundownError(1, "frontmatter is missing duration-minutes")
    dur_value, dur_line = fields["duration-minutes"]
    duration_s = _positive_int(dur_value, dur_line, "duration-minutes") * 60

    cue_density = None
    if "cue-density" in fields:
        cd_value, cd_line = fields["cue-density"]
        if cd_value not in CUE_DENSITIES:
            raise RundownError(
                cd_line,
                f"cue-density must be one of {', '.join(CUE_DENSITIES)}, "
                f"got {cd_value!r}")
        cue_density = cd_value

    wrap_s = None
    if "wrap-minutes" in fields:
        w_value, w_line = fields["wrap-minutes"]
        wrap_s = _positive_int(w_value, w_line, "wrap-minutes") * 60
        if wrap_s > duration_s:
            raise RundownError(
                w_line, "wrap-minutes exceeds duration-minutes")

    # Split the body into segments on "## " headings.
    segments = []
    current = None
    for idx in range(body_start, len(lines)):
        line = lines[idx]
        hm = HEADING_RE.match(line)
        if hm:
            title, minutes = _parse_heading(hm.group(1), idx + 1)
            current = {"title": title, "minutes": minutes,
                       "line": idx + 1, "body_lines": []}
            segments.append(current)
        elif current is None:
            if line.strip():
                raise RundownError(
                    idx + 1,
                    "content before the first ## segment heading")
        else:
            current["body_lines"].append(line)

    if not segments:
        raise RundownError(body_start + 1,
                           "rundown has no ## segment headings")

    out = []
    for i, seg in enumerate(segments):
        kind, points = _segment_kind(seg["body_lines"])
        if kind == "scripted" and any(BULLET_RE.match(ln)
                                      for ln in seg["body_lines"]):
            warnings.append(
                f"line {seg['line']}: segment {seg['title']!r} mixes "
                f"bullets with prose so it is scripted; its points are "
                f"not tracked (a hard-wrapped bullet line counts as "
                f"prose)")
        body = "\n".join(seg["body_lines"]).strip("\n").rstrip()
        out.append({
            "id": f"g{i}",
            "title": seg["title"],
            "kind": kind,
            "planned-s": None,
            "body": body,
            "points": [{"text": p, "covered": False} for p in points],
            "_minutes": seg["minutes"],
            "_line": seg["line"],
        })

    # Time math. The wrap segment (the last one, when wrap-minutes is set)
    # is budgeted from the frontmatter and protected from scaling.
    wrap_seg = out[-1] if wrap_s is not None else None
    if wrap_seg is not None:
        if (wrap_seg["_minutes"] is not None
                and wrap_seg["_minutes"] * 60 != wrap_s):
            warnings.append(
                f"line {wrap_seg['_line']}: wrap segment suffix "
                f"({wrap_seg['_minutes']} min) differs from wrap-minutes; "
                f"wrap-minutes wins")
        wrap_seg["planned-s"] = wrap_s

    pool = [s for s in out if s is not wrap_seg]
    explicit = [s for s in pool if s["_minutes"] is not None]
    unbudgeted = [s for s in pool if s["_minutes"] is None]
    for s in explicit:
        s["planned-s"] = s["_minutes"] * 60

    budget = duration_s - (wrap_s or 0)
    explicit_sum = sum(s["planned-s"] for s in explicit)

    if explicit_sum > budget:
        warnings.append(
            f"explicit segment budgets total {explicit_sum}s but only "
            f"{budget}s is available inside duration-minutes; "
            f"duration-minutes wins, explicit budgets scaled "
            f"proportionally")
        scaled = _apportion([s["planned-s"] for s in explicit], budget)
        for s, v in zip(explicit, scaled):
            s["planned-s"] = v
        if unbudgeted:
            names = ", ".join(s["title"] for s in unbudgeted)
            warnings.append(
                f"no time remains for unbudgeted segments: {names} "
                f"(planned 0s)")
            for s in unbudgeted:
                s["planned-s"] = 0
    elif unbudgeted:
        split = _even_split(budget - explicit_sum, len(unbudgeted))
        for s, v in zip(unbudgeted, split):
            s["planned-s"] = v
        zeroed = [s["title"] for s in unbudgeted if s["planned-s"] == 0]
        if zeroed:
            warnings.append(
                f"the wrap reserve and explicit budgets leave no time "
                f"for: {', '.join(zeroed)} (planned 0s)")
    elif explicit_sum < budget:
        warnings.append(
            f"segment budgets total {explicit_sum + (wrap_s or 0)}s, "
            f"leaving {budget - explicit_sum}s of the show unallocated")

    for s in out:
        del s["_minutes"]
        del s["_line"]

    return {
        "show": show,
        "duration-s": duration_s,
        "cue-density": cue_density,
        "wrap-s": wrap_s,
        "warnings": warnings,
        "segments": out,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Parse a rundown.md file and print the plan as JSON")
    parser.add_argument("file", help="path to the rundown file")
    args = parser.parse_args(argv)

    path = Path(args.file)
    if not path.is_file():
        print(f"error: rundown not found: {path}", file=sys.stderr)
        return 2
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return 2

    try:
        plan = parse_rundown(text)
    except RundownError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(plan, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)
