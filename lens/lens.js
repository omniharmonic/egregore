// lens.js — Egregore LENS. Vanilla ES2020 + WebGL2, no dependencies.
//
//   [video A] ─┐
//              ├─ crossfade ─► lens stack (ping-pong FBOs) ─► present ─► screen
//   [video B] ─┘                    ▲                            │
//                smoothed audio ────┴──────── u_prev ◄───────────┘
//
// Every failure path resolves to "keep showing something beautiful".

import { Renderer } from './gl.js';
import { Features } from './audio.js';
import { Playlist, Deck, ClipCache, safeJson } from './media.js';

// Every lens the client will accept from /api/config or ?stack=. An unknown
// name is dropped here rather than trusted; a known name whose .frag never
// arrives or fails to compile is dropped later, in loadShaders().
const KNOWN_LENSES = [
  'feedback', 'kaleidoscope', 'flow', 'chroma', 'bloom', 'liquid',
  'glitch', 'pixelsort', 'crt', 'corrupt', 'smoke',
];
const DEFAULT_STACK = ['flow', 'feedback', 'bloom'];
const DEFAULT_CROSSFADE = 2;
const MANIFEST_POLL_MS = 30000;

const q = new URLSearchParams(location.search);
const ZONE = q.get('zone') || 'main';
const SCREEN = q.get('screen') || '';
const HUD_ON = q.get('hud') === '1';
const AUDIO_LOCAL = q.get('audio') === 'local';
const CACHE_MB = Number(q.get('cache_mb'));
const CACHE_BYTES = (isFinite(CACHE_MB) && CACHE_MB > 0) ? Math.max(64, CACHE_MB) * 1e6 : 1.5e9;

const $ = (id) => document.getElementById(id);
const el = {
  canvas: $('gl'), vidA: $('vidA'), vidB: $('vidB'),
  enter: $('enter'), join: $('join'), joinForm: $('joinForm'),
  joinPass: $('joinPass'), joinErr: $('joinErr'), hud: $('hud'),
  notice: $('notice'), noticeText: $('noticeText'),
  bootLog: $('bootLog'), enterPrompt: $('enterPrompt'), enterHint: $('enterHint'),
};

const state = {
  stack: DEFAULT_STACK.slice(),
  activePasses: DEFAULT_STACK.length,
  phase: 0,
  crossfade: DEFAULT_CROSSFADE, crossfadePinned: false, playbackRate: 1,
  glOk: false, glLost: false,
  fps: 0, emaMs: 16, passes: 0,
  overBudgetSince: 0, underBudgetSince: 0,
  manifestWs: null, manifestRetry: 0, manifestState: 'init',
  authPending: false, started: false,
  configRevision: 0, stackPinned: false, params: {},
};

const cache = new ClipCache(CACHE_BYTES);
const renderer = new Renderer(el.canvas);
let playlist = null, deck = null, features = null;

// ---------------------------------------------------------------------------
// Auth veil
// ---------------------------------------------------------------------------

function showJoin() {
  if (state.authPending) return;
  state.authPending = true;
  el.join.hidden = false;
  el.join.classList.remove('out');
  document.body.classList.add('ui');
  sizePass();
  setTimeout(() => { try { el.joinPass.focus(); } catch { /* */ } }, 60);
}

function hideJoin() {
  state.authPending = false;
  el.join.classList.add('out');
  document.body.classList.remove('ui');
  setTimeout(() => { el.join.hidden = true; }, 950);
}

// The password field carries no native caret (the block cursor beside it is
// the caret), so its width has to track the value or the cursor drifts.
function sizePass() {
  if (!el.joinPass) return;
  const n = Math.max(1, el.joinPass.value.length);
  el.joinPass.style.width = `${n}ch`;
}
if (el.joinPass) el.joinPass.addEventListener('input', sizePass);

