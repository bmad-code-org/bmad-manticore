#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "parakeet-mlx; sys_platform == 'darwin' and platform_machine == 'arm64'",
#   "onnx-asr[cpu,hub]; sys_platform != 'darwin' or platform_machine != 'arm64'",
# ]
# ///
"""Word-level transcription for the cut stage (build-order item 1).

Usage:
    uv run {skill-root}/scripts/transcribe.py {projects-path}/<slug>/raw/<take> \
        -o {projects-path}/<slug>/transcript/words.json \
        [--provider auto|parakeet-mlx|onnx-asr] [--model <model-id>]

Contract:
    input     a media file (audio or video); the transcriber extracts audio via
              ffmpeg, so video containers (mp4, mov) are read directly.
    output    transcript JSON with word-level timestamps, confidence, and gap
              data, written to -o in a provider-neutral shape:
                  {
                    "provider": "parakeet-mlx",
                    "model":    "mlx-community/parakeet-tdt-0.6b-v3",
                    "media":    "<the media path as given>",
                    "duration": 172.67,
                    "text":     "<full transcript>",
                    "words": [
                      {"word": "Alright,", "start": 1.28, "end": 1.76,
                       "confidence": 0.9079, "i": 0,
                       "gap_before": 1.28, "gap_after": 0.0},
                      ...
                    ]
                  }
              times round to 2 decimals, confidence to 4. "i" is the word index.
              gap_before = start - previous word's end (first word: its start).
              gap_after  = next word's start - end (last word: duration - end).
              gaps never go negative (clamped to 0.0). The shape is identical
              across providers; downstream consumers (cutplan.py) never need to
              know which lane produced the file.
    provider  from the studio config [transcription] table; this script is the
              switch. "auto" (the default) picks per platform:
                  macOS Apple Silicon  -> parakeet-mlx (the reference lane)
                  everything else      -> onnx-asr (same weights, ONNX runtime)
              parakeet-mlx is the reference: free, local on Apple Silicon,
              native word timestamps, and empirically it keeps verbatim fillers
              ("uh", "um", "Hmm") that cutting depends on. onnx-asr runs the
              SAME parakeet-tdt-0.6b-v3 weights (the istupakov ONNX conversion,
              onnx-asr model name "nemo-parakeet-tdt-0.6b-v3"), so filler
              preservation and 80 ms frame-granular timestamps carry over on
              Windows, Linux, and Intel Macs. Metered API providers (for
              example elevenlabs-scribe or deepgram-nova3) are possible future
              OPT-IN lanes behind --provider and the [transcription] switch;
              none is implemented here, nothing defaults to them, and no API
              key name ships in any default.
    gpu       PEP 508 markers cannot detect GPUs, so the onnx-asr dependency
              ships with the cpu extra. On a CUDA box, escalate at the call
              site with a uv override:
                  uv run --with "onnx-asr[gpu,hub]" \
                      python {skill-root}/scripts/transcribe.py ...
              The `python` command is load-bearing: it makes uv skip this
              script's inline metadata, so ONLY the gpu extra installs.
              Running the script path directly with --with would merge the
              extras and co-install onnxruntime (cpu) with onnxruntime-gpu,
              which ship the same import package; ONNX Runtime does not
              support that, and the collision routinely leaves
              CUDAExecutionProvider unavailable. When onnxruntime reports
              CUDAExecutionProvider available, this script prefers it
              automatically; otherwise it runs on CPU, and if nvidia-smi is
              on PATH it prints a loud warning that the GPU escalation is
              needed (or failed) instead of silently running slow.
    chunking  parakeet-mlx chunks long audio internally. The onnx-asr lane
              caps around 20-30 s per call, so this script extracts fixed
              20 s windows with 2 s overlap via ffmpeg (16 kHz mono wav),
              offsets each chunk's timestamps by its window start, and merges
              at the overlap midpoint on word boundaries with a seam-repair
              pass (the two chunks time boundary words independently, so a
              take kept by both sides deduplicates and a take kept by
              neither restores from the nearer chunk): no word is split,
              duplicated, or dropped across chunks.
    confidence  parakeet-mlx reports per-token confidence natively. The
              onnx-asr lane maps per-token scores when the runtime exposes
              them (logprobs are exponentiated into probabilities, values
              already in 0..1 pass through, everything clamps to 0..1). When
              the runtime exposes no per-token scores, confidence degrades to
              1.0 for every word: downstream consumers see "no signal", never
              a fabricated number.

Exit codes:
    0  transcript written
    1  transcription failed (model, ffmpeg, or provider runtime error)
    2  usage error (bad arguments, media not found)
    3  provider not implemented (future opt-in lanes)

Why parakeet-mlx over generic Whisper: cutting needs verbatim fillers ("um",
"uh", restarts) plus word gap data; Whisper normalizes exactly those away.
"""

