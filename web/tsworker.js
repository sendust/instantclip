"use strict";
/* Decoder worker for TS clips: Range-fetch one GOP (bytes from /api/ts/gop), pull the video PES out of the
 * transport stream and decode it into raw YUV with tsdec.wasm (libavcodec's decoders, see wasm/tsdec.c).
 * The page compiles the wasm once and hands the module to every worker; a worker keeps one decoder per
 * codec and lowres, flushed between GOPs.
 *
 * The bytes run from the GOP's I-frame to the first picture after the next I-frame that displays after it.
 * Leading B-frames of the I-frame (open GOP: they reference the GOP before) are dropped here, so the decoder
 * outputs the GOP's frames in display order, then the next I-frame, which is cut off with `frames`.
 *
 * Audio (first audio PID, AC-3 / MP2 / AAC) comes from the PES in [astart, aend), muxed a little after the
 * video, so one Range request covers both. It is decoded to 48 kHz stereo and cut to the samples of the
 * GOP's display frames: sample s of the clip plays at frame-0 time + s / 48000, and a GOP gets samples
 * [floor(T0 * 8 / 15), floor(T1 * 8 / 15)) of its frame span [T0, T1) in 90 kHz ticks, so GOPs join seamlessly.
 */
let wasmModule = null, M = null;
const decs = new Map();          // codec + lowres -> Dec* in M
const aborts = new Map();

/* a fresh wasm instance (also after a failed job: a trap may leave the instance corrupt) */
async function instance() {
  if (M) return M;
  M = await createTsDec({
    instantiateWasm(imports, cb) {
      WebAssembly.instantiate(wasmModule, imports).then((inst) => cb(inst));
      return {};
    },
  });
  decs.clear();
  return M;
}

self.onmessage = async (e) => {
  const m = e.data;
  try {
    if (m.type === "init") {
      importScripts(m.coreUrl);
      wasmModule = m.wasmModule;
      await instance();
      self.postMessage({ type: "ready" });
    } else if (m.type === "cancel") {
      aborts.get(m.id)?.abort();
    } else if (m.type === "decode") {
      await instance();
      try { await decodeJob(m); }
      catch (err) { if (!(err instanceof NetError)) M = null; throw err; }
    }
  } catch (err) {
    self.postMessage({ type: "error", id: m.id, message: String(err?.message || err) });
  }
};

class NetError extends Error {}

const PKT = 188;
const WRAP = 2 ** 33;

function pts(b, i) {
  return ((b[i] >> 1) & 7) * 2 ** 30 + (b[i + 1] << 22) + ((b[i + 2] >> 1) << 15) + (b[i + 3] << 7) + (b[i + 4] >> 1);
}

/* PES payloads of one PID: [{pos, pts, data: [Uint8Array...]}] in stream order (pos = byte in b) */
function demux(b, pid) {
  let o = 0;
  while (o < PKT && !(b[o] === 0x47 && b[o + PKT] === 0x47)) o++;
  const out = [];
  let cur = null;
  for (let p = o; p + PKT <= b.length; p += PKT) {
    if (b[p] !== 0x47 || (((b[p + 1] & 0x1f) << 8) | b[p + 2]) !== pid) continue;
    const afc = (b[p + 3] >> 4) & 3;
    if (!(afc & 1)) continue;
    let q = afc & 2 ? p + 5 + b[p + 4] : p + 4;
    if (b[p + 1] & 0x40) {
      if (b[q] !== 0 || b[q + 1] !== 0 || b[q + 2] !== 1) { cur = null; continue; }
      cur = { pos: p, pts: b[q + 7] & 0x80 ? pts(b, q + 9) : null, data: [] };
      out.push(cur);
      q += 9 + b[q + 8];
    }
    if (cur && q < p + PKT) cur.data.push(b.subarray(q, p + PKT));
  }
  return out;
}

/* PES payloads concatenated straight into the wasm heap: [ptr, len] (the caller frees ptr) */
function toHeap(pes) {
  let n = 0;
  for (const p of pes) for (const a of p.data) n += a.length;
  const ptr = M._malloc(n || 1);
  let o = ptr;
  for (const p of pes) for (const a of p.data) { M.HEAPU8.set(a, o); o += a.length; }
  return [ptr, n];
}

