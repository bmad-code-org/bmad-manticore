/* mc-prompter /overlay page glue (Phase A placeholder).
 *
 * OBS browser source: transparent background, a small "session connected"
 * badge that hides itself 5 seconds after each (re)connect. Stays on the WS
 * so Phase C can mount the producer rail and cue cards without changing the
 * page contract.
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

  // Phase C seam: subscribe here for producer rail state.
  // ws.on('state', function (msg) { ... });
})();