import argparse
import json
import math
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROVIDER_AUTO = "auto"
PROVIDER_MLX = "parakeet-mlx"
PROVIDER_ONNX = "onnx-asr"
IMPLEMENTED_PROVIDERS = (PROVIDER_MLX, PROVIDER_ONNX)

DEFAULT_PROVIDER = PROVIDER_AUTO
DEFAULT_MODELS = {
    PROVIDER_MLX: "mlx-community/parakeet-tdt-0.6b-v3",
    # onnx-asr registered name; resolves to the HF hub conversion
    # istupakov/parakeet-tdt-0.6b-v3-onnx (same weights as the mlx lane).
    PROVIDER_ONNX: "nemo-parakeet-tdt-0.6b-v3",
}
# Back-compat alias: the historical single-lane default model id.
DEFAULT_MODEL = DEFAULT_MODELS[PROVIDER_MLX]

# Parakeet timestamps are 80 ms frame-granular on every runtime.
FRAME_S = 0.08
# onnx-asr reports start-only token timestamps. When the NEXT token opens a
# new word, its onset can sit on the far side of a pause (parakeet emits no
# tokens during silence), so the current token's derived end is capped at
# this many frames past its start; otherwise every pause would be absorbed
# into the preceding word and the gap data cutting depends on would read 0.
WORD_END_CAP_FRAMES = 3
# onnx-asr caps most models around 20-30 s per call; fixed windows + overlap.
CHUNK_WINDOW_S = 20.0
CHUNK_OVERLAP_S = 2.0

# SentencePiece word-boundary marker used by the ONNX tokenizer.
SP_MARK = "▁"


def default_provider(platform_name=None, machine=None):
    """Resolve the automatic provider for a platform (pure).

    macOS Apple Silicon gets the parakeet-mlx reference lane; every other
    platform (Windows, Linux, Intel Mac) gets onnx-asr with the same weights.
    """
    platform_name = platform_name if platform_name is not None else sys.platform
    machine = machine if machine is not None else platform.machine()
    if platform_name == "darwin" and machine == "arm64":
        return PROVIDER_MLX
    return PROVIDER_ONNX


def _get(t, key):
    return t[key] if isinstance(t, dict) else getattr(t, key)


def group_subwords(tokens):
    """Collapse parakeet subword tokens into whole words.

    parakeet emits subword tokens (e.g. " Al", "rig", "ht", ","), where a
    leading space in the RAW token text marks the start of a new word and
    tokens without one (continuations, punctuation) attach to the current word.
    Each word's text is the concatenation of its tokens (stripped); its span is
    the first token's start to the last token's end; its confidence is the
    minimum over its tokens (the weakest subword governs). Returns a list of
    (text, start, end, confidence) tuples.
    """
    words = []
    for t in tokens:
        raw = _get(t, "text")
        start = float(_get(t, "start"))
        end = float(_get(t, "end"))
        conf = float(_get(t, "confidence"))
        starts_word = not words or raw[:1] == " "
        if starts_word:
            words.append([raw, start, end, conf])
        else:
            w = words[-1]
            w[0] += raw
            w[2] = end
            w[3] = min(w[3], conf)
    return [(text.strip(), start, end, conf) for text, start, end, conf in words]


