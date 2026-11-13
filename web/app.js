"use strict";
/* bootstrap + status WebSocket (1 s push: recorders, jobs, proxies, system) */
const App = {
  async init() {
    const cfg = await api("GET", "/api/config");
    Record.init(cfg);
    Player.init(cfg);
    Codec.init(cfg.subclip);
    Browser.init();
    System.init();
    Splitter.init();
    Record.setDevices(await api("GET", "/api/devices"));
    this.connect();
  },

  connect() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.onopen = () => $("#ws-state").classList.remove("off");
    ws.onclose = () => {
      $("#ws-state").classList.add("off");
      setTimeout(() => this.connect(), 2000);
    };
    ws.onmessage = (e) => {
      const st = JSON.parse(e.data);
      Record.setDevices(st.recorders);
      Browser.onStatus(st);
      Player.onCounterRev(st.counter_rev);
      System.render(st.sys);
      System.renderJobs(st.jobs, st.proxies);
    };
  },
};
App.init().catch((e) => toast("초기화 실패: " + e.message, true));

/* divider between 녹화제어 and 시스템 상태: drag sets the 녹화제어 height (remembered per browser) */
const Splitter = {
  init() {
    const bar = $("#left-split"), top = $("#p-record");
    const apply = (h) => { top.style.flexBasis = h ? `${h}px` : ""; };
    let saved = null;
    try { saved = parseInt(localStorage.getItem("ic.leftSplit"), 10) || null; } catch (_) {}
    apply(saved);
    bar.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      bar.setPointerCapture(e.pointerId);
      bar.classList.add("drag");
      const y0 = e.clientY, h0 = top.getBoundingClientRect().height;
      const col = $("#left-col").getBoundingClientRect().height;
      const move = (ev) => apply(Math.round(Math.max(120, Math.min(col - 90, h0 + ev.clientY - y0))));
      const up = () => {
        bar.classList.remove("drag");
        bar.removeEventListener("pointermove", move);
        bar.removeEventListener("pointerup", up);
        try { localStorage.setItem("ic.leftSplit", Math.round(top.getBoundingClientRect().height)); } catch (_) {}
      };
      bar.addEventListener("pointermove", move);
      bar.addEventListener("pointerup", up);
    });
    bar.addEventListener("dblclick", () => { apply(null); try { localStorage.removeItem("ic.leftSplit"); } catch (_) {} });
  },
};
