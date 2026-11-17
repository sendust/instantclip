"use strict";
/* Stand-in for the editor's <video> element when a TS clip is decoded in the browser (tsdecode.js).
 * It offers the part of HTMLMediaElement the editor uses: currentTime, duration, paused, readyState,
 * buffered, play(), pause() and the events play / pause / seeked / timeupdate / loadedmetadata /
 * loadeddata / error, so Player and Scenes work on it unchanged.
 *
 * Time is editor time: frame k (display frame k of the TS, counted from its first I-frame) shows over
 * [k, k + 1) / 29.97 s. Pictures are drawn on a canvas (WebGL); an interlaced frame shows its first field,
 * like the encoder assumes. Audio of each decoded GOP is scheduled on the editor's AudioContext, which is
 * also the playback clock; output() gives the node to connect to the meters and volume.
 */
const TS_AHEAD = 4;               // GOPs decoded ahead while playing
const TS_CACHE = 640 << 20;       // decoded bytes kept

class TsMedia extends EventTarget {
  constructor(canvas, { lowres = 1, workers = 4 } = {}) {
    super();
    this.canvas = canvas;
    this.ren = new YuvRenderer(canvas);
    this.dec = new TsDecoder(workers);
    this.lowres = lowres;
    this.out = null;
    this.reset();
    this.tick = this.tick.bind(this);
    requestAnimationFrame(this.tick);
  }

  reset() {
    this.info = null;
    this.readyState = 0;
    this.duration = NaN;
    this.paused = true;
    this.stalled = false;
    this.t = 0;                   // position while paused / stalled
    this.cache = new Map();       // GOP first frame -> {status, g, bytes, used, abuf}
    this.keys = new Map();        // GOP first frame -> I-frame preview
    this.shown = null;            // {frame, exact}
    this.sources = new Map();     // GOP first frame -> {src: AudioBufferSourceNode, end: context time}
    this.seq = (this.seq || 0) + 1;
    this.wanting = false;          // want() running
    this.again = false;            // the position changed while it ran: look up again
    this.seekWanted = false;
    this.lastUpdate = 0;
  }

  /* ---------- loading ---------- */
  async open(info) {
    this.close();
    const seq = this.seq;
    this.info = info;
    this.dec.dropQueued();
    try {
      await this.dec.open(info.path, info);
    } catch (e) {
      if (seq === this.seq) this.fail(e);
      return;
    }
    if (seq !== this.seq) return;
    this.field = info.progressive ? -1 : info.field ?? 0;
    this.duration = info.frames / PROXY_FPS;
    this.readyState = 1;
    this.emit("loadedmetadata");
    this.want();
  }

  close() {
    this.stopAudio();
    this.dec.dropQueued();
    this.reset();
  }

  fail(e) {
    this.error = e;
    this.emit("error");
    console.warn("ts:", e);
  }

  /* ---------- HTMLMediaElement subset ---------- */
  get currentTime() {
    if (this.paused || this.stalled) return this.t;
    return Math.min(this.duration, this.anchorT + (this.clock() - this.anchorC));
  }

  set currentTime(t) {
    if (!this.info) return;
    this.t = Math.max(0, Math.min(this.duration, t));
    if (!this.paused) { this.stopAudio(); this.anchor(this.t); }
    this.seeking = true;
    this.dec.dropQueued();
    this.want(true);
  }

  play() {
    if (!this.info || !this.readyState) return Promise.reject(new Error("not loaded"));
    if (!this.paused) return Promise.resolve();
    if (this.t >= this.duration - 1 / PROXY_FPS) this.t = 0;
    this.paused = false;
    this.stalled = true;          // starts when the GOPs are ready (tick)
    this.emit("play");
    this.want();
    return Promise.resolve();
  }

  pause() {
    if (this.paused) return;
    this.t = this.currentTime;
    this.paused = true;
    this.stalled = false;
    this.stopAudio();
    this.emit("pause");
    this.want();
  }

  get buffered() {
    const r = [];
    for (const [f, e] of [...this.cache].sort((a, b) => a[0] - b[0])) {
      if (e.status !== "ready") continue;
      const a = f / PROXY_FPS, b = (f + e.g.count) / PROXY_FPS;
      if (r.length && Math.abs(r[r.length - 1][1] - a) < 1e-6) r[r.length - 1][1] = b;
      else r.push([a, b]);
    }
    return { length: r.length, start: (i) => r[i][0], end: (i) => r[i][1] };
  }