el.joinForm.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  el.joinErr.textContent = '';
  const password = el.joinPass.value;
  try {
    const r = await fetch('/api/join', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ password }),
    });
    if (!r.ok) { el.joinErr.textContent = 'denied.'; return; }
    el.joinPass.value = '';
    sizePass();
    hideJoin();
    await bootstrapData();          // retry everything now that we have a cookie
    if (features) { features.retry = 0; }
  } catch {
    el.joinErr.textContent = 'no route to host.';
  }
});

// ---------------------------------------------------------------------------
// Enter overlay — fullscreen is offered, never required
// ---------------------------------------------------------------------------

// --- boot log ---------------------------------------------------------------
// Four lines of quiet status, typed out. Each line's value is resolved at the
// moment it is typed rather than up front, so the log reports what is actually
// true by then — a slow or absent server shows "waiting", not a lie, and never
// delays the scrim. Nothing here is allowed to hold the enter overlay open.

const LABEL_W = 8;
const BOOT_LINES = [
  ['zone', () => ZONE + (SCREEN ? `/${SCREEN}` : '')],
  ['stack', () => state.stack.join('>') || 'none'],
  ['render', () => (state.glOk ? 'webgl2' : document.body.classList.contains('nogl') ? 'video (no webgl2)' : 'probing')],
  ['server', () => {
    if (playlist && playlist.ok) return `linked · ${playlist.entries.length} clips`;
    if (state.authPending) return 'locked';
    return 'waiting';
  }],
];

let bootTimer = 0;

function bootDone() {
  if (bootTimer) { clearTimeout(bootTimer); bootTimer = 0; }
  if (el.enterPrompt) el.enterPrompt.hidden = false;
  if (el.enterHint) el.enterHint.hidden = false;
}

const CHAR_MS = 7, LINE_GAP_MS = 80, TICK_MS = 16;

function typeBoot() {
  if (!el.bootLog) { bootDone(); return; }
  let li = 0, text = '', line = '', at = 0;
  const step = () => {
    bootTimer = 0;
    if (el.enter.hidden || el.enter.classList.contains('out')) return;
    if (li >= BOOT_LINES.length) { bootDone(); return; }

    const now = performance.now();
    if (at === 0) {
      // Latch the value once per line: re-reading it per character would let a
      // line change length while it is being typed.
      const [label, get] = BOOT_LINES[li];
      let value = 'waiting';
      try { value = String(get()); } catch { /* keep the placeholder */ }
      line = `${label} ${'.'.repeat(Math.max(1, LABEL_W - label.length))} ${value}`;
      at = now;
    }

    // Advance by elapsed time, not by one character per tick. The renderer can
    // be several frames behind on a weak GPU, and a per-tick typewriter there
    // crawls — the log must finish on schedule regardless of frame rate.
    const ci = Math.floor((now - at) / CHAR_MS);
    if (ci >= line.length) {
      text += line + '\n';
      el.bootLog.textContent = text;
      li++; at = 0;
      bootTimer = setTimeout(step, LINE_GAP_MS);
      return;
    }
    el.bootLog.textContent = text + line.slice(0, ci);
    bootTimer = setTimeout(step, TICK_MS);
  };
  step();
}

function dismissEnter(requestFs) {
  if (el.enter.classList.contains('out')) return;
  bootDone();
  el.enter.classList.add('out');
  setTimeout(() => { el.enter.hidden = true; }, 1000);
  if (requestFs && document.documentElement.requestFullscreen && !document.fullscreenElement) {
    try {
      const p = document.documentElement.requestFullscreen({ navigationUI: 'hide' });
      if (p && p.catch) p.catch(() => { /* declined; we render anyway */ });
    } catch { /* unsupported; we render anyway */ }
  }
  if (deck) deck.resync();
}

for (const evt of ['pointerdown', 'keydown', 'touchstart']) {
  window.addEventListener(evt, () => dismissEnter(true), { once: false, passive: true });
}
// An unattended screen must not sit behind an overlay forever.
setTimeout(() => dismissEnter(false), 12000);

// ---------------------------------------------------------------------------
// Data bootstrap: config, shaders, manifest
// ---------------------------------------------------------------------------

