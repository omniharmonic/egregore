// audio.js — feature transport (WS or local mic) + per-feature attack/release
// smoothing + an idle LFO fallback. Uniforms are never bound to raw values.

// [attack_s, release_s] per feature. Fast up, slow down.
const ENV = {
  rms: [0.080, 0.60], low: [0.080, 0.60], mid: [0.080, 0.60], high: [0.070, 0.55],
  centroid: [0.120, 0.80], onset: [0.012, 1.20],
  energy: [0.40, 2.5], valence: [0.60, 3.0], intensity: [0.40, 2.5],
};
const KEYS = Object.keys(ENV);

const clamp01 = (x) => (x > 1 ? 1 : x < 0 ? 0 : (x === x ? x : 0));

export class Features {
  constructor(opts = {}) {
    this.zone = opts.zone || 'main';
    this.local = !!opts.local;
    this.phase = opts.phase || 0;

    this.target = {}; this.value = {};
    for (const k of KEYS) { this.target[k] = 0; this.value[k] = 0; }
    // Mood defaults sit mid-scale so a feed that never sends mood still looks intentional.
    this.target.valence = this.value.valence = 0.5;
    this.target.energy = this.value.energy = 0.25;
    this.target.intensity = this.value.intensity = 0.25;

    this.state = 'init';        // init | connecting | live | idle
    this.lastFrame = 0;
    this.retry = 0;
    this.ws = null;
    this.stopped = false;
    this._t0 = performance.now() / 1000;
  }

  start() {
    if (this.local) this._startMic();
    else this._connect();
  }

  stop() {
    this.stopped = true;
    try { this.ws && this.ws.close(); } catch { /* already gone */ }
    try { this.ctx && this.ctx.close(); } catch { /* already gone */ }
  }

  // --- WebSocket transport ---------------------------------------------

