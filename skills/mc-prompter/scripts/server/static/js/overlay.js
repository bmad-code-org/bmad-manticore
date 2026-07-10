/* mc-prompter /overlay page glue.
 *
 * OBS browser source: transparent background, a small "session connected"
 * badge that hides itself 5 seconds after each (re)connect. Stays on the WS
 * so Phase C can mount the producer rail and cue cards without changing the
 * page contract.
 *
 * Phase B: voice-follow anchor/vad frames light a small voice badge
 * (speaking dot, HOLD tint, committed word index) so a live operator can
 * see tracking state without the prompter display in view.
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

  // Phase C seam: subscribe here for producer rail state.
  // ws.on('state', function (msg) { ... });
})();
