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
    syncVoiceControls();
  }).catch(function () { /* defaults are fine */ }).then(loadSource);
})();