def normalize_words(tokens, duration):
    """Map provider tokens to the pinned word schema (pure; no model needed).

    tokens is a list of dicts (or objects) carrying subword text, start, end,
    and confidence. Subwords are grouped into words (see group_subwords). Times
    round to 2 decimals, confidence to 4. gap_before/gap_after are computed on
    the rounded times and clamped to 0.0. Returns the list of word dicts.
    """
    rounded = [
        (text, round(start, 2), round(end, 2), round(conf, 4))
        for text, start, end, conf in group_subwords(tokens)
    ]
    dur = round(float(duration), 2)
    n = len(rounded)
    words = []
    for i, (text, start, end, conf) in enumerate(rounded):
        if i == 0:
            gap_before = start
        else:
            gap_before = round(start - rounded[i - 1][2], 2)
        if i == n - 1:
            gap_after = round(dur - end, 2)
        else:
            gap_after = round(rounded[i + 1][1] - end, 2)
        words.append({
            "word": text,
            "start": start,
            "end": end,
            "confidence": conf,
            "i": i,
            "gap_before": max(0.0, gap_before),
            "gap_after": max(0.0, gap_after),
        })
    return words


def probe_duration(media):
    """Media duration in seconds via ffprobe. Raises on failure."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nokey=1:noprint_wrappers=1",
            str(media),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def transcribe_parakeet(media, model_id):
    """Run parakeet-mlx and return (full_text, tokens, duration).

    tokens are AlignedToken objects (text/start/end/confidence). Import is
    local so the pure helpers stay importable without the model dependency.
    """
    import parakeet_mlx

    model = parakeet_mlx.from_pretrained(model_id)
    result = model.transcribe(str(media))
    duration = probe_duration(media)
    return result.text, result.tokens, duration


# --- onnx-asr lane -----------------------------------------------------------


def _score_to_confidence(value):
    """Map a per-token score into the 0..1 confidence shape (pure).

    None (runtime exposes no scores) -> 1.0, the documented no-signal value.
    Values <= 0 are treated as logprobs and exponentiated. Positive values are
    treated as probabilities. Everything clamps into 0..1.
    """
    if value is None:
        return 1.0
    v = float(value)
    if v <= 0.0:
        return math.exp(v)
    return min(1.0, v)


def onnx_tokens_to_parakeet(tokens, timestamps, logprobs=None, frame=FRAME_S,
                            clamp=None):
    """Convert onnx-asr token/timestamp arrays into parakeet-shaped dicts (pure).

    tokens are SentencePiece pieces where the word-boundary marker (U+2581)
    starts a new word; the marker becomes the leading space the parakeet
    grouping keys on, so normalize_words consumes both lanes identically.
    timestamps entries are either start seconds or (start, end) pairs. With
    start-only entries the end is derived: the next token's start when that
    token CONTINUES the same word; capped at start + WORD_END_CAP_FRAMES
    frames when the next token opens a NEW word (its onset may sit past a
    pause, and dragging the end across the silence would zero the gap data
    downstream cutting keys on); start + one 80 ms frame for the last token.
    logprobs, when provided, map per token through
    _score_to_confidence; when absent every token gets confidence 1.0.
    clamp, when given, caps end times (chunk length). A bare marker token
    carries its word boundary onto the next token instead of emitting an
    empty word. Raises ValueError when tokens and timestamps disagree in
    length.
    """
    if len(tokens) != len(timestamps):
        raise ValueError(
            f"token/timestamp count mismatch: {len(tokens)} tokens, "
            f"{len(timestamps)} timestamps"
        )

    def _start_of(entry):
        if isinstance(entry, (list, tuple)):
            return float(entry[0])
        return float(entry)

    def _starts_word(tok):
        # A raw token opens a new word when it begins with the SentencePiece
        # boundary marker (a bare marker token counts: it carries the
        # boundary onto the next token).
        return str(tok).replace(SP_MARK, " ").startswith(" ")

    out = []
    pending_space = False
    n = len(tokens)
    for i, tok in enumerate(tokens):
        text = str(tok).replace(SP_MARK, " ")
        if text.strip() == "":
            # bare boundary marker (or empty token): carry the word boundary
            # onto the next token rather than emitting an empty word.
            if text.startswith(" "):
                pending_space = True
            continue
        if pending_space:
            if not text.startswith(" "):
                text = " " + text
            pending_space = False

        entry = timestamps[i]
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            start = float(entry[0])
            end = float(entry[1])
        else:
            start = _start_of(entry)
            if i + 1 < n:
                nxt = _start_of(timestamps[i + 1])
                if nxt <= start:
                    end = start + frame
                elif _starts_word(tokens[i + 1]):
                    # The next token opens a new word; parakeet emits no
                    # tokens during silence, so its onset may sit across a
                    # pause. Cap the derived end near this token instead of
                    # dragging it over the silence.
                    end = min(nxt, start + WORD_END_CAP_FRAMES * frame)
                else:
                    end = nxt
            else:
                end = start + frame
        if clamp is not None:
            end = min(end, float(clamp))
        if end < start:
            end = start

        score = None
        if logprobs is not None and i < len(logprobs):
            score = logprobs[i]
        out.append({
            "text": text,
            "start": start,
            "end": end,
            "confidence": _score_to_confidence(score),
        })
    return out


def offset_tokens(tokens, offset):
    """Shift token dicts by a chunk's window start (pure)."""
    return [
        {**t, "start": t["start"] + offset, "end": t["end"] + offset}
        for t in tokens
    ]


