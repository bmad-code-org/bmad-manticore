/* mc-prompter scroll engine (classic script, attaches to window.MC).
 *
 * requestAnimationFrame based smooth scroll of a scrollable surface.
 *
 * Speed model (Phase A contract):
 *   px/s = wpm / 60 * (scrollable height / word count)
 * where scrollable height = scrollHeight - clientHeight. The ratio is read
 * fresh every frame, so font, size, margin, line-height changes and window
 * resizes are picked up automatically without an explicit recompute call.
 *
 * Position is reported as scrollTop / (scrollHeight - clientHeight), 0..1.
 *
 * Modes:
 *   manual  wpm is whatever the user set (live +/- adjustment)
 *   timed   given a total duration, wpm is continuously re-derived as
 *           remaining words / remaining minutes (recomputed every frame,
 *           which covers resume and jumps); drift from plan is exposed as
 *           elapsed - position * totalSeconds (positive means behind plan)
 *
 * Countdown: beginCountdown(seconds) holds the scroll and counts down, then
 * flips to playing. The page renders the big digits from view().countdown.
 *
 * Voice-follow (Phase B): setFollow(true) suspends the constant-rate WPM
 * integration and instead eases the surface toward a pixel target set by
 * setFollowTargetPx (the anchor word offset so it sits at the eyeline).
 * Easing is an exponential approach with time constant ~400 ms and a capped
 * speed, so a big re-anchor glides instead of teleporting. The target only
 * moves when the page feeds new anchors; VAD silence or a held anchor simply
 * stops feeding it, which freezes the scroll. setFollow(false) returns to
 * manual WPM mode at the current position. The surface, position math, and
 * word spans stay identical to Phase A.
 */
