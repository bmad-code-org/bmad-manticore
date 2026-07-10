/* mc-prompter browser audio capture (classic script, attaches to window.MC).
 *
 * Phase B voice-follow capture pipeline:
 *   getUserMedia with browser speech processing OFF (echoCancellation,
 *   noiseSuppression, autoGainControl all false; channelCount 1; the
 *   persisted deviceId as a non-exact preference so an unplugged mic falls
 *   back instead of failing) -> AudioContext({sampleRate: 16000}) with a
 *   linear-interp resampler fallback inside the worklet when the browser
 *   refuses 16 kHz -> AudioWorklet (/static/js/capture-worklet.js) posting
 *   ~120 ms Int16 frames (1920 samples) plus an RMS level value per frame.
 *
 * The page owns the WS send path (capture-granted plus bufferedAmount
 * gating, see prompt.js sendAudioFrame); this module only produces frames.
 * The ?sim-audio=1 dev seam (wavToFrames + streamFrames below) produces
 * identical frames from a fetched WAV so both sources share every line of
 * the send path except the source node.
 *
 * Device persistence: localStorage key "mc-prompter-mic" (deviceId string).
 */
(function () {
  'use strict';
  window.MC = window.MC || {};

  var MIC_KEY = 'mc-prompter-mic';
  var TARGET_RATE = 16000;
  var FRAME_SAMPLES = 1920; // 120 ms at 16 kHz
  var WORKLET_URL = '/static/js/capture-worklet.js';

  function getSavedMic() {
    try { return localStorage.getItem(MIC_KEY) || null; } catch (e) { return null; }
  }

  function saveMic(deviceId) {
    try {
      if (deviceId) localStorage.setItem(MIC_KEY, deviceId);
      else localStorage.removeItem(MIC_KEY);
    } catch (e) { /* storage blocked: picker stays session-only */ }
  }

  // Enumerate audio inputs. Labels are only populated after a successful
  // getUserMedia, so callers re-list once capture has started.
  function listInputs() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
      return Promise.resolve([]);
    }
    return navigator.mediaDevices.enumerateDevices().then(function (devices) {
      return devices.filter(function (d) { return d.kind === 'audioinput'; });
    });
  }

  // Compare what the track actually applied against what voice-follow needs.
  // Returns a list of human-readable warning strings (empty = all good).
  function constraintWarnings(settings, contextRate) {
    var warnings = [];
    function offWanted(key, label) {
      if (settings[key] === true) {
        warnings.push(label + ' is ON (the browser ignored the request to disable it); ASR quality will suffer');
      }
    }
    offWanted('echoCancellation', 'echo cancellation');
    offWanted('noiseSuppression', 'noise suppression');
    offWanted('autoGainControl', 'auto gain control');
    if (typeof settings.channelCount === 'number' && settings.channelCount > 1) {
      warnings.push('capture is ' + settings.channelCount + '-channel (mono requested); only channel 1 is used');
    }
    if (contextRate !== TARGET_RATE) {
      warnings.push('browser captures at ' + contextRate + ' Hz; resampling to 16 kHz in the worklet');
    }
    return warnings;
  }

  /* createCapture(opts) -> capture object.
   * opts: { onFrame: fn(Int16Array), onLevel: fn(rms 0..1), onError: fn(err) }
   * capture.start(deviceId) -> Promise<{settings, warnings, contextRate,
   *                                     resampling, deviceId, label}>
   * capture.stop()          tears the graph and the stream down.
   * capture.running         boolean.
   *
   * Concurrency: each start() carries a generation token. A newer start()
   * (device picker change while getUserMedia is still pending) supersedes
   * the older chain, which tears down its own stream and context and
   * rejects with err.superseded (no onError), instead of clobbering the
   * winner's state, which used to leak a hot mic and double-feed frames.
   * The graph is built on chain-local variables and published to the
   * module state only once the chain has won.
   *
   * Device loss: the live track's 'ended' event (mic unplugged, Bluetooth
   * dropped) stops the capture and reports onError(Error('microphone
   * disconnected')) so the page can surface it instead of silently
   * freezing; stop() removes the listener so deliberate teardowns never
   * report a phantom disconnect.
   */
  function createCapture(opts) {
    var stream = null;
    var ctx = null;
    var node = null;
    var source = null;
    var sink = null;
    var liveTrack = null;   // track carrying the 'ended' listener
    var onTrackEnded = null;
    var startGen = 0;       // generation token; bumped by every start()

    var capture = {
      running: false,

      start: function (deviceId) {
        capture.stop();
        var gen = ++startGen;
        // Everything this chain creates stays local until it wins.
        var myStream = null;
        var myCtx = null;
        var myNode = null;
        var mySource = null;
        var mySink = null;

        function teardownMine() {
          if (myNode) {
            try { myNode.port.onmessage = null; myNode.disconnect(); } catch (e) { /* torn */ }
            myNode = null;
          }
          if (mySource) { try { mySource.disconnect(); } catch (e) { /* torn */ } mySource = null; }
          if (mySink) { try { mySink.disconnect(); } catch (e) { /* torn */ } mySink = null; }
          if (myStream) {
            var tracks = myStream.getTracks();
            for (var i = 0; i < tracks.length; i++) {
              try { tracks[i].stop(); } catch (e) { /* torn */ }
            }
            myStream = null;
          }
          if (myCtx) { try { myCtx.close(); } catch (e) { /* torn */ } myCtx = null; }
        }

        // After every await: if a newer start() ran, clean up this chain's
        // resources and bail without touching the winner's.
        function checkCurrent() {
          if (gen === startGen) return;
          teardownMine();
          var err = new Error('capture start superseded by a newer start');
          err.superseded = true;
          throw err;
        }

        var audio = {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
          channelCount: 1
        };
        if (deviceId) audio.deviceId = deviceId;
        return navigator.mediaDevices.getUserMedia({ audio: audio }).then(function (s) {
          myStream = s;
          checkCurrent();
          // Ask for 16 kHz so the browser resamples for us; some engines
          // refuse and open at the hardware rate, in which case the worklet
          // resampler takes over (needResample inside the processor).
          try {
            myCtx = new AudioContext({ sampleRate: TARGET_RATE });
          } catch (e) {
            myCtx = new AudioContext();
          }
          return myCtx.audioWorklet.addModule(WORKLET_URL);
        }).then(function () {
          checkCurrent();
          mySource = myCtx.createMediaStreamSource(myStream);
          myNode = new AudioWorkletNode(myCtx, 'mc-capture', {
            numberOfInputs: 1,
            numberOfOutputs: 1,
            processorOptions: { targetRate: TARGET_RATE, frameSamples: FRAME_SAMPLES }
          });
          myNode.port.onmessage = function (ev) {
            var d = ev.data || {};
            if (d.pcm && opts.onFrame) opts.onFrame(new Int16Array(d.pcm));
            if (typeof d.rms === 'number' && opts.onLevel) opts.onLevel(d.rms);
          };
          // A muted sink keeps the worklet pulled by the graph without
          // feeding the mic back to the speakers.
          mySink = myCtx.createGain();
          mySink.gain.value = 0;
          mySource.connect(myNode);
          myNode.connect(mySink);
          mySink.connect(myCtx.destination);
          // Some browsers start contexts suspended until a user gesture;
          // preflight opens from a click, so resume() succeeds here.
          return myCtx.state === 'suspended' ? myCtx.resume() : Promise.resolve();
        }).then(function () {
          checkCurrent();
          // This chain won: publish its graph as the module's live capture.
          stream = myStream;
          ctx = myCtx;
          node = myNode;
          source = mySource;
          sink = mySink;
          capture.running = true;
          var track = stream.getAudioTracks()[0];
          if (track) {
            // Mic unplugged or Bluetooth dropped mid-capture: without this
            // the source just goes silent and the follow scroll freezes
            // with no error.
            liveTrack = track;
            onTrackEnded = function () {
              capture.stop();
              if (opts.onError) opts.onError(new Error('microphone disconnected'));
            };
            liveTrack.addEventListener('ended', onTrackEnded);
          }
          var settings = track && track.getSettings ? track.getSettings() : {};
          return {
            settings: settings,
            warnings: constraintWarnings(settings, ctx.sampleRate),
            contextRate: ctx.sampleRate,
            resampling: ctx.sampleRate !== TARGET_RATE,
            deviceId: settings.deviceId || deviceId || null,
            label: track ? track.label : ''
          };
        }).catch(function (err) {
          teardownMine();
          if (gen === startGen && !err.superseded) {
            capture.stop();
            if (opts.onError) opts.onError(err);
          }
          throw err;
        });
      },

      stop: function () {
        capture.running = false;
        if (liveTrack && onTrackEnded) {
          try { liveTrack.removeEventListener('ended', onTrackEnded); } catch (e) { /* torn */ }
        }
        liveTrack = null;
        onTrackEnded = null;
        if (node) {
          try { node.port.onmessage = null; node.disconnect(); } catch (e) { /* torn */ }
          node = null;
        }
        if (source) { try { source.disconnect(); } catch (e) { /* torn */ } source = null; }
        if (sink) { try { sink.disconnect(); } catch (e) { /* torn */ } sink = null; }
        if (stream) {
          var tracks = stream.getTracks();
          for (var i = 0; i < tracks.length; i++) {
            try { tracks[i].stop(); } catch (e) { /* torn */ }
          }
          stream = null;
        }
        if (ctx) { try { ctx.close(); } catch (e) { /* torn */ } ctx = null; }
      }
    };

    return capture;
  }

  // Same math as the worklet fallback: stateless variant for whole buffers.
  function resampleLinear(samples, fromRate, toRate) {
    if (fromRate === toRate) return samples;
    var outLen = Math.floor(samples.length * toRate / fromRate);
    var out = new Float32Array(outLen);
    var ratio = fromRate / toRate;
    for (var i = 0; i < outLen; i++) {
      var p = i * ratio;
      var idx = Math.floor(p);
      var frac = p - idx;
      var s0 = samples[idx];
      var s1 = idx + 1 < samples.length ? samples[idx + 1] : s0;
      out[i] = s0 + (s1 - s0) * frac;
    }
    return out;
  }

  function floatToInt16(samples, start, count) {
    var pcm = new Int16Array(count);
    for (var i = 0; i < count; i++) {
      var v = samples[start + i];
      if (v > 1) v = 1;
      else if (v < -1) v = -1;
      pcm[i] = v < 0 ? v * 32768 : v * 32767;
    }
    return pcm;
  }

  /* ?sim-audio=1 dev seam, part 1: decode a WAV ArrayBuffer with an
   * OfflineAudioContext and chunk it into the exact frames the worklet
   * would emit (1920-sample Int16 at 16 kHz, resampled if the decode rate
   * differs). Returns Promise<[{pcm: Int16Array, rms: number}]>.
   */
  function wavToFrames(arrayBuffer) {
    var off = new OfflineAudioContext(1, TARGET_RATE, TARGET_RATE);
    return new Promise(function (resolve, reject) {
      // Callback form for the widest browser support.
      off.decodeAudioData(arrayBuffer, resolve, reject);
    }).then(function (audioBuf) {
      var data = audioBuf.getChannelData(0);
      if (audioBuf.sampleRate !== TARGET_RATE) {
        data = resampleLinear(data, audioBuf.sampleRate, TARGET_RATE);
      }
      var frames = [];
      for (var start = 0; start + FRAME_SAMPLES <= data.length; start += FRAME_SAMPLES) {
        var sum = 0;
        for (var i = 0; i < FRAME_SAMPLES; i++) {
          var v = data[start + i];
          sum += v * v;
        }
        frames.push({
          pcm: floatToInt16(data, start, FRAME_SAMPLES),
          rms: Math.sqrt(sum / FRAME_SAMPLES)
        });
      }
      return frames;
    });
  }

  /* ?sim-audio=1 dev seam, part 2: stream pre-chunked frames at real-time
   * pacing (120 ms per frame) through the caller's push function, which is
   * the very same sendAudioFrame the mic path uses.
   * push: fn(Int16Array, rms). Returns {stop()}; calls onDone at the end.
   */
  function streamFrames(frames, push, onDone) {
    var i = 0;
    var timer = setInterval(function () {
      if (i >= frames.length) {
        clearInterval(timer);
        if (onDone) onDone();
        return;
      }
      var f = frames[i++];
      push(f.pcm, f.rms);
    }, Math.round(1000 * FRAME_SAMPLES / TARGET_RATE));
    return {
      stop: function () { clearInterval(timer); }
    };
  }

  window.MC.audio = {
    MIC_KEY: MIC_KEY,
    TARGET_RATE: TARGET_RATE,
    FRAME_SAMPLES: FRAME_SAMPLES,
    getSavedMic: getSavedMic,
    saveMic: saveMic,
    listInputs: listInputs,
    constraintWarnings: constraintWarnings,
    createCapture: createCapture,
    wavToFrames: wavToFrames,
    streamFrames: streamFrames
  };
})();