def plan_chunks(duration, window=CHUNK_WINDOW_S, overlap=CHUNK_OVERLAP_S):
    """Fixed transcription windows with overlap (pure).

    Returns [(start, length), ...] covering the full duration. A duration
    within one window yields a single chunk. Later chunks start one window
    minus one overlap after the previous, so adjacent chunks share `overlap`
    seconds; the merge cuts at the overlap midpoint.
    """
    duration = float(duration)
    if window <= 0:
        raise ValueError("window must be positive")
    if overlap < 0 or overlap >= window:
        raise ValueError("overlap must be >= 0 and smaller than window")
    if duration <= 0:
        return [(0.0, 0.0)]
    if duration <= window:
        return [(0.0, round(duration, 3))]
    chunks = []
    step = window - overlap
    start = 0.0
    while True:
        length = min(window, duration - start)
        chunks.append((round(start, 3), round(length, 3)))
        if start + window >= duration:
            break
        start += step
    return chunks


def _word_runs(tokens):
    """Group token dicts into word runs by the leading-space boundary (pure)."""
    runs = []
    for t in tokens:
        if not runs or t["text"][:1] == " ":
            runs.append([t])
        else:
            runs[-1].append(t)
    return runs


def _run_text(run):
    """Normalized word text of a subword run, for cross-chunk matching (pure)."""
    return "".join(t["text"] for t in run).strip().lower()


def _runs_overlap(a, b):
    """True when two runs' time spans intersect (pure)."""
    return a[0]["start"] < b[-1]["end"] and b[0]["start"] < a[-1]["end"]


