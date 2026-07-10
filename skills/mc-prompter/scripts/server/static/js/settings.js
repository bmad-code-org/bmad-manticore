/* mc-prompter display settings (classic script, attaches to window.MC).
 *
 * Persisted per device in localStorage under the key "mc-prompter-display".
 * Schema (kebab-case keys, all optional in storage, defaults below):
 *   mirror-h           bool    horizontal flip of the scroll surface
 *   mirror-v           bool    vertical flip (both may combine)
 *   font-family        string  CSS font-family (system stacks or free text)
 *   font-size          number  px
 *   text-color         string  CSS color
 *   background-color   string  CSS color
 *   margin-percent     number  horizontal margin, percent of surface width
 *   line-height        number  unitless multiplier
 *   eyeline-percent    number  marker position, percent from viewport top
 *   eyeline-style      string  "line" | "arrow"
 *   countdown-seconds  number  countdown before scroll starts (0 disables)
 *   hide-takes         bool    hide TAKE paragraphs entirely
 *   show-invented      bool    show the invented badge styling
 *   rail-dock          string  "top" | "bottom", producer rail position
 *
 * Server config defaults (from GET /api/state .config) may be passed to
 * load() as overrides; stored per-device values still win over them.
 */
(function () {
  'use strict';
  window.MC = window.MC || {};

  var KEY = 'mc-prompter-display';

  var DEFAULTS = {
    'mirror-h': false,
    'mirror-v': false,
    'font-family': 'system-ui, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
    'font-size': 64,
    'text-color': '#f2f2f2',
    'background-color': '#000000',
    'margin-percent': 12,
    'line-height': 1.5,
    'eyeline-percent': 33,
    'eyeline-style': 'line',
    'countdown-seconds': 3,
    'hide-takes': false,
    'show-invented': true,
    'rail-dock': 'top'
  };

  // A few known-safe offline font stacks for the settings drawer select.
  var FONT_STACKS = [
    { label: 'System sans', value: DEFAULTS['font-family'] },
    { label: 'Georgia serif', value: 'Georgia, "Times New Roman", serif' },
    { label: 'Verdana wide', value: 'Verdana, Geneva, Tahoma, sans-serif' },
    { label: 'Trebuchet', value: '"Trebuchet MS", "Segoe UI", sans-serif' },
    { label: 'Monospace', value: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace' }
  ];

  function load(overrides) {
    var out = {};
    var k;
    for (k in DEFAULTS) {
      if (Object.prototype.hasOwnProperty.call(DEFAULTS, k)) out[k] = DEFAULTS[k];
    }
    if (overrides) {
      for (k in overrides) {
        if (Object.prototype.hasOwnProperty.call(DEFAULTS, k) &&
            overrides[k] !== null && overrides[k] !== undefined) {
          out[k] = overrides[k];
        }
      }
    }
    try {
      var raw = localStorage.getItem(KEY);
      if (raw) {
        var saved = JSON.parse(raw);
        for (k in saved) {
          if (Object.prototype.hasOwnProperty.call(DEFAULTS, k)) out[k] = saved[k];
        }
      }
    } catch (e) {
      // Corrupted storage: fall back to defaults silently.
    }
    return out;
  }

  function save(settings) {
    var out = {};
    for (var k in DEFAULTS) {
      if (Object.prototype.hasOwnProperty.call(settings, k)) out[k] = settings[k];
    }
    try {
      localStorage.setItem(KEY, JSON.stringify(out));
    } catch (e) {
      // Storage full or blocked: settings stay session-only.
    }
  }

  window.MC.settings = {
    KEY: KEY,
    DEFAULTS: DEFAULTS,
    FONT_STACKS: FONT_STACKS,
    load: load,
    save: save
  };
})();