  emit(type) { this.dispatchEvent(new Event(type)); }

  /* ---------- clock and audio ---------- */
  output(ctx) {
    this.ctx = ctx;
    this.out ??= ctx.createGain();
    return this.out;
  }

  clock() {
    return this.ctx && this.ctx.state === "running" ? this.ctx.currentTime : performance.now() / 1000;
  }

  anchor(t) {
    this.anchorT = t;
    this.anchorC = this.clock();
  }

  stopAudio() {
    for (const s of this.sources.values()) { try { s.src.stop(); } catch (_) {} }
    this.sources.clear();
  }

  scheduleAudio() {
    if (!this.out || this.ctx.state !== "running") return;
    const now = this.ctx.currentTime;
    for (const [f, s] of this.sources) if (s.end < now) this.sources.delete(f);   // played out
    const g0 = this.gopAt(this.frameAt(this.currentTime));
    let f = g0?.frame;
    for (let i = 0; f != null && i < 3; i++) {
      const e = this.cache.get(f);
      if (!e || e.status !== "ready") break;
      if (!this.sources.has(f) && e.g.audio?.length && e.g.audio[0].length) {
        e.abuf ??= this.audioBuffer(e.g.audio);
        const when = this.anchorC + (f / PROXY_FPS - this.anchorT);
        const end = when + e.abuf.duration;
        if (end > now) {
          const src = this.ctx.createBufferSource();
          src.buffer = e.abuf;
          src.connect(this.out);
          if (when < now) src.start(now, now - when); else src.start(when);
          this.sources.set(f, { src, end });
        }
      }
      f += e.g.count;
    }
  }

  audioBuffer([l, r]) {
    const b = this.ctx.createBuffer(2, l.length, 48000);
    b.copyToChannel(l, 0);
    b.copyToChannel(r, 1);
    return b;
  }

  /* ---------- frames and GOPs ---------- */
  frameAt(t) { return Math.max(0, Math.min(this.info.frames - 1, frameOf(t))); }

  gopAt(n) {
    const i = this.dec.gopIndex(n);
    return i < 0 ? null : this.dec.gops[i];
  }

  ready(n) {
    const g = this.gopAt(n);
    const e = g && this.cache.get(g.frame);
    return !!(e && (e.status === "ready" || e.status === "error"));
  }

  /* decode the GOPs around the playhead: the current one first, then ahead (playing) or both sides */
  /* One lookup at a time, always for the latest position: while scrubbing, the positions passed over
   * meanwhile are skipped instead of queueing a server lookup each. */
  async want(seek = false) {
    if (!this.info) return;
    this.seekWanted ||= seek;
    this.again = true;
    if (this.wanting) return;
    this.wanting = true;
    const seq = this.seq;
    while (this.again && seq === this.seq) {
      this.again = false;
      const s = this.seekWanted, pos = this.frameAt(this.currentTime);
      this.seekWanted = false;
      try { await this.fill(pos, s, () => this.again || seq !== this.seq); }
      catch (e) { if (seq === this.seq) this.fail(e); }
    }
    if (seq === this.seq) this.wanting = false;
    this.evict();
  }

  async fill(pos, seek, stale) {
    const g = await this.dec.gop(pos);
    if (stale()) return;
    if (seek && !this.cache.has(g.frame)) this.preview(g);
    this.decode(g);
    const ahead = this.paused ? 1 : TS_AHEAD;
    let n = g.frame + g.frames;
    for (let i = 0; i < ahead && n < this.info.frames; i++) {
      const h = await this.dec.gop(n);
      if (stale()) return;
      this.decode(h);
      n = h.frame + h.frames;
    }
    if (this.paused && g.frame > 0) {
      const h = await this.dec.gop(g.frame - 1);   // one behind, for stepping back
      if (!stale()) this.decode(h);
    }
  }

