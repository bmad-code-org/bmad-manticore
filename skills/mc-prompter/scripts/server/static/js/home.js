/* mc-prompter home page glue.
 *
 * Source picker (path load or paste), edit-in-place with save (writes the
 * file after a timestamped backup) and session-only apply, script info,
 * links to /prompt and /overlay, and the remote URL with token plus copy.
 *
 * Token discovery: the launcher opens the home page with ?token=... in the
 * URL. We keep it in sessionStorage so in-app navigation survives. If no
 * token reaches this page, the remote URL is shown without one (read-only on
 * other devices) and a warning explains where to find the full URL.
 * SEAM(token): if the server later exposes the token to loopback callers in
 * GET /api/state config, read it in fetchState() below.
 */
(function () {
  'use strict';
  var MC = window.MC;

  var els = {
    session: document.getElementById('session'),
    conn: document.getElementById('conn'),
    connLabel: document.getElementById('conn-label'),
    pathInput: document.getElementById('path-input'),
    btnLoad: document.getElementById('btn-load'),
    infoPath: document.getElementById('info-path'),
    infoTitle: document.getElementById('info-title'),
    infoWords: document.getElementById('info-words'),
    infoEst: document.getElementById('info-est'),
    remoteUrl: document.getElementById('remote-url'),
    linkPrompt: document.getElementById('link-prompt'),
    linkOverlay: document.getElementById('link-overlay'),
    btnCopy: document.getElementById('btn-copy'),
    tokenWarning: document.getElementById('token-warning'),
    editor: document.getElementById('editor'),
    btnApply: document.getElementById('btn-apply'),
    btnSave: document.getElementById('btn-save'),
    btnRevert: document.getElementById('btn-revert'),
    dirtyChip: document.getElementById('dirty-chip'),
    msg: document.getElementById('msg')
  };

  var TOKEN_STORE = 'mc-prompter-token';
  var token = new URLSearchParams(location.search).get('token') || null;
  if (token) {
    try { sessionStorage.setItem(TOKEN_STORE, token); } catch (e) { /* fine */ }
  } else {
    try { token = sessionStorage.getItem(TOKEN_STORE); } catch (e) { /* fine */ }
  }

  var docVersion = null;
  var loadedRaw = '';
  var dirty = false;
  var currentWpm = 150;
  var wordCount = null;

  var ws = MC.createWS({ role: 'home', token: token });

  // ---------- messages ----------

  function say(text, kind) {
    els.msg.textContent = text || '';
    els.msg.className = kind || '';
  }

  function setDirty(d) {
    dirty = d;
    els.dirtyChip.classList.toggle('hidden', !d);
  }

  // ---------- remote URL ----------

  function renderRemoteUrl() {
    var url = location.origin + '/remote' + (token ? '?token=' + encodeURIComponent(token) : '');
    els.remoteUrl.textContent = url;
    els.tokenWarning.classList.toggle('hidden', !!token);
    // The prompter display and overlay links need the token too, or they are
    // rejected when the home page is opened from another device on the LAN.
    els.linkPrompt.href = MC.model.withToken('/prompt', token);
    els.linkOverlay.href = MC.model.withToken('/overlay', token);
  }

  els.btnCopy.addEventListener('click', function () {
    var text = els.remoteUrl.textContent;
    function fallbackCopy() {
      var range = document.createRange();
      range.selectNodeContents(els.remoteUrl);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      try { document.execCommand('copy'); say('copied', 'ok'); }
      catch (e) { say('copy failed, select it manually', 'bad'); }
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { say('copied', 'ok'); }, fallbackCopy);
    } else {
      fallbackCopy();
    }
  });

  // ---------- script info ----------

  function renderInfo(path, doc) {
    els.infoPath.textContent = path || 'none loaded';
    els.infoTitle.textContent = (doc && doc.title) || '-';
    wordCount = doc ? doc['word-count'] : null;
    els.infoWords.textContent = wordCount !== null && wordCount !== undefined ? String(wordCount) : '-';
    renderEst();
  }

  function renderEst() {
    var mins = MC.model.estimateMinutes(wordCount, currentWpm);
    els.infoEst.textContent = mins === null ? '-' :
      MC.model.fmtClock(mins * 60) + ' at ' + currentWpm + ' wpm';
  }

  // ---------- source loading ----------

  function refreshSource(overwriteEditor) {
    return MC.model.fetchSource(token).then(function (src) {
      docVersion = src['doc-version'];
      loadedRaw = src.raw || '';
      renderInfo(src.path, src.doc);
      if (overwriteEditor || !dirty) {
        els.editor.value = loadedRaw;
        setDirty(false);
      }
    }).catch(function (err) {
      say('could not fetch script: ' + err.message, 'bad');
    });
  }

  els.btnLoad.addEventListener('click', function () {
    var path = els.pathInput.value.trim();
    if (!path) { say('enter an absolute path first', 'bad'); return; }
    say('loading...');
    MC.model.postJSON('/api/source/load', { path: path }, token).then(function () {
      say('loaded', 'ok');
      return refreshSource(true);
    }).catch(function (err) {
      say('load failed: ' + err.message, 'bad');
    });
  });

  els.pathInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') els.btnLoad.click();
  });

  // ---------- editor ----------

  els.editor.addEventListener('input', function () {
    setDirty(els.editor.value !== loadedRaw);
  });

  function pushSource(save) {
    say(save ? 'saving...' : 'applying...');
    MC.model.postJSON('/api/source', { raw: els.editor.value, save: save }, token)
      .then(function (res) {
        docVersion = res ? res['doc-version'] : docVersion;
        loadedRaw = els.editor.value;
        setDirty(false);
        if (save && res && res.backup) {
          say('saved (backup: ' + res.backup + ')', 'ok');
        } else if (save) {
          say('saved', 'ok');
        } else {
          say('applied for this session', 'ok');
        }
        return refreshSource(false);
      })
      .catch(function (err) {
        say((save ? 'save' : 'apply') + ' failed: ' + err.message, 'bad');
      });
  }

  els.btnApply.addEventListener('click', function () { pushSource(false); });
  els.btnSave.addEventListener('click', function () { pushSource(true); });
  els.btnRevert.addEventListener('click', function () {
    els.editor.value = loadedRaw;
    setDirty(false);
    say('reverted to the last loaded text');
  });

  // ---------- WS wiring ----------

  ws.onStatus(function (s) {
    els.conn.classList.toggle('on', s.connected);
    els.connLabel.textContent = s.connected ? 'connected' : (s.rejected ? 'token rejected' : 'reconnecting');
    els.session.textContent = s.session || '';
  });

  ws.on('doc-updated', function (msg) {
    if (msg['doc-version'] !== docVersion) {
      refreshSource(false);
      if (dirty) say('script changed elsewhere; your unsaved edits are kept in the editor', 'bad');
    }
  });

  ws.on('state', function (msg) {
    if (typeof msg.wpm === 'number' && msg.wpm > 0) {
      currentWpm = msg.wpm;
      renderEst();
    }
  });

  // ---------- boot ----------

  renderRemoteUrl();
  MC.model.fetchState(token).then(function (state) {
    if (state && state.config && state.config['owner-wpm']) {
      currentWpm = state.config['owner-wpm'];
    }
    if (state && state.snapshot && typeof state.snapshot.wpm === 'number') {
      currentWpm = state.snapshot.wpm;
    }
    if (state && state.script && state.script.path) {
      els.pathInput.value = state.script.path;
    }
    renderEst();
  }).catch(function () { /* defaults are fine */ }).then(function () {
    return refreshSource(true);
  });
})();
