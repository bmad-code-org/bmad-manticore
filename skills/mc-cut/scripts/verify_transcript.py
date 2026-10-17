#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Transcript completeness gate: prove the transcript is not missing speech.

Usage:
    uv run {skill-root}/scripts/verify_transcript.py \
        {projects-path}/<slug>/transcript/words.json \
        --audio-map {projects-path}/<slug>/cut/audio-map.json \
        [--wpm 198] [-o {projects-path}/<slug>/cut/transcript-check.json]

Why this exists:
    On the first real project (2026-07-24) the transcriber silently dropped
    three whole paragraphs of clearly-spoken content. No error, no warning.
    Everything downstream ran on the hole: candidates, EDL, cutplan, render,
    and a passed gate 2. The dropped regions looked like dead air to the
    cutter, which nearly deleted three paragraphs of the actual video.

    Nothing in the pipeline ever checked. This script is that check, and it
    RUNS BEFORE CANDIDATE DETECTION. It exits non-zero, and a non-zero exit
    means stop: do not cut against this transcript.

The checks, in order of what actually catches things:

  1. COVERAGE SCAN (the decisive one, exit 1 on failure)
     Audio that is above the silence floor but has no transcribed words is
     DROPPED SPEECH. Computed as the complement of (silence + word spans):
     anything left over is audible, un-transcribed, and therefore missing.
     Contiguous leftovers are clustered and any cluster at or above
     --min-drop seconds fails the gate, reported with timecodes so the
     region can be re-transcribed in isolation and spliced.

     This check is deliberately built from the AUDIO side, not from word
     gaps. Word gaps cannot be trusted: parakeet absorbs pauses into the
     preceding word's end, so a gap across real silence reads about 0.0 (see
     analyze_audio.py). A check that scanned word gaps would inherit exactly
     the bug it is meant to catch.

  2. WORD-RATE SANITY (exit 1 on failure, but read the caveat)
     Effective words per minute over SPEECH time (dead air excluded, which
     wall-clock rate does not do) against the creator's configured wpm.

     CAVEAT, measured on the real failure: this check would NOT have caught
     it. The broken transcript had 3,446 words against a good 3,546, a 3
     percent deficit, because three missing paragraphs are a small fraction
     of a 20 minute take. Against speech time it reads 224 wpm versus 231
     for the good one; against wall-clock, 168 versus 173. Either way it
     sails past any sane floor. The original bug report proposed a 55
     percent floor and that would have passed the broken transcript too.

     So this check is real but narrow: it catches CATASTROPHIC failure (a
     lane that returned half a file, a wrong-language model, a silent
     truncation) and nothing subtler. Check 1 is the one that catches
     dropped paragraphs. Do not let this check's presence create confidence
     that check 1 is optional.

  3. VERBATIM FILLERS (exit 1 on failure)
     Cutting depends on verbatim fillers ("um", "uh"): they mark the stumbles
     and restarts the cut removes. A transcriber that normalizes them away
     passes checks 1 and 2, because no speech is lost, only rewritten. Tested
     2026-10-16 on a 7 minute take: parakeet kept 6 fillers in 190s of
     speech; Resolve Studio 21.1's transcription kept none and passed checks
     1 and 2. So a transcript with zero fillers over at least
     --filler-min-speech seconds of speech fails. A tightly scripted read can
     genuinely have none; the creator signs that off with
     --accept-no-fillers "<reason>", recorded in the report.

  4. CONFIDENCE CLUSTERS (advisory, never fails the gate)
     A run of near-zero-confidence words is a soft signal to re-check that
     region by ear. Reported, never blocking. On the onnx lane confidence
     degrades to 1.0 when the runtime exposes no scores, so absence of this
     signal means nothing.

Contract:
    input     words.json from transcribe.py, plus audio-map.json from
              analyze_audio.py (both must describe the same media).
    output    optional -o report JSON: {"ok", "checks": {...},
              "dropped_regions": [...], "accepted": [...], "word_rate": {...},
              "low_confidence": [...]}
    summary   json.dumps on stdout either way.

Exit codes:
    0  transcript passes; safe to build candidates against
    1  transcript INCOMPLETE or implausible; do not proceed
    2  usage error (missing file, bad arguments, media mismatch)

Remediation when this fails: re-run the take (see transcribe.py's `chunking`
note for the measured window sizes), then re-verify.

