#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Diff the creator's finished Resolve timeline against the pipeline's cut.

Usage:
    uv run {skill-root}/scripts/resolve_diff.py --edl cut/edl.json \
        --project <slug> [--timeline "<name>"] \
        [--export retro/final-timeline.otio] \
        -o retro/resolve-diff.md [--json retro/resolve-diff.json]

    uv run {skill-root}/scripts/resolve_diff.py --edl cut/edl.json \
        --otio <exported.otio> -o retro/resolve-diff.md

Contract:
    input   cut/edl.json (segments in source seconds) and either a running
            Resolve Studio (resolve_api.py) or an OTIO file the creator
            exported. With Resolve, it opens the project named --project
            (never saving the open project, and refusing to switch away from
            one that holds work), exports --timeline, default the project's
            current timeline, with Timeline.Export as OTIO to --export, and
            diffs that file. Nothing in Resolve is changed.
    diff    video track 1 is the cut. Each of its media clips is matched to
            the EDL segments it overlaps on the same source file:
              unchanged  both edges within one frame of the segment
              trimmed    an edge moved inward (head or tail, in seconds)
              extended   an edge moved outward
              split      several clips cover one segment; the creator cut
                         inside it
              joined     one clip covers several segments; the creator
                         restored what the cut removed between them
              removed    no clip covers the segment
              added      a clip covers no segment
              moved      matched segments out of EDL order (the ones outside
                         the longest in-order run)
            Every other track is counted: video tracks above 1 (graphics,
            titles, b-roll) and audio tracks above 1 (music, sound), with
            items split into media and generated (titles, generators).
    output  -o markdown for mc-retro to read as evidence of what the creator
            re-edited, and optional --json with the same data; json.dumps
            summary on stdout.

Exit codes: 0 diffed, 1 the timeline cannot be parsed or none of its track-1
clips use the EDL's sources (wrong timeline), 2 usage, 4 Resolve unavailable.
On 4, the creator can export the timeline as OTIO by hand (File > Export >
Timeline) and re-run with --otio.

