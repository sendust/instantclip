"use strict";
/* 5. 시스템 상태 + subclip 인코더 큐 */
const System = {
  init() {
    $("#jobs-clear").onclick = async () => { try { await api("POST", "/api/jobs/clear"); } catch (e) { toast(e.message, true); } };
    $("#jobs").addEventListener("click", async (e) => {
      const b = e.target.closest("[data-cancel]");
      if (!b) return;
      try { await api("POST", `/api/jobs/${b.dataset.cancel}/cancel`); } catch (err) { toast(err.message, true); }
    });
  },

  bar(pct, warn = 70, crit = 90) {
    const p = Math.max(0, Math.min(100, pct || 0));
    const cls = p >= crit ? "crit" : p >= warn ? "warn" : "";
    return `<div class="bar ${cls}"><i style="width:${p.toFixed(1)}%"></i></div>`;
  },
  item(k, pct, v, warn, crit) {
    // label and value share the first line (value gets the full width), the bar runs underneath
    return `<div class="sys-item"><span class="k">${k}</span><span class="v">${v}</span>${pct == null ? "" : this.bar(pct, warn, crit)}</div>`;
  },

  render(s) {
    if (!s || !s.cpu) return;
    const h = [];
    if (s.cpu.model) {
      const c = s.cpu, model = c.model.replace(/\((R|TM)\)/g, "").replace(/ CPU/, "").replace(/\s+/g, " ");
      h.push(`<div class="sys-cpu"><span class="mono">${esc(model)}</span><span class="muted">${c.sockets > 1 ? `${c.sockets} socket · ` : ""}${c.cores ?? "?"} core · ${c.threads} thread</span></div>`);
    }
    h.push(this.item("CPU", s.cpu.total, `${s.cpu.total.toFixed(0)}% · load ${s.cpu.load[0].toFixed(1)}`));
    h.push(`<div class="cores" style="grid-template-columns:repeat(${Math.min(20, s.cpu.per.length)},1fr)">${
      s.cpu.per.map((p, i) => `<i title="core ${i}: ${p.toFixed(0)}%"><b style="height:${p}%"></b></i>`).join("")}</div>`);
    h.push(this.item("RAM", s.mem.percent, `${bytes(s.mem.used)} / ${bytes(s.mem.total)}`));
    for (const g of s.gpu) {
      h.push('<div class="sys-sep"></div>');
      h.push(`<div class="sys-cpu"><span class="mono">${esc(g.name)}</span><span class="muted">${
        g.mem_total ? `${bytes(g.mem_total)} VRAM` : ""}${g.driver ? ` · driver ${esc(g.driver)}` : ""}</span></div>`);
      h.push(this.item(`GPU${g.index}`, g.util, `${g.util ?? "-"}%`));
      h.push(this.item("NVENC", g.enc, `${g.enc ?? "-"}% · ${g.enc_sessions ?? "?"} sess`));
      h.push(this.item("NVDEC", g.dec, `${g.dec ?? "-"}%`));
      if (g.mem_total) h.push(this.item("VRAM", (g.mem_used / g.mem_total) * 100, `${bytes(g.mem_used)} / ${bytes(g.mem_total)}`));
    }
    h.push('<div class="sys-sep"></div>');
    for (const n of s.net) {
      const v = `↓${bps(n.rx)} ↑${bps(n.tx)}${n.speed ? ` /${n.speed >= 1000 ? n.speed / 1000 + "G" : n.speed + "M"}` : ""}`;
      h.push(this.item(n.name, n.util, v, 60, 85));
    }
    h.push('<div class="sys-sep"></div>');
    for (const t of s.temps) h.push(this.item("온도", null, `${t.name}: ${t.value.toFixed(0)}°C`));
    for (const g of s.gpu) if (g.temp != null) h.push(this.item("온도", null, `GPU${g.index}: ${g.temp}°C${g.fan != null ? ` · fan ${g.fan}%` : ""}`));
    for (const p of s.power) h.push(this.item("전력", null, `CPU ${p.name}: ${p.watts.toFixed(1)} W`));
    for (const g of s.gpu) if (g.power != null) h.push(this.item("전력", g.power_limit ? (g.power / g.power_limit) * 100 : null, `GPU${g.index}: ${g.power.toFixed(1)} / ${g.power_limit?.toFixed(0) ?? "?"} W`));
    if (!s.power.length) h.push(this.item("전력", null, '<span class="muted">CPU RAPL 권한 없음 (README 참조)</span>'));
    h.push('<div class="sys-sep"></div>');
    for (const d of s.disks) {
      const label = d.name === "record" ? "녹화 경로" : "subclip 경로";
      if (d.error) { h.push(this.item(label, null, d.error)); continue; }
      h.push(this.item(label, (d.used / d.total) * 100, `${bytes(d.free)} 남음 / ${bytes(d.total)}`, 80, 92));
    }
    $("#sys-grid").innerHTML = h.join("");
  },

  renderJobs(jobs, proxies) {
    const h = [];
    for (const p of proxies) {
      h.push(`<div class="job"><div class="top"><span class="st waiting">PROXY</span><span class="nm">${esc(p.name)}</span>
        <span class="mono">${p.progress != null ? p.progress.toFixed(0) + "%" : hms(p.time)} ${esc(p.speed || "")}</span></div>
        ${this.bar(p.progress ?? 0, 101, 101)}</div>`);
    }
    // newest submit first (the server keeps active jobs ahead of finished ones; ids grow per submit)
    for (const j of [...jobs].sort((a, b) => b.id - a.id)) {
      const active = ["queued", "waiting", "running"].includes(j.state);
      h.push(`<div class="job"><div class="top"><span class="st ${j.state}">${j.state.toUpperCase()}</span>
        <span class="nm" title="${esc(j.output)}\n← ${esc(j.source)}">${esc(j.name)}</span>
        <span class="mono">${j.state === "running" ? `${j.progress.toFixed(0)}% ${esc(j.speed || "")}` : tc(j.dur)}</span>
        ${active ? `<button class="small" data-cancel="${j.id}">취소</button>` : ""}</div>
        ${j.state === "running" ? this.bar(j.progress, 101, 101) : ""}
        ${j.state === "waiting" ? '<div class="small muted">녹화 중인 소스 — out 지점이 기록될 때까지 대기</div>' : ""}
        ${j.error ? `<div class="err small">${esc(j.error)}</div>` : ""}</div>`);
    }
    $("#jobs").innerHTML = h.join("") || '<div class="small muted">대기 중인 작업 없음</div>';
  },
};
