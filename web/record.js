"use strict";
/* 1. 녹화제어: device select, REC/STOP, live WebRTC monitor (mediamtx WHEP) + level meter */
const Record = {
  device: localStorage.getItem("ic.device") || "",
  channels: [],
  reader: null,
  readerKey: null,      // "path|readyTime" of the live stream being shown
  audio: null,          // {listenGain, nodes, sink}
  muted: true,          // always start muted
  collapsed: localStorage.getItem("ic.liveCollapsed") === "1",
  whepBase: "",

  init(cfg) {
    const m = cfg.mediamtx;
    this.whepBase = (m.webrtc_url || `${location.protocol}//${location.hostname}:${m.webrtc_port}`).replace(/\/$/, "");
    this.meter = new StereoMeter($("#live-meter"));
    $("#rec-device").onchange = (e) => { this.device = e.target.value; localStorage.setItem("ic.device", this.device); this.render(); };
    $("#rec-refresh").onclick = async () => { this.setDevices(await api("GET", "/api/devices?refresh=1")); toast("장치 목록 갱신"); };
    $("#rec-start").onclick = () => this.start();
    $("#rec-stop").onclick = () => this.stop();
    $("#rec-extend").onclick = () => this.extend();
    this.extendMin = Math.round((cfg.capture.extend_sec || 600) / 60);
    $("#rec-extend").textContent = `+${this.extendMin}분`;
    $("#live-input").onchange = (e) => this.preview(e.target.checked);
    $("#live-mute").onclick = () => this.setMuted(!this.muted);
    $("#live-collapse").onclick = () => {
      this.collapsed = !this.collapsed;
      localStorage.setItem("ic.liveCollapsed", this.collapsed ? "1" : "0");
      this.render();
    };
    $("#rec-prefix").value = localStorage.getItem("ic.prefix") || "";
    $("#rec-prefix").onchange = (e) => localStorage.setItem("ic.prefix", e.target.value);
    // recording frame rate: remembered per browser, config.json capture.fps until chosen
    $("#rec-fps").value = localStorage.getItem("ic.recFps") || cfg.capture.fps || "60000/1001";
    if (!$("#rec-fps").value) $("#rec-fps").value = "60000/1001";
    $("#rec-fps").onchange = (e) => localStorage.setItem("ic.recFps", e.target.value);
    this.setMuted(true);
  },

  setDevices(list) {
    this.channels = list;
    const sel = $("#rec-device");
    const names = list.map((c) => c.device);
    if (sel.options.length !== names.length || names.some((n, i) => sel.options[i].value !== n)) {
      sel.innerHTML = names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    }
    if (!names.includes(this.device)) this.device = (list.find((c) => !c.busy) || list[0] || {}).device || "";
    sel.value = this.device;
    // mark channels in use: ours (REC / LIVE) and ones another process holds (greyed, not selectable)
    list.forEach((c, i) => {
      const tag = c.mode === "recording" ? " ● REC" : c.mode === "preview" ? " ◉ LIVE" : c.busy ? " — 사용 중" : "";
      const o = sel.options[i];
      if (o.textContent !== c.device + tag) o.textContent = c.device + tag;
      o.disabled = !!c.busy && c.mode === "idle";
      o.title = c.busy ? `다른 프로세스가 사용 중: ${c.busy}` : "";
    });
    this.render();
  },

  cur() { return this.channels.find((c) => c.device === this.device) || { mode: "idle" }; },

  render() {
    const c = this.cur();
    const rec = c.mode === "recording";
    const busy = c.mode === "idle" && c.busy;   // the selected device was taken by another process
    $("#rec-start").disabled = rec || !this.device || !!busy;
    $("#rec-busy").hidden = !busy;
    $("#rec-busy").textContent = busy ? `다른 프로세스가 사용 중인 장치입니다 — ${c.busy}` : "";
    $("#rec-start").classList.toggle("on", rec);
    $("#rec-stop").disabled = !rec;
    const st = $("#rec-state");
    st.textContent = rec ? "RECORDING" : c.mode === "preview" ? "LIVE INPUT" : c.error ? "ERROR" : "IDLE";
    st.className = "badge " + (rec ? "recording" : c.mode === "preview" ? "preview" : c.error ? "error" : "");
    $("#rec-time").textContent = hms(rec ? c.elapsed : 0);
    $("#rec-remain").textContent = rec ? hms(Math.ceil(c.remaining)) : "--:--:--";
    $("#remain-box").classList.toggle("warn", rec && c.remaining < 600);
    $("#remain-box").classList.toggle("crit", rec && c.remaining < 180);
    $("#rec-extend").disabled = !rec;
    const extra = rec && c.drop && c.drop !== "0" ? `  · drop ${c.drop}` : "";
    const fps = { "60000/1001": "  · 59.94p", "30000/1001": "  · 29.97p" }[c.fps] || "";
    $("#rec-clip").textContent = rec ? `${c.clip}${fps}${extra}` : "";
    $("#rec-fps").disabled = rec;
    $("#left-rec").hidden = !this.channels.some((ch) => ch.mode === "recording");   // shown on the folded strip
    $("#rec-error").hidden = !c.error;
    $("#rec-error").textContent = c.error || "";
    $("#live-input").checked = !!c.preview;
    $("#live-input").disabled = !this.device || (!!busy && !c.preview);   // no DeckLink on this host: nothing to monitor
    $("#live-nosig").hidden = !(c.no_signal && c.mode !== "idle");
    $("#live-body").hidden = this.collapsed;
    $("#live-collapse").textContent = (this.collapsed ? "▸" : "▾") + " Live 모니터";
    // connection key = path + publisher readyTime: preview -> REC swaps the ffmpeg on the same path,
    // and only a fresh WHEP session after the new publisher is up gets moving video again
    const show = !this.collapsed && c.mode !== "idle";
    const ready = c.live_ready === undefined ? "-" : c.live_ready;   // older server: no readiness info
    const want = show && ready ? `${c.live_path}|${ready}` : null;
    if (want !== this.readerKey) this.connect(want);
    if (!want) {
      $("#live-msg").hidden = false;
      $("#live-msg").textContent = this.collapsed ? "" : show ? "라이브 스트림 대기 중…"
        : "입력 모니터 꺼짐 — 녹화를 시작하거나 '입력 모니터'를 켜세요";
    }
  },

  connect(key) {
    if (this.reader) { this.reader.close(); this.reader = null; }
    this.disconnectAudio();
    $("#live-video").srcObject = null;
    this.readerKey = key;
    if (!key) return;
    const path = key.split("|")[0];
    $("#live-msg").hidden = false;
    $("#live-msg").textContent = `connecting ${path}…`;
    this.reader = new MediaMTXWebRTCReader({
      url: `${this.whepBase}/${encodeURIComponent(path)}/whep`,
      onError: (err) => {
        this.disconnectAudio();
        $("#live-msg").hidden = false;
        $("#live-msg").textContent = String(err);
      },
      onTrack: (evt) => {
        if (evt.track.kind === "video") {
          $("#live-video").srcObject = evt.streams[0] || new MediaStream([evt.track]);
          $("#live-msg").hidden = true;
        } else if (evt.track.kind === "audio") {
          this.connectAudio(evt.track);
        }
      },
    });
  },

  connectAudio(track) {
    this.disconnectAudio();
    const ctx = AudioMgr.get();
    const stream = new MediaStream([track]);
    // Chrome: a remote WebRTC stream reaches Web Audio as silence unless a media element plays it
    const sink = Object.assign(document.createElement("audio"), { muted: true, autoplay: true });
    sink.srcObject = stream;
    sink.play().catch(() => {});
    const src = ctx.createMediaStreamSource(stream);
    const silent = ctx.createGain();   // keeps the decoder pulling while muted
    silent.gain.value = 0;
    src.connect(silent).connect(ctx.destination);
    const listenGain = ctx.createGain();
    listenGain.gain.value = this.muted ? 0 : 1;
    src.connect(listenGain).connect(ctx.destination);
    const a = buildAnalysers(ctx, src);
    this.meter.attach(a.analysers);
    this.audio = { sink, listenGain, nodes: [src, silent, listenGain, ...a.nodes] };
  },

  disconnectAudio() {
    if (!this.audio) return;
    this.audio.nodes.forEach((n) => { try { n.disconnect(); } catch (_) {} });
    this.audio.sink.srcObject = null;
    this.audio = null;
    this.meter.attach(null);
  },

  setMuted(m) {
    this.muted = m;
    if (this.audio) this.audio.listenGain.gain.value = m ? 0 : 1;
    const b = $("#live-mute");
    b.textContent = m ? "MUTE" : "UNMUTE · 출력 중";
    b.classList.toggle("on", !m);
    if (!m) AudioMgr.resume();
  },

  async start() {
    try {
      const r = await api("POST", "/api/record/start", { device: this.device, prefix: $("#rec-prefix").value.trim(), fps: $("#rec-fps").value });
      toast(`녹화 시작: ${r.clip}`);
    } catch (e) { toast(e.message, true); }
  },

  async stop() {
    if (!confirm(`${this.device} 녹화를 정지할까요?`)) return;
    try { await api("POST", "/api/record/stop", { device: this.device }); toast("녹화 정지"); }
    catch (e) { toast(e.message, true); }
  },

  async extend() {
    try {
      const r = await api("POST", "/api/record/extend", { device: this.device });
      toast(`녹화 ${this.extendMin}분 연장 — 자동 종료까지 ${hms(r.remaining)}`);
    } catch (e) { toast(e.message, true); }
  },

  async preview(on) {
    try { await api("POST", "/api/record/preview", { device: this.device, on }); }
    catch (e) { toast(e.message, true); }
  },
};
window.addEventListener("beforeunload", () => { if (Record.reader) Record.reader.close(); });
