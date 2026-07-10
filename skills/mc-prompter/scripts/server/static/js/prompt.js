/* mc-prompter /prompt page glue.
 *
 * Owns the scroll engine, display settings, keyboard map, section list,
 * settings drawer, countdown overlay, and the WS state loop.
 *
 * Leadership: the first prompt connection is the leader (the server says so
 * in the welcome frame and promotes on disconnect via a role frame). Only
 * the leader runs the engine autonomously and sends state snapshots (~4 Hz
 * while playing plus on every change). Non-leader prompt pages are
 * display-only followers: they apply incoming state frames to their own
 * scroll surface so a tablet pointed at /prompt mirrors the leader.
 */
(function () {
  'use strict';
  var MC = window.MC;

  // ---------- elements ----------

  var els = {
    stage: document.getElementById('stage'),
    surface: document.getElementById('surface'),
    script: document.getElementById('script'),
    eyeline: document.getElementById('eyeline'),
    hud: document.getElementById('hud'),
    conn: document.getElementById('conn'),
    roleBadge: document.getElementById('role-badge'),
    btnToggle: document.getElementById('btn-toggle'),
    btnRestart: document.getElementById('btn-restart'),
    clockElapsed: document.getElementById('clock-elapsed'),
    clockRemaining: document.getElementById('clock-remaining'),
    driftChip: document.getElementById('drift-chip'),
    wpmVal: document.getElementById('wpm-val'),
    modeChip: document.getElementById('mode-chip'),
    countdown: document.getElementById('countdown'),
    countdownNum: document.getElementById('countdown-num'),
    sectionsDrawer: document.getElementById('sections-drawer'),
    sectionsList: document.getElementById('sections-list'),
    estTime: document.getElementById('est-time'),
    settingsDrawer: document.getElementById('settings-drawer'),
    helpOverlay: document.getElementById('help-overlay'),
    tokenError: document.getElementById('token-error'),
    toast: document.getElementById('toast')
  };

  // ---------- state ----------

  var settings = MC.settings.load();
  var docIndex = { wordCount: 0, takeWordCount: 0, words: [], paragraphs: [], sections: [] };
  var docVersion = null;
  var eyelineHidden = false;
  var lastRemoteState = null;   // latest leader snapshot seen while follower
  var stateDirty = false;
  var lastStateSent = 0;
  var toastTimer = null;
  var hudTimer = null;

  var token = new URLSearchParams(location.search).get('token') || null;

  var ws = MC.createWS({ role: 'prompt', token: token });

  // Words the reader actually sees: hidden TAKE paragraphs contribute no
  // scroll height, so they must not count toward pacing or time estimates.
  // The engine reads this fresh every frame, so the hide-takes toggle is
  // picked up immediately.
  function visibleWordCount() {
    var wc = docIndex.wordCount;
    if (settings['hide-takes']) wc -= (docIndex.takeWordCount || 0);
    return Math.max(wc, 0);
  }

  var engine = MC.createEngine({
    surface: els.surface,
    getWordCount: visibleWordCount,
    onFrame: onEngineFrame,
    onChange: onEngineChange,
    onFinish: function () { toast('end of script'); }
  });

  // ---------- helpers ----------

  function isLeader() { return ws.state.leader; }

  // Whether this page drives its own engine: the elected leader does, and so
  // does a page with no server connection at all (standalone fallback, so the
  // prompter never goes dead if the WS drops mid-take).
  function drives() { return ws.state.leader || !ws.state.connected; }

  function toast(msg) {
    els.toast.textContent = msg;
    els.toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { els.toast.classList.remove('show'); }, 1800);
  }

  function eyelinePx() {
    return els.surface.clientHeight * (Number(settings['eyeline-percent']) / 100);
  }

  // Document-space y of the eyeline (what the reader is looking at).
  function eyelineDocY() {
    return els.surface.scrollTop + eyelinePx();
  }

  function currentSectionId() {
    var y = eyelineDocY();
    var id = null;
    for (var i = 0; i < docIndex.sections.length; i++) {
      if (docIndex.sections[i].el.offsetTop <= y + 2) id = docIndex.sections[i].id;
      else break;
    }
    if (id === null && docIndex.sections.length) id = docIndex.sections[0].id;
    return id;
  }

  function jumpToEl(el) {
    engine.jumpToPx(el.offsetTop - eyelinePx());
  }

  function jumpSection(id) {
    for (var i = 0; i < docIndex.sections.length; i++) {
      if (docIndex.sections[i].id === id) { jumpToEl(docIndex.sections[i].el); return; }
    }
  }

  // delta paragraphs: negative = previous, positive = next.
  function jumpParagraphs(delta) {
    var paras = docIndex.paragraphs;
    if (!paras.length) return;
    var y = eyelineDocY();
    // Index of the paragraph the eyeline currently sits in (last with top <= y).
    var cur = -1;
    for (var i = 0; i < paras.length; i++) {
      if (paras[i].el.offsetTop <= y + 2) cur = i; else break;
    }
    var target = Math.min(Math.max(cur + delta, 0), paras.length - 1);
    if (target === cur && delta < 0 && cur >= 0) {
      // Already at this paragraph top? Snap to its top anyway (re-read).
      target = Math.max(cur - 1, 0);
    }
    jumpToEl(paras[target].el);
  }

  // ---------- display settings ----------

  function applyDisplay() {
    var s = settings;
    els.script.style.fontFamily = s['font-family'];
    els.script.style.fontSize = s['font-size'] + 'px';
    els.script.style.lineHeight = String(s['line-height']);
    els.script.style.color = s['text-color'];
    document.body.style.background = s['background-color'];
    els.script.style.paddingLeft = s['margin-percent'] + '%';
    els.script.style.paddingRight = s['margin-percent'] + '%';
    // Lead-in and run-out so the first and last words can reach the eyeline.
    els.script.style.paddingTop = s['eyeline-percent'] + 'vh';
    els.script.style.paddingBottom = (100 - Number(s['eyeline-percent'])) + 'vh';

    var t = '';
    if (s['mirror-h']) t += ' scaleX(-1)';
    if (s['mirror-v']) t += ' scaleY(-1)';
    els.stage.style.transform = t ? t.trim() : 'none';

    els.eyeline.style.top = s['eyeline-percent'] + '%';
    els.eyeline.classList.toggle('style-line', s['eyeline-style'] !== 'arrow');
    els.eyeline.classList.toggle('style-arrow', s['eyeline-style'] === 'arrow');
    els.eyeline.classList.toggle('off', eyelineHidden);

    document.body.classList.toggle('hide-takes', !!s['hide-takes']);
    document.body.classList.toggle('hide-invented', !s['show-invented']);
  }

  // Apply a settings mutation keeping the read position stable, since layout
  // changes rescale the scroll geometry (the engine reads the ratio fresh
  // every frame; we only need to preserve the position across the reflow).
  function changeSetting(key, value) {
    var ratio = engine.getPositionRatio();
    settings[key] = value;
    MC.settings.save(settings);
    applyDisplay();
    engine.setPositionRatio(ratio);
    syncSettingsControls();
    if (key === 'hide-takes') updateEstTime();
    markDirty();
  }

  // ---------- settings drawer wiring ----------

  var C = {
    wpm: document.getElementById('set-wpm'),
    mode: document.getElementById('set-mode'),
    minutes: document.getElementById('set-minutes'),
    rowMinutes: document.getElementById('row-minutes'),
    countdown: document.getElementById('set-countdown'),
    fontStack: document.getElementById('set-font-stack'),
    fontFamily: document.getElementById('set-font-family'),
    fontSize: document.getElementById('set-font-size'),
    fontSizeN: document.getElementById('set-font-size-n'),
    lineHeight: document.getElementById('set-line-height'),
    lineHeightV: document.getElementById('set-line-height-v'),
    margin: document.getElementById('set-margin'),
    marginV: document.getElementById('set-margin-v'),
    textColor: document.getElementById('set-text-color'),
    bgColor: document.getElementById('set-bg-color'),
    mirrorH: document.getElementById('set-mirror-h'),
    mirrorV: document.getElementById('set-mirror-v'),
    eyeline: document.getElementById('set-eyeline'),
    eyelineV: document.getElementById('set-eyeline-v'),
    eyelineStyle: document.getElementById('set-eyeline-style'),
    hideTakes: document.getElementById('set-hide-takes'),
    showInvented: document.getElementById('set-show-invented')
  };

  function initFontStackSelect() {
    var stacks = MC.settings.FONT_STACKS;
    for (var i = 0; i < stacks.length; i++) {
      var opt = document.createElement('option');
      opt.value = stacks[i].value;
      opt.textContent = stacks[i].label;
      C.fontStack.appendChild(opt);
    }
    var custom = document.createElement('option');
    custom.value = '';
    custom.textContent = 'custom (type below)';
    C.fontStack.appendChild(custom);
  }

  function syncSettingsControls() {
    var s = settings;
    C.countdown.value = s['countdown-seconds'];
    C.fontFamily.value = s['font-family'];
    var found = false;
    for (var i = 0; i < C.fontStack.options.length; i++) {
      if (C.fontStack.options[i].value === s['font-family']) {
        C.fontStack.selectedIndex = i;
        found = true;
        break;
      }
    }
    if (!found) C.fontStack.value = '';
    C.fontSize.value = s['font-size'];
    C.fontSizeN.value = s['font-size'];
    C.lineHeight.value = s['line-height'];
    C.lineHeightV.textContent = Number(s['line-height']).toFixed(2);
    C.margin.value = s['margin-percent'];
    C.marginV.textContent = s['margin-percent'] + '%';
    C.textColor.value = s['text-color'];
    C.bgColor.value = s['background-color'];
    C.mirrorH.checked = !!s['mirror-h'];
    C.mirrorV.checked = !!s['mirror-v'];
    C.eyeline.value = s['eyeline-percent'];
    C.eyelineV.textContent = s['eyeline-percent'] + '%';
    C.eyelineStyle.value = s['eyeline-style'];
    C.hideTakes.checked = !!s['hide-takes'];
    C.showInvented.checked = !!s['show-invented'];
  }

  function wireSettingsControls() {
    C.wpm.addEventListener('change', function () {
      engine.setWpm(Number(C.wpm.value));
    });
    C.mode.addEventListener('change', applyModeControls);
    C.minutes.addEventListener('change', applyModeControls);
    C.countdown.addEventListener('change', function () {
      changeSetting('countdown-seconds', Math.max(0, Number(C.countdown.value) || 0));
    });
    C.fontStack.addEventListener('change', function () {
      if (C.fontStack.value) changeSetting('font-family', C.fontStack.value);
    });
    C.fontFamily.addEventListener('change', function () {
      if (C.fontFamily.value.trim()) changeSetting('font-family', C.fontFamily.value.trim());
    });
    C.fontSize.addEventListener('input', function () {
      changeSetting('font-size', Number(C.fontSize.value));
    });
    C.fontSizeN.addEventListener('change', function () {
      changeSetting('font-size', Number(C.fontSizeN.value));
    });
    C.lineHeight.addEventListener('input', function () {
      changeSetting('line-height', Number(C.lineHeight.value));
    });
    C.margin.addEventListener('input', function () {
      changeSetting('margin-percent', Number(C.margin.value));
    });
    C.textColor.addEventListener('input', function () {
      changeSetting('text-color', C.textColor.value);
    });
    C.bgColor.addEventListener('input', function () {
      changeSetting('background-color', C.bgColor.value);
    });
    C.mirrorH.addEventListener('change', function () {
      changeSetting('mirror-h', C.mirrorH.checked);
    });
    C.mirrorV.addEventListener('change', function () {
      changeSetting('mirror-v', C.mirrorV.checked);
    });
    C.eyeline.addEventListener('input', function () {
      changeSetting('eyeline-percent', Number(C.eyeline.value));
    });
    C.eyelineStyle.addEventListener('change', function () {
      changeSetting('eyeline-style', C.eyelineStyle.value);
    });
    C.hideTakes.addEventListener('change', function () {
      changeSetting('hide-takes', C.hideTakes.checked);
    });
    C.showInvented.addEventListener('change', function () {
      changeSetting('show-invented', C.showInvented.checked);
    });
  }

  function applyModeControls() {
    var timed = C.mode.value === 'timed';
    C.rowMinutes.classList.toggle('hidden', !timed);
    engine.setMode(timed ? 'timed' : 'manual', Number(C.minutes.value));
  }

  // ---------- panels ----------

  function togglePanel(el) {
    el.classList.toggle('hidden');
  }

  function closePanels() {
    els.sectionsDrawer.classList.add('hidden');
    els.settingsDrawer.classList.add('hidden');
    els.helpOverlay.classList.add('hidden');
  }

  document.addEventListener('click', function (e) {
    var closer = e.target.closest('[data-close]');
    if (closer) document.getElementById(closer.dataset.close).classList.add('hidden');
    if (e.target === els.helpOverlay) els.helpOverlay.classList.add('hidden');
  });

  // ---------- section list ----------

  function renderSectionList() {
    var list = els.sectionsList;
    while (list.firstChild) list.removeChild(list.firstChild);
    for (var i = 0; i < docIndex.sections.length; i++) {
      (function (sec, i2) {
        var li = document.createElement('li');
        li.dataset.sid = sec.id;
        var name = document.createElement('span');
        name.textContent = sec.heading || (i2 === 0 ? 'Preamble' : 'Untitled section');
        var words = document.createElement('span');
        words.className = 'sec-words';
        var next = docIndex.sections[i2 + 1];
        var count = (next ? next.wordStart : docIndex.wordCount) - sec.wordStart;
        words.textContent = count + ' w';
        li.appendChild(name);
        li.appendChild(words);
        li.addEventListener('click', function () {
          if (drives()) jumpSection(sec.id);
          else ws.cmd('jump-section', sec.id);
          els.sectionsDrawer.classList.add('hidden');
        });
        list.appendChild(li);
      })(docIndex.sections[i], i);
    }
    updateEstTime();
  }

  function updateEstTime() {
    var wpm = engine.view().wpm || 150;
    var wc = visibleWordCount();
    var mins = MC.model.estimateMinutes(wc, wpm);
    els.estTime.textContent = wc + ' words, est. ' +
      MC.model.fmtClock(mins === null ? null : mins * 60) + ' at ' + wpm + ' wpm';
  }

  function highlightCurrentSection(id) {
    var items = els.sectionsList.children;
    for (var i = 0; i < items.length; i++) {
      items[i].classList.toggle('current', items[i].dataset.sid === id);
    }
  }

  // ---------- document loading ----------

  function loadSource() {
    return MC.model.fetchSource(token).then(function (src) {
      var ratio = engine.getPositionRatio();
      docIndex = MC.model.renderDoc(src.doc, els.script);
      docVersion = src['doc-version'];
      applyDisplay();
      engine.setPositionRatio(ratio);
      renderSectionList();
      document.title = 'mc-prompter | ' + ((src.doc && src.doc.title) || 'prompt');
      markDirty();
    }).catch(function (err) {
      toast('script load failed: ' + err.message);
    });
  }

  // ---------- WS: state out (leader) ----------

  function markDirty() { stateDirty = true; maybeSendState(); }

  function buildSnapshot() {
    var v = engine.view();
    return {
      type: 'state',
      position: Math.round(v.position * 10000) / 10000,
      section: currentSectionId(),
      playing: v.playing || (v.countdown !== null),
      wpm: v.wpm,
      mode: v.mode,
      elapsed: Math.round(v.elapsed * 10) / 10,
      remaining: v.remaining === null ? null : Math.round(v.remaining * 10) / 10,
      countdown: v.countdown === null ? null : Math.round(v.countdown * 10) / 10
    };
  }

  // ~4 Hz while playing, prompt on changes (force), never a flood: forced
  // sends still respect a 100 ms floor and leave the dirty flag for the
  // heartbeat to flush.
  function maybeSendState(force) {
    if (!isLeader()) return;
    var now = Date.now();
    var minGap = force ? 100 : 250;
    if (now - lastStateSent < minGap) { stateDirty = true; return; }
    if (ws.send(buildSnapshot())) {
      lastStateSent = now;
      stateDirty = false;
    }
  }

  // ---------- engine callbacks ----------

  function updateHud(v) {
    els.clockElapsed.textContent = MC.model.fmtClock(v.elapsed);
    els.clockRemaining.textContent = MC.model.fmtClock(v.remaining);
    els.wpmVal.textContent = String(v.wpm);
    els.modeChip.textContent = v.mode;
    els.btnToggle.textContent = (v.playing || v.countdown !== null) ? 'pause' : 'play';

    if (v.mode === 'timed' && v.drift !== null) {
      var d = Math.round(v.drift);
      els.driftChip.classList.remove('hidden');
      if (Math.abs(d) <= 3) {
        els.driftChip.textContent = 'on plan';
        els.driftChip.className = 'chip good';
      } else if (d > 0) {
        els.driftChip.textContent = d + 's behind';
        els.driftChip.className = 'chip warn';
      } else {
        els.driftChip.textContent = (-d) + 's ahead';
        els.driftChip.className = 'chip';
      }
    } else {
      els.driftChip.classList.add('hidden');
    }

    if (v.countdown !== null) {
      els.countdown.classList.remove('hidden');
      els.countdownNum.textContent = String(Math.ceil(v.countdown));
    } else {
      els.countdown.classList.add('hidden');
    }

    highlightCurrentSection(currentSectionId());
  }

  function onEngineFrame(v) {
    if (everConnected && !ws.state.connected && (v.playing || v.countdown !== null)) {
      droveWhileDisconnected = true;
    }
    if (drives()) updateHud(v);
    if (v.playing) maybeSendState();
  }

  function onEngineChange(reason, v) {
    if (everConnected && !ws.state.connected && reason !== 'seed') {
      droveWhileDisconnected = true;
    }
    if (!drives()) return;
    updateHud(v);
    if (reason === 'speed' || reason === 'mode') updateEstTime();
    C.wpm.value = engine.getManualWpm();
    maybeSendState(true);
  }

  // ---------- playback intents ----------

  function startPlay() {
    if (engine.getPositionRatio() < 0.001 && Number(settings['countdown-seconds']) > 0) {
      engine.beginCountdown(Number(settings['countdown-seconds']));
    } else {
      engine.play();
    }
  }

  function handleCmd(msg) {
    // Commands are relayed to everyone; only the leader executes them.
    if (!isLeader()) return;
    var v = msg.value;
    switch (msg.cmd) {
      case 'play': startPlay(); break;
      case 'pause': engine.pause(); break;
      case 'toggle': engine.toggle(Number(settings['countdown-seconds'])); break;
      case 'restart': engine.restart(); break;
      case 'speed-delta': engine.deltaWpm(typeof v === 'number' ? v : 2); break;
      case 'speed-set': if (typeof v === 'number') engine.setWpm(v); break;
      case 'jump-section': if (v) jumpSection(String(v)); break;
      case 'jump-words': jumpParagraphs((Number(v) || 0) >= 0 ? 1 : -1); break;
      case 'countdown':
        engine.beginCountdown(typeof v === 'number' ? v : Number(settings['countdown-seconds']));
        break;
      default: break;
    }
  }

  // ---------- follower: apply leader state ----------

  function applyRemoteState(msg) {
    lastRemoteState = msg;
    if (isLeader()) return;
    var scrollableH = Math.max(els.surface.scrollHeight - els.surface.clientHeight, 1);
    els.surface.scrollTop = (Number(msg.position) || 0) * scrollableH;
    els.clockElapsed.textContent = MC.model.fmtClock(msg.elapsed);
    els.clockRemaining.textContent = MC.model.fmtClock(msg.remaining);
    els.wpmVal.textContent = String(msg.wpm);
    els.modeChip.textContent = msg.mode || 'manual';
    els.btnToggle.textContent = msg.playing ? 'pause' : 'play';
    if (msg.countdown !== null && msg.countdown !== undefined) {
      els.countdown.classList.remove('hidden');
      els.countdownNum.textContent = String(Math.ceil(msg.countdown));
    } else {
      els.countdown.classList.add('hidden');
    }
    highlightCurrentSection(msg.section || null);
  }

  // ---------- WS wiring ----------

  var wasLeader = false;
  var everConnected = false;          // this page has reached the server before
  var droveWhileDisconnected = false; // engine ran or the user drove it during an outage

  ws.onStatus(function (s) {
    els.conn.classList.toggle('on', s.connected);
    els.roleBadge.classList.toggle('hidden', s.leader || !s.connected);
    if (s.rejected) {
      // Token rejected (close 4403): the WS layer has stopped retrying, so
      // surface the failure instead of silently running standalone.
      els.tokenError.classList.remove('hidden');
      wasLeader = false;
      return;
    }
    if (s.connected && !s.leader) {
      // Connected follower (fresh join or demotion after a reconnect):
      // exactly one driver per session, so stop a locally running engine
      // before mirroring the leader.
      if (engine.isPlaying()) engine.pause();
      // Seed the display from the welcome snapshot until live state frames
      // arrive (a paused leader sends none).
      if (!lastRemoteState && s.snapshot) applyRemoteState(s.snapshot);
    }
    if (s.connected && s.leader && !wasLeader) {
      // Just became leader (first welcome, reconnect, or promotion after the
      // previous leader dropped): seed the engine from the freshest snapshot
      // we know about, then start reporting state. Skip the seed if this
      // page kept driving through the outage (standalone fallback): the
      // server's cached snapshot predates the disconnect and seeding from it
      // would rewind the scroll mid-read.
      var snap = lastRemoteState || s.snapshot;
      if (snap && !droveWhileDisconnected) engine.seed(snap);
      markDirty();
    }
    if (s.connected) {
      everConnected = true;
      droveWhileDisconnected = false;
    }
    wasLeader = s.connected && s.leader;
  });

  ws.on('cmd', handleCmd);
  ws.on('state', applyRemoteState);
  ws.on('doc-updated', function (msg) {
    if (msg['doc-version'] !== docVersion) loadSource();
  });
  ws.on('error', function (msg) { toast('server: ' + msg.message); });

  // ---------- input: keyboard, wheel, click, scroll ----------

  document.addEventListener('keydown', function (e) {
    var tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

    var leaderAct = drives();
    function act(fn, cmdName, cmdValue) {
      if (leaderAct) fn();
      else ws.cmd(cmdName, cmdValue);
    }

    switch (e.key) {
      case ' ':
        e.preventDefault();
        act(function () { engine.toggle(Number(settings['countdown-seconds'])); }, 'toggle');
        break;
      case 'ArrowUp':
      case '+':
      case '=':
        e.preventDefault();
        act(function () { engine.deltaWpm(2); }, 'speed-delta', 2);
        break;
      case 'ArrowDown':
      case '-':
        e.preventDefault();
        act(function () { engine.deltaWpm(-2); }, 'speed-delta', -2);
        break;
      case 'ArrowLeft':
        e.preventDefault();
        act(function () { jumpParagraphs(-1); }, 'jump-words', -1);
        break;
      case 'ArrowRight':
        e.preventDefault();
        act(function () { jumpParagraphs(1); }, 'jump-words', 1);
        break;
      case 'Home':
        e.preventDefault();
        act(function () { engine.restart(); }, 'restart');
        break;
      case 'f':
      case 'F':
        e.preventDefault();
        if (document.fullscreenElement) document.exitFullscreen();
        else document.documentElement.requestFullscreen();
        break;
      case 'm':
      case 'M': {
        e.preventDefault();
        // Cycle off -> h -> v -> both -> off.
        var h = settings['mirror-h'], vv = settings['mirror-v'];
        var next = !h && !vv ? [true, false] : h && !vv ? [false, true] : !h && vv ? [true, true] : [false, false];
        changeSetting('mirror-h', next[0]);
        changeSetting('mirror-v', next[1]);
        toast('mirror: ' + (next[0] && next[1] ? 'both' : next[0] ? 'horizontal' : next[1] ? 'vertical' : 'off'));
        break;
      }
      case 'e':
      case 'E':
        e.preventDefault();
        eyelineHidden = !eyelineHidden;
        els.eyeline.classList.toggle('off', eyelineHidden);
        break;
      case 'c':
      case 'C':
        e.preventDefault();
        act(function () { engine.beginCountdown(Number(settings['countdown-seconds'])); },
            'countdown', Number(settings['countdown-seconds']));
        break;
      case 's':
      case 'S':
        e.preventDefault();
        togglePanel(els.sectionsDrawer);
        break;
      case 'd':
      case 'D':
        e.preventDefault();
        togglePanel(els.settingsDrawer);
        break;
      case '?':
        e.preventDefault();
        togglePanel(els.helpOverlay);
        break;
      case 'Escape':
        closePanels();
        break;
      default:
        break;
    }
  });

  els.surface.addEventListener('wheel', function (e) {
    e.preventDefault();
    var d = e.deltaY < 0 ? 2 : -2;
    if (drives()) engine.deltaWpm(d);
    else ws.cmd('speed-delta', d);
  }, { passive: false });

  els.surface.addEventListener('click', function (e) {
    var w = e.target.closest('.w');
    if (!w || !drives()) return;
    // Click-to-jump: put the clicked word on the eyeline.
    // Phase B seam: this same span is the click-to-anchor target.
    engine.jumpToPx(w.offsetTop - eyelinePx());
  });

  els.surface.addEventListener('scroll', function () {
    engine.adoptScrollTop();
  });

  window.addEventListener('resize', function () {
    var ratio = engine.getPositionRatio();
    engine.recalc();
    engine.setPositionRatio(ratio);
  });

  // HUD auto-fade while playing.
  function pokeHud() {
    els.hud.classList.remove('faded');
    clearTimeout(hudTimer);
    hudTimer = setTimeout(function () {
      if (engine.isPlaying()) els.hud.classList.add('faded');
    }, 3000);
  }
  document.addEventListener('mousemove', pokeHud);
  pokeHud();

  // ---------- HUD buttons ----------

  els.btnToggle.addEventListener('click', function () {
    if (drives()) engine.toggle(Number(settings['countdown-seconds']));
    else ws.cmd('toggle');
  });
  els.btnRestart.addEventListener('click', function () {
    if (drives()) engine.restart();
    else ws.cmd('restart');
  });
  document.getElementById('btn-sections').addEventListener('click', function () {
    togglePanel(els.sectionsDrawer);
  });
  document.getElementById('btn-settings').addEventListener('click', function () {
    togglePanel(els.settingsDrawer);
  });
  document.getElementById('btn-help').addEventListener('click', function () {
    togglePanel(els.helpOverlay);
  });

  // Send-state heartbeat: 4 Hz while playing, on-change otherwise.
  setInterval(function () {
    if (isLeader() && (engine.isPlaying() || stateDirty)) maybeSendState();
  }, 250);

  // ---------- boot ----------

  initFontStackSelect();
  applyDisplay();
  syncSettingsControls();
  wireSettingsControls();

  MC.model.fetchState(token).then(function (state) {
    var wpm = state && state.config && state.config['owner-wpm'];
    if (wpm) {
      engine.setWpm(wpm);
      C.wpm.value = wpm;
    }
  }).catch(function () { /* defaults are fine */ }).then(loadSource);
})();