async function loadConfig() {
  const url = `/api/config?zone=${encodeURIComponent(ZONE)}` +
    (SCREEN ? `&screen=${encodeURIComponent(SCREEN)}` : '');
  const c = await safeJson(url, showJoin);
  let stack = DEFAULT_STACK.slice();
  if (c && c.lens_params && typeof c.lens_params === 'object') {
    state.params = c.lens_params;
  }
  if (c && Array.isArray(c.lens_stack)) {
    const s = c.lens_stack.filter((n) => KNOWN_LENSES.includes(n));
    if (s.length) stack = s;
  }
  // ?stack=flow,bloom overrides everything — for tuning a screen on site
  // without touching the party config. `?stack=` (empty) means no lenses.
  const raw = q.get('stack');
  if (raw !== null) {
    stack = raw.split(',').map((n) => n.trim()).filter((n) => KNOWN_LENSES.includes(n));
    state.stackPinned = true;   // this screen was tuned by hand; leave it alone
  }
  if (c && typeof c.loop_phase_offset === 'number' && isFinite(c.loop_phase_offset)) {
    state.phase = ((c.loop_phase_offset % 1) + 1) % 1;
  }
  if (c && typeof c.crossfade_s === 'number' && c.crossfade_s > 0) {
    state.crossfade = c.crossfade_s;
    state.crossfadePinned = true;
  }
  if (c && typeof c.playback_rate === 'number' && c.playback_rate > 0) {
    state.playbackRate = Math.max(0.25, Math.min(2, c.playback_rate));
  }
  // The config may also route this screen to its own room's microphone
  // (`audio_source: "local_mic"` in the party YAML, §4). The URL param wins.
  if (!AUDIO_LOCAL && c && c.audio_source === 'local_mic' && features && !features.local) {
    features.stop();
    features = new Features({ zone: ZONE, local: true, phase: state.phase });
    features.start();
  }
  // `stack` already resolves to the URL override when one is present, and
  // the URL does not change under us — so assigning unconditionally is what
  // keeps a pinned screen pinned. The earlier guard skipped the assignment
  // whenever a stack was already set, which meant ?stack= never applied at
  // all: the default stack was always "already set" by then.
  state.stack = stack;
  state.activePasses = stack.length;
}

// Re-read /api/config and adopt any change to the stack, crossfade or audio
// routing without a reload. Safe to call at any time: loadConfig() is
// idempotent and loadShaders() skips shaders already compiled.
async function applyConfig() {
  const before = state.stack.join('>');
  try {
    await loadConfig();
    if (state.glOk) await loadShaders(state.stack);
  } catch (e) {
    console.warn('[lens] config reload failed:', e && e.message);
    return;
  }
  if (playlist) {
    playlist.phase = state.phase;
    playlist.crossfade = state.crossfade;
    playlist.crossfadePinned = !!state.crossfadePinned;
    playlist.playbackRate = state.playbackRate;
  }
  if (features) features.phase = state.phase;
  if (state.stack.join('>') !== before) {
    console.info(`[lens] stack ${before || 'none'} -> ${state.stack.join('>') || 'none'}`);
  }
}

async function loadShaders(names) {
  const want = ['crossfade', 'present', ...names];
  const jobs = want.map(async (n) => {
    if (renderer.sources.has(n)) return;
    try {
      const r = await fetch(`/static/shaders/${n}.frag`, { credentials: 'same-origin' });
      if (!r.ok) throw new Error(String(r.status));
      const src = await r.text();
      if (!/void\s+main/.test(src)) throw new Error('not a shader');
      renderer.addShader(n, src);
    } catch (e) {
      console.warn(`[lens] shader ${n} unavailable:`, e && e.message);
    }
  });
  await Promise.all(jobs);
  // Drop lenses whose shader never arrived or failed to compile.
  state.stack = state.stack.filter((n) => renderer.has(n));
  state.activePasses = Math.min(state.activePasses, state.stack.length);
}

