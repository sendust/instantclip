"use strict";
/* 2-1. scene change detection under the timeline: on request, ±N s around the playhead.
 * The server returns a scene score for every editor (29.97p) frame; cuts = frames whose score is at least
 * the threshold, i.e. the first frame of a new shot. Clicking a cut parks the playhead on it:
 * I there = the new shot starts the subclip, O there = the subclip ends on the last frame of the old shot.
 * The threshold only filters what was fetched, so changing it redraws without re-analysing.
 * While the strip is open, keys 1-9 jump to the first nine cuts (numbered on the strip).
 */
const Scenes = {
  data: null,      // {path, from, to, frames: [[k, score]...]}
  busy: false,

  init(cfg) {
    const ed = cfg.editor || {};
    const get = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch (_) { return d; } };
    const set = (k, v) => { try { localStorage.setItem(k, v); } catch (_) {} };
    this.range = $("#sc-range");
    this.th = $("#sc-th");
    this.range.value = get("ic.scRange", ed.scene_range ?? 5);
    this.th.value = get("ic.scTh", ed.scene_threshold ?? 0.3);
    this.range.onchange = () => set("ic.scRange", this.range.value);
    this.th.oninput = () => { set("ic.scTh", this.th.value); this.key = null; this.render(); };
    // the run button disables itself and ✕ hides its own strip: a focused element that goes away drops
    // focus out of the editor (shortcuts off), so these never take focus and hand it to the player
    for (const [id, fn] of [["#sc-run", () => this.run()], ["#sc-close", () => this.clear()]]) {
      $(id).addEventListener("mousedown", (e) => e.preventDefault());
      $(id).onclick = () => { Player.box.focus({ preventScroll: true }); fn(); };
    }
    this.box = $("#scenes");
    this.cv = $("#sc-canvas");
    this.cv.addEventListener("mousedown", (e) => e.preventDefault());   // keep focus in the editor
    this.cv.addEventListener("click", (e) => this.click(e));
    this.cv.addEventListener("mousemove", (e) => this.hover(e));
    this.cv.addEventListener("mouseleave", () => { $("#sc-hover").hidden = true; this.hot = null; this.key = null; this.render(); });
    new ResizeObserver(() => { this.key = null; this.render(); }).observe(this.box);
  },

  threshold() { return Math.max(0.01, Math.min(1, parseFloat(this.th.value) || 0.3)); },
  cuts() { const t = this.threshold(); return this.data ? this.data.frames.filter(([, s]) => s >= t).map(([k]) => k) : []; },

  async run() {
    const c = Player.clip;
    if (!c || !Player.hasMedia()) return toast("클립을 먼저 불러오세요", true);
    if (c.subclip) return Player.notEditable();
    if (this.busy) return;
    const r = Math.max(1, Math.min(30, parseFloat(this.range.value) || 5));
    this.range.value = r;
    const t = Player.v.currentTime;
    this.busy = true;
    $("#sc-run").disabled = true;
    $("#sc-info").textContent = "검출 중…";
    try {
      const d = await api("POST", "/api/clip/scenes", { path: c.path, t, before: r, after: r });
      if (Player.clip !== c) return;   // another clip was opened meanwhile
      this.data = { path: c.path, ...d };
      this.box.hidden = false;
      this.key = null;
      this.render();
    } catch (e) {
      $("#sc-info").textContent = "";
      toast(e.message, true);
    } finally {
      this.busy = false;
      $("#sc-run").disabled = false;
    }
  },

  clear() {
    this.data = null;
    this.box.hidden = true;
    $("#sc-info").textContent = "";
  },

  /* canvas x <-> frame; frames are drawn as equal-width slots from..to */
  slot() { return this.cv.clientWidth / (this.data.to - this.data.from + 1); },
  frameAt(e) {
    const r = this.cv.getBoundingClientRect();
    const k = this.data.from + Math.floor((e.clientX - r.left) / this.slot());
    return Math.max(this.data.from, Math.min(this.data.to, k));
  },
  /* the cut under the pointer (within a few px), else the frame itself */
  pick(e) {
    const k = this.frameAt(e), px = 6 / this.slot();
    let best = null;
    for (const c of this.cuts()) if (Math.abs(c - k) <= px && (best == null || Math.abs(c - k) < Math.abs(best - k))) best = c;
    return { k: best ?? k, cut: best != null };
  },

  click(e) {
    if (!this.data) return;
    const { k } = this.pick(e);
    Player.v.pause();
    Player.seek((k + 0.5) * FRAME);
    Player.box.focus({ preventScroll: true });
  },

  /* key n (1-9): park on the n-th cut, like clicking it */
  jump(n) {
    const k = this.cuts()[n - 1];
    if (k == null) return toast(`컷 ${n}번이 없습니다 (컷 ${this.cuts().length}개)`, true);
    Player.v.pause();
    Player.seek((k + 0.5) * FRAME);
  },

  hover(e) {
    if (!this.data) return;
    const { k, cut } = this.pick(e);
    const h = $("#sc-hover"), r = this.cv.getBoundingClientRect();
    const score = (this.data.frames.find(([f]) => f === k) || [, 0])[1];
    h.hidden = false;
    h.style.left = `${(k - this.data.from + 0.5) * this.slot()}px`;
    h.textContent = `${tc(k * FRAME)}${cut ? `  ✂ 컷 (${score.toFixed(2)})` : `  ${score.toFixed(2)}`}`;
    if (this.hot !== (cut ? k : null)) { this.hot = cut ? k : null; this.key = null; this.render(); }
  },

  /* called from Player.render every animation frame: playhead always, the canvas only on change */
  render() {
    const d = this.data;
    if (!d) return;
    if (!Player.clip || Player.clip.path !== d.path) return this.clear();
    const slot = this.slot();
    const cur = frameOf(Player.v.currentTime || 0);
    const head = $("#sc-head");
    head.hidden = cur < d.from || cur > d.to;
    head.style.left = `${(cur - d.from + 0.5) * slot}px`;
    const cuts = this.cuts();
    $("#sc-info").textContent = `${tc(d.from * FRAME)} – ${tc(d.to * FRAME)} · 컷 ${cuts.length}개`;

    const mi = Player.markIn != null ? frameOf(Player.markIn) : null, mo = Player.markOut != null ? frameOf(Player.markOut) : null;
    const key = `${this.cv.clientWidth}|${this.threshold()}|${mi}|${mo}|${d.from}|${this.hot}`;
    if (key === this.key) return;
    this.key = key;
    const W = this.cv.clientWidth, H = this.cv.clientHeight, dpr = devicePixelRatio || 1;
    this.cv.width = W * dpr;
    this.cv.height = H * dpr;
    const g = this.cv.getContext("2d");
    g.scale(dpr, dpr);
    g.clearRect(0, 0, W, H);
    const css = getComputedStyle(document.documentElement);
    const col = (v, f) => css.getPropertyValue(v).trim() || f;
    const th = this.threshold(), base = H - 14;   // bottom 14px: second ticks and cut labels
    const y = (v) => base - Math.sqrt(Math.min(1, v)) * (base - 4);   // sqrt: small scores stay visible
    // one tick per second, labelled with the timecode's seconds
    g.fillStyle = "#4a5868";
    g.font = "9px monospace";
    g.textAlign = "left";
    for (let k = Math.ceil(d.from / PROXY_FPS) * PROXY_FPS; k <= d.to + 1; k += PROXY_FPS) {
      const f = Math.round(k), x = (f - d.from) * slot;
      g.fillRect(x, base, 1, 4);
      g.fillText(tc(f * FRAME).slice(3, 8), x + 2, H - 2);
    }
    // score of every frame, threshold line
    g.fillStyle = "#3a4a5c";
    for (const [k, s] of d.frames) g.fillRect((k - d.from) * slot, y(s), Math.max(1, slot - 1), Math.max(1, base - y(s)));
    g.strokeStyle = col("--warn", "#e8b931");
    g.setLineDash([4, 3]);
    g.beginPath();
    g.moveTo(0, Math.round(y(th)) + 0.5);
    g.lineTo(W, Math.round(y(th)) + 0.5);
    g.stroke();
    g.setLineDash([]);
    // mark in / out inside the window
    for (const [m, c] of [[mi, col("--in", "#3ddc84")], [mo, col("--out", "#ff8a3d")]]) {
      if (m == null || m < d.from || m > d.to) continue;
      g.fillStyle = c;
      g.fillRect((m - d.from) * slot, 0, 2, H);
    }
    // cuts: full-height line at the first frame of the new shot
    for (const k of cuts) {
      g.fillStyle = k === this.hot ? "#fff" : col("--accent", "#3d9df3");
      g.fillRect((k - d.from) * slot, 0, Math.max(2, slot - 1), base);
    }
    // key numbers of the first nine cuts, at the top right of each line
    g.font = "bold 11px monospace";
    g.textBaseline = "top";
    cuts.slice(0, 9).forEach((k, i) => {
      g.fillStyle = k === this.hot ? "#fff" : col("--accent", "#3d9df3");
      g.fillText(String(i + 1), (k - d.from) * slot + Math.max(2, slot - 1) + 2, 2);
    });
  },
};