/* decode an elementary stream with the codec's decoder: {n, out (copy), w, h} — see wasm/tsdec.c */
function run(codec, lowres, pes, max) {
  const k = `${codec}/${lowres}`;
  let d = decs.get(k);
  if (!d) {
    d = M.ccall("dec_open", "number", ["string", "number"], [codec, lowres]);
    if (!d) throw new Error(`디코더를 열 수 없습니다: ${codec}`);
    decs.set(k, d);
  }
  const [ptr, len] = toHeap(pes);
  const n = M._dec_run(d, ptr, len, max);
  M._free(ptr);
  if (n < 0) throw new Error(`${codec} 디코딩 오류 ${n}`);
  const o = M._dec_out(d);
  return { n, out: M.HEAPU8.slice(o, o + M._dec_len(d)), w: M._dec_w(d), h: M._dec_h(d) };
}

async function decodeJob(m) {
  const t0 = performance.now();
  const ac = new AbortController();
  aborts.set(m.id, ac);
  const audio = !m.keyOnly && m.astart !== undefined;
  const base = audio ? Math.min(m.start, m.astart) : m.start;   // file byte of buf[0]
  let buf;
  try {
    const end = m.keyOnly ? Math.min(m.end, m.start + (m.keyBytes || 1 << 20)) : audio ? Math.max(m.end, m.aend) : m.end;
    const r = await fetch(m.url, { headers: { Range: `bytes=${base}-${end - 1}` }, signal: ac.signal });
    if (r.status !== 206) throw new NetError(`HTTP ${r.status}`);
    buf = new Uint8Array(await r.arrayBuffer());
  } catch (err) {
    if (ac.signal.aborted) { self.postMessage({ type: "cancelled", id: m.id }); return; }
    throw err instanceof NetError ? err : new NetError(String(err?.message || err));
  } finally {
    aborts.delete(m.id);
  }
  const t1 = performance.now();

  const inRange = (a, b) => (p) => p.pos + base >= a && p.pos + base < b;
  let pes = demux(buf, m.pid).filter(inRange(m.start, m.end));
  if (!pes.length || pes[0].pts !== m.pts) throw new NetError("GOP 가 I-프레임으로 시작하지 않습니다");
  // drop the I-frame's leading B-frames (displayed before it)
  pes = pes.filter((p, i) => i === 0 || p.pts === null || (p.pts - m.pts + WRAP) % WRAP < WRAP / 2);
  if (m.keyOnly) pes = pes.slice(0, 1);
  const L = m.lowres | 0;
  const v = run("mpeg2video", L, pes, m.keyOnly ? 1 : m.frames);
  if (!v.n) throw new Error("디코딩된 프레임이 없습니다");
  const yuv = v.out, w = v.w, h = v.h, cw = (w + 1) >> 1, ch = (h + 1) >> 1;
  const frameSize = w * h + 2 * cw * ch;
  const t2 = performance.now();

  const pcm = audio ? decodeAudio(demux(buf, m.apid).filter(inRange(m.astart, m.aend)), m) : null;
  const t3 = performance.now();

  self.postMessage({
    type: "done", id: m.id, keyOnly: !!m.keyOnly, lowres: L, frame: m.frame,
    w, h, cw, ch, count: v.n, frameSize, yuv, pictures: pes.length, audio: pcm || [],
    bytes: buf.length, tFetch: t1 - t0, tDecode: t2 - t1, tAudio: t3 - t2,
  }, [yuv.buffer, ...(pcm || []).map((a) => a.buffer)]);
}

/* decode the audio PES and cut them to the GOP's samples: [Float32Array L, R] at 48 kHz */
function decodeAudio(pes, m) {
  const rel = (p) => ((p - m.pts0 + WRAP * 1.5) % WRAP) - WRAP / 2;
  const s0 = Math.floor(((m.frame * m.fd) * 8) / 15);
  const s1 = Math.floor((((m.frame + m.frames) * m.fd) * 8) / 15);
  const out = [new Float32Array(s1 - s0), new Float32Array(s1 - s0)];
  // start at the first PES that begins a frame (AC-3 sync word) and has a pts
  const k = pes.findIndex((p) => p.pts !== null && (m.acodec !== "ac3" || (p.data[0]?.[0] === 0x0b && p.data[0]?.[1] === 0x77)));
  if (k < 0) return out;
  pes = pes.slice(k);
  const a = run(m.acodec, 0, pes, 0);
  if (!a.n) return out;
  const f = new Float32Array(a.out.buffer, 0, a.n * 2);
  const first = Math.round((rel(pes[0].pts) * 8) / 15);     // clip sample of the first decoded sample
  for (let i = Math.max(0, first - s0), j = s0 + i - first; i < out[0].length && j < a.n; i++, j++) {
    out[0][i] = f[2 * j];
    out[1][i] = f[2 * j + 1];
  }
  return out;
}
