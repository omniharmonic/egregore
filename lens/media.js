// media.js — manifest, weighted scheduler, bounded clip cache, and the
// two-<video> crossfade deck. Nothing in here is allowed to throw.

const CACHE_NAME = 'egregore-clips';
const LEDGER_KEY = 'egregore-clip-ledger';
const PIN_NEW = 5, PIN_OLD = 3;   // guaranteed floor: newest N + oldest M

/** Deterministic PRNG so a screen's sequence is a stable function of its phase. */
export function mulberry32(a) {
  a = (a >>> 0) || 1;
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const jsonHeaders = { 'accept': 'application/json' };

/** fetch that resolves to null instead of throwing; 401 raises the veil. */
export async function safeJson(url, onAuth) {
  try {
    const r = await fetch(url, { credentials: 'same-origin', headers: jsonHeaders });
    if (r.status === 401 || r.status === 403) { onAuth && onAuth(); return null; }
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

// ---------------------------------------------------------------------------
// Bounded Cache API store + localStorage ledger
// ---------------------------------------------------------------------------

export class ClipCache {
  constructor(budgetBytes) {
    this.budget = budgetBytes;
    this.ok = typeof caches !== 'undefined' && !!caches;
    this.ledger = this._read();
    this.inflight = new Set();
  }

  _read() {
    try {
      const raw = localStorage.getItem(LEDGER_KEY);
      const l = raw ? JSON.parse(raw) : null;
      if (l && l.items && typeof l.items === 'object') return l;
    } catch { /* corrupt or unavailable: start clean */ }
    return { v: 1, seq: 0, items: {} };
  }

  _write() {
    try { localStorage.setItem(LEDGER_KEY, JSON.stringify(this.ledger)); } catch { /* quota */ }
  }

  get bytes() {
    let t = 0;
    for (const k in this.ledger.items) t += this.ledger.items[k].size || 0;
    return t;
  }

  touch(id) {
    const it = this.ledger.items[id];
    if (it) { it.lastPlayed = Date.now(); this._write(); }
  }

  async open() {
    if (!this.ok) return null;
    try { return await caches.open(CACHE_NAME); } catch { this.ok = false; return null; }
  }

  /** Download-and-store, idempotent. Silent on every failure. */
  async warm(id, url) {
    if (!this.ok || this.inflight.has(id)) return;
    this.inflight.add(id);
    try {
      const c = await this.open();
      if (!c) return;
      if (this.ledger.items[id] && await c.match(url)) return;
      const r = await fetch(url, { credentials: 'same-origin' });
      if (!r.ok) return;
      const blob = await r.blob();
      if (!blob.size) return;
      await c.put(url, new Response(blob, {
        headers: { 'content-type': blob.type || 'video/mp4' },
      }));
      // Store the URL rather than reconstructing `/clips/<id>.mp4` at eviction
      // time: the ledger must be able to delete exactly what it put in, even
      // if the manifest ever hands out a different URL shape.
      this.ledger.items[id] = {
        url, size: blob.size, added: ++this.ledger.seq, lastPlayed: Date.now(),
      };
      this._write();
      await this.evict();
    } catch { /* offline, quota, opaque — all fine, we just do not cache */ }
    finally { this.inflight.delete(id); }
  }

  /** Object URL for a cached clip, or null. Caller revokes. */
  async objectUrl(url) {
    try {
      const c = await this.open();
      if (!c) return null;
      const m = await c.match(url);
      if (!m) return null;
      return URL.createObjectURL(await m.blob());
    } catch { return null; }
  }

  async evict() {
    if (this.bytes <= this.budget) return;
    const ids = Object.keys(this.ledger.items);
    if (ids.length <= PIN_NEW + PIN_OLD) return;
    const byAdded = ids.slice().sort((a, b) => this.ledger.items[a].added - this.ledger.items[b].added);
    const pinned = new Set([...byAdded.slice(0, PIN_OLD), ...byAdded.slice(-PIN_NEW)]);
    const victims = ids.filter((i) => !pinned.has(i))
      .sort((a, b) => (this.ledger.items[a].lastPlayed || 0) - (this.ledger.items[b].lastPlayed || 0));
    const c = await this.open();
    for (const id of victims) {
      if (this.bytes <= this.budget) break;
      const u = this.ledger.items[id].url || `/clips/${id}.mp4`;
      try { if (c) await c.delete(u); } catch { /* ignore */ }
      delete this.ledger.items[id];
    }
    this._write();
  }
}

// ---------------------------------------------------------------------------
// Manifest + weighted scheduler
// ---------------------------------------------------------------------------

export class Playlist {
  constructor(zone, phase, onAuth) {
    this.zone = zone;
    this.phase = phase;
    this.onAuth = onAuth;
    this.entries = [];
    this.revision = -1;
    this.crossfade = 2;
    // Below 1 the motion becomes languid and each clip holds the screen for
    // longer — the single strongest lever on whether a loop reads as pulsing
    // or as frantic, because it lengthens the shot and slows the movement in
    // it at the same time.
    this.playbackRate = 1;
    // Linger: least wall seconds a clip holds the screen. 0 = its own length.
    this.hold = 0;
    // A per-screen crossfade from /api/config is more specific than the
    // zone-wide one in the manifest, so it must not be clobbered on refresh.
    this.crossfadePinned = false;
    this.recent = [];
    this.rng = mulberry32(Math.round((phase % 1) * 1e6) ^ 0x5eed);
    this.cursor = 0;
    this.ok = false;
  }

  async refresh() {
    const m = await safeJson(`/api/manifest?zone=${encodeURIComponent(this.zone)}`, this.onAuth);
    if (!m || !Array.isArray(m.entries)) return false;
    const entries = m.entries.filter((e) => e && typeof e.url === 'string' && e.url).map((e) => ({
      clip_id: String(e.clip_id != null ? e.clip_id : e.url),
      url: e.url,
      duration_s: Number(e.duration_s) > 0 ? Number(e.duration_s) : 8,
      weight: Number(e.weight) > 0 ? Number(e.weight) : 1,
      movement_id: e.movement_id != null ? String(e.movement_id) : null,
      chain_index: Number.isFinite(Number(e.chain_index)) ? Number(e.chain_index) : 0,
    }));
    if (!entries.length) return false;
    if (!this.crossfadePinned && typeof m.crossfade_s === 'number' && m.crossfade_s > 0) {
      this.crossfade = m.crossfade_s;
    }
    if (this.revision < 0) {
      // Start each screen at a different point in the pool (VIS-5).
      this.cursor = Math.floor((this.phase % 1) * entries.length) % entries.length;
    }
    this.entries = entries;
    this.revision = typeof m.revision === 'number' ? m.revision : this.revision + 1;
    this.ok = true;
    return true;
  }

  /**
   * The clip that continues `entry` in its movement, if the pool has it.
   * Continuity seeds clip N+1 from clip N's last frame; playing them in
   * order is the whole point, and a random pick threw that away.
   */
  successor(entry) {
    if (!entry || !entry.movement_id) return null;
    const want = (entry.chain_index || 0) + 1;
    return this.entries.find((e) => e.movement_id === entry.movement_id && e.chain_index === want) || null;
  }

  /** Weighted pick, avoiding an immediate repeat once the pool is big enough. */
  pick() {
    const pool = this.entries;
    if (!pool.length) return null;
    if (pool.length === 1) return pool[0];

    if (this.cursor > 0) {                 // first pick honours the phase offset
      const e = pool[this.cursor % pool.length];
      this.cursor = 0;
      this._remember(e);
      return e;
    }
    const avoid = pool.length > 3 ? this.recent[this.recent.length - 1] : null;
    const usable = avoid ? pool.filter((e) => e.clip_id !== avoid) : pool;
    const list = usable.length ? usable : pool;

    let total = 0;
    for (const e of list) total += e.weight;
    let r = this.rng() * total;
    let chosen = list[list.length - 1];
    for (const e of list) { r -= e.weight; if (r <= 0) { chosen = e; break; } }
    this._remember(chosen);
    return chosen;
  }

  _remember(e) {
    this.recent.push(e.clip_id);
    if (this.recent.length > 4) this.recent.shift();
  }
}

// ---------------------------------------------------------------------------
// Two-video crossfade deck
// ---------------------------------------------------------------------------

export class Deck {
  /** @param {HTMLVideoElement[]} videos exactly two */
  constructor(videos, playlist, cache) {
    this.v = videos;
    this.pl = playlist;
    this.cache = cache;
    this.active = 0;       // slot currently on screen
    this.mix = 0;          // 0 = slot 0, 1 = slot 1
    this.fading = false;
    this.armed = false;    // idle slot holds a loaded next clip
    this.clipId = ['', ''];
    this.objUrl = [null, null];
    this._entry = [null, null];
    this._pending = false;
    this.offline = false;
    this.failures = 0;
    this._shownAt = 0;
    this._pendingAt = 0;
    this.watchdogTrips = 0;
    this._matchCut = false;

    for (let i = 0; i < 2; i++) {
      const el = this.v[i];
      el.muted = true; el.loop = true; el.playsInline = true;
      el.setAttribute('playsinline', ''); el.setAttribute('muted', '');
      el.preload = 'auto'; el.crossOrigin = 'anonymous';
      el.addEventListener('error', () => this._onError(i));
      el.addEventListener('stalled', () => this._kick(el));
      el.addEventListener('loadeddata', () => { if (i !== this.active) this.armed = true; });
    }
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) this.resync();
    });
  }

  get rate() {
    const r = this.pl.playbackRate || 1;
    return Math.max(0.25, Math.min(2, r));
  }

  get crossfade() {
    // Clamp against how long the clip actually occupies the screen, not its
    // media duration: slowed down, a clip can afford a longer dissolve, and
    // that is most of what makes a cut feel like a breath instead of a jump.
    const d = this._dur(this.active) / this.rate;
    return Math.max(0.2, Math.min(this.pl.crossfade || 2, d * 0.45));
  }

  /** Seconds the composition on screen has been there. */
  shownFor() {
    return this._shownAt ? (performance.now() - this._shownAt) / 1000 : 0;
  }

  /**
   * A wall must never sit on one clip. If the active clip has been up far
   * longer than linger + its own length + the dissolve, or a pending load
   * has not become playable in a reasonable time, stop trusting the
   * <video> and start again with a fresh pick. Observed: one browser
   * showed a single fill for sixteen minutes while another, on the same
   * manifest, cycled normally.
   */
  _watchdog(now) {
    const a = this.active, b = 1 - a;
    const allowed = Math.max(this.pl.hold || 0, this._dur(a) / this.rate) + this.crossfade + 20;
    const stuck = this._shownAt && (now - this._shownAt) / 1000 > allowed * 2;
    const stalled = this._pending && this._pendingAt && (now - this._pendingAt) / 1000 > 30;
    if (!stuck && !stalled) return false;
    this.watchdogTrips++;
    console.warn(`[lens] deck watchdog: ${stuck ? 'stuck on one clip' : 'next clip never became playable'} — repicking`);
    this.fading = false; this.mix = a === 0 ? 0 : 1; this._pending = false;
    const e = this.pl.pick();
    if (e) { this._pending = true; this._pendingAt = now; this._load(b, e); }
    this._shownAt = now;          // give the new attempt a full allowance
    return true;
  }

  /** Has the active clip held the screen for the configured linger yet? */
  _heldEnough() {
    const hold = this.pl.hold || 0;
    if (hold <= 0 || !this._shownAt) return true;
    return (performance.now() - this._shownAt) / 1000 >= hold;
  }

  /**
   * The next entry. A movement plays through in order — the continuation of
   * the clip on screen is the same composition, so linger does not apply —
   * then linger, then a weighted pick.
   */
  _next() {
    const cur = this._entry[this.active];
    const next = this.pl.successor(cur);
    if (next) { this._matchCut = true; return next; }
    this._matchCut = false;
    if (!this._heldEnough() && cur) return cur;
    return this.pl.pick();
  }

  _dur(i) {
    const el = this.v[i];
    if (Number.isFinite(el.duration) && el.duration > 0.2) return el.duration;
    const e = this._entry[i];
    return (e && e.duration_s) || 8;
  }

  async _load(slot, entry) {
    if (!entry) return false;
    this._entry[slot] = entry;
    this.clipId[slot] = entry.clip_id;
    const el = this.v[slot];

    let src = entry.url;
    if (this.offline) {
      const o = await this.cache.objectUrl(entry.url);
      if (o) src = o;
    }
    if (this.objUrl[slot]) { try { URL.revokeObjectURL(this.objUrl[slot]); } catch { /* */ } }
    this.objUrl[slot] = src.startsWith('blob:') ? src : null;

    try {
      el.src = src;
      el.load();
      const p = el.play();
      if (p && p.catch) p.catch(() => { /* autoplay policy; muted should be fine */ });
    } catch { return false; }
    // Warm the bounded cache in the background; never blocks playback.
    this.cache.warm(entry.clip_id, entry.url);
    return true;
  }

  /** Bring up the first clip. Safe to call repeatedly. */
  async begin() {
    const e = this.pl.pick();
    if (!e) return false;
    this.active = 0; this.mix = 0; this.fading = false; this.armed = false;
    this._shownAt = performance.now();
    return this._load(0, e);
  }

  async _onError(slot) {
    this.failures++;
    // First try the local cache for this exact clip; then move on to another.
    const entry = this._entry && this._entry[slot];
    if (entry && !this.offline) {
      this.offline = true;
      const o = await this.cache.objectUrl(entry.url);
      if (o) { this._load(slot, entry); return; }
    }
    const next = this.pl.pick();
    if (next) this._load(slot, next);
  }

  _kick(el) {
    // Re-apply on every kick: a browser resets playbackRate on some source
    // changes, and a deck running at 1.0 when it should be at 0.6 is exactly
    // the "too fast" complaint.
    try { el.playbackRate = this.rate; } catch { /* older engines */ }
    if (el.paused) { const p = el.play(); if (p && p.catch) p.catch(() => {}); }
  }

  resync() {
    for (const el of this.v) this._kick(el);
  }

  ready(slot) {
    const el = this.v[slot];
    return el.readyState >= 3 && el.videoWidth > 0;
  }

  size(slot) {
    const el = this.v[slot];
    return [el.videoWidth || 0, el.videoHeight || 0];
  }

  /** Advance the scheduler + the mix ramp. */
  tick(dt) {
    // Rate is applied here rather than only on load: an operator can change
    // it mid-clip, and a video already playing is never re-kicked, so
    // applying it at load alone meant the change was ignored until the next
    // crossfade — which for a slow party is minutes away.
    const r = this.rate;
    for (const el of this.v) {
      if (Math.abs((el.playbackRate || 1) - r) > 0.001) {
        try { el.playbackRate = r; } catch { /* older engines */ }
      }
    }

    const a = this.active, b = 1 - this.active;
    const cur = this.v[a];
    // A seeded successor starts on the frame this clip ends on: a short
    // dissolve right at the tail reads as one continuous shot, where the
    // usual long crossfade would overlap frames that do not match.
    const xf = this._matchCut ? Math.min(0.8, this.crossfade) : this.crossfade;

    if (this.fading) {
      const dir = b === 1 ? 1 : -1;
      this.mix = Math.max(0, Math.min(1, this.mix + dir * (dt / xf)));
      if ((dir > 0 && this.mix >= 1) || (dir < 0 && this.mix <= 0)) {
        this.fading = false;
        // A clip dissolving into itself is still the same composition on
        // screen: the linger clock keeps running rather than restarting.
        if (this.clipId[b] !== this.clipId[a]) this._shownAt = performance.now();
        this.active = b;
        this.armed = false;
        this._pending = false;
        this.cache.touch(this.clipId[b]);   // recency drives cache eviction
      }
      return;
    }

    if (!Number.isFinite(cur.duration) || cur.duration <= 0) return;
    const t = cur.currentTime;
    const dur = this._dur(a);
    // The trigger is in wall time, not media time: the crossfade ramps in
    // real seconds, so at half speed a media-time trigger would finish the
    // dissolve halfway through the clip and leave it looping behind a
    // finished fade.
    const remaining = (dur - t) / this.rate;
    const lead = Math.min(3.5, (dur / this.rate) * 0.35);

    if (this._watchdog(performance.now())) return;

    if (!this._pending && remaining <= xf + lead) {
      this._pending = true;
      this._pendingAt = performance.now();
      // While the clip still has to linger, the "next" clip is itself: the
      // field dissolves back into its own beginning through the same
      // two-slot crossfade, so there is never a hard loop cut.
      const e = this._next();
      if (e) this._load(b, e); else this._pending = false;
    }

    if (this._pending && remaining <= xf) {
      if (this.ready(b)) {
        this._kick(this.v[b]);
        this.fading = true;
      }
      // Not ready: `loop` keeps the current clip running, and we try again on
      // its next pass. There is never a black frame.
    }
  }
}
