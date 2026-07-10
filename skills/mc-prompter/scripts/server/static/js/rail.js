/* mc-prompter producer rail + cue card (classic script, attaches to
 * window.MC.rail). Shared by /prompt, /overlay, and /remote.
 *
 * Wire shapes (Phase C contract):
 *   <- producer {type:"producer", state:{
 *        live, hold, "elapsed-s", "remaining-s",
 *        "show-state": "green"|"yellow"|"red",
 *        current: "g1",
 *        "next-point": {segment, idx, text} | null,
 *        segments: [{id, title, kind, "planned-s", "replanned-s", "spent-s",
 *                    state: "done"|"current"|"pending",
 *                    timing: "green"|"yellow"|"red",
 *                    points: [{text, covered, skipped}]}],
 *        drop: {segment, text} | null}}
 *   <- cue       {type:"cue", id, tier:"card"|"attention", text}
 *   <- cue-clear {type:"cue-clear", id}
 *
 * createRail(container): the ambient strip. Show clock + green/yellow/red
 * show state, LIVE/HOLD/PRE badge, current segment title + its replanned
 * time left (colored by the segment's timing), NEXT point text, and one
 * progress dot per segment. No motion, glanceable, dark-theme. The DOM is
 * built once; update(state) only mutates text and classes.
 *
 * createCueCard(container): the single cue region. One active cue at a
 * time (a new cue replaces the old one; the server enforces the budget and
 * the one-active-cue rule). Card tier appears quietly; attention tier gets
 * the high-contrast flash pulse. Cleared on the matching cue-clear frame,
 * with a defensive local expiry in case a clear frame is lost.
 *
 * Helpers for the pages:
 *   normalizeRundown(resp): GET /api/rundown response -> {rundown, ranges}
 *     where ranges maps segment id -> {start, end} global word indices
 *     (scripted segments only carry meaningful ranges; bullets segments
 *     contribute no words). Accepts both a nested {rundown, segments:
 *     [word ranges]} response and a flat parse_rundown dict with
 *     word-start/word-end merged into each segment.
 *   preShowState(rundown): synthesize a pre-show producer state from the
 *     parsed rundown so the rail and point lists render before the first
 *     producer frame arrives (the server broadcasts only on change).
 *   activePointIndex(segment): first uncovered, unskipped point index, -1
 *     when the segment is fully covered or has no points.
 */
