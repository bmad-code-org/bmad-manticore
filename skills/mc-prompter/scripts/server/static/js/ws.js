/* mc-prompter WebSocket client (classic script, attaches to window.MC).
 *
 * Protocol (Phase A contract):
 *   -> hello   {type:"hello", role:"prompt"|"remote"|"overlay"|"home", token?}
 *   <- welcome {type:"welcome", session, leader, "doc-version", snapshot}
 *   <- role    {type:"role", leader:true}            leader promotion
 *   <> cmd     {type:"cmd", cmd, value?, from?}      relayed by server to all
 *   <> state   {type:"state", position, section, playing, wpm, mode,
 *               elapsed, remaining, countdown}       leader prompt -> everyone
 *   <- doc-updated {type:"doc-updated", "doc-version"}  clients refetch /api/source
 *   <- error   {type:"error", message}
 *
 * Reconnects with exponential backoff. Close code 4403 means the token was
 * rejected; we stop retrying and flag state.rejected so pages can go read-only.
 */
(function () {
  'use strict';
  window.MC = window.MC || {};

  function createWS(opts) {
    // opts: { role: string, token: string|null }
    var listeners = {};   // message type -> [fn]
    var statusFns = [];
    var ws = null;
    var closedByUser = false;
    var backoff = 500;
    var BACKOFF_MAX = 8000;

    var state = {
      connected: false,
      leader: false,
      session: null,
      docVersion: null,
      snapshot: null,   // last state snapshot delivered in welcome
      rejected: false   // token rejected (close 4403); no further retries
    };

    function emitStatus() {
      for (var i = 0; i < statusFns.length; i++) statusFns[i](state);
    }

    function dispatch(msg) {
      if (msg.type === 'welcome') {
        state.session = msg.session || null;
        state.leader = !!msg.leader;
        state.docVersion = msg['doc-version'];
        state.snapshot = msg.snapshot || null;
        emitStatus();
      } else if (msg.type === 'role') {
        state.leader = !!msg.leader;
        emitStatus();
      }
      var fns = (listeners[msg.type] || []).concat(listeners['*'] || []);
      for (var i = 0; i < fns.length; i++) {
        try { fns[i](msg); } catch (e) { /* one bad listener never kills dispatch */ }
      }
    }

    function connect() {
      if (closedByUser || state.rejected) return;
      var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
      var url = proto + location.host + '/ws';
      if (opts.token) url += '?token=' + encodeURIComponent(opts.token);
      try {
        ws = new WebSocket(url);
      } catch (e) {
        scheduleReconnect();
        return;
      }
      ws.onopen = function () {
        backoff = 500;
        state.connected = true;
        var hello = { type: 'hello', role: opts.role };
        if (opts.token) hello.token = opts.token;
        ws.send(JSON.stringify(hello));
        emitStatus();
      };
      ws.onmessage = function (ev) {
        if (typeof ev.data !== 'string') return; // binary frames reserved for Phase B
        var msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg && msg.type) dispatch(msg);
      };
      ws.onclose = function (ev) {
        state.connected = false;
        state.leader = false;
        if (ev && ev.code === 4403) {
          state.rejected = true;
          emitStatus();
          return;
        }
        emitStatus();
        scheduleReconnect();
      };
      ws.onerror = function () {
        try { ws.close(); } catch (e) { /* already closing */ }
      };
    }

    function scheduleReconnect() {
      if (closedByUser || state.rejected) return;
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, BACKOFF_MAX);
    }

    connect();

    return {
      state: state,

      // on(type, fn): subscribe to a message type; '*' catches everything.
      on: function (type, fn) {
        (listeners[type] = listeners[type] || []).push(fn);
      },

      // onStatus(fn): connection or role changes; called once immediately.
      onStatus: function (fn) {
        statusFns.push(fn);
        fn(state);
      },

      send: function (obj) {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify(obj));
          return true;
        }
        return false;
      },

      // cmd(name, value): send a command frame per the protocol.
      cmd: function (name, value) {
        var m = { type: 'cmd', cmd: name };
        if (value !== undefined && value !== null) m.value = value;
        return this.send(m);
      },

      close: function () {
        closedByUser = true;
        if (ws) { try { ws.close(); } catch (e) { /* noop */ } }
      }
    };
  }

  window.MC.createWS = createWS;
})();