When the finding is a FALSE POSITIVE, the escape valve:
    Not every audible span with no words is lost speech. A laugh, a music
    bed, an off-mic aside, or a long breath above the noise floor all read
    the same way to a coverage scan. So the creator can listen to a flagged
    region and sign it off:

        --accept-region 612.4-618.9 --reason "audience laugh, no speech"

    The acceptance must FULLY cover the flagged region, --reason is
    mandatory, and both land in the report, so the override is a recorded
    decision rather than a silent one. This is the same shape as preflight's
    --allow-qc-defects: the gate stays blocking by default, and the only way
    past it is a human saying why. A gate with no acknowledged override gets
    worked around in ways that leave no trace, which is worse.

STATUS: implemented (pure logic covered by
scripts/tests/test-verify_transcript.py).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze_audio as audio

# A cluster of audible-but-untranscribed audio at or above this many seconds
# is dropped speech.
#
# Calibrated against the real 20.5 minute take from 2026-07-24, sweeping this
# value over its known-good transcript (3,546 words) and its known-broken one
# (3,446 words, three paragraphs lost):
#
#   min-drop   false positives on good   real drops caught in broken
#   0.75       0                         5
#   1.00       0                         5
#   1.50       0                         4
#   2.50       0                         4
#   3.00       0                         3
#
# The good transcript produces ZERO false positives all the way down to
# 0.75s, because audio under the -30 dB floor (breaths, lip noise, room
# tone) is already classified as silence and never reaches this check. So
# the conservative 2.5s this shipped with bought no safety and cost
# sensitivity: it missed a real drop that 1.0s catches. 1.0s keeps a little
# headroom over the measured noise margin.
#
# Raise it for a noisy recording environment where room tone sits above the
# silence floor; the right fix there is usually a lower --noise in
# analyze_audio.py instead.
DEFAULT_MIN_DROP = 1.0
# Uncovered pieces closer together than this are one region for reporting; a
# dropped paragraph shows up as many short uncovered runs separated by the
# speaker's own natural pauses, and reporting them individually is noise.
DEFAULT_CLUSTER_GAP = 1.0
# Effective speech-time wpm must reach this fraction of the configured wpm.
# See the caveat in the module docstring: this is a catastrophe detector.
DEFAULT_WPM_FLOOR_RATIO = 0.6
# Zero fillers over this much speech means a normalizing transcriber.
DEFAULT_FILLER_MIN_SPEECH = 120.0
FILLERS = frozenset({"um", "umm", "uh", "uhh", "er", "erm", "ah", "hmm", "mm"})
DEFAULT_LOW_CONFIDENCE = 0.35
DEFAULT_LOW_CONFIDENCE_RUN = 5


def r2(x):
    return round(float(x), 2)


def tc(seconds):
    """Seconds to m:ss for human-readable region reports (pure)."""
    seconds = max(0.0, float(seconds))
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def word_spans(words):
    """Word intervals as (start, end) pairs (pure)."""
    return [(float(w["start"]), float(w["end"])) for w in words]


def uncovered_regions(silence, words, duration):
    """Audible spans with no transcribed words (pure).

    The complement of (silence + word spans) over [0, duration]. What is left
    is audio above the noise floor that produced no words, which is the
    definition of dropped speech. Merging the two lists first is what makes
    this immune to pause-absorbed word ends: an absorbed end simply covers a
    bit more, it cannot manufacture coverage of a genuinely empty region.
    """
    covered = audio._clean(list(silence) + word_spans(words), duration)
    return audio.complement(covered, duration)


def cluster(intervals, gap):
    """Merge intervals separated by less than `gap` (pure)."""
    out = []
    for start, end in intervals:
        if out and start - out[-1][1] < gap:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(s, e) for s, e in out]


def find_dropped(silence, words, duration, min_drop=DEFAULT_MIN_DROP,
                 cluster_gap=DEFAULT_CLUSTER_GAP):
    """Dropped-speech regions at or above min_drop seconds (pure).

    Returns a list of {start, end, dur, audible} records. `audible` is the
    seconds of actual uncovered audio inside the clustered region, which is
    less than `dur` when the cluster spans a short pause; the threshold is
    applied to `audible`, not to the cluster's wall span, so clustering can
    never inflate a region past the gate.
    """
    pieces = uncovered_regions(silence, words, duration)
    out = []
    for start, end in cluster(pieces, cluster_gap):
        audible = audio.overlap_seconds(pieces, start, end)
        if audible >= min_drop:
            out.append({
                "start": r2(start),
                "end": r2(end),
                "dur": r2(end - start),
                "audible": r2(audible),
                "at": tc(start),
            })
    return out


