#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Put the beat table on the creator's Resolve cut timeline as review markers.

Usage:
    uv run {skill-root}/scripts/resolve_beat_markers.py beats/beats.md \
        --import cut/resolve-import.json [-o beats/resolve-markers.json]

Contract:
    input   beats/beats.md (the engine-neutral beat table; `start` and `dur`
            are seconds on the edited timeline) and cut/resolve-import.json,
            the report mc-cut wrote when it imported the cut into Resolve. Its
            `project`, `timeline` and `fps` name the target.
    does    connects to a running Resolve Studio (resolve_api.py) and opens
            that project. It never saves the open project, and refuses to
            switch away from one that holds work. On the named timeline it
            first deletes every marker with customData
            "manticore-beat-table" (an earlier run of this script), then adds
            one marker per beat:
              frame     round(start * fps); Resolve numbers timeline
                        marker frames from the timeline start, whatever its
                        start timecode
              duration  the beat's dur in frames (at least 1)
              color     from the beat type: cta is Yellow; every other type
                        gets a stable color from its name (TYPE_PALETTE),
                        never Blue or Red, which mc-cut's markers use
              name      "<id> <type>"
              note      the composition, then the spoken phrase
            Every other marker is left alone. Resolve holds one marker per
            frame, so beats sharing a frame merge into one marker, and a beat
            whose frame already holds someone else's marker moves to the next
            free frame within MAX_NUDGE frames (reported).
    verify  reads the markers back and checks each placed marker exists at
            its frame with this script's customData and name, and that no
            stale marker of this script remains. Any miss exits 1.
    output  optional -o report JSON (timeline, placed, merged, nudged,
            problems); json.dumps summary on stdout either way.

Exit codes: 0 placed and verified, 1 placement or verify failure (including a
cut timeline that failed its own import check), 2 usage, 4 Resolve
unavailable. On 4 beats/beats.md is still the gate 3 artifact.

