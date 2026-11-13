"use strict";
/* 3. subclip 코덱제어: every change is written to config.json right away (PUT /api/config/subclip) */
const CODEC_OPTS = {
  h264_nvenc: {
    preset: ["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
    tune: ["hq", "ll", "ull", "lossless"],
    profile: ["baseline", "main", "high"],
  },
  hevc_nvenc: {
    preset: ["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
    tune: ["hq", "ll", "ull", "lossless"],
    profile: ["main"],
  },
  libx264: {
    preset: ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
    tune: ["", "film", "animation", "grain", "stillimage", "fastdecode", "zerolatency"],
    profile: ["baseline", "main", "high"],
  },
};
const PRESET_DEFAULT = { h264_nvenc: "p5", hevc_nvenc: "p5", libx264: "veryfast" };

const Codec = {
  cfg: {},
  init(sc) {
    this.cfg = sc;
    this.form = $("#codec-form");
    this.fillLists(sc.vcodec);
    this.toForm();
    this.form.addEventListener("change", (e) => this.onChange(e));
    this.form.audio_gain.addEventListener("input", () => this.showGain());
    this.initFold();
    $("#gain-reset").onclick = () => { this.form.audio_gain.value = 1; this.showGain(); this.save({ audio_gain: 1 }); };
  },

  /* fold the panel to the right (remembered per browser); a click anywhere on the strip unfolds it */
  initFold() {
    const layout = $(".layout"), btn = $("#codec-fold"), wide = matchMedia("(min-width: 1501px)");
    let folded = false;
    try { folded = localStorage.getItem("ic.codecFolded") === "1"; } catch (_) {}
    const apply = () => {
      // the right strip only exists in the 3-column layout; elsewhere the panel stays as is
      layout.classList.toggle("codec-folded", folded && wide.matches);
      btn.textContent = folded ? "«" : "»";
      btn.title = folded ? "펼치기" : "오른쪽으로 접기 (시스템 상태 포함)";
    };
    const set = (v) => {
      folded = v;
      try { localStorage.setItem("ic.codecFolded", v ? "1" : "0"); } catch (_) {}
      apply();
    };
    btn.onclick = (e) => { e.stopPropagation(); set(!folded); };
    $("#p-codec").addEventListener("click", () => { if (folded && wide.matches) set(false); });
    wide.addEventListener("change", apply);
    apply();

    // ▾/▸: collapse the settings to the header (independent of the right-side fold)
    const panel = $("#p-codec"), cbtn = $("#codec-collapse");
    let collapsed = false;
    try { collapsed = localStorage.getItem("ic.codecCollapsed") === "1"; } catch (_) {}
    const applyC = () => {
      panel.classList.toggle("collapsed", collapsed);
      cbtn.textContent = collapsed ? "▸" : "▾";
      cbtn.title = collapsed ? "펼치기" : "위로 접기";
    };
    cbtn.onclick = (e) => {
      e.stopPropagation();
      collapsed = !collapsed;
      try { localStorage.setItem("ic.codecCollapsed", collapsed ? "1" : "0"); } catch (_) {}
      applyC();
    };
    applyC();
  },

  fillLists(codec) {
    const o = CODEC_OPTS[codec] || CODEC_OPTS.h264_nvenc;
    for (const k of ["preset", "tune", "profile"]) {
      this.form[k].innerHTML = o[k].map((v) => `<option value="${v}">${v || "(없음)"}</option>`).join("");
    }
  },

  toForm() {
    const f = this.form, c = this.cfg;
    for (const el of f.elements) {
      if (!el.name || !(el.name in c)) continue;
      if (el.type === "checkbox") el.checked = !!c[el.name];
      else {
        // values not in a select list (hand-edited config.json) are added so they show up
        if (el.tagName === "SELECT" && ![...el.options].some((o) => o.value === String(c[el.name]))) {
          el.add(new Option(String(c[el.name]), String(c[el.name])));
        }
        el.value = c[el.name];
      }
    }
    this.showGain();
    this.syncVisibility();
    this.preview();
  },

  syncVisibility() {
    const f = this.form, nv = f.vcodec.value.includes("nvenc"), cq = f.rc.value === "cq";
    $$(".rc-bits", f).forEach((el) => { el.hidden = cq; });
    $$(".rc-cq", f).forEach((el) => { el.hidden = !cq; });
    $$(".nv-only", f).forEach((el) => { el.hidden = !nv; });
    const pcm = f.acodec.value.startsWith("pcm");
    f.abitrate.disabled = pcm;
    f.faststart.disabled = !["mp4", "mov"].includes(f.container.value);
    const warn = pcm && f.container.value === "mp4" ? "PCM 오디오는 mp4 에 넣을 수 없습니다 — mov 또는 mkv 를 선택하세요" : "";
    $("#codec-warn").hidden = !warn;
    $("#codec-warn").textContent = warn;
  },

  showGain() {
    const g = parseFloat(this.form.audio_gain.value);
    $("#gain-read").textContent = `×${g.toFixed(2)} (${g > 0 ? (20 * Math.log10(g)).toFixed(1) : "-∞"} dB)`;
  },

  onChange(e) {
    const el = e.target;
    if (!el.name) return;
    const patch = {};
    if (el.name === "vcodec") {
      const prevProfile = this.cfg.profile;
      this.fillLists(el.value);
      const o = CODEC_OPTS[el.value];
      patch.preset = PRESET_DEFAULT[el.value];
      patch.tune = o.tune[0];
      patch.profile = o.profile.includes(prevProfile) ? prevProfile : o.profile[o.profile.length - 1];
      this.form.preset.value = patch.preset;
      this.form.tune.value = patch.tune;
      this.form.profile.value = patch.profile;
    }
    if (el.type === "checkbox") patch[el.name] = el.checked;
    else if (el.name === "audio_gain") patch[el.name] = parseFloat(el.value);
    else if (["gop", "bframes", "cq", "workers"].includes(el.name)) patch[el.name] = parseInt(el.value, 10);
    else patch[el.name] = el.value;
    this.syncVisibility();
    this.save(patch);
  },

  async save(patch) {
    try {
      this.cfg = await api("PUT", "/api/config/subclip", patch);
      const s = $("#codec-saved");
      s.hidden = false;
      clearTimeout(this.savedTimer);
      this.savedTimer = setTimeout(() => { s.hidden = true; }, 1500);
      this.preview();
    } catch (e) { toast("설정 저장 실패: " + e.message, true); }
  },

  /* python str.format subset used by name_template: {src} {n:03d} {start:.2f} ... */
  preview() {
    const vals = { src: "REC_20261113_090000", n: 1, start: 125.4, end: 190.2, dur: 64.8 };
    const name = (this.cfg.name_template || "").replace(/\{(\w+)(?::([^}]*))?\}/g, (_, k, spec) => {
      const v = vals[k];
      if (v == null) return k;
      let m;
      if (spec && (m = spec.match(/^0?(\d*)d$/))) return String(v).padStart(+m[1] || 0, "0");
      if (spec && (m = spec.match(/^\.(\d+)f$/))) return v.toFixed(+m[1]);
      return String(v);
    });
    $("#name-preview").textContent = `${name}.${this.cfg.container}`;
  },
};
