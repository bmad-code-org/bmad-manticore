/* mc-prompter AudioWorklet capture processor (Phase B voice-follow).
 *
 * Loaded via audioWorklet.addModule('/static/js/capture-worklet.js') and
 * instantiated as new AudioWorkletNode(ctx, 'mc-capture', {processorOptions}).
 *
 * Emits mono PCM16 frames of ~120 ms at 16 kHz (1920 samples / 3840 bytes)
 * to the page via port.postMessage({pcm: <ArrayBuffer Int16>, rms: <float>})
 * with the buffer transferred. RMS is computed on the float frame before
 * quantization so the level meter reflects the true capture level.
 *
 * The AudioContext is created with {sampleRate: 16000}; when the browser
 * refuses that rate (this.needResample), a stateful linear-interpolation
 * resampler converts from the actual context rate to 16 kHz here in the
 * worklet, carrying the last sample and fractional read position across
 * render quanta so there are no seams (naive decimation would alias into
 * the speech band).
 *
 * processorOptions:
 *   targetRate    output sample rate, default 16000
 *   frameSamples  samples per emitted frame, default 1920 (120 ms at 16 kHz)
 */
'use strict';

class McCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    var opts = (options && options.processorOptions) || {};
    this.targetRate = opts.targetRate || 16000;
    this.frameSamples = opts.frameSamples || 1920;
    this.buf = new Float32Array(this.frameSamples);
    this.fill = 0;
    // `sampleRate` is the AudioWorkletGlobalScope context rate.
    this.needResample = sampleRate !== this.targetRate;
    this.ratio = sampleRate / this.targetRate;
    // Resampler state carried across quanta: position 0 is prevSample (the
    // last input sample of the previous quantum), 1 is quantum[0], and so on.
    this.p = 0;
    this.prevSample = 0;
  }

  emitSample(v) {
    this.buf[this.fill++] = v;
    if (this.fill >= this.frameSamples) this.flushFrame();
  }

  flushFrame() {
    var n = this.frameSamples;
    var sum = 0;
    var pcm = new Int16Array(n);
    for (var i = 0; i < n; i++) {
      var v = this.buf[i];
      sum += v * v;
      if (v > 1) v = 1;
      else if (v < -1) v = -1;
      pcm[i] = v < 0 ? v * 32768 : v * 32767;
    }
    var rms = Math.sqrt(sum / n);
    this.port.postMessage({ pcm: pcm.buffer, rms: rms }, [pcm.buffer]);
    this.fill = 0;
  }

  process(inputs) {
    var input = inputs[0];
    var ch = input && input[0];
    if (!ch || ch.length === 0) return true; // keep alive through gaps

    if (!this.needResample) {
      for (var i = 0; i < ch.length; i++) this.emitSample(ch[i]);
      return true;
    }

    // Linear interpolation over the virtual stream a[0]=prevSample,
    // a[j]=ch[j-1]. For p < n the pair a[i]..a[i+1] (i = floor(p)) is
    // always available.
    var n = ch.length;
    var p = this.p;
    var prev = this.prevSample;
    var ratio = this.ratio;
    while (p < n) {
      var idx = Math.floor(p);
      var frac = p - idx;
      var s0 = idx === 0 ? prev : ch[idx - 1];
      var s1 = ch[idx];
      this.emitSample(s0 + (s1 - s0) * frac);
      p += ratio;
    }
    this.p = p - n;
    this.prevSample = ch[n - 1];
    return true;
  }
}

registerProcessor('mc-capture', McCaptureProcessor);