STATUS: implemented (pure logic covered by
scripts/tests/test-resolve_beat_markers.py).
"""

import argparse
import json
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve_api

CUSTOM = "manticore-beat-table"
CTA_COLOR = "Yellow"
TYPE_PALETTE = ["Cyan", "Green", "Pink", "Purple", "Fuchsia", "Rose",
                "Lavender", "Sky", "Mint", "Lemon", "Sand"]
MAX_NUDGE = 5
FAILED_SUFFIX = " (FAILED VERIFY)"


def type_color(beat_type):
    """Marker color for a beat type (pure, stable across runs)."""
    t = str(beat_type or "").strip().lower()
    if t == "cta":
        return CTA_COLOR
    return TYPE_PALETTE[zlib.crc32(t.encode("utf-8")) % len(TYPE_PALETTE)]


def parse_table(text):
    """Beat rows {id, start, dur, type, phrase, composition} (pure).

    Columns are matched by header name, so extra columns and any order work.
    Returns (rows, skipped).
    """
    header, col = None, {}
    rows, skipped = [], []
    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        low = [c.lower() for c in cells]
        if header is None:
            if "id" in low and "start" in low:
                header, col = low, {n: i for i, n in enumerate(low)}
            continue
        if all(set(c) <= set("-: ") for c in cells if c):
            continue
        if len(cells) < len(header):
            skipped.append(f"short row: {cells[:2]}")
            continue

        def cell(name):
            i = col.get(name)
            return cells[i] if i is not None and i < len(cells) else ""

        bid = cell("id")
        try:
            start = float(cell("start"))
        except ValueError:
            skipped.append(f"beat {bid}: start {cell('start')!r} is not a number")
            continue
        try:
            dur = float(cell("dur"))
        except ValueError:
            dur = 0.0
        rows.append({"id": bid, "start": start, "dur": dur,
                     "type": cell("type") or "overlay",
                     "phrase": cell("spoken phrase"),
                     "composition": cell("composition")})
    return rows, skipped


def plan_markers(rows, fps):
    """One marker per frame, beats sharing a frame merged (pure)."""
    by_frame = {}
    for r in rows:
        frame = round(r["start"] * fps)
        name = f"{r['id']} {r['type']}"
        note = r["composition"]
        if r["phrase"]:
            note = f"{note}\n\"{r['phrase']}\"" if note else f"\"{r['phrase']}\""
        dur = max(1, round(r["dur"] * fps))
        if frame in by_frame:
            m = by_frame[frame]
            m["name"] += f" / {name}"
            m["note"] += f"\n{name}: {note}"
            m["duration"] = max(m["duration"], dur)
            m["ids"].append(r["id"])
        else:
            by_frame[frame] = {"frame": frame, "color": type_color(r["type"]),
                               "name": name, "note": note, "duration": dur,
                               "ids": [r["id"]]}
    return [by_frame[f] for f in sorted(by_frame)]


def place(planned, occupied, max_nudge=MAX_NUDGE):
    """Move markers off frames other markers hold (pure).

    Returns (placed, nudged, blocked). `occupied` is the set of frames held
    by markers this script does not own.
    """
    taken = set(occupied)
    placed, nudged, blocked = [], [], []
    for m in planned:
        frame = next((m["frame"] + d for d in range(max_nudge + 1)
                      if m["frame"] + d not in taken), None)
        if frame is None:
            blocked.append(m)
            continue
        if frame != m["frame"]:
            nudged.append({"ids": m["ids"], "from": m["frame"], "to": frame})
        taken.add(frame)
        placed.append(dict(m, frame=frame))
    return placed, nudged, blocked


def check(placed, markers):
    """Problems between placed markers and what Resolve reports (pure).

    `markers` is Timeline.GetMarkers(): {frame: {name, customData, ...}}.
    """
    problems = []
    ours = {int(f): m for f, m in markers.items()
            if m.get("customData") == CUSTOM}
    for p in placed:
        got = ours.get(p["frame"])
        if got is None:
            problems.append({"what": "missing", "ids": p["ids"],
                             "frame": p["frame"]})
        elif got.get("name") != p["name"]:
            problems.append({"what": "name", "frame": p["frame"],
                             "expected": p["name"], "got": got.get("name")})
    stale = sorted(set(ours) - {p["frame"] for p in placed})
    if stale:
        problems.append({"what": "stale", "frames": stale})
    return problems


def open_project(pm, name):
    """The target project, or (None, reason). Never saves the open project."""
    current = pm.GetCurrentProject()
    if current and current.GetName() == name:
        return current, None
    if current:
        root = current.GetMediaPool().GetRootFolder()
        if current.GetTimelineCount() or root.GetClipList() or root.GetSubFolderList():
            return None, (f"Resolve has project {current.GetName()!r} open "
                          f"with work in it. Save and close it, or open "
                          f"{name!r}, then re-run.")
    project = pm.LoadProject(name)
    if not project:
        return None, (f"Resolve project {name!r} does not exist. Run mc-cut's "
                      "Resolve import first.")
    return project, None


def find_timeline(project, name):
    for i in range(project.GetTimelineCount()):
        tl = project.GetTimelineByIndex(i + 1)
        if tl.GetName() == name:
            return tl
    return None


def clear_ours(timeline, limit=10000):
    """Delete every marker this script placed. DeleteMarkerByCustomData
    removes only the first match, so loop until none is left."""
    n = 0
    while n < limit and timeline.GetMarkerByCustomData(CUSTOM):
        if not timeline.DeleteMarkerByCustomData(CUSTOM):
            break
        n += 1
    return n


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("beats", help="path to beats/beats.md")
    p.add_argument("--import", dest="imported", required=True,
                   help="path to cut/resolve-import.json")
    p.add_argument("-o", "--output", default=None,
                   help="optional path to write the report JSON")
    args = p.parse_args(argv)

    try:
        rows, skipped = parse_table(Path(args.beats).read_text(encoding="utf-8"))
        imported = json.loads(Path(args.imported).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"resolve_beat_markers: cannot read inputs: {e}", file=sys.stderr)
        return 2
    if not rows:
        print("resolve_beat_markers: no beat rows found in the table",
              file=sys.stderr)
        return 2
    for key in ("project", "timeline", "fps"):
        if key not in imported:
            print(f"resolve_beat_markers: {args.imported} has no {key!r}; "
                  "re-run mc-cut's Resolve import", file=sys.stderr)
            return 2
    if not imported.get("ok") or imported["timeline"].endswith(FAILED_SUFFIX):
        print(f"resolve_beat_markers: the cut timeline "
              f"{imported['timeline']!r} failed its import check. Re-run "
              "mc-cut's Resolve import before placing beats on it.",
              file=sys.stderr)
        return 1

    try:
        resolve = resolve_api.connect()
    except resolve_api.ResolveUnavailable as e:
        print(f"resolve_beat_markers: {e}", file=sys.stderr)
        return resolve_api.UNAVAILABLE_EXIT

    pm = resolve.GetProjectManager()
    project, problem = open_project(pm, imported["project"])
    if not project:
        print(f"resolve_beat_markers: {problem}", file=sys.stderr)
        return 1
    timeline = find_timeline(project, imported["timeline"])
    if not timeline:
        print(f"resolve_beat_markers: timeline {imported['timeline']!r} is "
              f"not in project {imported['project']!r}", file=sys.stderr)
        return 1

    cleared = clear_ours(timeline)
    occupied = {int(f) for f in (timeline.GetMarkers() or {})}
    planned = plan_markers(rows, float(imported["fps"]))
    placed, nudged, blocked = place(planned, occupied)
    failed = [m for m in placed
              if not timeline.AddMarker(m["frame"], m["color"], m["name"],
                                        m["note"], m["duration"], CUSTOM)]
    problems = check(placed, timeline.GetMarkers() or {})
    problems += [{"what": "blocked", "ids": m["ids"], "frame": m["frame"]}
                 for m in blocked]
    problems += [{"what": "add refused", "ids": m["ids"], "frame": m["frame"]}
                 for m in failed if not any(x.get("frame") == m["frame"]
                                            for x in problems)]
    project.SetCurrentTimeline(timeline)
    pm.SaveProject()

    report = {"ok": not problems, "project": imported["project"],
              "timeline": imported["timeline"], "beats": len(rows),
              "skipped_rows": skipped, "cleared": cleared,
              "markers": len(placed) - len(failed),
              "merged": [m["ids"] for m in placed if len(m["ids"]) > 1],
              "nudged": nudged, "problems": problems}
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in
                      ("ok", "timeline", "beats", "cleared", "markers",
                       "merged", "nudged")}, indent=2))
    for s in skipped:
        print(f"note: skipped row ({s})", file=sys.stderr)
    if problems:
        print(f"\nBEAT MARKERS INCOMPLETE on {imported['timeline']!r}: "
              f"{len(problems)} problem(s), first: {problems[0]}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