def word_rate(words, duration, speech_seconds, wpm=None,
              floor_ratio=DEFAULT_WPM_FLOOR_RATIO):
    """Effective word rates and the pass/fail verdict (pure).

    speech_wpm excludes dead air, which is why it is the one gated on: a take
    with five minutes of pauses is not a slow speaker. wall_wpm is reported
    for continuity with hand calculations but never gated.
    """
    n = len(words)
    wall_wpm = (n / (duration / 60.0)) if duration > 0 else 0.0
    speech_wpm = (n / (speech_seconds / 60.0)) if speech_seconds > 0 else 0.0
    result = {
        "words": n,
        "wall_wpm": r2(wall_wpm),
        "speech_wpm": r2(speech_wpm),
        "configured_wpm": wpm,
        "floor_ratio": floor_ratio,
    }
    if not wpm:
        result["ok"] = True
        result["note"] = ("no configured wpm supplied; rate check skipped "
                          "(pass --wpm from [owner] wpm in the studio config)")
        return result
    floor = wpm * floor_ratio
    result["floor_wpm"] = r2(floor)
    result["ok"] = speech_wpm >= floor
    return result


def filler_check(words, speech_seconds,
                 min_speech=DEFAULT_FILLER_MIN_SPEECH, accepted_reason=None):
    """Verbatim-filler verdict (pure). See check 3 in the module docstring."""
    count = sum(1 for w in words
                if str(w.get("word", "")).strip(" .,!?;:\"'").lower()
                in FILLERS)
    result = {"fillers": count, "speech_seconds": r2(speech_seconds),
              "min_speech": min_speech}
    if count or speech_seconds < min_speech:
        result["ok"] = True
    elif accepted_reason:
        result["ok"] = True
        result["accepted_reason"] = accepted_reason
    else:
        result["ok"] = False
    return result


def low_confidence_runs(words, threshold=DEFAULT_LOW_CONFIDENCE,
                        run_min=DEFAULT_LOW_CONFIDENCE_RUN):
    """Runs of consecutive low-confidence words (pure, advisory only)."""
    out = []
    run = []
    for w in words:
        if float(w.get("confidence", 1.0)) < threshold:
            run.append(w)
            continue
        if len(run) >= run_min:
            out.append(run)
        run = []
    if len(run) >= run_min:
        out.append(run)
    return [{
        "start": r2(r[0]["start"]),
        "end": r2(r[-1]["end"]),
        "words": len(r),
        "at": tc(r[0]["start"]),
        "text": " ".join(w["word"] for w in r)[:120],
    } for r in out]


# An audio map coarser than this cannot be trusted by the coverage scan.
# See map_too_coarse.
MAX_USABLE_MAP_GRANULARITY = 0.2


def map_too_coarse(audio_map, min_drop=DEFAULT_MIN_DROP):
    """Warning text when the audio map is too coarse to scan against (pure).

    The coverage scan calls anything that is neither silence nor a word
    "dropped speech". Natural inter-word gaps are 0.1 to 0.2s, so they are
    only correctly classified when the audio map actually CONTAINS silences
    that small. A map generated with a coarse --map-granularity omits them,
    they read as uncovered, and enough of them in a row cluster into a
    phantom dropped region. The gate would then fail a good transcript.

    analyze_audio.py defaults fine enough for this; the check exists because
    someone passing --map-granularity to speed that step up would otherwise
    get a confusing false failure rather than an explanation.

    `min_silence` is read as a fallback for maps written before the flag was
    renamed to say what it actually controls.
    """
    granularity = audio_map.get("map_granularity",
                                audio_map.get("min_silence"))
    if granularity is None or granularity <= MAX_USABLE_MAP_GRANULARITY:
        return None
    return (
        f"WARNING: the audio map was built with --map-granularity "
        f"{granularity}s, which is coarser than natural inter-word gaps. "
        f"Silences below {granularity}s are missing from it, so ordinary "
        "pauses between words can register as untranscribed audio and cluster "
        "into phantom dropped regions. Regenerate the map with "
        f"--map-granularity {MAX_USABLE_MAP_GRANULARITY} or finer before "
        "trusting a failure from this gate."
    )


def parse_region(text):
    """Parse a "START-END" acceptance region into (start, end) (pure).

    Raises ValueError on anything malformed, because a mistyped acceptance
    that silently covered nothing would look like the gate passing.
    """
    raw = str(text).strip()
    body = raw[1:] if raw.startswith("-") else raw
    parts = body.rsplit("-", 1)
    if len(parts) != 2:
        raise ValueError(f"expected START-END, got {text!r}")
    head = ("-" + parts[0]) if raw.startswith("-") else parts[0]
    start, end = float(head), float(parts[1])
    if end <= start:
        raise ValueError(f"region {text!r} does not end after it starts")
    return start, end


