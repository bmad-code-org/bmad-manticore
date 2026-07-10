/* mc-prompter /remote page glue.
 *
 * Phone-as-remote. Requires the session token from ?token= in the URL; a
 * non-loopback WS connect without a valid token is closed with 4403 and the
 * page goes read-only (error panel, no controls). Commands go out as cmd
 * frames; the live display is driven by the leader's state frames.
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
    secCount: document.getElementById('sec-count')
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

  // ---------- boot ----------

  loadSections();
  MC.model.fetchState(token).then(function (state) {
    if (state && state.snapshot) applyState(state.snapshot);
  }).catch(function () { /* WS state frames will fill in */ });
})();
