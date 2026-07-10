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
 *
 * Voice follow (Phase B): OFF by default; the toggle (settings drawer, HUD
 * chip, key v) only appears on loopback pages when /api/state reports ASR
 * available, and only the leader may enable it. Enabling opens the preflight
 * panel (device picker, level meter, applied-constraints readback, live
 * partial line, tracking check) and requests capture ownership over the WS
 * ({"type":"capture-request"} -> capture-granted|capture-denied). Granted
 * frames flow: MC.audio worklet -> sendAudioFrame -> ws.sendBinary, gated on
 * capture-granted and ws.bufferedAmount < 256 KiB (dropped and counted
 * otherwise). Incoming anchor frames ease the engine toward the anchor word
 * at the eyeline (engine follow mode; WPM scroll suspended), tint matched
 * words in O(changed words) batches, and drive the FOLLOWING/HOLD/BEHIND
 * state chip (vad silence or held anchors freeze the target; asr-status
 * behind shows BEHIND). Clicking a word while following sends
 * {"type":"anchor-set","i"} instead of a local jump. State snapshots carry
 * follow:true while voice follow drives, so followers and the remote render
 * FOLLOWING instead of paused, and transport commands answer with a toast.
 * Losing the server connection (or a 4403 token rejection) turns voice
 * follow off, stops the mic, and returns manual controls immediately.
 *
 * Dev seam: ?sim-audio=1 adds "sim wav" buttons that fetch and decode a WAV
 * (path via prompt) and stream it through the exact same sendAudioFrame path
 * as the mic, so the end-to-end can be tested without a human speaking.
 *
 * Producer mode (Phase C): active when producer frames arrive (or /api/state
 * reports producer.active). The ambient rail docks top or bottom (rail-dock
 * setting); cue / cue-clear frames drive the single cue card near the
 * eyeline. Scripted rundown segments use the normal scroll surface; bullets
 * segments switch the stage to a large-type rail view (current point huge,
 * next below, the rest dimmed). Segment handoff is manual (n / p keys, the
 * remote's make-current) and, while voice follow drives the leader, anchor
 * crossing a scripted segment's word-end sends make-current for the next
 * segment. GO LIVE (big button pre-show, g key), hold / resume (h), and a
 * two-tap end-show live in the HUD. All show / point commands go out as
 * {"type":"show","cmd":...} / {"type":"point","cmd":...,"segment","point"};
 * nothing here changes tier 1/2 behavior when no rundown is loaded.
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

  // ---------- voice-follow state (Phase B) ----------

  var isLoopbackHost =
    ['localhost', '127.0.0.1', '[::1]'].indexOf(location.hostname) >= 0;
  var simAudio = new URLSearchParams(location.search).get('sim-audio') === '1';
  var SEND_BUFFER_LIMIT = 256 * 1024; // bytes queued on the WS before dropping

  var asrAvailable = false;   // /api/state .asr.available
  var followEnabled = false;  // session-only, never persisted
  var captureGranted = false; // this connection owns binary audio frames
  var capturePending = false; // capture-request sent, no verdict yet
  var speaking = false;       // last vad frame
  var asrReady = false;       // last asr-status frame
  var asrBehind = false;
  var anchorHeld = false;     // last anchor frame
  var micError = false;       // capture reported an error (device lost etc.)
  var lastAnchor = -1;        // committed global word index
  var tintedUpTo = -1;        // highest word index carrying the said tint
  var framesDropped = 0;      // audio frames dropped by the send gate
  var preflightBaseline = -1; // anchor when preflight opened (tracking check)
  var trackingPassed = false;
  var simStream = null;       // active sim-audio streamer
  var simActive = false;      // mutes mic frames while the sim streams

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

    // Producer chrome (Phase C): the cue card rides the eyeline, the rail
    // follows the dock setting, and the bullets stage follows the reading
    // colors so a beam-splitter rig keeps one look across segment kinds.
    // The cue card is clamped to the free right margin so it never covers
    // the words at the eyeline; when that margin is too narrow for a
    // readable card, it docks as a band whose bottom edge sits just above
    // the eyeline (over already-read text), keeping the read line clear.
    var cueSideVw = Number(s['margin-percent']) - 2; // minus the 1vw offsets
    if (cueSideVw >= 8) {
      P.cueRegion.classList.remove('dock-band');
      P.cueRegion.style.top = s['eyeline-percent'] + '%';
      P.cueRegion.style.bottom = '';
      P.cueRegion.style.maxWidth = 'min(' + cueSideVw + 'vw, 22rem)';
    } else {
      P.cueRegion.classList.add('dock-band');
      P.cueRegion.style.top = 'auto';
      P.cueRegion.style.bottom =
        'calc(' + (100 - Number(s['eyeline-percent'])) + '% + 1rem)';
      P.cueRegion.style.maxWidth = '';
    }
    P.rail.classList.toggle('dock-top', s['rail-dock'] !== 'bottom');
    P.rail.classList.toggle('dock-bottom', s['rail-dock'] === 'bottom');
    P.bstage.style.background = s['background-color'];
    P.bstage.style.color = s['text-color'];
    P.bstage.style.fontFamily = s['font-family'];
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
    showInvented: document.getElementById('set-show-invented'),
    railDock: document.getElementById('set-rail-dock')
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
    C.railDock.value = s['rail-dock'] === 'bottom' ? 'bottom' : 'top';
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
    C.railDock.addEventListener('change', function () {
      changeSetting('rail-dock', C.railDock.value);
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
      // A new doc means new word indexing: the server rebuilds the aligner
      // and re-broadcasts the anchor, so drop all local match state.
      tintedUpTo = -1;
      lastAnchor = -1;
      anchorHeld = false;
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
      // Voice follow drives the leader: followers and the remote render
      // FOLLOWING instead of paused (playing stays false in follow mode).
      // The server relays state frames verbatim, so no server change.
      follow: !!v.follow,
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
    els.modeChip.textContent = v.follow ? 'follow' : v.mode;
    els.btnToggle.textContent = v.follow
      ? 'following'
      : (v.playing || v.countdown !== null) ? 'pause' : 'play';

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
    // Follow mode counts as driving: a follow-mode leader that rode out an
    // outage must not be re-seeded from the server's stale snapshot.
    if (everConnected && !ws.state.connected &&
        (v.playing || v.countdown !== null || v.follow)) {
      droveWhileDisconnected = true;
    }
    if (drives()) updateHud(v);
    if (v.playing || v.follow) maybeSendState();
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

  // Transport intents are dead while voice follow drives the scroll
  // (engine.play/beginCountdown early-return in follow mode). Surface that
  // as a toast instead of a silent no-op; remotes and followers also render
  // FOLLOWING from the follow flag in state snapshots.
  function followBlocksTransport() {
    if (!engine.isFollowing()) return false;
    toast('voice follow drives the scroll; press v to turn it off');
    maybeSendState(true);
    return true;
  }

  function handleCmd(msg) {
    // Commands are relayed to everyone; only the leader executes them.
    if (!isLeader()) return;
    if ((msg.cmd === 'play' || msg.cmd === 'pause' || msg.cmd === 'toggle' ||
         msg.cmd === 'countdown') && followBlocksTransport()) {
      return;
    }
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
    // While the leader voice-follows (follow flag in the snapshot), the
    // view is in motion even though playing is false: say FOLLOWING, not
    // paused.
    els.modeChip.textContent = msg.follow ? 'follow' : (msg.mode || 'manual');
    els.btnToggle.textContent = msg.follow
      ? 'following'
      : msg.playing ? 'pause' : 'play';
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
    els.roleBadge.classList.toggle('hidden', s.leader || !s.connected || !s.leaderKnown);
    if (!s.connected && (captureGranted || capturePending)) {
      // Capture ownership dies with the connection (server releases it on
      // disconnect); the welcome handler re-requests it after reconnect.
      captureGranted = false;
      capturePending = false;
      updateVoiceChips();
    }
    if (!s.connected && followEnabled) {
      // The server (and with it ASR and anchor frames) is gone: fall back
      // to manual pacing so play/pause work again immediately and the mic
      // is released, whether this is a transient drop or a terminal one
      // (server killed, token rejected). The follow-off change fires while
      // disconnected, which marks the engine as locally driven, so a later
      // reconnect will not rewind the scroll from the server's stale
      // snapshot.
      setFollowEnabled(false);
      toast(s.rejected
        ? 'voice follow off: the session token was rejected'
        : 'voice follow off: server connection lost; manual controls are back');
    }
    if (s.connected && s.leaderKnown && !s.leader && followEnabled) {
      // Demoted to follower: this page no longer drives the engine, so
      // voice follow (which drives it) must release everything. Leadership
      // is only trusted once a welcome or role frame said who leads
      // (leaderKnown); between onopen and welcome it is unknown, and acting
      // on it here used to kill voice follow on every transient reconnect.
      setFollowEnabled(false);
      toast('voice follow off: another display leads this session');
    }
    if (s.rejected) {
      // Token rejected (close 4403): the WS layer has stopped retrying, so
      // surface the failure instead of silently running standalone. Voice
      // follow and the mic were already torn down above.
      els.tokenError.classList.remove('hidden');
      wasLeader = false;
      return;
    }
    if (s.connected && s.leaderKnown && !s.leader) {
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
    if (s.connected && s.leaderKnown) {
      // Only settle once leadership is known (the welcome or role frame):
      // resetting droveWhileDisconnected on the bare onopen status would
      // defeat the reseed guard before the welcome branch above could see it.
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
        act(function () {
          if (followBlocksTransport()) return;
          engine.toggle(Number(settings['countdown-seconds']));
        }, 'toggle');
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
        // Bullets rail view (producer mode): the clicker's forward key marks
        // the current point covered instead of a meaningless paragraph jump.
        if (bulletsViewActive()) { coverCurrentPoint(); break; }
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
        act(function () {
          if (followBlocksTransport()) return;
          engine.beginCountdown(Number(settings['countdown-seconds']));
        }, 'countdown', Number(settings['countdown-seconds']));
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
      case 'v':
      case 'V':
        e.preventDefault();
        setFollowEnabled(!followEnabled);
        break;
      case 'g':
      case 'G':
        if (!producerActive || !producerState || producerState.live) break;
        e.preventDefault();
        sendShow('go-live');
        break;
      case 'h':
      case 'H':
        if (!producerActive || !producerState || !producerState.live) break;
        e.preventDefault();
        sendShow(producerState.hold ? 'resume' : 'hold');
        break;
      case 'n':
      case 'N':
        if (!producerActive) break;
        e.preventDefault();
        producerAdvance(1);
        break;
      case 'p':
      case 'P':
        if (!producerActive) break;
        e.preventDefault();
        producerAdvance(-1);
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
    if (!w) return;
    if (followEnabled && ws.state.connected) {
      // Voice follow: clicking a word re-anchors the aligner (the human is
      // the authority); the server broadcasts the new anchor back and the
      // follow easing takes the view there.
      ws.send({ type: 'anchor-set', i: Number(w.dataset.i) });
      return;
    }
    if (!drives()) return;
    // Click-to-jump: put the clicked word on the eyeline.
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
    if (drives()) {
      if (followBlocksTransport()) return;
      engine.toggle(Number(settings['countdown-seconds']));
    } else {
      ws.cmd('toggle');
    }
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

  // Send-state heartbeat: 4 Hz while playing or following, on-change otherwise.
  setInterval(function () {
    if (isLeader() && (engine.isPlaying() || engine.isFollowing() || stateDirty)) {
      maybeSendState();
    }
  }, 250);

  // ---------- voice follow (Phase B) ----------

  var V = {
    voiceChip: document.getElementById('voice-chip'),
    voiceStateChip: document.getElementById('voice-state-chip'),
    btnSim: document.getElementById('btn-sim'),
    voiceSep: document.getElementById('voice-sep'),
    voiceH: document.getElementById('voice-h'),
    rowFollow: document.getElementById('row-follow'),
    setFollow: document.getElementById('set-follow'),
    preflight: document.getElementById('preflight'),
    pfDevice: document.getElementById('pf-device'),
    pfLevelBar: document.getElementById('pf-level-bar'),
    pfConstraints: document.getElementById('pf-constraints'),
    pfStatus: document.getElementById('pf-status'),
    pfPartial: document.getElementById('pf-partial'),
    pfTrack: document.getElementById('pf-track'),
    pfSim: document.getElementById('pf-sim'),
    pfCancel: document.getElementById('pf-cancel'),
    pfStart: document.getElementById('pf-start')
  };

  var capture = MC.audio.createCapture({
    onFrame: function (pcm) {
      // The sim seam mutes the mic source so streams never interleave;
      // everything below the source runs the identical send path.
      if (!simActive) sendAudioFrame(pcm);
    },
    onLevel: updateLevel,
    onError: function (err) {
      var msg = 'microphone error: ' + (err && err.message ? err.message : err);
      pfStatus(msg, true);
      if (!followEnabled) return;
      // Mic died mid-follow (unplugged, Bluetooth dropped, permission pulled):
      // make it loud instead of a silent freeze, and reopen the device picker
      // so the reader can recover without hunting for the v key.
      micError = true;
      toast(msg);
      updateVoiceChips();
      // openPreflight auto-restarts capture once (the saved deviceId is a
      // bare preference, so a vanished device falls back to the default
      // input); if the panel is already open, the reader picks from the
      // refreshed list, which avoids an onError -> retry -> onError loop.
      if (!preflightOpen()) openPreflight();
      refreshDeviceList(null);
      pfStatus(msg, true);
    }
  });

  // THE audio send path (mic and sim both end here): WS binary, gated on
  // capture ownership and socket backpressure; gated-out frames are dropped
  // and counted, never queued.
  function sendAudioFrame(pcm) {
    if (!captureGranted) { framesDropped += 1; return false; }
    if (ws.bufferedAmount() >= SEND_BUFFER_LIMIT) { framesDropped += 1; return false; }
    if (!ws.sendBinary(pcm.buffer)) { framesDropped += 1; return false; }
    return true;
  }

  function voiceOffered() {
    return isLoopbackHost && asrAvailable;
  }

  function preflightOpen() {
    return !V.preflight.classList.contains('hidden');
  }

  function pfStatus(msg, bad) {
    V.pfStatus.textContent = msg;
    V.pfStatus.classList.toggle('bad', !!bad);
  }

  function updateLevel(rms) {
    if (!preflightOpen()) return;
    // Speech RMS lives around 0.05..0.3; x4 makes the meter readable.
    var pct = Math.min(1, rms * 4) * 100;
    V.pfLevelBar.style.width = pct.toFixed(1) + '%';
  }

  function updateVoiceChips() {
    V.voiceChip.textContent = followEnabled ? 'voice on' : 'voice off';
    V.voiceChip.classList.toggle('on', followEnabled);
    if (!followEnabled) {
      V.voiceStateChip.classList.add('hidden');
      return;
    }
    V.voiceStateChip.classList.remove('hidden');
    if (micError) {
      V.voiceStateChip.textContent = 'MIC ERROR';
      V.voiceStateChip.className = 'chip bad';
    } else if (asrBehind) {
      V.voiceStateChip.textContent = 'BEHIND';
      V.voiceStateChip.className = 'chip bad';
    } else if (anchorHeld || !speaking) {
      V.voiceStateChip.textContent = 'HOLD';
      V.voiceStateChip.className = 'chip warn';
    } else {
      V.voiceStateChip.textContent = 'FOLLOWING';
      V.voiceStateChip.className = 'chip good';
    }
  }

  function syncVoiceControls() {
    var show = voiceOffered();
    V.voiceChip.classList.toggle('hidden', !show);
    V.voiceSep.classList.toggle('hidden', !show);
    V.voiceH.classList.toggle('hidden', !show);
    V.rowFollow.classList.toggle('hidden', !show);
    V.setFollow.checked = followEnabled;
    V.btnSim.classList.toggle('hidden', !(simAudio && show));
    updateVoiceChips();
  }

  // Batched match tinting: only the words whose state changed since the
  // last anchor are touched, so cost is O(changed words) per frame.
  function applyTint(anchor) {
    var words = docIndex.words;
    var upTo = Math.min(anchor, words.length - 1);
    var i;
    if (upTo > tintedUpTo) {
      for (i = Math.max(tintedUpTo + 1, 0); i <= upTo; i++) {
        words[i].classList.add('said');
      }
    } else if (upTo < tintedUpTo) {
      for (i = Math.max(upTo + 1, 0); i <= tintedUpTo && i < words.length; i++) {
        words[i].classList.remove('said');
      }
    }
    tintedUpTo = upTo;
  }

  function updateTrackCheck() {
    if (!preflightOpen()) return;
    if (!trackingPassed && lastAnchor - preflightBaseline >= 3) {
      trackingPassed = true;
      V.pfStart.disabled = false;
    }
    if (trackingPassed) {
      V.pfTrack.textContent = 'tracking OK: the prompter is following you';
      V.pfTrack.className = 'chip good';
    } else {
      V.pfTrack.textContent = 'tracking: read the script from the eyeline until this passes';
      V.pfTrack.className = 'chip warn';
    }
  }

  // ----- capture ownership over the WS -----

  function requestCapture() {
    if (captureGranted || capturePending) return;
    if (ws.send({ type: 'capture-request' })) capturePending = true;
    else if (preflightOpen()) pfStatus('waiting for the server connection...', true);
  }

  ws.on('welcome', function () {
    // Every (re)connect is a new connection: ownership was released with
    // the old one, so re-request it whenever voice follow wants the mic.
    captureGranted = false;
    capturePending = false;
    if (followEnabled) requestCapture();
  });

  ws.on('capture-granted', function () {
    capturePending = false;
    captureGranted = true;
    if (preflightOpen()) pfStatus('microphone ownership granted; starting capture...');
    if (!capture.running && !simActive) startMic(MC.audio.getSavedMic());
    updateVoiceChips();
  });

  ws.on('capture-denied', function (msg) {
    capturePending = false;
    captureGranted = false;
    var reason = msg && msg.reason;
    var text = reason === 'owned'
      ? 'another page owns audio capture for this session; close it or release capture there'
      : reason === 'loopback-only'
        ? 'audio capture is only allowed from the machine running the server'
        : 'capture denied: ' + reason;
    if (preflightOpen()) pfStatus(text, true);
    else toast(text);
  });

  // ----- ASR / VAD / anchor frames (broadcast to all clients) -----

  ws.on('asr', function (msg) {
    if (msg.kind !== 'partial' && msg.kind !== 'final') return;
    if (preflightOpen()) {
      V.pfPartial.textContent = msg.text || '';
      V.pfPartial.classList.add('live');
    }
  });

  ws.on('vad', function (msg) {
    speaking = !!msg.speaking;
    updateVoiceChips();
  });

  ws.on('asr-status', function (msg) {
    asrReady = !!msg.ready;
    asrBehind = !!msg.behind;
    updateVoiceChips();
    if (preflightOpen() && captureGranted) {
      pfStatus(asrReady
        ? 'ASR ready' + (asrBehind ? ' (running behind real time)' : '')
        : 'ASR loading...');
    }
  });

  ws.on('anchor', function (msg) {
    if (typeof msg.i === 'number') lastAnchor = msg.i;
    anchorHeld = !!msg.held;
    // Tint is cosmetic and renders on every page that gets anchor frames,
    // leader and followers alike.
    applyTint(lastAnchor);
    updateTrackCheck();
    updateVoiceChips();
    if (followEnabled && drives() && engine.isFollowing() && !anchorHeld) {
      var span = docIndex.words[lastAnchor];
      // Skip words inside hidden TAKE paragraphs (offsetParent null): they
      // have no geometry to scroll to.
      if (span && span.offsetParent !== null) {
        engine.setFollowTargetPx(span.offsetTop - eyelinePx());
      }
    }
  });

  // ----- preflight panel -----

  function renderConstraints(info) {
    var list = V.pfConstraints;
    while (list.firstChild) list.removeChild(list.firstChild);
    function row(text, warn) {
      var li = document.createElement('li');
      li.textContent = text;
      if (warn) li.className = 'warn';
      list.appendChild(li);
    }
    function onOff(v) {
      return v === true ? 'on' : v === false ? 'off' : 'not reported';
    }
    var s = info.settings || {};
    row('device: ' + (info.label || '(unnamed input)'));
    row('capture rate: ' + info.contextRate + ' Hz' +
        (info.resampling ? ' (worklet resamples to 16 kHz)' : ''));
    row('echo cancellation: ' + onOff(s.echoCancellation), s.echoCancellation === true);
    row('noise suppression: ' + onOff(s.noiseSuppression), s.noiseSuppression === true);
    row('auto gain control: ' + onOff(s.autoGainControl), s.autoGainControl === true);
    row('channels: ' + (typeof s.channelCount === 'number' ? s.channelCount : 'not reported'),
        typeof s.channelCount === 'number' && s.channelCount > 1);
    for (var i = 0; i < (info.warnings || []).length; i++) {
      row('warning: ' + info.warnings[i], true);
    }
  }

  function refreshDeviceList(activeId) {
    MC.audio.listInputs().then(function (devices) {
      var sel = V.pfDevice;
      while (sel.firstChild) sel.removeChild(sel.firstChild);
      for (var i = 0; i < devices.length; i++) {
        var opt = document.createElement('option');
        opt.value = devices[i].deviceId;
        opt.textContent = devices[i].label || ('microphone ' + (i + 1));
        sel.appendChild(opt);
      }
      var want = activeId || MC.audio.getSavedMic();
      if (want) sel.value = want;
    }).catch(function () { /* picker stays empty; capture still works */ });
  }

  function startMic(deviceId) {
    pfStatus('starting microphone...');
    capture.start(deviceId).then(function (info) {
      micError = false;
      renderConstraints(info);
      refreshDeviceList(info.deviceId);
      pfStatus(asrReady ? 'ASR ready' : 'capture running; waiting for ASR...');
      updateVoiceChips();
    }).catch(function () { /* onError already reported it (superseded starts stay silent) */ });
  }

  function openPreflight() {
    V.preflight.classList.remove('hidden');
    preflightBaseline = lastAnchor;
    trackingPassed = false;
    V.pfStart.disabled = true;
    V.pfPartial.textContent = 'waiting for speech...';
    V.pfPartial.classList.remove('live');
    V.pfSim.classList.toggle('hidden', !simAudio);
    updateTrackCheck();
    pfStatus(ws.state.connected
      ? 'requesting microphone ownership...'
      : 'waiting for the server connection...');
    requestCapture();
    if (captureGranted && !capture.running && !simActive) {
      startMic(MC.audio.getSavedMic());
    }
  }

  V.pfDevice.addEventListener('change', function () {
    var id = V.pfDevice.value;
    if (!id) return;
    MC.audio.saveMic(id);
    if (!simActive) startMic(id);
  });

  V.pfCancel.addEventListener('click', function () {
    setFollowEnabled(false);
  });

  V.pfStart.addEventListener('click', function () {
    // Capture keeps running and follow stays on; the panel just goes away.
    V.preflight.classList.add('hidden');
  });

  // ----- the follow toggle -----

  function setFollowEnabled(on) {
    on = !!on;
    if (on && !voiceOffered()) {
      if (isLoopbackHost && !asrAvailable) toast('voice follow needs the ASR workspace (tier 2)');
      syncVoiceControls();
      return;
    }
    if (on && !(ws.state.connected && isLeader())) {
      toast(ws.state.connected
        ? 'voice follow is driven by the leading display; this page only mirrors'
        : 'voice follow needs the server connection');
      syncVoiceControls();
      return;
    }
    if (followEnabled === on) { syncVoiceControls(); return; }
    followEnabled = on;
    if (on) {
      engine.setFollow(true);
      if (!captureGranted) openPreflight();
      else if (!capture.running && !simActive) startMic(MC.audio.getSavedMic());
    } else {
      engine.setFollow(false);
      V.preflight.classList.add('hidden');
      stopSim();
      capture.stop();
      if (captureGranted) ws.send({ type: 'capture-release' });
      captureGranted = false;
      capturePending = false;
      micError = false;
    }
    syncVoiceControls();
    updateHud(engine.view());
    markDirty();
  }

  V.voiceChip.addEventListener('click', function () {
    setFollowEnabled(!followEnabled);
  });

  V.setFollow.addEventListener('change', function () {
    setFollowEnabled(V.setFollow.checked);
  });

  // ----- ?sim-audio=1 dev seam -----

  function stopSim() {
    if (simStream) simStream.stop();
    simStream = null;
    simActive = false;
  }

  function runSim() {
    if (!simAudio) return;
    var path = window.prompt(
      'WAV path (same-origin URL, e.g. /static/fixtures/spike.wav)');
    if (!path) return;
    fetch(path).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' fetching ' + path);
      return r.arrayBuffer();
    }).then(MC.audio.wavToFrames).then(function (frames) {
      stopSim();
      simActive = true;
      toast('sim: streaming ' + frames.length + ' frames (~' +
            Math.round(frames.length * 0.12) + ' s)');
      simStream = MC.audio.streamFrames(frames, function (pcm, rms) {
        updateLevel(rms);
        sendAudioFrame(pcm); // the exact mic send path
      }, function () {
        simActive = false;
        simStream = null;
        toast('sim: done (' + framesDropped + ' frames dropped total)');
      });
    }).catch(function (err) {
      toast('sim failed: ' + err.message);
    });
  }

  V.btnSim.addEventListener('click', runSim);
  V.pfSim.addEventListener('click', runSim);

  // ---------- producer mode (Phase C) ----------

  var P = {
    rail: document.getElementById('prompter-rail'),
    cueRegion: document.getElementById('cue-region'),
    goLivePanel: document.getElementById('golive-panel'),
    btnGoLive: document.getElementById('btn-golive'),
    prodBadge: document.getElementById('prod-badge'),
    btnHold: document.getElementById('btn-hold'),
    btnEnd: document.getElementById('btn-end'),
    bstage: document.getElementById('bullets-stage'),
    bSeg: document.getElementById('bstage-seg'),
    bCurrent: document.getElementById('bstage-current'),
    bNext: document.getElementById('bstage-next'),
    bRest: document.getElementById('bstage-rest'),
    bDone: document.getElementById('bstage-done'),
    prodSep: document.getElementById('prod-sep'),
    prodH: document.getElementById('prod-h'),
    rowRailDock: document.getElementById('row-rail-dock')
  };

  var producerActive = false;
  var producerState = null;    // latest producer frame or pre-show synth
  var rundownInfo = null;      // {rundown, ranges} from GET /api/rundown
  var prevSegmentId = null;    // for segment-handoff detection
  var bulletsKey = '';         // rebuild the bullets DOM only on change
  var handoffSentFor = null;   // scripted segment already advanced past
  var handoffArmed = false;    // an anchor below the current segment's end
                               // was seen since it became current
  var coverPendingKey = null;  // covered sent, waiting for the next frame
  var prodEndArmed = false;
  var prodEndTimer = null;

  var prodRail = MC.rail.createRail(P.rail);
  var cueCard = MC.rail.createCueCard(P.cueRegion);

  function sendShow(cmd) { ws.send({ type: 'show', cmd: cmd }); }

  function sendPoint(cmd, segId, idx) {
    var m = { type: 'point', cmd: cmd, segment: segId };
    if (idx !== undefined && idx !== null) m.point = idx;
    ws.send(m);
  }

  function currentProdSegment() {
    return producerState ? MC.rail.segById(producerState, producerState.current) : null;
  }

  function bulletsViewActive() {
    return producerActive && !P.bstage.classList.contains('hidden');
  }

  function activateProducer() {
    if (producerActive) return;
    producerActive = true;
    P.prodSep.classList.remove('hidden');
    P.prodH.classList.remove('hidden');
    P.rowRailDock.classList.remove('hidden');
    P.prodBadge.classList.remove('hidden');
    if (!rundownInfo) fetchRundownInfo();
  }

  function fetchRundownInfo() {
    MC.model.fetchRundown(token).then(function (resp) {
      rundownInfo = MC.rail.normalizeRundown(resp);
      // Pre-show seed: the server broadcasts producer frames only on
      // change, so render the reconciled plan until the first one lands.
      if (rundownInfo && !producerState) {
        applyProducerState(MC.rail.preShowState(rundownInfo.rundown));
      }
    }).catch(function () { /* producer frames still drive the rail */ });
  }

  function disarmProdEnd() {
    prodEndArmed = false;
    clearTimeout(prodEndTimer);
    P.btnEnd.classList.remove('armed');
    P.btnEnd.textContent = 'end';
  }

  function renderProducerControls(state) {
    var live = !!state.live;
    var preShow = !live && (state['elapsed-s'] || 0) === 0;
    P.goLivePanel.classList.toggle('hidden',
      !(producerActive && preShow && ws.state.connected));
    P.prodBadge.textContent = live ? (state.hold ? 'HOLD' : 'LIVE')
      : preShow ? 'PRE-SHOW' : 'ENDED';
    P.prodBadge.className = 'chip ' + (live ? (state.hold ? 'warn' : 'live') : '');
    P.btnHold.classList.toggle('hidden', !live);
    P.btnHold.textContent = state.hold ? 'resume' : 'hold';
    P.btnEnd.classList.toggle('hidden', !live);
    if (!live) disarmProdEnd();
  }

  // Manual segment handoff: n / p keys (and the remote's make-current).
  function producerAdvance(delta) {
    if (!producerState) return;
    var segs = producerState.segments || [];
    var idx = -1;
    for (var i = 0; i < segs.length; i++) {
      if (segs[i].id === producerState.current) { idx = i; break; }
    }
    var t = idx + delta;
    if (t < 0 || t >= segs.length) {
      toast('no ' + (delta > 0 ? 'next' : 'previous') + ' segment');
      return;
    }
    sendPoint('make-current', segs[t].id);
    toast('segment: ' + (segs[t].title || segs[t].id));
  }

  // Bullets rail view: the clicker forward key covers the current point;
  // once the segment is fully covered it advances to the next segment.
  function coverCurrentPoint() {
    var seg = currentProdSegment();
    if (!seg) return;
    var idx = MC.rail.activePointIndex(seg);
    if (idx < 0) { producerAdvance(1); return; }
    var key = seg.id + ':' + idx;
    if (coverPendingKey === key) return; // debounce until the next frame
    coverPendingKey = key;
    sendPoint('covered', seg.id, idx);
  }

  // Scripted <-> bullets stage switching and the make-current scroll jump.
  function onSegmentChange(seg) {
    if (!seg) return;
    if (seg.kind === 'bullets') {
      // The scroll surface is covered by the bullets stage; stop the WPM
      // integrator so elapsed reading time is not silently consumed.
      if (drives() && engine.isPlaying()) engine.pause();
      return;
    }
    // Scripted: put the segment's first word on the eyeline. In voice
    // follow the anchor is normally already there (the jump is a no-op);
    // a make-current jump from the remote lands here too.
    var range = rundownInfo && rundownInfo.ranges[seg.id];
    if (drives() && range && docIndex.words[range.start]) {
      var span = docIndex.words[range.start];
      if (span.offsetParent !== null) {
        engine.jumpToPx(span.offsetTop - eyelinePx());
      }
    }
  }

  function pointFlags(pt) {
    return pt.covered ? 'c' : pt.skipped ? 's' : '.';
  }

  function updateBulletsStage(seg) {
    var show = producerActive && !!seg && seg.kind === 'bullets';
    P.bstage.classList.toggle('hidden', !show);
    if (!show) { bulletsKey = ''; return; }

    // Per-second refresh: segment title + replanned time left, colored.
    var left = (seg['replanned-s'] || 0) - (seg['spent-s'] || 0);
    P.bSeg.textContent = (seg.title || seg.id) + '  ' +
      MC.rail.fmtClock(left) + ' left';
    P.bSeg.className = MC.rail.timingClass(seg.timing);

    var pts = seg.points || [];
    var actIdx = MC.rail.activePointIndex(seg);
    var nextIdx = -1;
    if (actIdx >= 0) {
      for (var i = actIdx + 1; i < pts.length; i++) {
        if (!pts[i].covered && !pts[i].skipped) { nextIdx = i; break; }
      }
    }
    var key = seg.id + ':' + actIdx + ':' + nextIdx + ':' +
      pts.map(pointFlags).join('');
    if (key === bulletsKey) return;
    bulletsKey = key;

    P.bCurrent.textContent = actIdx >= 0 ? pts[actIdx].text : '';
    P.bCurrent.classList.toggle('hidden', actIdx < 0);
    P.bDone.classList.toggle('hidden', actIdx >= 0 || !pts.length);
    if (nextIdx >= 0) {
      P.bNext.textContent = pts[nextIdx].text;
      P.bNext.classList.remove('hidden');
    } else {
      P.bNext.classList.add('hidden');
    }
    while (P.bRest.firstChild) P.bRest.removeChild(P.bRest.firstChild);
    for (var j = 0; j < pts.length; j++) {
      if (j === actIdx || j === nextIdx) continue;
      var li = document.createElement('li');
      li.textContent = pts[j].text || '';
      if (pts[j].covered) li.className = 'covered';
      else if (pts[j].skipped) li.className = 'skipped';
      P.bRest.appendChild(li);
    }
  }

  function applyProducerState(state) {
    if (!state) return;
    producerState = state;
    coverPendingKey = null;
    prodRail.update(state);
    P.rail.classList.remove('hidden');
    renderProducerControls(state);
    var seg = currentProdSegment();
    if (state.current !== prevSegmentId) {
      prevSegmentId = state.current;
      handoffSentFor = null;
      handoffArmed = false;
      onSegmentChange(seg);
    }
    updateBulletsStage(seg);
  }

  // Anchor-driven handoff. Fires only when (a) voice follow drives the
  // leader, (b) the rail's current segment is scripted, (c) the committed
  // anchor CROSSED that segment's word-end while it was current (an anchor
  // below the end must be seen first, so a stale anchor held over from a
  // previous position, e.g. right after a backward make-current, can never
  // fire it), and (d) at most once per segment per time it becomes current
  // (both flags reset in applyProducerState when the current id changes).
  // The server re-anchors the aligner whenever a scripted segment becomes
  // current, so the arming frame arrives within a beat of any manual jump.
  function maybeAnchorHandoff() {
    if (!producerActive || !producerState || !followEnabled || !isLeader()) return;
    if (!producerState.live) return;
    var seg = currentProdSegment();
    if (!seg || seg.kind !== 'scripted' || handoffSentFor === seg.id) return;
    var range = rundownInfo && rundownInfo.ranges[seg.id];
    if (!range || typeof range.end !== 'number') return;
    // word-end is half-open ([start, end) in global word indices), so the
    // segment's last word is end - 1: fire when the anchor commits it.
    if (lastAnchor < Math.max(range.end - 1, range.start)) {
      // Behind the end while this segment is current: any later crossing
      // is genuine reading progress, so the handoff is now armed.
      handoffArmed = true;
      return;
    }
    if (!handoffArmed) return; // stale anchor from before this segment was current
    var segs = producerState.segments || [];
    for (var i = 0; i < segs.length; i++) {
      if (segs[i].id === seg.id) {
        if (i + 1 < segs.length) {
          handoffSentFor = seg.id;
          handoffArmed = false;
          sendPoint('make-current', segs[i + 1].id);
          toast('segment done: ' + (segs[i + 1].title || segs[i + 1].id));
        }
        return;
      }
    }
  }

  P.btnGoLive.addEventListener('click', function () { sendShow('go-live'); });

  P.btnHold.addEventListener('click', function () {
    if (producerState) sendShow(producerState.hold ? 'resume' : 'hold');
  });

  P.btnEnd.addEventListener('click', function () {
    if (!prodEndArmed) {
      prodEndArmed = true;
      P.btnEnd.classList.add('armed');
      P.btnEnd.textContent = 'really end?';
      clearTimeout(prodEndTimer);
      prodEndTimer = setTimeout(disarmProdEnd, 3000);
      return;
    }
    disarmProdEnd();
    sendShow('end');
  });

  ws.on('producer', function (msg) {
    if (!msg.state) return;
    activateProducer();
    applyProducerState(msg.state);
  });

  ws.on('cue', cueCard.onCue);
  ws.on('cue-clear', cueCard.onClear);

  // Runs after the Phase B anchor listener (registration order), so
  // lastAnchor is already updated when the handoff check reads it.
  ws.on('anchor', function () { maybeAnchorHandoff(); });

  ws.onStatus(function () {
    // Keep the GO LIVE button honest across reconnects (it needs a live
    // socket to mean anything).
    if (producerState) renderProducerControls(producerState);
  });

  // ---------- boot ----------

  initFontStackSelect();
  applyDisplay();
  syncSettingsControls();
  wireSettingsControls();
  syncVoiceControls();

  MC.model.fetchState(token).then(function (state) {
    var wpm = state && state.config && state.config['owner-wpm'];
    if (wpm) {
      engine.setWpm(wpm);
      C.wpm.value = wpm;
    }
    asrAvailable = !!(state && state.asr && state.asr.available);
    if (state && state.producer && state.producer.active) activateProducer();
    syncVoiceControls();
  }).catch(function () { /* defaults are fine */ }).then(loadSource);
})();