def apply_acceptances(dropped, accepted):
    """Split dropped regions into (still failing, accepted) (pure).

    A dropped region is only cleared when an acceptance FULLY covers it.
    Partial overlap leaves it failing: the creator signed off on what they
    listened to, and a region that extends past that is something else.
    """
    if not accepted:
        return list(dropped), []
    remaining, cleared = [], []
    for d in dropped:
        cover = next((a for a in accepted
                      if a["start"] <= d["start"] and d["end"] <= a["end"]),
                     None)
        if cover is None:
            remaining.append(d)
        else:
            record = dict(d)
            record["accepted_by"] = {"start": cover["start"],
                                     "end": cover["end"],
                                     "reason": cover["reason"]}
            cleared.append(record)
    return remaining, cleared


def build_report(transcript, audio_map, wpm=None, min_drop=DEFAULT_MIN_DROP,
                 cluster_gap=DEFAULT_CLUSTER_GAP,
                 floor_ratio=DEFAULT_WPM_FLOOR_RATIO, accepted=None,
                 filler_min_speech=DEFAULT_FILLER_MIN_SPEECH,
                 no_fillers_reason=None):
    """Run every check and assemble the verdict (pure)."""
    words = transcript.get("words", [])
    duration = float(audio_map["duration"])
    silence = audio.to_pairs(audio_map.get("silence", []))
    speech_seconds = float(audio_map.get("speech_seconds", 0.0))

    found = find_dropped(silence, words, duration, min_drop, cluster_gap)
    dropped, cleared = apply_acceptances(found, accepted)
    rate = word_rate(words, duration, speech_seconds, wpm, floor_ratio)
    low_conf = low_confidence_runs(words)
    fillers = filler_check(words, speech_seconds, filler_min_speech,
                           no_fillers_reason)

    checks = {
        "coverage": {
            "ok": not dropped,
            "dropped_regions": len(dropped),
            "dropped_seconds": r2(sum(d["audible"] for d in dropped)),
            "accepted_regions": len(cleared),
        },
        "word_rate": {"ok": rate["ok"]},
        "fillers": {"ok": fillers["ok"], "count": fillers["fillers"]},
        "confidence": {"ok": True, "low_confidence_runs": len(low_conf)},
    }
    return {
        "ok": (checks["coverage"]["ok"] and checks["word_rate"]["ok"]
               and checks["fillers"]["ok"]),
        "map_warning": map_too_coarse(audio_map, min_drop),
        "media": audio_map.get("media"),
        "duration": r2(duration),
        "speech_seconds": r2(speech_seconds),
        "checks": checks,
        "dropped_regions": dropped,
        "accepted": cleared,
        "word_rate": rate,
        "fillers": fillers,
        "low_confidence": low_conf,
    }


