/* mc-prompter script model client (classic script, attaches to window.MC).
 *
 * Fetches the ingested script model from the server and renders it to DOM.
 *
 * Model shape (produced by script_ingest.py, see the Phase A contract):
 *   { title, "word-count", sections: [ { id, heading, level, blocks: [
 *       {type:"para", runs:[{text, flags}]},
 *       {type:"note", text},
 *       {type:"take", source, start, end, runs:[...]} ] } ] }
 *
 * Rendering contract (Phase B alignment and click-to-anchor build on this):
 *   - every speakable word is wrapped in <span class="w" data-i="<global index>">
 *   - take paragraphs get class "take" (dimmed, "have it already" badge,
 *     hidden entirely when body has class hide-takes)
 *   - invented runs get class "invented" (subtle badge, muted when body has
 *     class hide-invented)
 *   - notes render as class "note" (dimmed, italic, never counted)
 */
(function () {
  'use strict';
  window.MC = window.MC || {};

  function withToken(url, token) {
    if (!token) return url;
    return url + (url.indexOf('?') >= 0 ? '&' : '?') + 'token=' + encodeURIComponent(token);
  }

  function fetchJSON(url, token) {
    return fetch(withToken(url, token), { cache: 'no-store' }).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (body) {
          var err = new Error('HTTP ' + r.status + ' for ' + url + (body ? ': ' + body : ''));
          err.status = r.status;
          throw err;
        });
      }
      return r.json();
    });
  }

  function postJSON(url, body, token) {
    return fetch(withToken(url, token), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { /* non-JSON error body */ }
        if (!r.ok) {
          var msg = (data && data.message) ? data.message : ('HTTP ' + r.status);
          var err = new Error(msg);
          err.status = r.status;
          throw err;
        }
        return data;
      });
    });
  }

  // GET /api/source -> {path, raw, doc, "doc-version"}
  function fetchSource(token) { return fetchJSON('/api/source', token); }

  // GET /api/state -> {snapshot, "doc-version", script, config}
  // (Phase C adds .producer {active, live, llm {provider, model, ok}}.)
  function fetchState(token) { return fetchJSON('/api/state', token); }

  // GET /api/rundown -> the parsed rundown + segment word ranges (Phase C).
  // 404 when no rundown is loaded; callers treat that as producer-off.
  function fetchRundown(token) { return fetchJSON('/api/rundown', token); }

  /* Render the script model into `container` (emptied first).
   * Returns an index used by the scroll engine and jump logic:
   *   { wordCount, takeWordCount, words: [span],
   *     paragraphs: [{el, wordStart, sectionId}],
   *     sections: [{id, heading, level, el, wordStart}] }
   * takeWordCount is the share of wordCount inside TAKE paragraphs, so the
   * page can pace against the visible count when hide-takes is on.
   */
  function renderDoc(doc, container) {
    while (container.firstChild) container.removeChild(container.firstChild);

    var index = { wordCount: 0, takeWordCount: 0, words: [], paragraphs: [], sections: [] };
    if (!doc || !doc.sections) return index;

    var wordIndex = 0;

    function appendWords(text, parent) {
      // Split preserving whitespace so spacing survives verbatim.
      var chunks = String(text).split(/(\s+)/);
      for (var i = 0; i < chunks.length; i++) {
        var chunk = chunks[i];
        if (!chunk) continue;
        if (/^\s+$/.test(chunk)) {
          parent.appendChild(document.createTextNode(chunk));
        } else {
          var span = document.createElement('span');
          span.className = 'w';
          span.dataset.i = String(wordIndex);
          span.textContent = chunk;
          parent.appendChild(span);
          index.words.push(span);
          wordIndex += 1;
        }
      }
    }

    function appendRuns(runs, parent) {
      for (var i = 0; i < (runs || []).length; i++) {
        var run = runs[i];
        var flags = run.flags || [];
        if (flags.indexOf('invented') >= 0) {
          var inv = document.createElement('span');
          inv.className = 'run invented';
          appendWords(run.text, inv);
          parent.appendChild(inv);
        } else {
          appendWords(run.text, parent);
        }
      }
    }

    for (var s = 0; s < doc.sections.length; s++) {
      var sec = doc.sections[s];
      var secEl = document.createElement('section');
      secEl.className = 'sec';
      secEl.dataset.sid = sec.id;
      var secEntry = {
        id: sec.id,
        heading: sec.heading || null,
        level: sec.level || 2,
        el: secEl,
        wordStart: wordIndex
      };

      if (sec.heading) {
        var h = document.createElement('h2');
        h.className = 'sec-h';
        h.textContent = sec.heading;
        secEl.appendChild(h);
      }

      var blocks = sec.blocks || [];
      for (var b = 0; b < blocks.length; b++) {
        var block = blocks[b];
        if (block.type === 'note') {
          var noteEl = document.createElement('p');
          noteEl.className = 'note';
          noteEl.textContent = block.text || '';
          secEl.appendChild(noteEl);
        } else if (block.type === 'take') {
          var takeEl = document.createElement('p');
          takeEl.className = 'para take';
          // Phase B seam: source clip identity rides on data attributes.
          if (block.source !== undefined) takeEl.dataset.source = String(block.source);
          if (block.start !== undefined) takeEl.dataset.start = String(block.start);
          if (block.end !== undefined) takeEl.dataset.end = String(block.end);
          var takeStart = wordIndex;
          appendRuns(block.runs, takeEl);
          index.takeWordCount += wordIndex - takeStart;
          secEl.appendChild(takeEl);
          index.paragraphs.push({ el: takeEl, wordStart: takeStart, sectionId: sec.id });
        } else {
          // para (default)
          var paraEl = document.createElement('p');
          paraEl.className = 'para';
          var paraStart = wordIndex;
          appendRuns(block.runs, paraEl);
          secEl.appendChild(paraEl);
          index.paragraphs.push({ el: paraEl, wordStart: paraStart, sectionId: sec.id });
        }
      }

      container.appendChild(secEl);
      index.sections.push(secEntry);
    }

    index.wordCount = wordIndex;
    return index;
  }

  function estimateMinutes(wordCount, wpm) {
    if (!wordCount || !wpm) return null;
    return wordCount / wpm;
  }

  // Format seconds as m:ss or h:mm:ss. Returns "--:--" for null/NaN.
  function fmtClock(seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) return '--:--';
    var t = Math.max(0, Math.round(seconds));
    var h = Math.floor(t / 3600);
    var m = Math.floor((t % 3600) / 60);
    var sec = t % 60;
    var mm = (h > 0 && m < 10 ? '0' : '') + m;
    var ss = (sec < 10 ? '0' : '') + sec;
    return h > 0 ? h + ':' + mm + ':' + ss : mm + ':' + ss;
  }

  window.MC.model = {
    fetchSource: fetchSource,
    fetchState: fetchState,
    fetchRundown: fetchRundown,
    postJSON: postJSON,
    withToken: withToken,
    renderDoc: renderDoc,
    estimateMinutes: estimateMinutes,
    fmtClock: fmtClock
  };
})();