def merge_chunk_tokens(chunks):
    """Merge per-chunk token lists across overlaps (pure).

    chunks is [(chunk_start, chunk_end, tokens_abs), ...] in order, where
    tokens_abs already carry absolute (offset) times. For each overlap the cut
    point is the midpoint; a word (run of subword tokens) belongs to the chunk
    whose side of the cut its own start falls on. Both chunks transcribe the
    boundary words independently (different acoustic context, 80 ms frame
    granularity), so the two takes' start estimates can disagree about the
    cut; a seam pass over each overlap then repairs the two failure shapes:
    a word kept by BOTH chunks (time spans overlap, identical normalized
    text) keeps only the take from the chunk whose window center is nearer,
    and a word kept by NEITHER (the takes landed crosswise around the cut)
    is restored from the nearer chunk's take. No word is split, duplicated,
    or dropped across a seam. Words are emitted sorted by start time.
    """
    n = len(chunks)
    kept = []     # per chunk: runs on their own side of the cuts
    dropped = []  # per chunk: runs a cut excluded
    for i, (cstart, cend, tokens) in enumerate(chunks):
        left = (chunks[i - 1][1] + cstart) / 2 if i > 0 else float("-inf")
        right = (cend + chunks[i + 1][0]) / 2 if i < n - 1 else float("inf")
        k, d = [], []
        for run in _word_runs(tokens):
            (k if left <= run[0]["start"] < right else d).append(run)
        kept.append(k)
        dropped.append(d)

    # Seam pass over each overlap window.
    for i in range(n - 1):
        cut = (chunks[i][1] + chunks[i + 1][0]) / 2
        center_i = (chunks[i][0] + chunks[i][1]) / 2
        center_j = (chunks[i + 1][0] + chunks[i + 1][1]) / 2

        def _first_is_nearer(a, b):
            # midpoint of the two takes; ties go to the later chunk (matching
            # the base rule that a start exactly at the cut is the later
            # chunk's).
            mid = (a[0]["start"] + a[-1]["end"]
                   + b[0]["start"] + b[-1]["end"]) / 4
            return abs(mid - center_i) < abs(mid - center_j)

        # Duplicates: the same word kept by both chunks (chunk i's estimate
        # fell before the cut, chunk i+1's on or after it). Keep one take.
        overlap_start = chunks[i + 1][0]
        for a in list(kept[i]):
            if a[-1]["end"] <= overlap_start:
                continue  # entirely before the overlap window, no seam risk
            for b in list(kept[i + 1]):
                if _runs_overlap(a, b) and _run_text(a) == _run_text(b):
                    if _first_is_nearer(a, b):
                        kept[i + 1].remove(b)
                    else:
                        kept[i].remove(a)
                    break

        # Lost words: both takes landed crosswise (chunk i's estimate on or
        # after the cut, chunk i+1's before it), so neither side kept the
        # word. Restore the nearer chunk's take.
        lost_left = [b for b in dropped[i + 1] if b[0]["start"] < cut]
        for a in dropped[i]:
            if a[0]["start"] < cut:
                continue  # dropped at chunk i's left seam, not this one
            for b in list(lost_left):
                if _runs_overlap(a, b) and _run_text(a) == _run_text(b):
                    if _first_is_nearer(a, b):
                        kept[i].append(a)
                    else:
                        kept[i + 1].append(b)
                    lost_left.remove(b)
                    break

    ordered = sorted(
        (run for chunk_runs in kept for run in chunk_runs),
        key=lambda run: run[0]["start"],
    )
    merged = []
    for run in ordered:
        merged.extend(run)
    return merged


def extract_chunk(media, start, length, out_wav):
    """Extract one 16 kHz mono wav window via ffmpeg. Raises on failure."""
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
            "-i", str(media),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(out_wav),
        ],
        capture_output=True, text=True, check=True,
    )


def _preferred_onnx_providers():
    """Prefer CUDA when the installed onnxruntime exposes it, else CPU."""
    try:
        import onnxruntime
        available = list(onnxruntime.get_available_providers())
    except Exception:
        return None
    ordered = [
        p for p in ("CUDAExecutionProvider", "CPUExecutionProvider")
        if p in available
    ]
    return ordered or None


def cuda_fallback_warning(available_providers, nvidia_smi_present):
    """Warning text when a CUDA GPU is visible but unusable, else None (pure).

    An NVIDIA box (nvidia-smi on PATH) whose installed onnxruntime lacks
    CUDAExecutionProvider means the GPU escalation was skipped or failed
    (typically onnxruntime and onnxruntime-gpu co-installed and colliding);
    transcription would silently run on CPU at multi-x realtime. This makes
    that state loud instead of silent."""
    if not nvidia_smi_present:
        return None
    if "CUDAExecutionProvider" in (available_providers or []):
        return None
    return (
        "WARNING: nvidia-smi is on PATH but the installed onnxruntime has no "
        "CUDAExecutionProvider; transcription will run on CPU. For GPU, "
        'escalate with: uv run --with "onnx-asr[gpu,hub]" python '
        "<skill-root>/scripts/transcribe.py ... (the `python` command skips "
        "this script's cpu-extra dependency so only the GPU build installs; "
        "onnxruntime and onnxruntime-gpu must never be co-installed)."
    )


def _load_onnx_model(model_id):
    """Load the onnx-asr model, preferring CUDA when available."""
    import onnx_asr

    providers = _preferred_onnx_providers()
    warning = cuda_fallback_warning(
        providers, shutil.which("nvidia-smi") is not None)
    if warning:
        print(warning, file=sys.stderr)
    if providers:
        try:
            return onnx_asr.load_model(model_id, providers=providers)
        except TypeError:
            pass  # older onnx-asr without a providers kwarg
    return onnx_asr.load_model(model_id)