def _load(path, label):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"verify_transcript: cannot read {label} {path}: {e}",
              file=sys.stderr)
        return None


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("words", help="path to transcript/words.json")
    p.add_argument("--audio-map", required=True,
                   help="path to cut/audio-map.json (from analyze_audio.py)")
    p.add_argument("-o", "--output", default=None,
                   help="optional path to write the full report JSON")
    p.add_argument("--wpm", type=float, default=None,
                   help="creator's configured words per minute ([owner] wpm)")
    p.add_argument("--min-drop", type=float, default=DEFAULT_MIN_DROP,
                   help=f"seconds of audible-but-untranscribed audio that "
                        f"count as dropped speech (default {DEFAULT_MIN_DROP})")
    p.add_argument("--cluster-gap", type=float, default=DEFAULT_CLUSTER_GAP,
                   help=f"merge uncovered runs closer than this (default "
                        f"{DEFAULT_CLUSTER_GAP}s)")
    p.add_argument("--wpm-floor-ratio", type=float,
                   default=DEFAULT_WPM_FLOOR_RATIO,
                   help=f"fraction of configured wpm the speech-time rate "
                        f"must reach (default {DEFAULT_WPM_FLOOR_RATIO})")
    p.add_argument("--accept-region", action="append", metavar="START-END",
                   help="acknowledge a flagged region the creator has "
                        "LISTENED TO and confirmed carries no lost speech "
                        "(a laugh, a music bed, an off-mic aside). Repeatable. "
                        "Requires --reason. The acceptance must fully cover "
                        "the flagged region, and is recorded in the report")
    p.add_argument("--reason", default=None,
                   help="why the accepted regions are not dropped speech; "
                        "required with --accept-region so the override is "
                        "auditable rather than silent")
    p.add_argument("--filler-min-speech", type=float,
                   default=DEFAULT_FILLER_MIN_SPEECH,
                   help=f"seconds of speech after which zero fillers fails "
                        f"the gate (default {DEFAULT_FILLER_MIN_SPEECH})")
    p.add_argument("--accept-no-fillers", metavar="REASON", default=None,
                   help="the creator confirms the take genuinely has no "
                        "fillers (a tightly scripted read); the reason is "
                        "recorded in the report")
    args = p.parse_args(argv)
    if args.accept_no_fillers is not None and not args.accept_no_fillers.strip():
        print("verify_transcript: --accept-no-fillers needs a reason",
              file=sys.stderr)
        return 2

    accepted = []
    if args.accept_region:
        if not args.reason or not args.reason.strip():
            print("verify_transcript: --accept-region requires --reason. An "
                  "unexplained override of the completeness gate is exactly "
                  "the failure this gate exists to catch.", file=sys.stderr)
            return 2
        for text in args.accept_region:
            try:
                start, end = parse_region(text)
            except ValueError as e:
                print(f"verify_transcript: bad --accept-region: {e}",
                      file=sys.stderr)
                return 2
            accepted.append({"start": start, "end": end,
                             "reason": args.reason.strip()})
    elif args.reason:
        print("verify_transcript: --reason given with no --accept-region",
              file=sys.stderr)
        return 2

    transcript = _load(args.words, "transcript")
    if transcript is None:
        return 2
    audio_map = _load(args.audio_map, "audio map")
    if audio_map is None:
        return 2
    if "words" not in transcript:
        print("verify_transcript: transcript has no 'words' key",
              file=sys.stderr)
        return 2
    if "silence" not in audio_map or "duration" not in audio_map:
        print("verify_transcript: audio map missing 'silence'/'duration'; "
              "regenerate it with analyze_audio.py", file=sys.stderr)
        return 2

    report = build_report(transcript, audio_map, wpm=args.wpm,
                          min_drop=args.min_drop,
                          cluster_gap=args.cluster_gap,
                          floor_ratio=args.wpm_floor_ratio,
                          accepted=accepted,
                          filler_min_speech=args.filler_min_speech,
                          no_fillers_reason=(args.accept_no_fillers or "").strip()
                          or None)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")

    print(json.dumps({k: report[k] for k in
                      ("ok", "duration", "speech_seconds", "checks")},
                     indent=2))

    if report["map_warning"]:
        print(report["map_warning"], file=sys.stderr)

    if report["low_confidence"]:
        print(f"note: {len(report['low_confidence'])} low-confidence run(s); "
              "advisory only, listed in the report", file=sys.stderr)

    for a in report["accepted"]:
        print(f"note: flagged region at {a['at']} accepted by the creator "
              f"({a['accepted_by']['reason']})", file=sys.stderr)

    if report["fillers"].get("accepted_reason"):
        print("note: zero fillers accepted by the creator "
              f"({report['fillers']['accepted_reason']})", file=sys.stderr)

    if report["ok"]:
        return 0

    if not report["fillers"]["ok"]:
        print(f"\nTRANSCRIPT NOT VERBATIM: no fillers in "
              f"{report['fillers']['speech_seconds']}s of speech. The "
              "transcriber normalized them away, and the cut needs them. "
              "Re-transcribe with the reference lane (parakeet), or, if the "
              "creator confirms the take really has none, pass "
              '--accept-no-fillers "<reason>".', file=sys.stderr)
        if report["checks"]["coverage"]["ok"] and report["word_rate"]["ok"]:
            return 1


    print("\nTRANSCRIPT INCOMPLETE. Do NOT build candidates against it.",
          file=sys.stderr)
    for d in report["dropped_regions"]:
        print(f"  dropped speech at {d['at']} "
              f"({d['start']}s to {d['end']}s, {d['audible']}s audible, "
              "no transcribed words)", file=sys.stderr)
    if not report["word_rate"]["ok"]:
        wr = report["word_rate"]
        print(f"  word rate {wr['speech_wpm']} wpm over speech time is below "
              f"the {wr['floor_wpm']} floor "
              f"({wr['configured_wpm']} configured x {wr['floor_ratio']})",
              file=sys.stderr)
    print("\nRemediation: re-transcribe the named regions in isolation and "
          "splice, or re-run the take with a smaller transcribe.py --window "
          "(20s is the validated default; anything larger drops speech).",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
