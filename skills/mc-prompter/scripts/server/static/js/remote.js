/* mc-prompter /remote page glue.
 *
 * Phone-as-remote. Requires the session token from ?token= in the URL; a
 * non-loopback WS connect without a valid token is closed with 4403 and the
 * page goes read-only (error panel, no controls). Commands go out as cmd
 * frames; the live display is driven by the leader's state frames.
 *
 * Producer tab (Phase C): appears when a rundown session is running
 * (producer frames arrive, or /api/state reports producer.active). Carries
 * GO LIVE / hold / end show controls ({"type":"show","cmd":...}), a live
 * rail summary, and the per-segment point list with covered / skip /
 * make-current taps ({"type":"point","cmd":...,"segment","point"}). The
 * human is the final authority: one tap rescues any model misjudgment.
 * End show is two-tap armed so a pocket brush cannot kill a live show.
 */
(function () {
  'use strict';
  var MC = window.MC;

  var els = {
    app: document.getElementById('app'),
    errorPanel: document.getElementById('error-panel'),
    errorMsg: document.getElementById('error-msg'),
    conn: document.getElementById('conn'),
    session: document.getElementById('session'),
    elapsed: document.getElementById('st-elapsed'),
    remaining: document.getElementById('st-remaining'),
    wpm: document.getElementById('st-wpm'),
    posFill: document.getElementById('pos-fill'),
    btnToggle: document.getElementById('btn-toggle'),
    btnRestart: document.getElementById('btn-restart'),
    btnSlower: document.getElementById('btn-slower'),
    btnFaster: document.getElementById('btn-faster'),
    btnPrev: document.getElementById('btn-prev'),
    btnNext: document.getElementById('btn-next'),
    sectionList: document.getElementById('section-list'),
    secCount: document.getElementById('sec-count'),
    tabs: document.getElementById('tabs'),
    tabTransport: document.getElementById('tab-transport'),
    tabProducer: document.getElementById('tab-producer'),
    viewTransport: document.getElementById('view-transport'),
    viewProducer: document.getElementById('view-producer'),
    btnGoLive: document.getElementById('btn-golive'),
    btnHold: document.getElementById('btn-hold'),
    btnEnd: document.getElementById('btn-end'),
    prodRail: document.getElementById('prod-rail'),
    prodSegList: document.getElementById('prod-seg-list')
  };

  var token = new URLSearchParams(location.search).get('token') || null;
  var sections = [];          // [{id, heading}]
  var currentSectionId = null;
  var docVersion = null;

  function showError(msg) {
    els.app.classList.add('hidden');
    els.errorPanel.classList.remove('hidden');
    if (msg) els.errorMsg.textContent = msg;
  }

  if (!token) {
    showError('This remote needs the session token. Open the exact remote URL ' +
      'shown on the prompter home page (it ends in ?token=...).');
    return;
  }

  els.app.classList.remove('hidden');

  var ws = MC.createWS({ role: 'remote', token: token });

  // ---------- sections ----------

  function loadSections() {
    return MC.model.fetchSource(token).then(function (src) {
      docVersion = src['doc-version'];
      sections = [];
      var docSections = (src.doc && src.doc.sections) || [];
      for (var i = 0; i < docSections.length; i++) {
        sections.push({
          id: docSections[i].id,
          heading: docSections[i].heading || (i === 0 ? 'Preamble' : 'Untitled section')
        });
      }
      renderSections();
    }).catch(function (err) {
      if (err.status === 401 || err.status === 403) {
        showError('The session token was rejected. Grab a fresh remote URL from the home page.');
      }
    });
  }

  function renderSections() {
    var list = els.sectionList;
    while (list.firstChild) list.removeChild(list.firstChild);
    els.secCount.textContent = sections.length ? '(' + sections.length + ')' : '';
    for (var i = 0; i < sections.length; i++) {
      (function (sec) {
        var li = document.createElement('li');
        li.dataset.sid = sec.id;
        li.textContent = sec.heading;
        li.addEventListener('click', function () {
          sendJumpSection(sec.id);
        });
        list.appendChild(li);
      })(sections[i]);
    }
    highlightSection();
  }

  function highlightSection() {
    var items = els.sectionList.children;
    for (var i = 0; i < items.length; i++) {
      items[i].classList.toggle('current', items[i].dataset.sid === currentSectionId);
    }
  }

  function sectionIndex(id) {
    for (var i = 0; i < sections.length; i++) {
      if (sections[i].id === id) return i;
    }
    return -1;
  }

  // Send a jump and update currentSectionId optimistically so rapid prev or
  // next taps chain from the target instead of re-targeting the same section
  // while the leader's next state frame is still in flight. The next state
  // frame reconciles the real position.
  function sendJumpSection(id) {
    ws.cmd('jump-section', id);
    currentSectionId = id;
    highlightSection();
  }

  function jumpRelativeSection(delta) {
    if (!sections.length) return;
    var idx = sectionIndex(currentSectionId);
    var target = idx < 0 ? 0 : Math.min(Math.max(idx + delta, 0), sections.length - 1);
    sendJumpSection(sections[target].id);
  }

  // ---------- live state ----------

  function applyState(msg) {
    currentSectionId = msg.section || null;
    els.elapsed.textContent = MC.model.fmtClock(msg.elapsed);
    els.remaining.textContent = MC.model.fmtClock(msg.remaining);
    els.wpm.textContent = String(msg.wpm !== undefined ? msg.wpm : '--');
    els.posFill.style.width = (Math.min(Math.max(Number(msg.position) || 0, 0), 1) * 100) + '%';
    var playing = !!msg.playing;
    if (msg.follow) {
      // The leader is in voice follow: the view is moving with the speaker
      // and play/pause commands are ignored there, so say FOLLOWING instead
      // of offering a dead 'play'.
      els.btnToggle.textContent = 'following';
      els.btnToggle.classList.add('playing');
    } else {
      els.btnToggle.textContent = playing ? 'pause' : 'play';
      els.btnToggle.classList.toggle('playing', playing);
      if (msg.countdown !== null && msg.countdown !== undefined) {
        els.btnToggle.textContent = 'in ' + Math.ceil(msg.countdown) + 's';
      }
    }
    highlightSection();
  }

  // ---------- wiring ----------

  ws.onStatus(function (s) {
    els.conn.classList.toggle('on', s.connected);
    els.session.textContent = s.session || '';
    if (s.rejected) {
      showError('The session token was rejected. Grab a fresh remote URL from the home page.');
      return;
    }
    if (s.connected && s.snapshot) applyState(s.snapshot);
  });

  ws.on('state', applyState);
  ws.on('doc-updated', function (msg) {
    if (msg['doc-version'] !== docVersion) loadSections();
  });

  els.btnToggle.addEventListener('click', function () { ws.cmd('toggle'); });
  els.btnRestart.addEventListener('click', function () { ws.cmd('restart'); });
  els.btnSlower.addEventListener('click', function () { ws.cmd('speed-delta', -2); });
  els.btnFaster.addEventListener('click', function () { ws.cmd('speed-delta', 2); });
  els.btnPrev.addEventListener('click', function () { jumpRelativeSection(-1); });
  els.btnNext.addEventListener('click', function () { jumpRelativeSection(1); });

  // ---------- producer tab (Phase C) ----------

  var producerActive = false;
  var producerState = null;      // latest producer frame or pre-show synth
  var prodBuildKey = '';         // seg ids + point counts: the DOM is built
                                 // once per rundown and updated in place
  var segEls = {};               // segment id -> node refs for updates
  var endArmed = false;
  var endArmTimer = null;

  var prodRail = MC.rail.createRail(els.prodRail);

  function setTab(name) {
    var producer = name === 'producer';
    els.tabProducer.classList.toggle('active', producer);
    els.tabTransport.classList.toggle('active', !producer);
    els.viewProducer.classList.toggle('hidden', !producer);
    els.viewTransport.classList.toggle('hidden', producer);
  }

  els.tabTransport.addEventListener('click', function () { setTab('transport'); });
  els.tabProducer.addEventListener('click', function () { setTab('producer'); });

  function activateProducer() {
    if (producerActive) return;
    producerActive = true;
    els.tabs.classList.remove('hidden');
  }

  function sendShow(cmd) { ws.send({ type: 'show', cmd: cmd }); }

  function sendPoint(cmd, segId, idx) {
    var m = { type: 'point', cmd: cmd, segment: segId };
    if (idx !== undefined && idx !== null) m.point = idx;
    ws.send(m);
  }

  function disarmEnd() {
    endArmed = false;
    clearTimeout(endArmTimer);
    els.btnEnd.classList.remove('armed');
    els.btnEnd.textContent = 'end show';
  }

  els.btnGoLive.addEventListener('click', function () { sendShow('go-live'); });

  els.btnHold.addEventListener('click', function () {
    if (!producerState) return;
    sendShow(producerState.hold ? 'resume' : 'hold');
  });

  els.btnEnd.addEventListener('click', function () {
    if (!endArmed) {
      endArmed = true;
      els.btnEnd.classList.add('armed');
      els.btnEnd.textContent = 'really end?';
      clearTimeout(endArmTimer);
      endArmTimer = setTimeout(disarmEnd, 3000);
      return;
    }
    disarmEnd();
    sendShow('end');
  });

  function renderShowControls(state) {
    var live = !!state.live;
    // Ended is final: producer.go_live is a no-op once the show has ended,
    // so the button must say so honestly instead of promising a restart.
    var ended = !live && (state['elapsed-s'] || 0) > 0;
    els.btnGoLive.disabled = live || ended;
    els.btnGoLive.textContent = live
      ? (state.hold ? 'ON HOLD' : 'LIVE')
      : ended ? 'SHOW ENDED' : 'GO LIVE';
    els.btnHold.disabled = !live;
    els.btnHold.textContent = state.hold ? 'resume' : 'hold';
    els.btnHold.classList.toggle('holding', !!state.hold);
    els.btnEnd.disabled = !live;
    if (!live) disarmEnd();
  }

  // The point list keeps one stable DOM node per segment and per point,
  // keyed by segment id and point index (point counts never change during
  // a show). Every frame updates classes, labels, and button visibility in
  // place, so an auto-coverage flip landing mid-tap can never shift the
  // rows and land the tap on a different point's button.
  function buildKey(state) {
    var parts = [];
    var segs = state.segments || [];
    for (var i = 0; i < segs.length; i++) {
      parts.push(segs[i].id + ':' + (segs[i].points || []).length);
    }
    return parts.join('|');
  }

  function segTimeText(seg) {
    var left = (seg['replanned-s'] || 0) - (seg['spent-s'] || 0);
    return MC.rail.fmtClock(left) + ' left';
  }

  // Taps are delegated to the list root and resolved from data attributes
  // on the button AT TAP TIME, so even if a producer frame rebuilds the
  // list mid-tap the command goes to whatever the finger is actually on.
  els.prodSegList.addEventListener('click', function (e) {
    var btn = e.target && e.target.closest ? e.target.closest('button[data-act]') : null;
    if (!btn) return;
    var act = btn.dataset.act;
    if (act === 'make-current') sendPoint('make-current', btn.dataset.seg);
    else sendPoint(act, btn.dataset.seg, Number(btn.dataset.point));
  });

  function buildSegList(state) {
    var root = els.prodSegList;
    while (root.firstChild) root.removeChild(root.firstChild);
    segEls = {};
    var segs = state.segments || [];

    for (var i = 0; i < segs.length; i++) {
      var seg = segs[i];
      var refs = { points: [] };

      var box = document.createElement('div');
      box.className = 'p-seg';

      var head = document.createElement('div');
      head.className = 'p-seg-head';
      var title = document.createElement('span');
      title.className = 'p-seg-title';
      title.textContent = seg.title || seg.id;
      var time = document.createElement('span');
      time.className = 'p-seg-time';
      var make = document.createElement('button');
      make.className = 'p-make';
      make.textContent = 'make current';
      make.dataset.act = 'make-current';
      make.dataset.seg = seg.id;
      head.appendChild(title);
      head.appendChild(time);
      head.appendChild(make);
      box.appendChild(head);
      refs.box = box;
      refs.time = time;
      refs.make = make;

      var pts = seg.points || [];
      if (pts.length) {
        var ul = document.createElement('ul');
        ul.className = 'p-points';
        for (var j = 0; j < pts.length; j++) {
          var li = document.createElement('li');
          li.className = 'p-point';
          var text = document.createElement('span');
          text.className = 'p-text';
          text.textContent = pts[j].text || '';
          li.appendChild(text);
          var flag = document.createElement('span');
          flag.className = 'p-flag hidden';
          li.appendChild(flag);
          var actions = document.createElement('span');
          actions.className = 'p-actions';
          var done = document.createElement('button');
          done.textContent = 'done';
          done.dataset.act = 'covered';
          done.dataset.seg = seg.id;
          done.dataset.point = String(j);
          var skip = document.createElement('button');
          skip.textContent = 'skip';
          skip.dataset.act = 'skip';
          skip.dataset.seg = seg.id;
          skip.dataset.point = String(j);
          actions.appendChild(done);
          actions.appendChild(skip);
          li.appendChild(actions);
          ul.appendChild(li);
          refs.points.push({ li: li, flag: flag, actions: actions });
        }
        box.appendChild(ul);
      }

      root.appendChild(box);
      segEls[seg.id] = refs;
    }
  }

  function updateSegList(state) {
    var segs = state.segments || [];
    var np = state['next-point'];
    for (var i = 0; i < segs.length; i++) {
      var seg = segs[i];
      var refs = segEls[seg.id];
      if (!refs) continue;
      refs.box.className = 'p-seg ' + (seg.state || 'pending');
      refs.time.className = 'p-seg-time ' + MC.rail.timingClass(seg.timing);
      refs.time.textContent = segTimeText(seg);
      refs.make.classList.toggle('hidden', seg.state === 'current');
      var pts = seg.points || [];
      for (var j = 0; j < refs.points.length && j < pts.length; j++) {
        var pt = pts[j];
        var pr = refs.points[j];
        var cls = 'p-point';
        if (pt.covered) cls += ' covered';
        else if (pt.skipped) cls += ' skipped';
        else if (np && np.segment === seg.id && np.idx === j) cls += ' active';
        pr.li.className = cls;
        var settled = !!(pt.covered || pt.skipped);
        pr.flag.classList.toggle('hidden', !settled);
        pr.flag.textContent = settled ? (pt.covered ? 'covered' : 'skipped') : '';
        pr.actions.classList.toggle('hidden', settled);
      }
    }
  }

  function renderProducer(state) {
    if (!state) return;
    producerState = state;
    prodRail.update(state);
    renderShowControls(state);
    var key = buildKey(state);
    if (key !== prodBuildKey) {
      prodBuildKey = key;
      buildSegList(state);
    }
    updateSegList(state);
  }

  ws.on('producer', function (msg) {
    if (!msg.state) return;
    activateProducer();
    renderProducer(msg.state);
  });

  // ---------- boot ----------

  loadSections();
  MC.model.fetchState(token).then(function (state) {
    if (state && state.snapshot) applyState(state.snapshot);
    if (state && state.producer && state.producer.active) {
      activateProducer();
      // Pre-show seed until the first producer frame arrives.
      return MC.model.fetchRundown(token).then(function (resp) {
        var info = MC.rail.normalizeRundown(resp);
        if (info && !producerState) {
          renderProducer(MC.rail.preShowState(info.rundown));
        }
      });
    }
  }).catch(function () { /* WS state frames will fill in */ });
})();