(function () {
  'use strict';
  window.MC = window.MC || {};

  var WPM_MIN = 10;
  var WPM_MAX = 1200;

  function createEngine(opts) {
    // opts: { surface: element, getWordCount: fn -> int,
    //         onFrame: fn(view), onChange: fn(reason), onFinish: fn }
    var surface = opts.surface;

    var st = {
      playing: false,
      mode: 'manual',        // 'manual' | 'timed'
      wpm: 150,              // manual-mode wpm
      totalSeconds: null,    // timed-mode plan length
      elapsed: 0,            // seconds of play time accumulated
      countdownLeft: 0,
      finished: false,
      follow: false          // voice-follow mode (Phase B)
    };

    var FOLLOW_TAU = 0.4;    // easing time constant, seconds
    var followTarget = 0;    // px the follow easing approaches

    var pos = 0;             // float scroll position (scrollTop rounds)
    var rafId = null;
    var lastTs = null;
    var selfScroll = false;  // guards the scroll listener against our own writes

    function scrollable() {
      return Math.max(surface.scrollHeight - surface.clientHeight, 1);
    }

    function position() {
      var p = pos / scrollable();
      return Math.min(Math.max(p, 0), 1);
    }

    function remainingWords() {
      var wc = opts.getWordCount() || 0;
      return wc * (1 - position());
    }

    function currentWpm() {
      if (st.mode === 'timed' && st.totalSeconds) {
        var remS = Math.max(st.totalSeconds - st.elapsed, 1);
        var w = remainingWords() / (remS / 60);
        return Math.min(Math.max(w, WPM_MIN), WPM_MAX);
      }
      return st.wpm;
    }

    function pxPerSecond() {
      var wc = opts.getWordCount();
      if (!wc) return 0;
      return currentWpm() / 60 * (scrollable() / wc);
    }

    function remainingSeconds() {
      if (st.mode === 'timed' && st.totalSeconds) {
        return Math.max(st.totalSeconds - st.elapsed, 0);
      }
      var w = currentWpm();
      if (!w) return null;
      return remainingWords() / w * 60;
    }

    function driftSeconds() {
      if (st.mode !== 'timed' || !st.totalSeconds) return null;
      return st.elapsed - position() * st.totalSeconds;
    }

    function view() {
      return {
        playing: st.playing,
        position: position(),
        wpm: Math.round(currentWpm()),
        mode: st.mode,
        totalSeconds: st.totalSeconds,
        elapsed: st.elapsed,
        remaining: remainingSeconds(),
        countdown: st.countdownLeft > 0 ? st.countdownLeft : null,
        drift: driftSeconds(),
        finished: st.finished,
        follow: st.follow
      };
    }

    // Speed cap for follow easing: brisk enough to recover from a jump-cut
    // re-anchor in about a second, never a full-viewport teleport per frame.
    function maxFollowSpeed() {
      return Math.max(surface.clientHeight * 1.5, 300);
    }

    function setScrollTop(v) {
      selfScroll = true;
      surface.scrollTop = v;
      // The scroll event fires async; clear the guard on the next frame.
      requestAnimationFrame(function () { selfScroll = false; });
    }

    function changed(reason) {
      if (opts.onChange) opts.onChange(reason, view());
    }

    function frame(ts) {
      rafId = requestAnimationFrame(frame);
      if (lastTs === null) { lastTs = ts; return; }
      var dt = Math.min((ts - lastTs) / 1000, 0.25);
      lastTs = ts;

      if (st.follow) {
        // Voice-follow: exponential approach to followTarget, capped speed.
        // Elapsed keeps counting (it is the take clock, silence included).
        st.elapsed += dt;
        var k = 1 - Math.exp(-dt / FOLLOW_TAU);
        var step = (followTarget - pos) * k;
        var maxStep = maxFollowSpeed() * dt;
        if (step > maxStep) step = maxStep;
        else if (step < -maxStep) step = -maxStep;
        if (Math.abs(step) > 0.01) {
          pos = Math.min(Math.max(pos + step, 0), scrollable());
          setScrollTop(pos);
        }
      } else if (st.countdownLeft > 0) {
        st.countdownLeft = Math.max(st.countdownLeft - dt, 0);
        if (st.countdownLeft === 0) {
          st.playing = true;
          changed('countdown-done');
        }
      } else if (st.playing) {
        st.elapsed += dt;
        pos += pxPerSecond() * dt;
        var max = scrollable();
        if (pos >= max) {
          pos = max;
          setScrollTop(pos);
          st.playing = false;
          st.finished = true;
          changed('finished');
          if (opts.onFinish) opts.onFinish();
        } else {
          setScrollTop(pos);
        }
      }

      if (opts.onFrame) opts.onFrame(view());

      if (!st.playing && st.countdownLeft <= 0 && !st.follow) stopLoop();
    }

    function startLoop() {
      if (rafId === null) {
        lastTs = null;
        rafId = requestAnimationFrame(frame);
      }
    }

    function stopLoop() {
      if (rafId !== null) {
        cancelAnimationFrame(rafId);
        rafId = null;
        lastTs = null;
      }
    }

    var engine = {
      view: view,
      isPlaying: function () { return st.playing || st.countdownLeft > 0; },
      isFollowing: function () { return st.follow; },

      // Voice-follow mode (Phase B). While on, WPM integration and the
      // countdown are suspended; frame() eases toward followTarget instead.
      // Turning it off returns to manual pacing at the current position.
      setFollow: function (on) {
        var want = !!on;
        if (st.follow === want) return;
        st.follow = want;
        st.playing = false;
        st.countdownLeft = 0;
        st.finished = false;
        if (st.follow) {
          followTarget = pos;
          startLoop();
        }
        changed('follow');
      },

      // Set the pixel offset the follow easing approaches (the page passes
      // anchor word offsetTop minus the eyeline offset). Clamped to range.
      setFollowTargetPx: function (px) {
        followTarget = Math.min(Math.max(Number(px) || 0, 0), scrollable());
        if (st.follow) startLoop();
      },

      play: function () {
        if (st.playing || st.follow) return;
        st.finished = false;
        st.countdownLeft = 0;
        st.playing = true;
        startLoop();
        changed('play');
      },

      // Countdown, then play. seconds <= 0 plays immediately.
      beginCountdown: function (seconds) {
        if (st.follow) return;
        var s = Number(seconds) || 0;
        if (s <= 0) { engine.play(); return; }
        st.finished = false;
        st.playing = false;
        st.countdownLeft = s;
        startLoop();
        changed('countdown');
      },

      pause: function () {
        var was = st.playing || st.countdownLeft > 0;
        st.playing = false;
        st.countdownLeft = 0;
        if (was) changed('pause');
      },

      toggle: function (countdownSeconds) {
        if (engine.isPlaying()) {
          engine.pause();
        } else if (position() < 0.001 && countdownSeconds > 0) {
          engine.beginCountdown(countdownSeconds);
        } else {
          engine.play();
        }
      },

      restart: function () {
        pos = 0;
        followTarget = 0;
        setScrollTop(0);
        st.elapsed = 0;
        st.playing = false;
        st.countdownLeft = 0;
        st.finished = false;
        changed('restart');
      },

      setWpm: function (n) {
        st.wpm = Math.min(Math.max(Math.round(Number(n) || st.wpm), WPM_MIN), WPM_MAX);
        changed('speed');
      },

      deltaWpm: function (d) {
        engine.setWpm(st.wpm + (Number(d) || 0));
      },

      getManualWpm: function () { return st.wpm; },

      // mode: 'manual' | 'timed'; totalMinutes required for timed.
      setMode: function (mode, totalMinutes) {
        if (mode === 'timed' && Number(totalMinutes) > 0) {
          st.mode = 'timed';
          st.totalSeconds = Number(totalMinutes) * 60;
        } else {
          st.mode = 'manual';
          st.totalSeconds = null;
        }
        changed('mode');
      },

      // Jump to an absolute pixel offset within the surface. In follow mode
      // the target snaps along so the easing does not drag the view back.
      jumpToPx: function (px) {
        pos = Math.min(Math.max(px, 0), scrollable());
        followTarget = pos;
        setScrollTop(pos);
        st.finished = false;
        changed('jump');
      },

      // Jump to a 0..1 position ratio.
      setPositionRatio: function (r) {
        engine.jumpToPx((Number(r) || 0) * scrollable());
      },

      getPositionRatio: position,
      getScrollTop: function () { return pos; },

      // Adopt an externally caused scrollTop (manual drag or touch while
      // paused) so the next play resumes from where the user left the view.
      adoptScrollTop: function () {
        if (selfScroll || st.playing || st.countdownLeft > 0 || st.follow) return;
        pos = surface.scrollTop;
        st.finished = false;
        changed('scroll');
      },

      // Re-clamp after layout changes; the page keeps the position ratio
      // stable across settings changes by capturing it before and restoring
      // after (see prompt.js applyDisplay).
      recalc: function () {
        pos = Math.min(pos, scrollable());
        setScrollTop(pos);
      },

      // Seed from a state snapshot (welcome payload or leader promotion).
      seed: function (snap) {
        if (!snap) return;
        if (typeof snap.wpm === 'number' && snap.mode !== 'timed') st.wpm = snap.wpm;
        if (snap.mode === 'timed') {
          var total = (Number(snap.elapsed) || 0) + (Number(snap.remaining) || 0);
          st.mode = 'timed';
          st.totalSeconds = total > 0 ? total : null;
          if (!st.totalSeconds) st.mode = 'manual';
        } else {
          st.mode = 'manual';
        }
        st.elapsed = Number(snap.elapsed) || 0;
        if (typeof snap.position === 'number') {
          pos = Math.min(Math.max(snap.position, 0), 1) * scrollable();
          setScrollTop(pos);
        }
        st.finished = false;
        st.countdownLeft = 0;
        st.playing = !!snap.playing;
        if (st.playing) startLoop();
        changed('seed');
      },

      destroy: stopLoop
    };

    return engine;
  }

  window.MC.createEngine = createEngine;
})();
