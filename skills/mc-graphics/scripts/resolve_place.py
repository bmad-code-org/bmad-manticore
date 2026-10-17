#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Place the graphics on the cut in DaVinci Resolve Studio and prove they landed.

Usage:
    uv run {skill-root}/scripts/resolve_place.py --project <slug> \
        --import-report cut/resolve-import.json --beats beats/beats.md \
        --graphics graphics [--titles graphics/titles.json] \
        [--title-template "Text+"] [-o graphics/resolve-place.json]

Contract:
    input   the beat table (start and dur are law), the rendered overlays in
            --graphics named <beat id>.mov/.webm/.mp4/.mkv, and optionally
            graphics/titles.json: a list of Fusion title beats,
                [{"id": "b02-guest-lt", "text": "Marlon Vidal\\nEngineer",
                  "font": "Inter", "style": "Bold", "color": "#FFFFFF",
                  "size": 0.06,
                  "center": [0.25, 0.15]}]
            where every key after "text" is optional and only applied to
            Resolve's own "Text+" template ("style" defaults to "Regular"
            when "font" is set: Text+ keeps its previous style otherwise, and a
            font without that style fails at render time). The cut timeline is the one named
            in --import-report (written by mc-cut's resolve_import.py), which
            must report "ok": true.
    does    connects to a running Resolve Studio and opens --project (never
            creates one, never saves or leaves a project holding other work).
            Duplicates the cut timeline as "<slug> graphics[ vN]", so the cut
            and every earlier graphics timeline stay untouched, and adds new
            video tracks above the cut's:
              - overlays: imported once into a "Manticore Graphics" bin and
                appended at their beat frames, packed into the fewest
                non-overlapping tracks ("Manticore graphics N")
              - titles: built on a separate "<slug> titles[ vN]" timeline,
                one per beat with mark in/out, then nested onto the graphics
                timeline on its own top track ("Manticore titles"). Titles are
                never inserted on the cut timeline: Resolve inserts them on V1
                and overwrites the cut.
    verify  every overlay's track, start, duration and media; every title's
            start, duration and text; the titles track enabled; a three-frame
            test render of every title, which must complete (a title whose Fusion comp cannot
            render reads back fine and fails only here; the test files go to
            a temp folder and are deleted, and the project's render format is
            restored); the nested titles clip's start, length and source in
            point, which must cover the whole titles timeline; and the
            graphics timeline's V1 still identical to the cut's. Any mismatch
            exits 1 and renames the graphics timeline with a
            " (FAILED VERIFY)" suffix.
    output  -o report JSON; json.dumps summary on stdout either way.

Frames: timeline frame = timeline start + round(beat seconds * fps).
Overlays must be video files; a PNG cannot be given a length through the API.

Exit codes: 0 placed and verified, 1 Resolve-side failure or verify mismatch,
2 usage or plan error (missing overlay, overlapping titles, a beat that is
both a title and a rendered file), 3 the title template was not found (the
overlays are placed and verified; titles are not), 4 Resolve unavailable.

STATUS: implemented (pure logic covered by
scripts/tests/test-resolve_place.py).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve_api

BIN_NAME = "Manticore Graphics"
FAILED_SUFFIX = " (FAILED VERIFY)"
OVERLAY_EXTS = (".mov", ".webm", ".mp4", ".mkv")
TEMPLATE_MISSING_EXIT = 3


# --- beat table (a copy of mc-cut/scripts/composite_core.py's parser, per the
# script-duplication convention) -------------------------------------------


def parse_seconds(text):
    """'90', '90.5', '12.5s', '1:30', '01:02:03.25' -> seconds (pure)."""
    s = str(text).strip()
    if s.endswith("s") and ":" not in s:
        s = s[:-1]
    if not s:
        raise ValueError("empty time")
    total = 0.0
    for part in s.split(":"):
        total = total * 60 + float(part)
    return total


def parse_beats(text):
    """Beat rows {id, start, dur, type, asset} and skipped reasons (pure)."""
    header = None
    col = {}
    beats, skipped = [], []
    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        low = [c.lower() for c in cells]
        if header is None:
            if "id" in low and "start" in low:
                header = low
                col = {name: i for i, name in enumerate(low)}
            continue
        if set("".join(cells)) <= set("-: "):
            continue

        def cell(name):
            i = col.get(name)
            return cells[i] if i is not None and i < len(cells) else ""

        bid = cell("id")
        if not bid:
            skipped.append("row with empty id")
            continue
        try:
            start = parse_seconds(cell("start"))
            if cell("dur"):
                dur = parse_seconds(cell("dur"))
            else:
                dur = parse_seconds(cell("end")) - start
        except ValueError:
            skipped.append(f"{bid}: unparseable start/dur")
            continue
        if dur <= 0:
            skipped.append(f"{bid}: no usable dur")
            continue
        asset = cell("asset")
        beats.append({"id": bid, "start": start, "dur": dur,
                      "type": cell("type").lower() or "overlay",
                      "asset": None if asset.lower() in
                      ("", "null", "none", "-") else asset})
    return beats, skipped


# --- plan (pure) -------------------------------------------------------------


def to_frames(seconds, fps):
    return int(round(float(seconds) * float(fps)))


def find_overlay(graphics_dir, bid):
    """The rendered file for a beat id, or None (reads the folder)."""
    for ext in OVERLAY_EXTS:
        cand = Path(graphics_dir) / f"{bid}{ext}"
        if cand.is_file():
            return cand
    return None


def pack_lanes(entries):
    """Fewest non-overlapping lanes over {start, dur} frame entries (pure)."""
    lanes = []
    for e in sorted(entries, key=lambda x: (x["start"], x["dur"])):
        for lane in lanes:
            last = lane[-1]
            if last["start"] + last["dur"] <= e["start"]:
                lane.append(e)
                break
        else:
            lanes.append([e])
    return lanes


def hex_rgb(color):
    """'#RRGGBB' -> (r, g, b) floats 0..1 (pure)."""
    c = str(color).lstrip("#")
    if len(c) != 6:
        raise ValueError(f"color {color!r} is not #RRGGBB")
    return tuple(int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def validate_titles(titles, beats_by_id):
    """Problems with a titles.json list (pure)."""
    problems = []
    if not isinstance(titles, list):
        return ["titles.json must be a list of title objects"]
    seen = set()
    for t in titles:
        bid = t.get("id") if isinstance(t, dict) else None
        if not bid or bid not in beats_by_id:
            problems.append(f"title {bid!r} is not a beat id in the table")
            continue
        if bid in seen:
            problems.append(f"title {bid!r} is listed twice")
        seen.add(bid)
        if not str(t.get("text", "")).strip():
            problems.append(f"title {bid!r} has no text")
        if "color" in t:
            try:
                hex_rgb(t["color"])
            except ValueError as e:
                problems.append(f"title {bid!r}: {e}")
        if "style" in t and "font" not in t:
            problems.append(f"title {bid!r}: style needs a font")
        if "center" in t and (not isinstance(t["center"], list)
                              or len(t["center"]) != 2):
            problems.append(f"title {bid!r}: center must be [x, y]")
    return problems


def build_plan(beats, title_list, overlay_files, fps):
    """Frame plan for overlays and titles, plus plan errors (pure).

    `overlay_files` maps beat id -> path for every beat with a rendered file.
    A beat is a title when titles.json lists it, an overlay when it has a
    file, and missing when it has neither.
    """
    errors = []
    title_ids = {t["id"] for t in title_list}
    overlays, titles, missing = [], [], []
    by_id = {b["id"]: b for b in beats}
    for b in beats:
        frames = {"start": to_frames(b["start"], fps),
                  "dur": to_frames(b["dur"], fps)}
        if b["id"] in title_ids:
            if b["id"] in overlay_files:
                errors.append(f"beat {b['id']} is both a title in "
                              "titles.json and a rendered file; keep one")
            continue
        if b["id"] in overlay_files:
            path = str(overlay_files[b["id"]])
            if path.lower().endswith(".png"):
                errors.append(f"beat {b['id']}: PNG overlays cannot be "
                              "given a length in Resolve; render a MOV")
                continue
            overlays.append({"id": b["id"], "path": path, **frames})
        else:
            missing.append(b["id"])
    for t in title_list:
        b = by_id[t["id"]]
        titles.append({**t, "start": to_frames(b["start"], fps),
                       "dur": to_frames(b["dur"], fps)})
    titles.sort(key=lambda t: t["start"])
    for a, b in zip(titles, titles[1:]):
        if a["start"] + a["dur"] > b["start"]:
            errors.append(f"titles {a['id']} and {b['id']} overlap; one "
                          "title track cannot hold both")
    if missing:
        errors.append("no rendered file and no title for: "
                      + ", ".join(missing))
    lanes = pack_lanes(overlays)
    return {"lanes": lanes, "titles": titles}, errors


def unique_name(wanted, existing):
    """`wanted`, or `wanted vN` for the first N >= 2 not taken (pure)."""
    if wanted not in existing:
        return wanted
    n = 2
    while f"{wanted} v{n}" in existing:
        n += 1
    return f"{wanted} v{n}"


def frames_to_tc(frame, fps):
    """Absolute frame -> non-drop HH:MM:SS:FF timecode (pure)."""
    rate = int(round(float(fps)))
    frame = int(frame)
    ff = frame % rate
    s = frame // rate
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{ff:02d}"


def compare_overlays(lanes, placed, base_track, tl_start):
    """Mismatches between planned lanes and placed items (pure).

    `placed` maps track index -> list of {start, duration, name} as read from
    the timeline.
    """
    problems = []
    for k, lane in enumerate(lanes):
        track = base_track + k
        got = {round(i["start"]) - tl_start: i for i in placed.get(track, [])}
        if len(placed.get(track, [])) != len(lane):
            problems.append({"what": "overlay count", "track": track,
                             "expected": len(lane),
                             "got": len(placed.get(track, []))})
        for e in lane:
            item = got.get(e["start"])
            if item is None:
                problems.append({"what": "overlay missing", "id": e["id"],
                                 "track": track, "start": e["start"]})
                continue
            if round(item["duration"]) != e["dur"]:
                problems.append({"what": "overlay duration", "id": e["id"],
                                 "expected": e["dur"],
                                 "got": round(item["duration"])})
            if item.get("name") and Path(e["path"]).name != item["name"]:
                problems.append({"what": "overlay media", "id": e["id"],
                                 "expected": Path(e["path"]).name,
                                 "got": item["name"]})
    return problems


def compare_titles(titles, placed, tt_start):
    """Mismatches between planned titles and the titles timeline (pure)."""
    problems = []
    got = {round(i["start"]) - tt_start: i for i in placed}
    if len(placed) != len(titles):
        problems.append({"what": "title count", "expected": len(titles),
                         "got": len(placed)})
    for t in titles:
        item = got.get(t["start"])
        if item is None:
            problems.append({"what": "title missing", "id": t["id"],
                             "start": t["start"]})
            continue
        if round(item["duration"]) != t["dur"]:
            problems.append({"what": "title duration", "id": t["id"],
                             "expected": t["dur"],
                             "got": round(item["duration"])})
        if item.get("text") != t["text"]:
            problems.append({"what": "title text", "id": t["id"],
                             "expected": t["text"], "got": item.get("text")})
    return problems


# --- Resolve side ------------------------------------------------------------


def _track_items(tl, track):
    return tl.GetItemListInTrack("video", track) or []


def _v1_signature(tl):
    start = tl.GetStartFrame()
    return [(round(i.GetStart()) - start, round(i.GetDuration()),
             round(i.GetLeftOffset())) for i in _track_items(tl, 1)]


def _find_bin(mp):
    root = mp.GetRootFolder()
    for f in root.GetSubFolderList() or []:
        if f.GetName() == BIN_NAME:
            return f
    return mp.AddSubFolder(root, BIN_NAME)


def _import_overlays(mp, folder, paths):
    """Media pool items keyed by absolute path, importing only new files."""
    have = {}
    for clip in folder.GetClipList() or []:
        have[clip.GetClipProperty("File Path")] = clip
    mp.SetCurrentFolder(folder)
    new = [p for p in paths if p not in have]
    if new:
        for clip in mp.ImportMedia(new) or []:
            have[clip.GetClipProperty("File Path")] = clip
    return have


def _first_textplus(item):
    comp = item.GetFusionCompByIndex(1)
    if not comp:
        return None
    tools = comp.GetToolList(False, "TextPlus") or {}
    return next(iter(tools.values()), None)


def _style_title(tool, title, template):
    if template != "Text+":
        return
    if "font" in title:
        tool.SetInput("Font", title["font"])
        tool.SetInput("Style", title.get("style") or "Regular")
    if "color" in title:
        r, g, b = hex_rgb(title["color"])
        tool.SetInput("Red1", r)
        tool.SetInput("Green1", g)
        tool.SetInput("Blue1", b)
    if "size" in title:
        tool.SetInput("Size", float(title["size"]))
    if "center" in title:
        x, y = title["center"]
        tool.SetInput("Center", {1: float(x), 2: float(y)})


def _build_titles(mp, project, titles, name, template, fps):
    """Create the titles timeline; returns (timeline, error)."""
    tt = mp.CreateEmptyTimeline(name)
    if not tt:
        return None, f"could not create timeline {name!r}"
    project.SetCurrentTimeline(tt)
    start = tt.GetStartFrame()
    for t in titles:
        tt.SetMarkInOut(t["start"], t["start"] + t["dur"] - 1)
        tt.SetCurrentTimecode(frames_to_tc(start + t["start"], fps))
        item = tt.InsertFusionTitleIntoTimeline(template)
        tt.ClearMarkInOut()
        if not item:
            return tt, "template"
        tool = _first_textplus(item)
        if tool is None:
            return tt, (f"title template {template!r} has no Text+ tool to "
                        "carry the text")
        tool.SetInput("StyledText", t["text"])
        _style_title(tool, t, template)
    return tt, None


def _render_check(resolve, project, tt, titles):
    """Titles whose Fusion comp fails a three-frame test render."""
    import tempfile
    import time
    page = resolve.GetCurrentPage()
    previous = project.GetCurrentRenderFormatAndCodec() or {}
    project.SetCurrentTimeline(tt)
    resolve.OpenPage("deliver")
    project.SetCurrentRenderFormatAndCodec("mp4", "H264")
    start = tt.GetStartFrame()
    failed = []
    with tempfile.TemporaryDirectory() as tmp:
        for t in titles:
            mid = start + t["start"] + t["dur"] // 2
            project.SetRenderSettings({
                "SelectAllFrames": False, "MarkIn": mid, "MarkOut": mid + 2,
                "TargetDir": tmp, "CustomName": f"check-{t['id']}",
                "ExportVideo": True, "ExportAudio": False})
            job = project.AddRenderJob()
            if not job or not project.StartRendering([job], False):
                failed.append({"what": "title render", "id": t["id"],
                               "error": "render did not start"})
                continue
            deadline = time.monotonic() + 60
            while project.IsRenderingInProgress() \
                    and time.monotonic() < deadline:
                time.sleep(0.5)
            status = project.GetRenderJobStatus(job) or {}
            project.DeleteRenderJob(job)
            if status.get("JobStatus") != "Complete":
                failed.append({"what": "title render", "id": t["id"],
                               "error": status.get("Error")
                               or status.get("JobStatus")})
    # Render MarkIn/MarkOut become the timeline's marks, and AppendToTimeline
    # of a nested timeline honours them.
    tt.ClearMarkInOut()
    if previous.get("format") and previous.get("codec"):
        project.SetCurrentRenderFormatAndCodec(previous["format"],
                                               previous["codec"])
    if page:
        resolve.OpenPage(page)
    return failed


def _read_titles(tt):
    out = []
    for item in _track_items(tt, 1):
        tool = _first_textplus(item)
        out.append({"start": item.GetStart(), "duration": item.GetDuration(),
                    "text": tool.GetInput("StyledText") if tool else None})
    return out


def _open_project(pm, name):
    current = pm.GetCurrentProject()
    if current and current.GetName() == name:
        return current, None
    if current:
        root = current.GetMediaPool().GetRootFolder()
        if current.GetTimelineCount() or root.GetClipList() \
                or root.GetSubFolderList():
            return None, (f"Resolve has project {current.GetName()!r} open "
                          "with work in it. Save and close it, or open "
                          f"{name!r}, then re-run.")
    project = pm.LoadProject(name)
    if not project:
        return None, (f"Resolve has no project {name!r}; import the cut with "
                      "mc-cut first")
    return project, None


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True, help="Resolve project (slug)")
    p.add_argument("--import-report", required=True,
                   help="cut/resolve-import.json from mc-cut")
    p.add_argument("--beats", required=True, help="beats/beats.md")
    p.add_argument("--graphics", required=True, help="graphics/ folder")
    p.add_argument("--titles", default=None, help="graphics/titles.json")
    p.add_argument("--title-template", default="Text+",
                   help='Fusion title template name (default "Text+")')
    p.add_argument("-o", "--output", default=None, help="report JSON path")
    args = p.parse_args(argv)

    try:
        report_in = json.loads(Path(args.import_report).read_text("utf-8"))
        beats, skipped = parse_beats(Path(args.beats).read_text("utf-8"))
        title_list = (json.loads(Path(args.titles).read_text("utf-8"))
                      if args.titles else [])
    except (OSError, ValueError) as e:
        print(f"resolve_place: cannot read inputs: {e}", file=sys.stderr)
        return 2
    if not report_in.get("ok") or not report_in.get("timeline"):
        print("resolve_place: the import report does not show a verified "
              "cut timeline; re-run mc-cut's Resolve import first",
              file=sys.stderr)
        return 2
    by_id = {b["id"]: b for b in beats}
    problems = validate_titles(title_list, by_id)
    if title_list and not args.title_template:
        problems.append("titles.json lists titles but the title template is "
                        "empty, which turns Fusion titles off")
    if skipped:
        problems.extend(f"beat table: {s}" for s in skipped)
    if problems:
        print("resolve_place: " + "; ".join(problems), file=sys.stderr)
        return 2
    title_ids = {t["id"] for t in title_list}
    overlay_files = {}
    for b in beats:
        f = find_overlay(args.graphics, b["id"])
        if f is None and b["id"] not in title_ids:
            png = Path(args.graphics) / f"{b['id']}.png"
            f = png if png.is_file() else None
        if f is not None:
            overlay_files[b["id"]] = f.resolve()

    try:
        resolve = resolve_api.connect()
    except resolve_api.ResolveUnavailable as e:
        print(f"resolve_place: {e}", file=sys.stderr)
        return resolve_api.UNAVAILABLE_EXIT

    pm = resolve.GetProjectManager()
    project, problem = _open_project(pm, args.project)
    if not project:
        print(f"resolve_place: {problem}", file=sys.stderr)
        return 1
    timelines = {project.GetTimelineByIndex(i + 1).GetName():
                 project.GetTimelineByIndex(i + 1)
                 for i in range(project.GetTimelineCount())}
    cut = timelines.get(report_in["timeline"])
    if not cut:
        print(f"resolve_place: no timeline {report_in['timeline']!r} in "
              f"project {args.project!r}", file=sys.stderr)
        return 1
    fps = float((cut.GetSettings() or {}).get("timelineFrameRate")
                or (project.GetSettings() or {}).get("timelineFrameRate"))
    plan, errors = build_plan(beats, title_list, overlay_files, fps)
    if errors:
        print("resolve_place: " + "; ".join(errors), file=sys.stderr)
        return 2

    mp = project.GetMediaPool()
    folder = _find_bin(mp)
    paths = [e["path"] for lane in plan["lanes"] for e in lane]
    media = _import_overlays(mp, folder, paths)
    for lane in plan["lanes"]:
        for e in lane:
            clip = media.get(e["path"])
            if clip is None:
                print(f"resolve_place: Resolve did not import {e['path']}",
                      file=sys.stderr)
                return 1
            frames = int(float(clip.GetClipProperty("Frames") or 0))
            if frames and frames < e["dur"]:
                print(f"resolve_place: {Path(e['path']).name} is {frames} "
                      f"frames but beat {e['id']} needs {e['dur']}; "
                      "re-render it to the beat's dur", file=sys.stderr)
                return 1

    cut_v1 = _v1_signature(cut)
    slug = args.project
    gname = unique_name(f"{slug} graphics", set(timelines))
    project.SetCurrentTimeline(cut)
    gtl = cut.DuplicateTimeline(gname)
    if not gtl:
        print("resolve_place: could not duplicate the cut timeline",
              file=sys.stderr)
        return 1
    project.SetCurrentTimeline(gtl)
    tl_start = gtl.GetStartFrame()
    base = gtl.GetTrackCount("video") + 1
    for k in range(len(plan["lanes"])):
        gtl.AddTrack("video")
        gtl.SetTrackName("video", base + k, f"Manticore graphics {k + 1}")
    for k, lane in enumerate(plan["lanes"]):
        infos = [{"mediaPoolItem": media[e["path"]], "startFrame": 0,
                  "endFrame": e["dur"], "trackIndex": base + k,
                  "recordFrame": tl_start + e["start"], "mediaType": 1}
                 for e in lane]
        mp.AppendToTimeline(infos)

    placed = {base + k: [{"start": i.GetStart(), "duration": i.GetDuration(),
                          "name": i.GetName()}
                         for i in _track_items(gtl, base + k)]
              for k in range(len(plan["lanes"]))}
    mismatches = compare_overlays(plan["lanes"], placed, base, tl_start)

    title_state, tname, title_track = "none", None, None
    if plan["titles"]:
        tname = unique_name(f"{slug} titles", set(timelines) | {gname})
        mp.SetCurrentFolder(folder)
        tt, err = _build_titles(mp, project, plan["titles"], tname,
                                args.title_template, fps)
        if err == "template":
            title_state = "template-missing"
            if tt:
                mp.DeleteTimelines([tt])
            tname = None
        elif err:
            print(f"resolve_place: {err}", file=sys.stderr)
            mismatches.append({"what": "titles", "error": err})
            title_state = "failed"
        else:
            mismatches += compare_titles(plan["titles"], _read_titles(tt),
                                         tt.GetStartFrame())
            mismatches += _render_check(resolve, project, tt, plan["titles"])
            tt.SetTrackEnable("video", 1, True)
            if not tt.GetIsTrackEnabled("video", 1):
                mismatches.append({"what": "titles track disabled"})
            length = tt.GetEndFrame() - tt.GetStartFrame()
            project.SetCurrentTimeline(gtl)
            title_track = gtl.GetTrackCount("video") + 1
            gtl.AddTrack("video")
            gtl.SetTrackName("video", title_track, "Manticore titles")
            mp.AppendToTimeline([{"mediaPoolItem": tt.GetMediaPoolItem(),
                                  "startFrame": 0, "endFrame": length,
                                  "trackIndex": title_track,
                                  "recordFrame": tl_start, "mediaType": 1}])
            got = [(round(i.GetStart()) - tl_start, round(i.GetDuration()),
                    round(i.GetLeftOffset()))
                   for i in _track_items(gtl, title_track)]
            if got != [(0, length, 0)]:
                mismatches.append({"what": "nested titles clip",
                                   "expected": [(0, length, 0)],
                                   "got": got})
            title_state = "placed"

    project.SetCurrentTimeline(gtl)
    if _v1_signature(gtl) != cut_v1 or _v1_signature(cut) != cut_v1:
        mismatches.append({"what": "V1 changed",
                           "detail": "the cut on V1 no longer matches"})
    if mismatches:
        gtl.SetName(gname + FAILED_SUFFIX)
        gname += FAILED_SUFFIX
    pm.SaveProject()

    report = {"ok": not mismatches and title_state != "template-missing",
              "project": slug, "cut_timeline": report_in["timeline"],
              "timeline": gname, "titles_timeline": tname,
              "fps": fps, "overlay_tracks": len(plan["lanes"]),
              "overlays": sum(len(lane) for lane in plan["lanes"]),
              "titles": len(plan["titles"]), "titles_state": title_state,
              "title_track": title_track,
              "mismatches": mismatches[:50],
              "mismatch_count": len(mismatches)}
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "mismatches"},
                     indent=2))
    if mismatches:
        print(f"\nRESOLVE GRAPHICS OUT OF PLACE: {len(mismatches)} "
              f"mismatch(es), first: {mismatches[0]}. The timeline is "
              f"renamed {gname!r}; do not edit on it.", file=sys.stderr)
        return 1
    if title_state == "template-missing":
        print(f"\nTITLE TEMPLATE NOT FOUND: Resolve has no Fusion title "
              f"named {args.title_template!r}. The overlays are placed on "
              f"{gname!r}; the titles were not built. Nothing was "
              "substituted.", file=sys.stderr)
        return TEMPLATE_MISSING_EXIT
    return 0


if __name__ == "__main__":
    sys.exit(main())
