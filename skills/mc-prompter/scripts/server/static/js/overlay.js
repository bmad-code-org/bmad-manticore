/* mc-prompter /overlay page glue.
 *
 * OBS browser source: transparent background, a small "session connected"
 * badge that hides itself 5 seconds after each (re)connect.
 *
 * Phase B: voice-follow anchor/vad frames light a small voice badge
 * (speaking dot, HOLD tint, committed word index) so a live operator can
 * see tracking state without the prompter display in view.
 *
 * Phase C: the real producer surface. Producer frames drive the ambient
 * rail (show clock, G/Y/R, current segment, NEXT point, segment dots) and
 * cue / cue-clear frames drive the single cue card. Before the first
 * producer frame (the server broadcasts only on change), the rail is
 * seeded from GET /api/rundown when /api/state reports producer.active.
 * Everything stays hidden in tier 1/2 sessions (no rundown, no frames).
 */
(function () {
  'use strict';
  var MC = window.MC;

  var badge = document.getElementById('badge');
  var hideTimer = null;

  var token = new URLSearchParams(location.search).get('token') || null;
  var ws = MC.createWS({ role: 'overlay', token: token });

  function showBadge(connected) {
    badge.classList.remove('fade');
    badge.classList.toggle('disconnected', !connected);
    clearTimeout(hideTimer);
    if (connected) {
      hideTimer = setTimeout(function () { badge.classList.add('fade'); }, 5000);
    }
  }

  ws.onStatus(function (s) {
    showBadge(s.connected);
  });

  // ----- Phase B: voice-follow indicator -----

  var voiceBadge = document.getElementById('voice-badge');
  var voiceText = document.getElementById('voice-text');
  var anchor = -1;
  var speaking = false;
  var held = false;

  function renderVoice() {
    voiceBadge.classList.remove('hidden');
    voiceBadge.classList.toggle('speaking', speaking);
    voiceBadge.classList.toggle('held', held);
    voiceText.textContent =
      (held ? 'HOLD' : speaking ? 'voice' : 'silent') +
      (anchor >= 0 ? ' @ word ' + (anchor + 1) : '');
  }

  ws.on('vad', function (msg) {
    speaking = !!msg.speaking;
    renderVoice();
  });

  ws.on('anchor', function (msg) {
    if (typeof msg.i === 'number') anchor = msg.i;
    held = !!msg.held;
    renderVoice();
  });

  // ----- Phase C: producer rail + cue card -----

  var rail = MC.rail.createRail(document.getElementById('rail'));
  var cue = MC.rail.createCueCard(document.getElementById('cue'));
  var gotProducerFrame = false;

  ws.on('producer', function (msg) {
    if (!msg.state) return;
    gotProducerFrame = true;
    rail.update(msg.state);
  });

  ws.on('cue', cue.onCue);
  ws.on('cue-clear', cue.onClear);

  // Pre-show seed: a live frame always wins over the synthesized state.
  MC.model.fetchState(token).then(function (state) {
    if (!(state && state.producer && state.producer.active)) return null;
    return MC.model.fetchRundown(token).then(function (resp) {
      var info = MC.rail.normalizeRundown(resp);
      if (info && !gotProducerFrame) {
        rail.update(MC.rail.preShowState(info.rundown));
      }
    });
  }).catch(function () { /* tier 1/2 session: the rail stays hidden */ });
})();