async function bootstrapData() {
  await loadConfig();
  if (state.glOk) await loadShaders(state.stack);

  if (features) features.phase = state.phase;   // idle LFOs diverge per screen
  if (!playlist) playlist = new Playlist(ZONE, state.phase, showJoin);
  playlist.phase = state.phase;
  playlist.crossfade = state.crossfade;
  playlist.crossfadePinned = !!state.crossfadePinned;
  playlist.playbackRate = state.playbackRate;

  const got = await playlist.refresh();
  if (!got) {
    // A screen on a zone the party does not have used to sit black forever.
    // Say so — the fix is in the address bar, not in the room.
    try {
      const r = await fetch(`/api/manifest?zone=${encodeURIComponent(ZONE)}`,
                            { credentials: 'same-origin' });
      if (r.status === 404) {
        el.noticeText.textContent =
          `this party has no zone called "${ZONE}" — check ?zone= in the address`;
        el.notice.hidden = false;
        document.body.classList.add('ui');
      }
    } catch { /* keep retrying below */ }
    console.warn('[lens] manifest unavailable; will keep retrying');
    setTimeout(() => { if (!playlist.ok) bootstrapData(); }, 5000);
    return;
  }
  if (!el.notice.hidden) { el.notice.hidden = true; document.body.classList.remove('ui'); }
  if (!deck) deck = new Deck([el.vidA, el.vidB], playlist, cache);
  if (!deck.clipId[deck.active]) await deck.begin();
}

// ---------------------------------------------------------------------------
// Manifest change notifications (WS, with polling fallback)
// ---------------------------------------------------------------------------