STATUS: implemented (pure logic covered by
scripts/tests/test-resolve_diff.py).
"""

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve_api


def seconds(rt):
    """OTIO RationalTime dict to seconds (pure)."""
    if not rt:
        return 0.0
    return float(rt["value"]) / float(rt["rate"])


def norm_path(path):
    """Comparable source path: file URLs decoded, resolved when possible (pure)."""
    text = str(path or "")
    if text.startswith("file:"):
        text = unquote(urlparse(text).path)
    return str(Path(text).expanduser())


def _media_ref(clip):
    refs = clip.get("media_references")
    if refs:
        return refs.get(clip.get("active_media_reference_key", "DEFAULT_MEDIA"))
    return clip.get("media_reference")


def parse_otio(doc):
    """Tracks with record-ordered items, times in seconds (pure).

    Source times are media-relative: the media's own start timecode
    (available_range) is subtracted, matching the EDL's file-relative seconds.
    """
    if doc.get("OTIO_SCHEMA", "").split(".")[0] != "Timeline":
        raise ValueError("not an OTIO timeline")
    tracks = []
    counts = {"Video": 0, "Audio": 0}
    for t in doc["tracks"]["children"]:
        kind = t.get("kind", "Video")
        counts[kind] = counts.get(kind, 0) + 1
        rec, items = 0.0, []
        for c in t.get("children", []):
            schema = c.get("OTIO_SCHEMA", "").split(".")[0]
            if schema == "Transition":
                continue
            dur = seconds((c.get("source_range") or {}).get("duration"))
            if schema == "Clip":
                ref = _media_ref(c) or {}
                ref_schema = ref.get("OTIO_SCHEMA", "").split(".")[0]
                avail = (ref.get("available_range") or {}).get("start_time")
                start = (seconds(c["source_range"]["start_time"])
                         - seconds(avail))
                is_media = ref_schema == "ExternalReference" and ref.get("target_url")
                items.append({
                    "name": c.get("name", ""),
                    "kind": "media" if is_media else "generated",
                    "path": norm_path(ref.get("target_url")) if is_media else None,
                    "src_start": start, "src_end": start + dur,
                    "rec_start": rec, "rec_end": rec + dur,
                    "enabled": c.get("enabled", True),
                })
            elif schema not in ("Gap",):
                items.append({"name": c.get("name", ""), "kind": "generated",
                              "path": None, "src_start": 0.0, "src_end": dur,
                              "rec_start": rec, "rec_end": rec + dur,
                              "enabled": c.get("enabled", True)})
            rec += dur
        tracks.append({"kind": kind, "index": counts[kind],
                       "name": t.get("name", ""), "items": items})
    return tracks


def _same_source(a, b):
    return a == b or Path(a).name == Path(b).name


def _overlap(a0, a1, b0, b1):
    return min(a1, b1) - max(a0, b0)


def longest_run(seq):
    """Indices of seq forming its longest non-decreasing run (pure).

    Non-decreasing, so the pieces of a split segment count as in order.
    """
    if not seq:
        return set()
    best_len = [1] * len(seq)
    prev = [-1] * len(seq)
    for i in range(len(seq)):
        for j in range(i):
            if seq[j] <= seq[i] and best_len[j] + 1 > best_len[i]:
                best_len[i], prev[i] = best_len[j] + 1, j
    i = max(range(len(seq)), key=best_len.__getitem__)
    keep = set()
    while i != -1:
        keep.add(i)
        i = prev[i]
    return keep


def diff(segments, clips, fps):
    """Classify every EDL segment and track-1 clip (pure).

    `segments` are EDL dicts {source, start, end}; `clips` are parse_otio
    items from video track 1. Returns {changes, totals}.
    """
    tol = 1.0 / fps
    segs = [{"i": i, "source": norm_path(s["source"]),
             "start": float(s["start"]), "end": float(s["end"])}
            for i, s in enumerate(segments)]
    media = [c for c in clips if c["kind"] == "media" and c["enabled"]]
    covers = {s["i"]: [] for s in segs}
    changes = []
    single = []
    for ci, c in enumerate(media):
        hit = [s for s in segs if _same_source(c["path"], s["source"])
               and _overlap(c["src_start"], c["src_end"],
                            s["start"], s["end"]) > tol / 2]
        for s in hit:
            covers[s["i"]].append(ci)
        if not hit:
            changes.append({"kind": "added", "clip": ci,
                            "source": Path(c["path"]).name,
                            "src": [c["src_start"], c["src_end"]],
                            "rec": c["rec_start"],
                            "seconds": c["src_end"] - c["src_start"]})
        elif len(hit) > 1:
            restored = sum(max(0.0, b["start"] - a["end"])
                           for a, b in zip(hit, hit[1:]))
            changes.append({"kind": "joined", "clip": ci,
                            "segments": [s["i"] for s in hit],
                            "rec": c["rec_start"], "seconds": restored})
        else:
            single.append((ci, hit[0]["i"]))
    for s in segs:
        cs = covers[s["i"]]
        if not cs:
            changes.append({"kind": "removed", "segment": s["i"],
                            "src": [s["start"], s["end"]],
                            "seconds": s["end"] - s["start"]})
        elif len(cs) > 1:
            parts = sorted((media[ci]["src_start"], media[ci]["src_end"])
                           for ci in cs)
            dropped = sum(max(0.0, b[0] - a[1]) for a, b in zip(parts, parts[1:]))
            changes.append({"kind": "split", "segment": s["i"],
                            "src": [s["start"], s["end"]], "pieces": len(cs),
                            "seconds": dropped})
    unchanged = 0
    for ci, si in single:
        if len(covers[si]) > 1:
            continue
        c, s = media[ci], segs[si]
        head = c["src_start"] - s["start"]
        tail = s["end"] - c["src_end"]
        if abs(head) <= tol and abs(tail) <= tol:
            unchanged += 1
            continue
        kind = "trimmed" if head >= -tol and tail >= -tol else (
            "extended" if head <= tol and tail <= tol else "trimmed+extended")
        changes.append({"kind": kind, "segment": si, "clip": ci,
                        "src": [s["start"], s["end"]], "rec": c["rec_start"],
                        "head": round(head, 3), "tail": round(tail, 3)})
    order = [si for _, si in sorted(single)]
    keep = longest_run(order)
    for k, si in enumerate(order):
        if k not in keep:
            changes.append({"kind": "moved", "segment": si,
                            "src": [segs[si]["start"], segs[si]["end"]]})
    totals = {}
    for ch in changes:
        totals[ch["kind"]] = totals.get(ch["kind"], 0) + 1
    return {"changes": changes, "totals": totals,
            "unchanged": unchanged, "segments": len(segs),
            "matched": bool(single) or any(
                ch["kind"] in ("joined", "split") for ch in changes)}


def track_counts(tracks):
    """Items per track beyond V1/A1, split media vs generated (pure)."""
    out = []
    for t in tracks:
        if t["index"] == 1 or not t["items"]:
            continue
        out.append({"track": f"{t['kind'][0]}{t['index']}", "name": t["name"],
                    "media": sum(1 for i in t["items"] if i["kind"] == "media"),
                    "generated": sum(1 for i in t["items"]
                                     if i["kind"] == "generated")})
    return out


def mmss(t):
    t = max(0.0, float(t))
    return f"{int(t // 60)}:{t % 60:04.1f}"


def to_markdown(result, extra, edl_runtime, timeline_runtime, label):
    lines = [f"# Resolve edits: {label}", "",
             f"EDL: {result['segments']} segments, {mmss(edl_runtime)}. "
             f"Finished timeline track 1: {mmss(timeline_runtime)}. "
             f"Unchanged segments: {result['unchanged']}.", ""]
    if result["totals"]:
        lines.append("Changes: " + ", ".join(
            f"{v} {k}" for k, v in sorted(result["totals"].items())) + ".")
    else:
        lines.append("No changes to the cut on track 1.")
    lines += ["", "| change | EDL segment | source span | timeline | detail |",
              "|---|---|---|---|---|"]
    def order(ch):
        first = ch.get("segment", (ch.get("segments") or [1e9])[0])
        return (first, ch.get("rec", 0.0))

    for ch in sorted(result["changes"], key=order):
        seg = ch.get("segment", ",".join(map(str, ch.get("segments", []))))
        src = ch.get("src")
        span = f"{mmss(src[0])}-{mmss(src[1])}" if src else ""
        rec = mmss(ch["rec"]) if "rec" in ch else ""
        if ch["kind"] in ("trimmed", "extended", "trimmed+extended"):
            detail = f"head {ch['head']:+.2f}s, tail {ch['tail']:+.2f}s (+ is trimmed)"
        elif ch["kind"] == "joined":
            detail = f"restored {ch['seconds']:.2f}s the cut removed"
        elif ch["kind"] == "split":
            detail = f"{ch['pieces']} pieces, {ch['seconds']:.2f}s removed inside"
        elif ch["kind"] in ("removed", "added"):
            detail = f"{ch['seconds']:.2f}s" + (
                f" from {ch['source']}" if ch.get("source") else "")
        else:
            detail = "out of EDL order"
        lines.append(f"| {ch['kind']} | {seg} | {span} | {rec} | {detail} |")
    lines += ["", "## Other tracks", ""]
    if extra:
        lines += ["| track | name | media | generated |", "|---|---|---|---|"]
        lines += [f"| {e['track']} | {e['name']} | {e['media']} | "
                  f"{e['generated']} |" for e in extra]
    else:
        lines.append("None: the creator added no tracks above V1 and A1.")
    return "\n".join(lines) + "\n"


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
        return None, f"Resolve project {name!r} does not exist"
    return project, None


def export_from_resolve(project_name, timeline_name, out_path):
    """Export the timeline as OTIO; returns (timeline name, error)."""
    resolve = resolve_api.connect()
    pm = resolve.GetProjectManager()
    project, problem = open_project(pm, project_name)
    if not project:
        return None, problem
    timeline = None
    if timeline_name:
        for i in range(project.GetTimelineCount()):
            tl = project.GetTimelineByIndex(i + 1)
            if tl.GetName() == timeline_name:
                timeline = tl
    else:
        timeline = project.GetCurrentTimeline()
    if not timeline:
        names = [project.GetTimelineByIndex(i + 1).GetName()
                 for i in range(project.GetTimelineCount())]
        return None, (f"timeline {timeline_name or '(current)'!r} not found; "
                      f"project timelines: {names}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Resolve resolves a relative path against its own working directory.
    if not timeline.Export(str(out_path.resolve()), resolve.EXPORT_OTIO,
                           resolve.EXPORT_NONE):
        return None, f"Resolve refused to export {timeline.GetName()!r} as OTIO"
    return timeline.GetName(), None


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--edl", required=True, help="path to cut/edl.json")
    p.add_argument("--project", help="Resolve project name (the video slug)")
    p.add_argument("--timeline", default=None,
                   help="timeline to diff (default: the current timeline)")
    p.add_argument("--export", default="retro/final-timeline.otio",
                   help="where to write the OTIO export")
    p.add_argument("--otio", default=None,
                   help="diff this OTIO file instead of exporting from Resolve")
    p.add_argument("-o", "--output", required=True,
                   help="markdown report path (retro/resolve-diff.md)")
    p.add_argument("--json", default=None, help="optional JSON report path")
    args = p.parse_args(argv)
    if not args.otio and not args.project:
        print("resolve_diff: give --project (export from Resolve) or --otio",
              file=sys.stderr)
        return 2
    try:
        edl = json.loads(Path(args.edl).read_text(encoding="utf-8"))
        segments = edl["segments"]
    except (OSError, ValueError, KeyError) as e:
        print(f"resolve_diff: cannot read {args.edl}: {e}", file=sys.stderr)
        return 2

    label = args.otio
    otio_path = Path(args.otio) if args.otio else Path(args.export)
    if not args.otio:
        try:
            label, problem = export_from_resolve(args.project, args.timeline,
                                                 otio_path)
        except resolve_api.ResolveUnavailable as e:
            print(f"resolve_diff: {e} Or export the timeline as OTIO by hand "
                  "and re-run with --otio.", file=sys.stderr)
            return resolve_api.UNAVAILABLE_EXIT
        if problem:
            print(f"resolve_diff: {problem}", file=sys.stderr)
            return 1

    try:
        doc = json.loads(otio_path.read_text(encoding="utf-8"))
        tracks = parse_otio(doc)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"resolve_diff: cannot parse {otio_path}: {e}", file=sys.stderr)
        return 1
    v1 = next((t for t in tracks if t["kind"] == "Video" and t["index"] == 1),
              None)
    if v1 is None:
        print("resolve_diff: the timeline has no video track", file=sys.stderr)
        return 1
    rate = (doc.get("global_start_time") or {}).get("rate") or \
        edl.get("frame_rate") or 30.0
    result = diff(segments, v1["items"], float(rate))
    if not result["matched"]:
        print("resolve_diff: no clip on video track 1 uses the EDL's sources. "
              "This is not the video's timeline; name the right one with "
              "--timeline.", file=sys.stderr)
        return 1
    extra = track_counts(tracks)
    edl_runtime = sum(float(s["end"]) - float(s["start"]) for s in segments)
    tl_runtime = max((i["rec_end"] for i in v1["items"]), default=0.0)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_markdown(result, extra, edl_runtime, tl_runtime, label),
                   encoding="utf-8")
    report = {"timeline": label, "otio": str(otio_path), **result,
              "other_tracks": extra, "edl_runtime": round(edl_runtime, 3),
              "timeline_runtime": round(tl_runtime, 3)}
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n",
                                   encoding="utf-8")
    print(json.dumps({k: report[k] for k in
                      ("timeline", "segments", "unchanged", "totals",
                       "other_tracks", "edl_runtime", "timeline_runtime")},
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
