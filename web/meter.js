"use strict";
/* dBFS level meters (Web Audio, RMS, attack/release, peak hold).
 * Same method as mediarouter web/meter.js: fixed colour track + mask, one rAF loop.
 */
const METER_MIN_DB = -60;
const METER_MAX_DB = 0;
const METER_TICKS = [-60, -45, -30, -18, -6, 0];
const ATTACK = 0.6;
const RELEASE = 0.08;
const PEAK_HOLD_MS = 1200;
const PEAK_DECAY_DB_S = 20;

const AudioMgr = {
  ctx: null,
  get() {
    if (!this.ctx) {
      this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      this.ctx.onstatechange = () => this.checkUnlock();
    }
    this.checkUnlock();
    return this.ctx;
  },
  resume() { if (this.ctx && this.ctx.state !== "running") this.ctx.resume().then(() => this.checkUnlock()); },
  checkUnlock() { $("#audio-unlock").hidden = !this.ctx || this.ctx.state === "running"; },
};
["click", "keydown"].forEach((ev) => document.addEventListener(ev, () => AudioMgr.resume(), true));

const dbToPct = (db) => ((Math.min(METER_MAX_DB, Math.max(METER_MIN_DB, db)) - METER_MIN_DB) / (METER_MAX_DB - METER_MIN_DB)) * 100;

class ChannelMeter {
  constructor(parent, vertical) {
    this.vertical = vertical;
    this.track = Object.assign(document.createElement("div"), { className: "mtrack" });
    this.mask = Object.assign(document.createElement("div"), { className: "mmask" });
    this.peakEl = Object.assign(document.createElement("div"), { className: "mpeak" });
    this.track.append(this.mask, this.peakEl);
    parent.append(this.track);
    this.level = this.peak = METER_MIN_DB;
    this.peakTs = 0;
    this.analyser = null;
  }
  attach(an) { this.analyser = an; this.buf = an ? new Float32Array(an.fftSize) : null; }
  update(now, dt) {
    let db = METER_MIN_DB;
    if (this.analyser) {
      this.analyser.getFloatTimeDomainData(this.buf);
      let s = 0;
      for (let i = 0; i < this.buf.length; i++) s += this.buf[i] * this.buf[i];
      const rms = Math.sqrt(s / this.buf.length);
      db = rms > 0 ? 20 * Math.log10(rms) : METER_MIN_DB;   // full-scale sine = -3.01 dBFS
    }
    this.level += (db - this.level) * (db > this.level ? ATTACK : RELEASE);
    if (this.level >= this.peak) { this.peak = this.level; this.peakTs = now; }
    else if (now - this.peakTs > PEAK_HOLD_MS) this.peak = Math.max(this.level, this.peak - PEAK_DECAY_DB_S * dt);
    const lv = dbToPct(this.level), pk = dbToPct(this.peak);
    if (this.vertical) {
      this.mask.style.height = (100 - lv).toFixed(2) + "%";
      this.peakEl.style.bottom = `calc(${pk.toFixed(2)}% - 2px)`;
    } else {
      this.mask.style.width = (100 - lv).toFixed(2) + "%";
      this.peakEl.style.left = `calc(${pk.toFixed(2)}% - 2px)`;
    }
    return this.peak;
  }
}

/* L/R meter with scale and peak read-out */
class StereoMeter {
  constructor(el, vertical = false) {
    this.el = el;
    el.innerHTML = "";
    const scale = Object.assign(document.createElement("div"), { className: "scale" });
    scale.innerHTML = METER_TICKS.map((d) => `<span style="${vertical ? "bottom" : "left"}:${dbToPct(d)}%">${d}</span>`).join("");
    this.read = Object.assign(document.createElement("div"), { className: "mread", textContent: "-∞" });
    if (vertical) {
      const box = Object.assign(document.createElement("div"), { className: "vbox" });
      this.ch = [new ChannelMeter(box, true), new ChannelMeter(box, true)];
      box.append(scale);
      el.append(box, this.read);
    } else {
      this.ch = [new ChannelMeter(el, false), new ChannelMeter(el, false)];
      el.append(scale, this.read);
    }
    Meters.views.add(this);
  }
  attach(analysers) { this.ch.forEach((m, i) => m.attach(analysers ? analysers[i] : null)); }
  update(now, dt) {
    const p = Math.max(this.ch[0].update(now, dt), this.ch[1].update(now, dt));
    this.read.textContent = p <= METER_MIN_DB + 0.5 ? "-∞" : p.toFixed(1) + " dBFS";
  }
}

/* srcNode -> up-mix to 2ch -> splitter -> 2 analysers. returns {analysers, nodes} */
function buildAnalysers(ctx, srcNode) {
  // ChannelSplitter's channel props are fixed by spec; up-mix with a unity GainNode in front
  const upmix = ctx.createGain();
  upmix.channelCount = 2;
  upmix.channelCountMode = "explicit";
  upmix.channelInterpretation = "speakers";
  const split = ctx.createChannelSplitter(2);
  srcNode.connect(upmix).connect(split);
  const analysers = [0, 1].map((i) => {
    const an = ctx.createAnalyser();
    an.fftSize = 1024;
    split.connect(an, i);
    return an;
  });
  return { analysers, nodes: [upmix, split, ...analysers] };
}

const Meters = {
  views: new Set(),
  last: performance.now(),
  loop(now) {
    const dt = Math.min(0.2, (now - Meters.last) / 1000);
    Meters.last = now;
    Meters.views.forEach((v) => v.update(now, dt));
    requestAnimationFrame(Meters.loop);
  },
};
requestAnimationFrame(Meters.loop);