def _result_scores(result):
    """Best-effort per-token scores off an onnx-asr result, else None."""
    for attr in ("logprobs", "scores", "confidences"):
        scores = getattr(result, attr, None)
        if scores is not None:
            return list(scores)
    return None


def transcribe_onnx(media, model_id, window=CHUNK_WINDOW_S,
                    overlap=CHUNK_OVERLAP_S):
    """Run onnx-asr over fixed windows and return (full_text, tokens, duration).

    Every window is extracted to a 16 kHz mono wav via ffmpeg (so video
    containers work exactly like the mlx lane), recognized with timestamps,
    converted to parakeet-shaped token dicts, offset by the window start, and
    merged at overlap midpoints. Imports are local so the pure helpers stay
    importable without the onnx-asr dependency.
    """
    duration = probe_duration(media)
    chunks = plan_chunks(duration, window=window, overlap=overlap)
    model = _load_onnx_model(model_id)

    per_chunk = []
    with tempfile.TemporaryDirectory(prefix="mc-transcribe-") as tmp:
        for i, (start, length) in enumerate(chunks):
            print(
                f"chunk {i + 1}/{len(chunks)}: {start:.2f}s +{length:.2f}s",
                file=sys.stderr,
            )
            wav = Path(tmp) / f"chunk{i:04d}.wav"
            extract_chunk(media, start, length, wav)
            result = model.with_timestamps().recognize(str(wav))
            tokens = onnx_tokens_to_parakeet(
                list(result.tokens),
                list(result.timestamps),
                logprobs=_result_scores(result),
                clamp=length,
            )
            per_chunk.append((start, start + length, offset_tokens(tokens, start)))

    merged = merge_chunk_tokens(per_chunk)
    text = "".join(t["text"] for t in merged).strip()
    return text, merged, duration


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("media", help="path to the media file (audio or video)")
    parser.add_argument("-o", "--output", required=True,
                        help="path to write the words.json transcript")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER,
                        help="transcription provider: auto (default; "
                             "parakeet-mlx on macOS Apple Silicon, onnx-asr "
                             "elsewhere), parakeet-mlx, or onnx-asr")
    parser.add_argument("--model", default=None,
                        help="model id (default per provider: "
                             f"{DEFAULT_MODELS[PROVIDER_MLX]} for parakeet-mlx, "
                             f"{DEFAULT_MODELS[PROVIDER_ONNX]} for onnx-asr)")
    args = parser.parse_args(argv)

    provider = args.provider
    if provider == PROVIDER_AUTO:
        provider = default_provider()
        print(f"provider resolved: {provider} (auto)", file=sys.stderr)

    if provider not in IMPLEMENTED_PROVIDERS:
        print(
            f"provider {provider} not implemented; implemented lanes are "
            f"{', '.join(IMPLEMENTED_PROVIDERS)} (auto picks per platform). "
            "See the [transcription] switch in the studio config.",
            file=sys.stderr,
        )
        return 3

    media = Path(args.media)
    if not media.is_file():
        print(f"error: media not found: {media}", file=sys.stderr)
        return 2

    output = Path(args.output)
    model_id = args.model or DEFAULT_MODELS[provider]

    try:
        if provider == PROVIDER_MLX:
            text, tokens, duration = transcribe_parakeet(media, model_id)
        else:
            text, tokens, duration = transcribe_onnx(media, model_id)
    except ImportError as exc:
        print(
            f"error: provider {provider} dependencies unavailable on this "
            f"platform: {exc}. Use --provider auto to pick the platform "
            "default lane.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # transcription failure
        print(f"error: transcription failed: {exc}", file=sys.stderr)
        return 1

    words = normalize_words(tokens, duration)
    payload = {
        "provider": provider,
        "model": model_id,
        "media": args.media,
        "duration": round(float(duration), 2),
        "text": text.strip(),
        "words": words,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                      encoding="utf-8")

    print(json.dumps({
        "provider": provider,
        "model": model_id,
        "media": args.media,
        "duration": payload["duration"],
        "words": len(words),
        "output": str(output),
    }))
    return 0


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)
