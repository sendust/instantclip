"use strict";
/* In-browser decoding of TS clips (tvheadend MPEG-2 recordings) for the editor.
 * The server locates GOPs (/api/ts/open, /api/ts/gop); a pool of tsworker.js workers fetches each GOP's
 * bytes straight from the original file (HTTP Range) and decodes it with tsdec.wasm (libavcodec, wasm/).
 *
 *   const d = new TsDecoder(4); const info = await d.open(path);
 *   const g = await d.decode(frame, {lowres: 1});   // {frame, count, w, h, cw, ch, frameSize, yuv, ...}
 *   // display frame n is at offset (n - g.frame) * g.frameSize;
 *   // g.audio = [L, R] Float32Array at 48 kHz, sample 0 at the start of frame g.frame
 */
const TS_CORE = "/static/vendor/tsdec/tsdec.js";
const TS_WASM = "/static/vendor/tsdec/tsdec.wasm";
const TS_WORKER = "/static/tsworker.js";

let tsWasm = null;
function tsWasmModule() {
  tsWasm ??= (async () => {
    try { return await WebAssembly.compileStreaming(fetch(TS_WASM)); }
    catch (_) { return WebAssembly.compile(await (await fetch(TS_WASM)).arrayBuffer()); }
  })();
  return tsWasm;
}

class TsDecoder {
  constructor(workers = 4) {
    this.n = workers;
    this.workers = [];
    this.queue = [];             // jobs waiting for a worker
    this.jobs = new Map();       // id -> job
    this.jobId = 1;
    this.info = null;
    this.gops = [];              // known GOPs sorted by frame: {frame, frames, start, end, pts}
  }

  async start() {
    if (this.workers.length) return;
    const mod = await tsWasmModule();
    this.workers = await Promise.all(Array.from({ length: this.n }, () => new Promise((res, rej) => {
      const w = { worker: new Worker(TS_WORKER), job: null };
      w.worker.onmessage = (e) => (e.data.type === "ready" ? res(w) : this.onDone(w, e.data));
      w.worker.onerror = (e) => rej(new Error(e.message));
      w.worker.postMessage({ type: "init", coreUrl: TS_CORE, wasmModule: mod });
    })));
  }

  /* info: /api/ts/open of the clip, when the caller has it already (/api/clip "ts") */
  async open(path, info = null) {
    await this.start();
    this.info = info || await api("GET", `/api/ts/open?path=${encodeURIComponent(path)}`);
    this.url = `/api/files/download?root=capture&inline=1&path=${encodeURIComponent(path)}`;
    this.gops = [];
    return this.info;
  }

  /* GOP holding display frame n (cached; the server is asked only for frames outside known GOPs) */
  async gop(n) {
    const i = this.gopIndex(n);
    if (i >= 0) return this.gops[i];
    const g = await api("GET", `/api/ts/gop?path=${encodeURIComponent(this.info.path)}&frame=${n}`);
    if (this.gopIndex(g.frame) < 0) {
      const k = this.gops.findIndex((x) => x.frame > g.frame);
      this.gops.splice(k < 0 ? this.gops.length : k, 0, g);
    }
    return g;
  }

  gopIndex(n) {
    let lo = 0, hi = this.gops.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1, g = this.gops[mid];
      if (n < g.frame) hi = mid - 1;
      else if (n >= g.frame + g.frames) lo = mid + 1;
      else return mid;
    }
    return -1;
  }

  /* decode the GOP holding frame n; keyOnly decodes just its I-frame; urgent jumps the queue */
  async decode(n, { lowres = 0, keyOnly = false, urgent = false } = {}) {
    const t0 = performance.now();
    const g = await this.gop(n);
    const tGop = performance.now() - t0;
    const r = await new Promise((resolve, reject) => {
      const job = {
        id: this.jobId++, resolve, reject,
        msg: { type: "decode", url: this.url, pid: this.info.video.pid, width: this.info.width,
               height: this.info.height, pts0: this.info.pts0, fd: this.info.fd,
               apid: this.info.audio?.pid, acodec: this.info.audio?.codec, lowres, keyOnly, ...g },
      };
      job.msg.id = job.id;
      this.jobs.set(job.id, job);
      if (urgent) this.queue.unshift(job); else this.queue.push(job);
      this.pump();
    });
    return { ...r, tGop };
  }

  /* forget jobs not started yet (they reject with "cancelled") */
  dropQueued() {
    for (const job of this.queue.splice(0)) {
      this.jobs.delete(job.id);
      job.reject(new Error("cancelled"));
    }
  }

  pump() {
    let w;
    while (this.queue.length && (w = this.workers.find((x) => !x.job))) {
      const job = this.queue.shift();
      w.job = job;
      w.worker.postMessage(job.msg);
    }
  }

  onDone(w, m) {
    const job = this.jobs.get(m.id);
    w.job = null;
    this.jobs.delete(m.id);
    if (job) {
      if (m.type === "done") job.resolve(m);
      else job.reject(new Error(m.type === "cancelled" ? "cancelled" : m.message));
    }
    this.pump();
  }
}