function connectManifestWs() {
  let ws;
  state.manifestState = 'connecting';
  try {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${location.host}/ws/manifest?zone=${encodeURIComponent(ZONE)}`);
  } catch { return scheduleManifestRetry(); }
  state.manifestWs = ws;
  ws.onopen = () => { state.manifestRetry = 0; state.manifestState = 'live'; };
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m && m.type === 'manifest' && playlist && m.revision !== playlist.revision) {
      playlist.refresh();
    }
    // The operator changed this zone's look. Screens only read /api/config
    // at boot, so without this the room would have to be reloaded by hand.
    if (m && m.type === 'config' && m.revision !== state.configRevision) {
      state.configRevision = m.revision;
      applyConfig();
    }
  };
  ws.onerror = () => { /* close follows */ };
  ws.onclose = () => { state.manifestWs = null; scheduleManifestRetry(); };
}

function scheduleManifestRetry() {
  state.manifestState = 'down';
  const wait = Math.min(20000, 700 * Math.pow(1.7, state.manifestRetry++)) * (0.75 + Math.random() * 0.5);
  setTimeout(connectManifestWs, wait);
}

// Poll regardless: if the socket is down this is the only refresh path, and if
// it is up a 30 s poll costs nothing.
setInterval(() => {
  if (!playlist) return;
  if (state.manifestState !== 'live' || !playlist.ok) playlist.refresh();
}, MANIFEST_POLL_MS);

window.addEventListener('online', () => {
  if (deck) deck.offline = false;
  if (playlist) playlist.refresh();
});
window.addEventListener('offline', () => { if (deck) deck.offline = true; });

// ---------------------------------------------------------------------------
// Canvas sizing + context loss
// ---------------------------------------------------------------------------

function resize() {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = Math.max(2, Math.round(innerWidth * dpr));
  const h = Math.max(2, Math.round(innerHeight * dpr));
  if (el.canvas.width !== w || el.canvas.height !== h) {
    el.canvas.width = w; el.canvas.height = h;
  }
  renderer.resize(w, h);
}
window.addEventListener('resize', resize);
window.addEventListener('orientationchange', resize);

el.canvas.addEventListener('webglcontextlost', (e) => {
  e.preventDefault();
  state.glLost = true;
  console.warn('[lens] GL context lost');
});
el.canvas.addEventListener('webglcontextrestored', () => {
  console.warn('[lens] GL context restored; rebuilding');
  renderer.forget();
  state.glOk = renderer.init();
  if (state.glOk) { resize(); state.glLost = false; }
  else enterFallback();
});

function enterFallback() {
  document.body.classList.add('nogl');
  console.warn('[lens] running plain-video fallback (no WebGL2)');
}

// ---------------------------------------------------------------------------
// Frame loop
// ---------------------------------------------------------------------------

let last = performance.now();
let fpsAcc = 0, fpsN = 0, fpsAt = last;

function frame(now) {
  requestAnimationFrame(frame);
  const dt = Math.min(0.25, Math.max(0.0005, (now - last) / 1000));
  last = now;

  const audio = features ? features.update(dt) : null;
  if (deck) { try { deck.tick(dt); } catch (e) { console.warn('[lens] deck', e); } }

  if (state.glOk && !state.glLost) {
    try { drawGL(now / 1000, audio); } catch (e) { console.warn('[lens] draw', e); }
  } else if (deck) {
    drawFallback();
  }

  // --- perf metering + VIS-7 pass governor ---
  // The metric is the mean rAF interval over a 500 ms window, then an EMA of
  // those windows. Two things rule out the obvious alternatives: our own JS
  // duration says nothing (GPU work is async), and a per-frame EMA is fooled
  // by bursty delivery — a stalled compositor emits one long frame then a
  // clutch of ~1 ms ones, which drags a per-frame EMA under budget and makes
  // the governor oscillate instead of settling.
  fpsAcc += dt; fpsN++;
  if (now - fpsAt > 500) {
    state.fps = fpsN / Math.max(fpsAcc, 1e-6);
    if (!document.hidden && now > 1500) {
      state.emaMs = state.emaMs * 0.70 + (1000 / Math.max(state.fps, 0.5)) * 0.30;
    }
    fpsAcc = 0; fpsN = 0; fpsAt = now;
  }
  governPasses(now);
  if (HUD_ON) updateHud();
}

function drawGL(time, audio) {
  deck && renderer.uploadVideo(0, el.vidA);
  deck && renderer.uploadVideo(1, el.vidB);
  const s = {
    time,
    mix: deck ? deck.mix : 0,
    sizeA: deck ? deck.size(0) : [0, 0],
    sizeB: deck ? deck.size(1) : [0, 0],
    audio: audio || {},
    params: state.params,
  };
  state.passes = renderer.render(state.stack.slice(0, state.activePasses), s);
}

function drawFallback() {
  // CSS crossfade between the two <video> elements.
  el.vidA.style.opacity = String(1 - deck.mix);
  el.vidB.style.opacity = String(deck.mix);
  state.passes = 0;
}

function governPasses(now) {
  if (!state.glOk || document.hidden || now < 2500) return;
  if (state.emaMs > 24) {
    state.underBudgetSince = 0;
    if (!state.overBudgetSince) state.overBudgetSince = now;
    else if (now - state.overBudgetSince > 3000 && state.activePasses > 0) {
      state.activePasses--;
      state.overBudgetSince = now;
      console.warn(`[lens] VIS-7: frame ${state.emaMs.toFixed(1)}ms — dropping to ` +
        `${state.activePasses} lens pass(es): [${state.stack.slice(0, state.activePasses).join(', ')}]`);
    }
  } else if (state.emaMs < 12) {
    state.overBudgetSince = 0;
    if (!state.underBudgetSince) state.underBudgetSince = now;
    else if (now - state.underBudgetSince > 30000 && state.activePasses < state.stack.length) {
      state.activePasses++;
      state.underBudgetSince = now;
      console.info(`[lens] VIS-7: headroom — restoring lens pass ${state.activePasses}`);
    }
  } else {
    state.overBudgetSince = 0; state.underBudgetSince = 0;
  }
}

// ---------------------------------------------------------------------------
// HUD
// ---------------------------------------------------------------------------

/** Wrap fixed-width lines in a box-drawing frame with the title in the rule. */
function boxed(title, lines) {
  let w = title.length + 1;
  for (const l of lines) w = Math.max(w, l.length);
  const top = `┌─ ${title} ` + '─'.repeat(w - title.length - 1) + '┐';
  const bot = '└' + '─'.repeat(w + 2) + '┘';
  const body = lines.map((l) => `│ ${l.padEnd(w)} │`);
  return [top, ...body, bot].join('\n');
}

let hudAt = 0;
function updateHud() {
  const now = performance.now();
  if (now - hudAt < 250) return;
  hudAt = now;
  el.hud.hidden = false;
  const mb = (cache.bytes / 1e6).toFixed(0);
  const budget = (CACHE_BYTES / 1e6).toFixed(0);
  const cid = deck ? (deck.clipId[deck.active] || '-') : '-';
  const over = state.overBudgetSince ? ((now - state.overBudgetSince) / 1000).toFixed(1) + 's' : '-';
  const under = state.underBudgetSince ? ((now - state.underBudgetSince) / 1000).toFixed(1) + 's' : '-';
  const text = boxed(`lens ${ZONE}${SCREEN ? '/' + SCREEN : ''}`, [
    `fps     ${state.fps.toFixed(1).padStart(5)}   frame ${state.emaMs.toFixed(1)}ms`,
    `passes  ${String(state.passes).padStart(5)}   lens  ${state.activePasses}/${state.stack.length}`,
    // Whole stack, with a bar at the governor's cut: an operator needs to see
    // which passes were shed, not just how many.
    `stack   ${state.stack.slice(0, state.activePasses).join('>') || '-'}` +
      (state.activePasses < state.stack.length
        ? ` ┊ ${state.stack.slice(state.activePasses).join('>')}` : ''),
    `clip    ${cid}  mix ${(deck ? deck.mix : 0).toFixed(2)}${deck && deck.fading ? ' ~fade' : ''}`,
    `feed    ${features ? features.state : '-'}${features && features.local ? ' (mic)' : ''}`,
    `bus     ${state.manifestState} r${playlist ? playlist.revision : '-'}  ` +
      `clips ${playlist ? playlist.entries.length : 0}${deck && deck.offline ? '  OFFLINE' : ''}`,
    `cache   ${mb}/${budget} MB`,
    `gov     over ${over}  under ${under}`,
  ]);

  // The title rule carries the one accent colour; the body stays dim. Built
  // from text nodes rather than innerHTML on purpose — `zone` and `screen`
  // come straight off the query string, and nothing user-supplied should ever
  // reach an HTML parser, however local the page feels.
  const nl = text.indexOf('\n');
  el.hud.textContent = '';
  const head = document.createElement('span');
  head.className = 'accent';
  head.textContent = text.slice(0, nl);
  el.hud.append(head, document.createTextNode(text.slice(nl)));
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function boot() {
  if (state.started) return;
  state.started = true;

  state.glOk = renderer.init();
  if (!state.glOk) enterFallback();
  resize();

  features = new Features({ zone: ZONE, local: AUDIO_LOCAL, phase: state.phase });
  features.start();

  requestAnimationFrame(frame);        // render from frame zero, data or not

  // A short head start so the first line usually types after /api/config has
  // landed; the log never waits on it, and never blocks the scrim either way.
  setTimeout(typeBoot, 380);

  await bootstrapData();

  // If the crossfade shader never arrived, WebGL cannot show video at all.
  if (state.glOk && !renderer.has('crossfade')) { state.glOk = false; enterFallback(); }

  connectManifestWs();
  if (HUD_ON) el.hud.hidden = false;
}

// Nothing below this line is allowed to take the screen down.
window.addEventListener('error', (e) => console.warn('[lens] window error:', e && e.message));
window.addEventListener('unhandledrejection', (e) =>
  console.warn('[lens] unhandled rejection:', e && e.reason && e.reason.message));

boot().catch((e) => {
  console.warn('[lens] boot failed:', e && e.message);
  enterFallback();
});
