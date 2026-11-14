"use strict";
/* 2. 편집제어: proxy HLS player, timeline with thumbnails and marks, keyboard shortcuts.
 * Shortcuts listen on the whole 편집제어 panel (#p-edit), so they work while focus is anywhere inside it.
 * Keys are matched by e.code so they also work with the Korean IME active.
 */
// actions that edit (marks, submit); on a loaded subclip they only show a message
const EDIT_ACTS = ["mark-in", "mark-out", "clear-marks", "queue", "in-jump", "out-jump"];

const Player = {
  clip: null,          // /api/clip info, or {subclip:true,...} for subclip preview
  hls: null,
  hlsDur: 0,
  markIn: null,
  markOut: null,
  pollTimer: null,

  init(cfg) {
    // arrow-key seek amounts come from config.json "editor"
    this.seekLR = Number(cfg.editor?.seek_lr) || 10;
    this.seekUD = Number(cfg.editor?.seek_ud) || 60;
    const n = (x) => (x % 60 === 0 ? `${x / 60}m` : `${x}`);
    $('[data-act="lr-"]').textContent = `«${n(this.seekLR)}`;
    $('[data-act="lr+"]').textContent = `${n(this.seekLR)}»`;
    $('[data-act="ud-"]').textContent = `«${n(this.seekUD)}`;
    $('[data-act="ud+"]').textContent = `${n(this.seekUD)}»`;
    $("#help-lr").textContent = this.seekLR;
    $("#help-ud").textContent = this.seekUD;
    this.v = $("#ed-video");
    this.box = $("#player");
    this.meter = new StereoMeter($("#ed-meter"), true);
    Meters.views.add(this);

    // the panel is focusable (tabindex=-1): a click on any empty spot of it enables the shortcuts
    this.panel = $("#p-edit");
    this.panel.addEventListener("keydown", (e) => this.onKey(e));
    // shortcut on/off badge beside the 단축키 help line follows focus entering/leaving the panel
    const keys = $("#ed-keys");
    this.panel.addEventListener("focusin", () => keys.classList.add("active"));
    this.panel.addEventListener("focusout", (e) => {
      if (!this.panel.contains(e.relatedTarget)) keys.classList.remove("active");
    });
    this.box.addEventListener("click", () => this.box.focus());
    this.v.addEventListener("dblclick", () => this.toggle());
    this.v.addEventListener("play", () => this.syncPlayBtn());
    this.v.addEventListener("pause", () => this.syncPlayBtn());
    this.v.addEventListener("loadeddata", () => this.setAudio());

    // drag & drop from the media browser
    this.box.addEventListener("dragover", (e) => {
      if (e.dataTransfer.types.includes("application/x-instantclip")) { e.preventDefault(); this.box.classList.add("dragover"); }
    });
    this.box.addEventListener("dragleave", () => this.box.classList.remove("dragover"));
    this.box.addEventListener("drop", (e) => {
      e.preventDefault();
      this.box.classList.remove("dragover");
      const d = e.dataTransfer.getData("application/x-instantclip");
      if (!d) return;
      const { root, path } = JSON.parse(d);
      this.load(root, path);
      this.box.focus();
    });

    // editor buttons: act, then hand focus back to the player so shortcuts keep working
    $$("#p-edit [data-act]").forEach((b) => {
      b.addEventListener("mousedown", (e) => e.preventDefault());   // keep focus on the player while pressing
      b.addEventListener("click", () => { this.act(b.dataset.act); this.box.focus(); });
    });
    $("#ed-vol").value = localStorage.getItem("ic.vol") || "1";
    $("#ed-vol").oninput = () => { localStorage.setItem("ic.vol", $("#ed-vol").value); this.applyVol(); };
    $("#ed-mute").onclick = () => { this.muted = !this.muted; this.applyVol(); };
    $("#cnt-reset").onclick = () => { this.resetCounter(); this.box.focus(); };
    $("#ed-eject").onclick = () => this.eject();
    this.initTimeline();
    this.render();
  },

  /* ---------- loading ---------- */
  async load(root, path, polling = false) {
    clearTimeout(this.pollTimer);
    // opened from the browser (double-click / Enter / button): the shortcuts need the player focused,
    // else Space etc. go to the file list. Not on proxy polls, which would steal focus every 2 s.
    if (!polling) this.box.focus({ preventScroll: true });
    if (root === "subclip") return this.loadSubclip(path);
    let info;
    try { info = await api("GET", `/api/clip?path=${encodeURIComponent(path)}`); }
    catch (e) { return toast(e.message, true); }
    this.saveMarks();
    this.clip = info;
    this.restoreMarks();
    this.queued = false;
    $("#ed-title").textContent = info.path;
    this.loadCounter();
    if (!info.proxy) {
      this.detach();
      // a poll that finds no proxy and no running job means generation failed: don't loop
      if (polling && !info.proxy_building) return this.msg(`프록시 생성 실패: ${info.proxy_error || "알 수 없는 오류"}`);
      this.msg(info.proxy_building ? "프록시 생성 중…" : "프록시가 없어 생성합니다…");
      if (!info.proxy_building) {
        try { await api("POST", "/api/clip/proxy", { path: info.path }); }
        catch (e) { this.msg(e.message); return; }
      }
      this.pollTimer = setTimeout(() => this.load("capture", path, true), 2000);
      return;
    }
    this.attach(info.proxy);
  },

  /* unload the clip: back to the empty "drop a clip here" state (marks stay saved for that clip) */
  eject() {
    if (!this.clip) return;
    clearTimeout(this.pollTimer);
    this.saveMarks();
    this.detach();
    this.clip = null;
    this.markIn = this.markOut = null;
    this.queued = false;
    $("#ed-title").textContent = "";
    this.msg(null);
    this.loadCounter();
    this.render();
  },

  loadSubclip(path) {
    this.saveMarks();
    this.detach();
    this.clip = { subclip: true, path, name: path.split("/").pop() };
    this.markIn = this.markOut = null;
    $("#ed-title").textContent = path;
    this.loadCounter();
    this.v.src = `/api/files/download?root=subclip&path=${encodeURIComponent(path)}&inline=1`;
    this.msg(null);
    this.render();
  },

  attach(url) {
    this.detach();
    this.msg(null);
    $("#ed-drop").hidden = true;
    if (window.Hls && Hls.isSupported()) {
      const hls = new Hls({
        startPosition: 0,           // EVENT playlist of a growing clip: start at the top, not the live edge
        lowLatencyMode: false,
        liveDurationInfinity: false,
        maxBufferLength: 30,
        backBufferLength: 90,
      });
      this.hls = hls;
      hls.on(Hls.Events.LEVEL_LOADED, (_, d) => { this.hlsDur = d.details.totalduration; this.live = d.details.live; });
      hls.on(Hls.Events.LEVEL_UPDATED, (_, d) => { this.hlsDur = d.details.totalduration; this.live = d.details.live; });
      hls.on(Hls.Events.MANIFEST_PARSED, () => { this.v.pause(); });
      hls.on(Hls.Events.ERROR, (_, d) => {
        if (!d.fatal) return;
        if (d.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
        else if (d.type === Hls.ErrorTypes.NETWORK_ERROR) setTimeout(() => hls.startLoad(), 1000);
        else this.msg(`재생 오류: ${d.details}`);
      });
      hls.loadSource(url);
      hls.attachMedia(this.v);
    } else {
      this.v.src = url;   // Safari native HLS
    }
    this.v.pause();
    this.render();
  },

  detach() {
    if (this.hls) { this.hls.destroy(); this.hls = null; }
    this.v.removeAttribute("src");
    this.v.load();
    this.hlsDur = 0;
    this.live = false;
  },

  msg(text) {
    const el = $("#ed-msg");
    el.hidden = !text;
    el.textContent = text || "";
    if (text) $("#ed-drop").hidden = true;
  },

  /* playable length: grows while the clip records */
  duration() {
    const d = this.v.duration;
    if (this.hlsDur > 0) return Math.max(this.hlsDur, isFinite(d) ? d : 0);
    return isFinite(d) ? d : 0;
  },

  /* ---------- audio (Web Audio so the meter can read it) ---------- */
  setAudio() {
    if (this.audio) return;
    const ctx = AudioMgr.get();
    const src = ctx.createMediaElementSource(this.v);
    const out = ctx.createGain();
    src.connect(out).connect(ctx.destination);
    const a = buildAnalysers(ctx, src);
    this.meter.attach(a.analysers);
    this.audio = { src, out };
    this.applyVol();
  },
  applyVol() {
    const vol = this.muted ? 0 : parseFloat($("#ed-vol").value);
    if (this.audio) this.audio.out.gain.value = vol;
    $("#ed-mute").textContent = this.muted ? "MUTE" : "SND";
    $("#ed-mute").classList.toggle("off", !!this.muted);
  },

  /* ---------- transport ---------- */
  toggle() {
    if (!this.hasMedia()) return;
    AudioMgr.resume();
    if (this.v.paused) this.v.play().catch(() => {}); else this.v.pause();
  },
  hasMedia() { return !!this.clip && this.v.readyState > 0; },
  seek(t) {
    if (!this.hasMedia()) return;
    const d = this.duration();
    t = Math.max(0, Math.min(d > 0 ? d - FRAME / 2 : 0, t));
    this.v.currentTime = t;
    this.render();
  },
  seekBy(dt) { this.seek(this.v.currentTime + dt); },
  step(n) {
    if (!this.hasMedia()) return;
    this.v.pause();
    // park mid-frame so the decoded picture and the TC agree
    this.seek((frameOf(this.v.currentTime) + n + 0.5) * FRAME);
  },
  curFrameTime() { return frameOf(this.v.currentTime) * FRAME; },
  syncPlayBtn() { $("#btn-play").textContent = this.v.paused ? "▶" : "❚❚"; },

  editable() { return this.clip && !this.clip.subclip && this.hasMedia(); },
  notEditable() {
    toast("subclip 은 미리보기 전용입니다 — 편집(mark in/out, subclip 등록)은 녹화 원본에서만 할 수 있습니다", true);
  },

  act(a) {
    // a subclip is preview-only: say so instead of silently ignoring the edit keys/buttons
    if (this.clip?.subclip && EDIT_ACTS.includes(a)) return this.notEditable();
    switch (a) {
      case "play": return this.toggle();
      case "lr-": return this.seekBy(-this.seekLR);
      case "lr+": return this.seekBy(this.seekLR);
      case "ud-": return this.seekBy(-this.seekUD);
      case "ud+": return this.seekBy(this.seekUD);
      case "back05": return this.seekBy(-0.5);
      case "fwd05": return this.seekBy(0.5);
      case "frame-": return this.step(-1);
      case "frame+": return this.step(1);
      // jumps always land paused, parked on the marked frame
      case "in-jump": if (this.markIn != null) { this.v.pause(); this.seek(this.markIn + FRAME / 2); } return;
      case "out-jump": if (this.markOut != null) { this.v.pause(); this.seek(this.markOut + FRAME / 2); } return;
      case "mark-in":
        this.queued = false;
        if (!this.editable()) return;
        this.markIn = this.curFrameTime();
        if (this.markOut != null && this.markOut < this.markIn) this.markOut = null;
        this.saveMarks(); return this.render();
      case "mark-out":
        this.queued = false;
        if (!this.editable()) return;
        this.markOut = this.curFrameTime();   // start of the out frame; the frame itself is included
        if (this.markIn != null && this.markIn > this.markOut) this.markIn = null;
        this.saveMarks(); return this.render();
      case "clear-marks":
        this.queued = false;
        this.markIn = this.markOut = null; this.saveMarks(); return this.render();
      case "queue": return this.queue();
    }
  },

  onKey(e) {
    if (e.altKey || e.metaKey) return;
    const ctrl = e.ctrlKey;
    const map = {
      KeyI: ctrl ? "in-jump" : "mark-in",
      KeyO: ctrl ? "out-jump" : "mark-out",
      Enter: "queue", NumpadEnter: "queue",
      Space: "play", KeyK: "play",
      ArrowLeft: "lr-", ArrowRight: "lr+",
      ArrowDown: "ud-", ArrowUp: "ud+",
      KeyJ: "back05", KeyL: "fwd05",
      Comma: "frame-", Period: "frame+",
    };
    const a = map[e.code];
    if (!a || (ctrl && !["KeyI", "KeyO"].includes(e.code))) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.repeat && ["queue", "play", "mark-in", "mark-out"].includes(a)) return;
    this.act(a);
  },

  async queue() {
    if (!this.editable()) return toast("녹화 클립을 먼저 불러오세요", true);
    if (this.markIn == null || this.markOut == null) return toast("mark in / mark out 을 모두 지정하세요", true);
    if (this.markOut < this.markIn) return toast("mark out 이 mark in 보다 앞에 있습니다", true);
    // one submit per marking: a repeated Enter (or one pressed while the request is in flight)
    // is ignored until mark in/out is set again or cleared
    if (this.queueBusy) return;
    if (this.queued) return toast("이미 큐에 등록한 구간입니다 — mark in/out 을 다시 지정하면 등록할 수 있습니다", true);
    this.queueBusy = true;
    this.render();
    try {
      // tout = end of the out frame, so the out frame is part of the subclip
      const j = await api("POST", "/api/jobs", { source: this.clip.path, tin: this.markIn, tout: this.outEnd(), folder: Browser.folder("subclip") });
      this.queued = true;
      toast(`인코더 큐 등록: ${j.name}`);
    } catch (e) { toast(e.message, true); }
    this.queueBusy = false;
    this.render();
    this.loadCounter();
  },
  outEnd() { return this.markOut + FRAME; },

  /* next subclip number of the loaded source clip */
  /* another user (or tab) took or reset a number: refresh the "next" box */
  onCounterRev(rev) {
    if (rev !== this.counterRev && this.counterRev !== undefined) this.loadCounter();
    this.counterRev = rev;
  },
  async loadCounter() {
    const el = $("#cnt-next");
    if (!this.clip || this.clip.subclip) { el.textContent = "[---]"; return; }
    try {
      const r = await api("GET", `/api/counter?source=${encodeURIComponent(this.clip.path)}&folder=${encodeURIComponent(Browser.folder("subclip"))}`);
      el.textContent = `[${pad(r.next, 3)}]`;
    } catch (_) { el.textContent = "[---]"; }
  },
  async resetCounter() {
    if (this.clip?.subclip) return this.notEditable();
    if (!this.clip) return toast("녹화 클립을 먼저 불러오세요", true);
    const stem = this.clip.path.split("/").pop().replace(/\.[^.]+$/, "");
    if (!confirm(`${stem} 의 subclip 번호를 [001] 부터 다시 시작할까요?`)) return;
    try { await api("POST", "/api/counter/reset", { path: this.clip.path }); toast("subclip 번호 리셋"); }
    catch (e) { toast(e.message, true); }
    this.loadCounter();
  },

  /* marks are remembered per clip in this browser */
  saveMarks() {
    if (!this.clip || this.clip.subclip) return;
    try { localStorage.setItem("ic.marks." + this.clip.path, JSON.stringify([this.markIn, this.markOut])); } catch (_) {}
  },
  restoreMarks() {
    this.markIn = this.markOut = null;
    try { [this.markIn, this.markOut] = JSON.parse(localStorage.getItem("ic.marks." + this.clip.path)) || [null, null]; } catch (_) {}
  },

  /* ---------- timeline ---------- */
  initTimeline() {
    const tl = $("#timeline");
    const tAt = (e) => {
      const r = tl.getBoundingClientRect();
      return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * this.duration();
    };
    // pressing the timeline must not take focus from the player (the shortcut badge would flicker)
    tl.addEventListener("mousedown", (e) => e.preventDefault());
    tl.addEventListener("pointerdown", (e) => {
      if (!this.hasMedia()) return;
      tl.setPointerCapture(e.pointerId);
      this.dragging = true;
      this.seek(tAt(e));
    });
    tl.addEventListener("pointermove", (e) => {
      if (this.dragging) this.seek(tAt(e));
      this.hover(e, tAt(e));
    });
    tl.addEventListener("pointerup", () => { this.dragging = false; this.box.focus(); });
    tl.addEventListener("pointerleave", () => { $("#tl-hover").hidden = true; });
    $("#tl-thumb").onerror = () => $("#tl-thumb").classList.add("missing");
    $("#tl-thumb").onload = () => $("#tl-thumb").classList.remove("missing");
  },

  hover(e, t) {
    const d = this.duration();
    const hv = $("#tl-hover");
    if (!this.clip || d <= 0) { hv.hidden = true; return; }
    const r = $("#timeline").getBoundingClientRect();
    const x = Math.max(85, Math.min(r.width - 85, e.clientX - r.left));
    hv.style.left = x + "px";
    hv.hidden = false;
    $("#tl-hover-time").textContent = tc(t);
    const img = $("#tl-thumb");
    if (this.clip.thumb) {
      const n = Math.floor(t / this.clip.thumb.interval) + 1;
      const src = `${this.clip.thumb.base}${pad(n, 6)}.jpg`;
      if (img.dataset.src !== src) { img.dataset.src = src; img.src = src; }
      img.style.display = "";
    } else {
      img.style.display = "none";
    }
  },

  /* ---------- per-frame UI ---------- */
  update() { this.render(); },

  render() {
    const d = this.duration();
    const cur = this.v.currentTime || 0;
    const pct = (t) => (d > 0 ? Math.max(0, Math.min(100, (t / d) * 100)) : 0) + "%";
    $("#tl-head").style.left = pct(cur);
    $("#tl-played").style.width = pct(cur);
    let bufEnd = 0;
    for (let i = 0; i < this.v.buffered.length; i++) if (this.v.buffered.start(i) <= cur + 0.5) bufEnd = this.v.buffered.end(i);
    $("#tl-buffer").style.width = pct(bufEnd);
    const mi = $("#tl-in"), mo = $("#tl-out"), rg = $("#tl-range");
    mi.style.display = this.markIn != null && d > 0 ? "block" : "none";
    mo.style.display = this.markOut != null && d > 0 ? "block" : "none";
    if (this.markIn != null) mi.style.left = pct(this.markIn);
    if (this.markOut != null) mo.style.left = pct(this.outEnd());
    const a = this.markIn ?? 0, b = this.markOut != null ? this.outEnd() : d;
    rg.style.display = (this.markIn != null || this.markOut != null) && d > 0 ? "block" : "none";
    rg.style.left = pct(a);
    rg.style.width = `calc(${pct(b)} - ${pct(a)})`;

    $("#tc-pos").textContent = tc(cur);
    $("#tc-total").textContent = tc(d);
    $("#tc-in").textContent = tc(this.markIn);
    $("#tc-out").textContent = tc(this.markOut);
    $("#sec-in").textContent = this.markIn != null ? this.markIn.toFixed(3) + " s" : "";
    $("#sec-out").textContent = this.markOut != null ? this.markOut.toFixed(3) + " s" : "";
    const dur = this.markIn != null && this.markOut != null ? this.outEnd() - this.markIn : null;
    $("#tc-dur").textContent = tc(dur);
    $("#sec-dur").textContent = dur != null ? dur.toFixed(3) + " s" : "";
    $("#btn-queue").disabled = !(this.editable() && dur > 0) || this.queued || this.queueBusy;
    $("#btn-queue").title = this.queued ? "이미 등록한 구간 — mark in/out 을 다시 지정하세요" : "Enter";
    $("#ed-rec").hidden = !(this.clip && !this.clip.subclip && this.live);   // open EVENT playlist = still recording
    $("#ed-drop").hidden = !!this.clip;
    $("#ed-eject").disabled = !this.clip;
    const mode = $("#ed-mode");
    mode.hidden = !this.clip;
    if (this.clip) {
      const sub = !!this.clip.subclip;
      mode.className = "ed-mode " + (sub ? "preview" : "edit");
      mode.textContent = sub ? "▶ 프리뷰 전용 — 편집 불가 (SUBCLIP)" : "✎ 편집 가능 — 녹화 원본";
    }
  },
};