  decode(g) {
    const old = this.cache.get(g.frame);   // a failed GOP is tried again, at most every 2 s
    if (old && !(old.status === "error" && performance.now() - old.used > 2000)) return;
    const seq = this.seq, e = { status: "pending", g: null, bytes: 0, used: performance.now() };
    this.cache.set(g.frame, e);
    this.dec.decode(g.frame, { lowres: this.lowres }).then((r) => {
      if (seq !== this.seq || this.cache.get(g.frame) !== e) return;
      Object.assign(e, { status: "ready", g: r, bytes: r.yuv.byteLength + r.audio.reduce((a, x) => a + x.byteLength, 0) });
      if (this.readyState < 2) { this.readyState = 2; this.emit("loadeddata"); }
      this.shown = null;
    }).catch((err) => {
      if (seq !== this.seq || this.cache.get(g.frame) !== e) return;
      if (err.message === "cancelled") { this.cache.delete(g.frame); return; }
      e.status = "error";
      console.warn("ts decode", g.frame, err.message);
    });
  }

  /* quick I-frame of a GOP not decoded yet, shown while seeking/scrubbing */
  preview(g) {
    if (this.keys.has(g.frame)) return;
    const seq = this.seq;
    this.keys.set(g.frame, null);
    this.dec.decode(g.frame, { lowres: Math.max(2, this.lowres), keyOnly: true, urgent: true }).then((r) => {
      if (seq !== this.seq) return;
      this.keys.set(g.frame, r);
      if (this.keys.size > 100) this.keys.delete(this.keys.keys().next().value);
      this.shown = null;
    }).catch(() => { if (seq === this.seq) this.keys.delete(g.frame); });
  }

  evict() {
    let total = 0;
    for (const e of this.cache.values()) total += e.bytes;
    if (total <= TS_CACHE) return;
    const cur = this.frameAt(this.currentTime);
    const far = [...this.cache].filter(([, e]) => e.status !== "pending")
      .sort((a, b) => Math.abs(b[0] - cur) - Math.abs(a[0] - cur));
    for (const [f, e] of far) {
      if (total <= TS_CACHE * 0.8) break;
      total -= e.bytes;
      this.cache.delete(f);
    }
  }

  /* ---------- per animation frame: clock, stalls, drawing ---------- */
  tick() {
    requestAnimationFrame(this.tick);
    if (!this.info || !this.readyState) return;
    if (!this.paused) {
      if (this.stalled) {
        const n = this.frameAt(this.t), g = this.gopAt(n);
        const next = g && g.frame + g.frames;
        if (this.ready(n) && (!next || next >= this.info.frames || this.ready(next))) {
          this.stalled = false;
          this.anchor(this.t);
        }
      } else {
        const t = this.currentTime;
        if (t >= this.duration - 0.5 / PROXY_FPS) {
          this.t = this.duration - 0.5 / PROXY_FPS;
          this.pause();
          this.emit("ended");
        } else if (!this.ready(this.frameAt(t))) {
          this.t = t;
          this.stalled = true;
          this.stopAudio();
        } else {
          const g = this.gopAt(this.frameAt(t));
          if (!this.wanting && g && this.aheadMissing(g)) this.want();
          this.scheduleAudio();
        }
      }
      const now = performance.now();
      if (now - this.lastUpdate > 250) { this.lastUpdate = now; this.emit("timeupdate"); }
    }
    this.draw();
  }

  /* a GOP of the playing window is not decoded or on its way */
  aheadMissing(g) {
    let n = g.frame;
    for (let i = 0; i <= TS_AHEAD && n < this.info.frames; i++) {
      const h = this.gopAt(n);
      if (!h || !this.cache.has(h.frame)) return true;
      n = h.frame + h.frames;
    }
    return false;
  }

  draw() {
    const n = this.frameAt(this.currentTime);
    if (this.shown && this.shown.frame === n && this.shown.exact) return;
    const g = this.gopAt(n);
    if (!g) return;
    const e = this.cache.get(g.frame);
    let f = null, exact = false;
    if (e?.status === "ready" && e.g.count) {
      f = { ...e.g, offset: Math.min(e.g.count - 1, n - g.frame) * e.g.frameSize };
      exact = true;
      e.used = performance.now();
    } else if (this.keys.get(g.frame)) {
      f = { ...this.keys.get(g.frame), offset: 0 };
    }
    if (!f) return;
    if (this.shown && this.shown.frame === n && this.shown.exact === exact) return;
    this.ren.field = this.field;
    this.ren.draw({ ...f, srcH: this.info.height, progressive: this.field < 0 });
    this.shown = { frame: n, exact };
    if (exact && this.seeking) { this.seeking = false; this.emit("seeked"); this.emit("timeupdate"); }
  }
}
