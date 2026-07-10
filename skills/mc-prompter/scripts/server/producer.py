#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Producer state machine for mc-prompter (Phase C). Pure stdlib.

Deterministic show-time replanner and cue-candidate source. No I/O, no
threads, no LLM: server/main.py owns the wiring (tick loop, cue engine,
LLM proposals, WS broadcast). The binding behavior contract lives in
references/cueing.md; the rundown input shape in references/rundown-spec.md.

    producer = Producer(parse_rundown(text), cue_density="normal",
                        now_fn=time.monotonic)
    producer.go_live()
    state = producer.tick()          # the rail state dict (shape below)
    cues = producer.cue_candidates() # [{"tier","text","key"}, ...]

Clock:
    now_fn is injectable for tests (fake clock); time.monotonic is only the
    default. now_fn must be non-decreasing (time.monotonic satisfies
    this). The show clock counts live, non-hold time only. Before
    go_live() everything is pre-show (live false, elapsed 0). hold()
    freezes elapsed (VAD and the transcript keep running outside this
    module); resume() unfreezes; end_show() freezes elapsed permanently.

Authority and coverage:
    Coverage is sticky and monotonic. propose_coverage() (LLM or keyword
    first-pass) may only flip an uncovered, unskipped point to covered and
    silently ignores unknown ids (model output is untrusted).
    mark_covered() and skip_point() are human-authoritative and raise
    ValueError on unknown ids (those calls come from validated UI paths).
    Nothing ever un-covers a point. "Next" is the first uncovered,
    unskipped point in rundown order.

Segments:
    The first segment is current from construction. advance_segment()
    marks the current segment done and moves to the next pending one (a
    manual or anchor-driven handoff). make_current(seg_id) jumps: when the
    old pointer segment is actually current it becomes done when it sits
    before the target in rundown order, or returns to pending when it sits
    after (a backwards jump reopens work); a pointer resting on a done
    segment stays done. The target becomes current even if it was done.
    Per-segment spent-s accrues only to a segment whose state is
    "current". Advancing past the last segment leaves the pointer on that
    done segment: the wire state's "current" then names a done segment,
    elapsed keeps counting, and no segment accrues spent (done rows are
    frozen history).

Replan (every tick):
    remaining = duration-s - elapsed. The wrap reserve (wrap-s, the last
    segment when set) is subtracted first and protected: the wrap's
    replanned-s always equals its planned-s. What is left is distributed
    across not-done, non-wrap segments proportionally to their ORIGINAL
    budgets; each not-done segment's replanned-s IS its share of that
    future, and its spent-s is consumed against it. Green/yellow/red is
    computed against the REPLANNED budgets: green under 80 percent
    consumed, yellow 80 to 100, red over. Done segments leave the replan:
    they report replanned-s equal to their planned-s and their timing
    reads spent against plan (history, not replan). The show state uses
    elapsed against duration-s with the same thresholds.

    Feasibility floor: max(45 s, 25 percent of the original budget). When
    the replan pushes any pending non-wrap segment below its floor while
    live, a DROP candidate names the lowest-priority pending segment (the
    last non-wrap pending segment in rundown order) with the alternative
    even split: "DROP: <title>, or <n>s each" where n is the distributable
    future divided across the pending non-wrap segments.

