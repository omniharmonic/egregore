// Capture, downsample, gate, send.
//
// Kept out of lens.js, which is about rendering. Gating here rather than on
// the server is deliberate twice over: a party of phones streaming
// continuously is real bandwidth, and silence never leaving the device is a
// better privacy story than silence the server receives and discards.

const TARGET_RATE = 16000;
const FRAME = 800;          // 50 ms at 16 kHz — one ingest message
const OPEN_RMS = 0.015;     // start sending above this
const CLOSE_RMS = 0.008;    // stop below this (hysteresis, so speech pauses
                            // do not chop a sentence into fragments)
const HANGOVER_MS = 700;    // keep sending this long after dropping quiet

/**
 * Start transmitting this device's microphone into a zone.
 *
 * @param {object} opts
 * @param {string} opts.zone     zone id to join
 * @param {string} opts.nodeId   this device's enrolled node id
 * @param {(rms:number, sending:boolean)=>void} [opts.onLevel]
 * @param {(state:string)=>void} [opts.onState]
 * @returns {Promise<{stop:()=>void}>}
 */
export async function startTransmit({ zone, nodeId, onLevel, onState }) {
  const say = (s) => { if (onState) onState(s); };

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });

  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  if (ctx.state === 'suspended') await ctx.resume();
  const source = ctx.createMediaStreamSource(stream);
  const ratio = ctx.sampleRate / TARGET_RATE;

  let ws = null;
  let closed = false;
  let retry = 0;
  let openUntil = 0;
  let pending = [];          // 16 kHz float samples awaiting a full frame

  function connect() {
    if (closed) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${location.host}/ws/ingest` +
      `?zone=${encodeURIComponent(zone)}&node=${encodeURIComponent(nodeId)}`;
    try {
      ws = new WebSocket(url);
    } catch {
      return scheduleRetry();
    }
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => { retry = 0; say('live'); };
    ws.onclose = () => { ws = null; say('reconnecting'); scheduleRetry(); };
    ws.onerror = () => { /* close follows */ };
  }

  function scheduleRetry() {
    if (closed) return;
    retry = Math.min(retry + 1, 6);
    setTimeout(connect, Math.min(500 * 2 ** (retry - 1), 15000));
  }

  /** Average-decimate to 16 kHz. Cheap, and adequate for speech. */
  function downsample(input) {
    const out = new Float32Array(Math.floor(input.length / ratio));
    for (let i = 0; i < out.length; i++) {
      const start = Math.floor(i * ratio);
      const end = Math.min(Math.floor((i + 1) * ratio), input.length);
      let sum = 0;
      for (let j = start; j < end; j++) sum += input[j];
      out[i] = end > start ? sum / (end - start) : 0;
    }
    return out;
  }

  function handle(input) {
    const down = downsample(input);
    for (let i = 0; i < down.length; i++) pending.push(down[i]);

    while (pending.length >= FRAME) {
      const frame = pending.slice(0, FRAME);
      pending = pending.slice(FRAME);

      let sum = 0;
      for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
      const rms = Math.sqrt(sum / frame.length);

      const now = Date.now();
      if (rms >= OPEN_RMS) openUntil = now + HANGOVER_MS;
      else if (rms < CLOSE_RMS && now > openUntil) openUntil = 0;
      const sending = now <= openUntil;

      if (onLevel) onLevel(rms, sending);

      if (sending && ws && ws.readyState === 1) {
        const pcm = new Int16Array(FRAME);
        for (let i = 0; i < FRAME; i++) {
          const v = Math.max(-1, Math.min(1, frame[i]));
          pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
        }
        ws.send(pcm.buffer);
      }
    }
  }

  // AudioWorklet where available; ScriptProcessor is the fallback iOS Safari
  // still needs. Both end up calling handle() with Float32 at ctx.sampleRate.
  let node = null;
  let usingWorklet = false;
  try {
    const src = `
      class Tap extends AudioWorkletProcessor {
        process(inputs) {
          const ch = inputs[0] && inputs[0][0];
          if (ch && ch.length) this.port.postMessage(ch.slice(0));
          return true;
        }
      }
      registerProcessor('egregore-tap', Tap);
    `;
    const url = URL.createObjectURL(new Blob([src], { type: 'text/javascript' }));
    await ctx.audioWorklet.addModule(url);
    URL.revokeObjectURL(url);
    node = new AudioWorkletNode(ctx, 'egregore-tap');
    node.port.onmessage = (ev) => handle(ev.data);
    usingWorklet = true;
  } catch {
    node = ctx.createScriptProcessor(4096, 1, 1);
    node.onaudioprocess = (ev) => handle(ev.inputBuffer.getChannelData(0));
  }

  source.connect(node);
  if (!usingWorklet) {
    // A ScriptProcessor only runs while connected to a destination. Route it
    // through a silent gain so nothing is played back into the room.
    const mute = ctx.createGain();
    mute.gain.value = 0;
    node.connect(mute);
    mute.connect(ctx.destination);
  }

  connect();
  say('connecting');

  return {
    stop() {
      closed = true;
      try { if (ws) ws.close(); } catch { /* already gone */ }
      try { source.disconnect(); node.disconnect(); } catch { /* already gone */ }
      for (const track of stream.getTracks()) track.stop();
      ctx.close().catch(() => {});
      say('stopped');
    },
  };
}