(function () {
  'use strict';
  window.MC = window.MC || {};

  // Standalone clock formatter so /overlay does not need model.js just for
  // this. Negative seconds render as +m:ss (time over).
  function fmtClock(seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) return '--:--';
    var neg = seconds < 0;
    var t = Math.round(Math.abs(seconds));
    var h = Math.floor(t / 3600);
    var m = Math.floor((t % 3600) / 60);
    var s = t % 60;
    var mm = (h > 0 && m < 10 ? '0' : '') + m;
    var ss = (s < 10 ? '0' : '') + s;
    var out = h > 0 ? h + ':' + mm + ':' + ss : mm + ':' + ss;
    return neg ? '+' + out : out;
  }

  function el(tag, className, text) {
    var e = document.createElement(tag);
    if (className) e.className = className;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  var TIMING_CLASSES = ['t-green', 't-yellow', 't-red'];

  function timingClass(t) {
    return t === 'red' ? 't-red' : t === 'yellow' ? 't-yellow' : 't-green';
  }

  function setTiming(node, t) {
    for (var i = 0; i < TIMING_CLASSES.length; i++) node.classList.remove(TIMING_CLASSES[i]);
    node.classList.add(timingClass(t));
  }

  function segById(state, id) {
    var segs = (state && state.segments) || [];
    for (var i = 0; i < segs.length; i++) {
      if (segs[i].id === id) return segs[i];
    }
    return null;
  }

  // First uncovered, unskipped point in the segment, -1 when none remain.
  function activePointIndex(seg) {
    var pts = (seg && seg.points) || [];
    for (var i = 0; i < pts.length; i++) {
      if (!pts[i].covered && !pts[i].skipped) return i;
    }
    return -1;
  }

  // ---------- the ambient rail ----------

  function createRail(container) {
    var root = el('div', 'mc-rail hidden');
    var liveBadge = el('span', 'r-live chip', 'PRE');
    var clockEl = el('span', 'r-clock mono t-green', '00:00 / --:--');
    var segEl = el('span', 'r-seg');
    var segTitle = el('span', 'r-seg-title', '');
    var segTime = el('span', 'r-seg-time mono t-green', '');
    segEl.appendChild(segTitle);
    segEl.appendChild(segTime);
    var nextEl = el('span', 'r-next', '');
    var dotsEl = el('span', 'r-dots');
    root.appendChild(liveBadge);
    root.appendChild(clockEl);
    root.appendChild(segEl);
    root.appendChild(nextEl);
    root.appendChild(dotsEl);
    container.appendChild(root);

    var dotCount = -1;

    function ensureDots(n) {
      if (n === dotCount) return;
      while (dotsEl.firstChild) dotsEl.removeChild(dotsEl.firstChild);
      for (var i = 0; i < n; i++) dotsEl.appendChild(el('span', 'r-dot'));
      dotCount = n;
    }

    function update(state) {
      if (!state) { clear(); return; }
      root.classList.remove('hidden');

      // LIVE / HOLD / PRE / ENDED badge. After end-show the clock stops with
      // elapsed still on it, which is how ENDED is told apart from PRE.
      var badgeText, badgeClass;
      if (state.live && state.hold) { badgeText = 'HOLD'; badgeClass = 'chip warn'; }
      else if (state.live) { badgeText = 'LIVE'; badgeClass = 'chip bad'; }
      else if ((state['elapsed-s'] || 0) > 0) { badgeText = 'ENDED'; badgeClass = 'chip'; }
      else { badgeText = 'PRE'; badgeClass = 'chip'; }
      liveBadge.textContent = badgeText;
      liveBadge.className = 'r-live ' + badgeClass;

      clockEl.textContent =
        fmtClock(state['elapsed-s']) + ' / ' + fmtClock(state['remaining-s']);
      setTiming(clockEl, state['show-state']);

      var cur = segById(state, state.current);
      if (cur) {
        segTitle.textContent = cur.title || cur.id;
        var left = (cur['replanned-s'] || 0) - (cur['spent-s'] || 0);
        segTime.textContent = fmtClock(left >= 0 ? left : left);
        setTiming(segTime, cur.timing);
      } else {
        segTitle.textContent = '';
        segTime.textContent = '';
      }

      var np = state['next-point'];
      nextEl.textContent = np && np.text ? 'NEXT: ' + np.text : '';

      var segs = state.segments || [];
      ensureDots(segs.length);
      var dots = dotsEl.children;
      for (var i = 0; i < segs.length; i++) {
        var d = dots[i];
        d.className = 'r-dot ' + (segs[i].state || 'pending') + ' ' + timingClass(segs[i].timing);
        d.title = (segs[i].title || segs[i].id) + ' (' + (segs[i].state || 'pending') + ')';
      }
    }

    function clear() {
      root.classList.add('hidden');
    }

    return { update: update, clear: clear, el: root };
  }

  // ---------- the cue card ----------

  // The server auto-expires cues at 15 s and broadcasts cue-clear; the
  // local expiry is a fallback for a lost clear frame, not the contract.
  var CUE_FALLBACK_EXPIRY_MS = 30000;

  function createCueCard(container) {
    var root = el('div', 'mc-cue hidden');
    var textEl = el('div', 'mc-cue-text', '');
    root.appendChild(textEl);
    container.appendChild(root);

    var currentId = null;
    var expireTimer = null;

    function hide() {
      root.classList.add('hidden');
      currentId = null;
      clearTimeout(expireTimer);
    }

    function onCue(msg) {
      if (!msg || !msg.text) return;
      currentId = msg.id !== undefined ? msg.id : null;
      textEl.textContent = msg.text;
      root.classList.remove('tier-card', 'tier-attention', 'hidden');
      // Force a reflow so the attention pulse animation restarts when a new
      // attention cue replaces a previous one.
      void root.offsetWidth;
      root.classList.add(msg.tier === 'attention' ? 'tier-attention' : 'tier-card');
      clearTimeout(expireTimer);
      expireTimer = setTimeout(hide, CUE_FALLBACK_EXPIRY_MS);
    }

    function onClear(msg) {
      // Clear only the active cue: a stale clear for a cue already replaced
      // must not kill its successor.
      if (msg && msg.id !== undefined && currentId !== null && msg.id !== currentId) return;
      hide();
    }

    return { onCue: onCue, onClear: onClear, clear: hide, el: root };
  }

  // ---------- rundown helpers ----------

  function normalizeRundown(resp) {
    if (!resp) return null;
    // A response carrying an explicit rundown key is the nested server form;
    // {"rundown": null} means no rundown is loaded (producer off).
    var rundown = Object.prototype.hasOwnProperty.call(resp, 'rundown')
      ? resp.rundown
      : resp;
    if (!rundown || !rundown.segments) return null;
    var ranges = {};
    var i, e;
    // Nested form: {rundown: {...}, segments: [{id, word-start, word-end}]}.
    var rangeList = resp.rundown ? resp.segments : null;
    rangeList = rangeList || resp['word-ranges'] || null;
    if (rangeList) {
      for (i = 0; i < rangeList.length; i++) {
        e = rangeList[i];
        if (e && e.id !== undefined && e['word-start'] !== undefined) {
          ranges[e.id] = { start: e['word-start'], end: e['word-end'] };
        }
      }
    }
    // Flat form: word-start/word-end merged into the rundown segments.
    for (i = 0; i < rundown.segments.length; i++) {
      e = rundown.segments[i];
      if (e && e['word-start'] !== undefined && !ranges[e.id]) {
        ranges[e.id] = { start: e['word-start'], end: e['word-end'] };
      }
    }
    return { rundown: rundown, ranges: ranges };
  }

  // Synthesize the wire-shape producer state for the pre-show rail: the
  // server broadcasts producer frames only on change, so a page that joins
  // before go-live renders this until the first frame lands.
  function preShowState(rundown) {
    if (!rundown || !rundown.segments) return null;
    var segments = [];
    var nextPoint = null;
    for (var i = 0; i < rundown.segments.length; i++) {
      var s = rundown.segments[i];
      var points = [];
      var srcPts = s.points || [];
      for (var j = 0; j < srcPts.length; j++) {
        points.push({ text: srcPts[j].text, covered: false, skipped: false });
        if (!nextPoint) nextPoint = { segment: s.id, idx: j, text: srcPts[j].text };
      }
      segments.push({
        id: s.id,
        title: s.title,
        kind: s.kind,
        'planned-s': s['planned-s'],
        'replanned-s': s['planned-s'],
        'spent-s': 0,
        state: i === 0 ? 'current' : 'pending',
        timing: 'green',
        points: points
      });
    }
    return {
      live: false,
      hold: false,
      'elapsed-s': 0,
      'remaining-s': rundown['duration-s'] || 0,
      'show-state': 'green',
      current: segments.length ? segments[0].id : null,
      'next-point': nextPoint,
      segments: segments,
      drop: null
    };
  }

  window.MC.rail = {
    createRail: createRail,
    createCueCard: createCueCard,
    normalizeRundown: normalizeRundown,
    preShowState: preShowState,
    activePointIndex: activePointIndex,
    segById: segById,
    timingClass: timingClass,
    fmtClock: fmtClock
  };
})();