Cue candidates (deterministic, broadcast vocabulary):
    Emitted only while live and not on hold. Stateless: the same condition
    yields the same candidate every tick; the cue engine dedupes on "key".
    - "<m>:<ss> OVER" (attention, key "over:<m>") when past duration
    - "WRAP" (attention, key "wrap") when remaining <= wrap-s
    - "30 seconds" (card, key "seg-30:<id>") at 30 s left in the current
      segment's replanned budget
    - DROP text (card, key "drop:<id>") while the drop stands
    - "STRETCH" (card, key "stretch") when the show is more than 20
      percent ahead of plan (plan credit for finished segments plus the
      current segment's capped spend exceeds 1.2 x elapsed)

The rail state dict (wire shape, broadcast as {"type":"producer",
"state":...}):

    {"live": bool, "hold": bool, "elapsed-s": int, "remaining-s": int,
     "show-state": "green"|"yellow"|"red", "current": "g1",
     "next-point": {"segment": "g1", "idx": 2, "text": str} | null,
     "segments": [{"id", "title", "kind", "planned-s", "replanned-s",
                   "spent-s", "state": "done"|"current"|"pending",
                   "timing": "green"|"yellow"|"red",
                   "points": [{"text", "covered", "skipped"}]}],
     "drop": {"segment": id, "text": str} | null}

state and tick() return deep copies; callers may mutate them freely.
"""

import copy
import time

FLOOR_MIN_S = 45
FLOOR_FRACTION = 0.25
GREEN_BELOW = 0.8
STRETCH_AHEAD = 1.2
SEG_CUE_AT_S = 30


class Producer:
    """Deterministic rundown state machine. See the module docstring."""

    def __init__(self, rundown, cue_density="normal", now_fn=time.monotonic):
        self._now = now_fn
        self.duration_s = rundown["duration-s"]
        self.wrap_s = rundown.get("wrap-s")
        # Frontmatter cue-density overrides the config value passed in
        # (most specific wins); main.py normally resolves this already.
        self.cue_density = rundown.get("cue-density") or cue_density
        self._segments = []
        for seg in rundown["segments"]:
            self._segments.append({
                "id": seg["id"],
                "title": seg["title"],
                "kind": seg["kind"],
                "planned": int(seg["planned-s"]),
                "points": [{"text": p["text"],
                            "covered": bool(p.get("covered", False)),
                            "skipped": False}
                           for p in seg.get("points", [])],
                "spent": 0.0,
                "state": "pending",
            })
        if not self._segments:
            raise ValueError("rundown has no segments")
        self._wrap_idx = (len(self._segments) - 1
                          if self.wrap_s is not None else None)
        self._segments[0]["state"] = "current"
        self._current = 0
        self._live = False
        self._hold = False
        self._ended = False
        self._accum = 0.0      # committed live seconds
        self._mark = 0.0       # now() at the last go_live/resume
        self._synced = 0.0     # elapsed already attributed to segments
        self._snapshot = None
        self._recompute()

    # ----- clock -----

    def _elapsed(self):
        e = self._accum
        if self._live and not self._hold:
            e += self._now() - self._mark
        return e

    def _sync_spent(self):
        # _synced never rewinds: only a positive delta advances it, so a
        # clock hiccup can never double-count a span into spent.
        e = self._elapsed()
        delta = e - self._synced
        if delta > 0:
            # Spent accrues only while the pointer segment is actually
            # current. After advancing past the last segment the pointer
            # rests on a done segment: elapsed keeps counting but the done
            # row's spent is frozen (history does not rewrite).
            if self._segments[self._current]["state"] == "current":
                self._segments[self._current]["spent"] += delta
            self._synced = e
        return e

    def go_live(self):
        """Start the show clock. No-op when already live or ended."""
        if self._live or self._ended:
            return
        self._live = True
        self._hold = False
        self._mark = self._now()
        self._recompute()

    def hold(self):
        """Freeze the show clock (BRB, technical trouble)."""
        if not self._live or self._hold or self._ended:
            return
        self._accum += self._now() - self._mark
        self._hold = True
        self._recompute()

    def resume(self):
        """Unfreeze the show clock after hold()."""
        if not self._live or not self._hold or self._ended:
            return
        self._hold = False
        self._mark = self._now()
        self._recompute()

    def end_show(self):
        """End the show; elapsed freezes permanently."""
        if self._ended:
            return
        # Sync BEFORE folding the live span into _accum: folding first
        # would leave _mark stale while _live is still True, so _elapsed()
        # would count the final span twice and corrupt the current
        # segment's spent.
        self._sync_spent()
        if self._live and not self._hold:
            self._accum += self._now() - self._mark
        self._live = False
        self._hold = False
        self._ended = True
        self._recompute()

    # ----- points -----

    def _find(self, seg_id):
        for i, seg in enumerate(self._segments):
            if seg["id"] == seg_id:
                return i
        return None

    def _point(self, seg_id, point_idx, strict):
        i = self._find(seg_id)
        if i is None:
            if strict:
                raise ValueError(f"unknown segment: {seg_id}")
            return None
        points = self._segments[i]["points"]
        if not isinstance(point_idx, int) or not 0 <= point_idx < len(points):
            if strict:
                raise ValueError(
                    f"unknown point: {seg_id}[{point_idx}]")
            return None
        return points[point_idx]

    def mark_covered(self, seg_id, point_idx):
        """Human authority: cover a point (sticky, works on skipped too)."""
        point = self._point(seg_id, point_idx, strict=True)
        point["covered"] = True
        self._recompute()

    def skip_point(self, seg_id, point_idx):
        """Human authority: exclude a point from next and proposals."""
        point = self._point(seg_id, point_idx, strict=True)
        point["skipped"] = True
        self._recompute()

    def propose_coverage(self, seg_id, point_idx):
        """LLM/keyword proposal: uncovered and unskipped -> covered ONLY.

        Unknown ids are ignored (untrusted input). Returns True when the
        point flipped.
        """
        point = self._point(seg_id, point_idx, strict=False)
        if point is None or point["covered"] or point["skipped"]:
            return False
        point["covered"] = True
        self._recompute()
        return True

    # ----- segments -----

    def make_current(self, seg_id):
        """Jump the current segment (human authority, any direction)."""
        target = self._find(seg_id)
        if target is None:
            raise ValueError(f"unknown segment: {seg_id}")
        self._sync_spent()
        old = self._current
        if target != old and self._segments[old]["state"] == "current":
            # Demote only a segment that is actually current. When the
            # pointer rests on a done segment (advanced past the end) a
            # backward jump must not resurrect it as pending.
            if old < target:
                self._segments[old]["state"] = "done"
            else:
                self._segments[old]["state"] = "pending"
        self._segments[target]["state"] = "current"
        self._current = target
        self._recompute()

    def advance_segment(self):
        """Manual or anchor-driven handoff to the next pending segment.

        Marks the current segment done. On the last segment there is
        nothing to advance to: it is marked done and stays the pointer.
        """
        self._sync_spent()
        self._segments[self._current]["state"] = "done"
        for i in range(self._current + 1, len(self._segments)):
            if self._segments[i]["state"] == "pending":
                self._segments[i]["state"] = "current"
                self._current = i
                break
        self._recompute()

    # ----- replan -----

    def _recompute(self):
        e = self._sync_spent()
        remaining = self.duration_s - e

        wrap = (self._segments[self._wrap_idx]
                if self._wrap_idx is not None else None)
        wrap_future = 0.0
        if wrap is not None and wrap["state"] != "done":
            wrap_future = max(0.0, wrap["planned"] - wrap["spent"])

        pool = [s for i, s in enumerate(self._segments)
                if s["state"] != "done" and i != self._wrap_idx]
        avail = max(0.0, remaining - wrap_future)
        weights = [s["planned"] for s in pool]
        wsum = sum(weights)

        replanned = {}
        for s, w in zip(pool, weights):
            if wsum > 0:
                share = avail * w / wsum
            else:
                share = avail / len(pool) if pool else 0.0
            replanned[s["id"]] = int(round(share))
        if wrap is not None and wrap["state"] != "done":
            replanned[wrap["id"]] = wrap["planned"]
        for s in self._segments:
            if s["state"] == "done":
                replanned[s["id"]] = s["planned"]
        self._last_replanned = replanned

        # Feasibility floor and the DROP candidate (live shows only).
        drop = None
        pending = [s for i, s in enumerate(self._segments)
                   if s["state"] == "pending" and i != self._wrap_idx]
        if self._live and pending:
            below = any(
                replanned[s["id"]]
                < max(FLOOR_MIN_S, FLOOR_FRACTION * s["planned"])
                for s in pending)
            if below:
                target = pending[-1]
                n = int(avail // len(pending))
                drop = {"segment": target["id"],
                        "text": f"DROP: {target['title']}, or {n}s each"}

        elapsed_i = int(e)
        segments_out = []
        for s in self._segments:
            rp = replanned[s["id"]]
            spent_i = int(s["spent"])
            segments_out.append({
                "id": s["id"],
                "title": s["title"],
                "kind": s["kind"],
                "planned-s": s["planned"],
                "replanned-s": rp,
                "spent-s": spent_i,
                "state": s["state"],
                "timing": self._timing(s["spent"], rp),
                "points": [dict(p) for p in s["points"]],
            })

        self._snapshot = {
            "live": self._live,
            "hold": self._hold,
            "elapsed-s": elapsed_i,
            "remaining-s": self.duration_s - elapsed_i,
            "show-state": self._timing(e, self.duration_s),
            "current": self._segments[self._current]["id"],
            "next-point": self._next_point(),
            "segments": segments_out,
            "drop": drop,
        }

    @staticmethod
    def _timing(spent, budget):
        if budget <= 0:
            return "green" if spent <= 0 else "red"
        ratio = spent / budget
        if ratio < GREEN_BELOW:
            return "green"
        if ratio <= 1.0:
            return "yellow"
        return "red"

    def _next_point(self):
        for seg in self._segments:
            for idx, p in enumerate(seg["points"]):
                if not p["covered"] and not p["skipped"]:
                    return {"segment": seg["id"], "idx": idx,
                            "text": p["text"]}
        return None

    # ----- outputs -----

    def tick(self):
        """Recompute the replan and timing states; return the rail state."""
        self._recompute()
        return copy.deepcopy(self._snapshot)

    @property
    def state(self):
        """The last computed rail state (deep copy)."""
        return copy.deepcopy(self._snapshot)

    def cue_candidates(self):
        """Deterministic time cues for the cue engine (see cueing.md).

        Stateless: repeated conditions re-emit the same key every call;
        the cue engine dedupes on key. Empty pre-show, on hold, and after
        end_show().
        """
        if not self._live or self._hold or self._ended:
            return []
        self._recompute()
        e = self._elapsed()
        remaining = self.duration_s - e
        cues = []

        if e > self.duration_s:
            over = int(e - self.duration_s)
            m, s = divmod(over, 60)
            cues.append({"tier": "attention",
                         "text": f"{m}:{s:02d} OVER",
                         "key": f"over:{m}"})

        if self.wrap_s is not None and remaining <= self.wrap_s:
            cues.append({"tier": "attention", "text": "WRAP",
                         "key": "wrap"})

        cur = self._segments[self._current]
        if cur["state"] == "current":
            seg_left = self._last_replanned[cur["id"]] - cur["spent"]
            if seg_left <= SEG_CUE_AT_S:
                cues.append({"tier": "card", "text": "30 seconds",
                             "key": f"seg-30:{cur['id']}"})

        drop = self._snapshot["drop"]
        if drop is not None:
            cues.append({"tier": "card", "text": drop["text"],
                         "key": f"drop:{drop['segment']}"})

        plan_credit = sum(s["planned"] for s in self._segments
                          if s["state"] == "done")
        if cur["state"] == "current":
            plan_credit += min(cur["spent"], cur["planned"])
        if e > 0 and plan_credit > STRETCH_AHEAD * e:
            cues.append({"tier": "card", "text": "STRETCH",
                         "key": "stretch"})

        return cues