  _connect() {
    if (this.stopped) return;
    this.state = 'connecting';
    let ws;
    try {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      ws = new WebSocket(`${proto}//${location.host}/ws/features?zone=${encodeURIComponent(this.zone)}`);
    } catch {
      return this._reconnect();
    }
    this.ws = ws;
    ws.onopen = () => { this.retry = 0; this.state = 'live'; this.lastFrame = performance.now(); };
    ws.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      this.ingest(m);
    };
    ws.onerror = () => { /* onclose always follows; nothing to do here */ };
    ws.onclose = () => { this.ws = null; this._reconnect(); };
  }

  _reconnect() {
    if (this.stopped) return;
    this.state = 'idle';
    const wait = Math.min(15000, 500 * Math.pow(1.7, this.retry++)) * (0.75 + Math.random() * 0.5);
    setTimeout(() => this._connect(), wait);
  }

  /** Accept a features/mood message from any source. */
  ingest(m) {
    if (!m || typeof m !== 'object') return;
    if (m.type === 'features') {
      const n = (v, d) => (typeof v === 'number' && v === v ? clamp01(v) : d);
      this.target.rms = n(m.rms, this.target.rms);
      this.target.low = n(m.low, this.target.low);
      this.target.mid = n(m.mid, this.target.mid);
      this.target.high = n(m.high, this.target.high);
      this.target.centroid = n(m.centroid, this.target.centroid);
      // Onset is a spike: latch it up and let the release envelope bring it down.
      const on = n(m.onset, 0);
      if (on > this.target.onset) this.target.onset = on;
      else this.target.onset = 0;
      this.lastFrame = performance.now();
      this.state = 'live';
    } else if (m.type === 'mood') {
      const n = (v, d) => (typeof v === 'number' && v === v ? clamp01(v) : d);
      this.target.energy = n(m.energy, this.target.energy);
      this.target.valence = n(m.valence, this.target.valence);
      this.target.intensity = n(m.intensity, this.target.intensity);
    }
  }

  // --- local microphone -------------------------------------------------

  async _startMic() {
    this.state = 'connecting';
    try {
      const md = navigator.mediaDevices;
      if (!md || !md.getUserMedia) throw new Error('no getUserMedia');
      const stream = await md.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      });
      const AC = window.AudioContext || window.webkitAudioContext;
      const ctx = new AC();
      this.ctx = ctx;
      if (ctx.state === 'suspended') ctx.resume().catch(() => {});
      const an = ctx.createAnalyser();
      an.fftSize = 2048;
      an.smoothingTimeConstant = 0.55;
      ctx.createMediaStreamSource(stream).connect(an);
      this.analyser = an;
      this.freq = new Uint8Array(an.frequencyBinCount);
      this.td = new Float32Array(an.fftSize);
      this.prevRms = 0;
      this.state = 'live';
    } catch (e) {
      console.warn('[lens] local mic unavailable, falling back to zone feed:', e && e.message);
      this.local = false;
      this._connect();
    }
  }

  _pollMic() {
    const an = this.analyser;
    if (!an || !this.ctx) return;
    an.getFloatTimeDomainData(this.td);
    let s = 0;
    for (let i = 0; i < this.td.length; i++) s += this.td[i] * this.td[i];
    const rms = clamp01(Math.sqrt(s / this.td.length) * 3.2);

    an.getByteFrequencyData(this.freq);
    const nyq = this.ctx.sampleRate / 2;
    const bin = (hz) => Math.max(0, Math.min(this.freq.length - 1, Math.round(hz / nyq * this.freq.length)));
    const band = (a, b) => {
      const i0 = bin(a), i1 = Math.max(bin(b), i0 + 1);
      let t = 0; for (let i = i0; i < i1; i++) t += this.freq[i];
      return clamp01(t / (i1 - i0) / 190);
    };
    let num = 0, den = 0;
    for (let i = 0; i < this.freq.length; i++) { num += i * this.freq[i]; den += this.freq[i]; }
    const centroid = den > 0 ? clamp01((num / den) / (this.freq.length * 0.42)) : 0;

    const d = rms - this.prevRms;
    this.prevRms = rms;

    this.ingest({
      type: 'features', rms, low: band(30, 250), mid: band(250, 2000),
      high: band(2000, 9000), centroid, onset: clamp01(d * 9),
    });
    // Derive slow mood locally so intensity/energy uniforms are not dead.
    this.target.energy = this.target.energy * 0.995 + rms * 0.005;
    this.target.intensity = this.target.intensity * 0.995 + clamp01(rms * 0.7 + centroid * 0.3) * 0.005;
  }

  // --- smoothing --------------------------------------------------------

  /** Advance envelopes by dt seconds. Returns the smoothed uniform bag. */
  update(dt) {
    if (this.local) this._pollMic();

    const now = performance.now();
    const stale = this.state !== 'live' || (now - this.lastFrame) > 2500;
    if (stale && !this.local) {
      if (this.state === 'live') this.state = 'idle';
      this._idle(now / 1000 - this._t0);
    }
    if (this.target.onset > 0) this.target.onset = 0;  // spike is one-shot

    dt = Math.max(1 / 240, Math.min(0.25, dt));
    for (const k of KEYS) {
      const t = this.target[k], v = this.value[k];
      const tc = t > v ? ENV[k][0] : ENV[k][1];
      this.value[k] = v + (t - v) * (1 - Math.exp(-dt / tc));
    }
    return this.value;
  }

  /** No feed: breathe on slow LFOs instead of freezing or going flat. */
  _idle(t) {
    const p = this.phase * 6.283;
    const s = (f, o) => 0.5 + 0.5 * Math.sin(t * f + o + p);
    this.target.rms = 0.10 + 0.26 * s(0.089, 0.0);
    this.target.low = 0.12 + 0.30 * s(0.061, 1.1);
    this.target.mid = 0.14 + 0.26 * s(0.113, 2.3);
    this.target.high = 0.08 + 0.20 * s(0.157, 3.7);
    this.target.centroid = 0.30 + 0.28 * s(0.043, 0.6);
    this.target.energy = 0.16 + 0.18 * s(0.021, 1.7);
    this.target.intensity = 0.14 + 0.18 * s(0.017, 2.9);
    this.target.valence = 0.44 + 0.14 * s(0.013, 4.2);
    // A rare, gentle swell stands in for onsets so chroma is not frozen.
    const swell = Math.pow(s(0.037, 5.1), 8);
    if (swell > 0.55) this.target.onset = (swell - 0.55) * 0.9;
  }
}
