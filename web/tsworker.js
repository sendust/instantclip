"use strict";
/* Decoder worker for TS clips: Range-fetch one GOP (bytes from /api/ts/gop), pull the video PES out of the
 * transport stream and decode it with ffmpeg.wasm into raw YUV. One single-threaded ffmpeg core per worker;
 * the page compiles the wasm once and hands the module to every worker.
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
let core = null;
const aborts = new Map();
const log = [];

self.onmessage = async (e) => {
  const m = e.data;
  try {
    if (m.type === "init") {
      importScripts(m.coreUrl);
      core = await createFFmpegCore({
        instantiateWasm(imports, cb) {
          WebAssembly.instantiate(m.wasmModule, imports).then((inst) => cb(inst));
          return {};
        },
      });
      core.setLogger(({ message }) => { log.push(message); if (log.length > 50) log.shift(); });
      self.postMessage({ type: "ready" });
    } else if (m.type === "cancel") {
      aborts.get(m.id)?.abort();
    } else if (m.type === "decode") {
      await decodeJob(m);
    }
  } catch (err) {
    self.postMessage({ type: "error", id: m.id, message: String(err?.message || err) });
  }
};

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

function concat(arrs) {
  let n = 0;
  for (const a of arrs) n += a.length;
  const out = new Uint8Array(n);
  let o = 0;
  for (const a of arrs) { out.set(a, o); o += a.length; }
  return out;
}

const shr = (v, s) => -((-v) >> s); // AV_CEIL_RSHIFT

function runFfmpeg(args) {
  log.length = 0;
  core.setTimeout(-1);
  const ret = core.exec(...args);
  core.reset();
  return ret;
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
    if (r.status !== 206) throw new Error(`HTTP ${r.status}`);
    buf = new Uint8Array(await r.arrayBuffer());
  } catch (err) {
    if (ac.signal.aborted) { self.postMessage({ type: "cancelled", id: m.id }); return; }
    throw err;
  } finally {
    aborts.delete(m.id);
  }
  const t1 = performance.now();

  const inRange = (a, b) => (p) => p.pos + base >= a && p.pos + base < b;
  let pes = demux(buf, m.pid).filter(inRange(m.start, m.end));
  if (!pes.length || pes[0].pts !== m.pts) throw new Error("GOP 가 I-프레임으로 시작하지 않습니다");
  // drop the I-frame's leading B-frames (displayed before it)
  pes = pes.filter((p, i) => i === 0 || p.pts === null || (p.pts - m.pts + WRAP) % WRAP < WRAP / 2);
  if (m.keyOnly) pes = pes.slice(0, 1);
  const es = concat(pes.flatMap((p) => p.data));

  const L = m.lowres | 0;
  const w = shr(m.width, L), h = shr(m.height, L);
  const cw = shr(w, 1), ch = shr(h, 1);
  const frameSize = w * h + 2 * cw * ch;
  core.FS.writeFile("/in.es", es);
  const args = ["-hide_banner", "-loglevel", "error", "-threads", "1"];
  if (L) args.push("-lowres", String(L));
  args.push("-f", "mpegvideo", "-i", "/in.es", "-frames:v", String(m.keyOnly ? 1 : m.frames),
    "-an", "-f", "rawvideo", "-pix_fmt", "yuv420p", "/out.yuv");
  const ret = runFfmpeg(args);
  let yuv = null;
  try { yuv = core.FS.readFile("/out.yuv"); } catch (_) { /* no output */ }
  try { core.FS.unlink("/in.es"); } catch (_) {}
  try { core.FS.unlink("/out.yuv"); } catch (_) {}
  if (!yuv || !yuv.length) throw new Error(`ffmpeg ${ret}: ${log.slice(-3).join(" | ")}`);
  const t2 = performance.now();

  const pcm = audio ? decodeAudio(demux(buf, m.apid).filter(inRange(m.astart, m.aend)), m) : null;
  const t3 = performance.now();

  const count = Math.floor(yuv.length / frameSize);
  self.postMessage({
    type: "done", id: m.id, keyOnly: !!m.keyOnly, lowres: L, frame: m.frame,
    w, h, cw, ch, count, frameSize, yuv, pictures: pes.length, audio: pcm || [],
    bytes: buf.length, tFetch: t1 - t0, tDecode: t2 - t1, tAudio: t3 - t2,
  }, [yuv.buffer, ...(pcm || []).map((a) => a.buffer)]);
}

const AFMT = { ac3: "ac3", eac3: "eac3", mp2: "mp3", aac: "aac" };   // raw demuxer per codec (mp3 reads MPEG audio)

/* decode the audio PES and cut them to the GOP's samples: [Float32Array L, R] at 48 kHz */
function decodeAudio(pes, m) {
  const sr = 48000, rel = (p) => ((p - m.pts0 + WRAP * 1.5) % WRAP) - WRAP / 2;
  const s0 = Math.floor(((m.frame * m.fd) * 8) / 15);
  const s1 = Math.floor((((m.frame + m.frames) * m.fd) * 8) / 15);
  const out = [new Float32Array(s1 - s0), new Float32Array(s1 - s0)];
  // start at the first PES that begins a frame (AC-3 sync word) and has a pts
  const k = pes.findIndex((p) => p.pts !== null && (m.acodec !== "ac3" || (p.data[0]?.[0] === 0x0b && p.data[0]?.[1] === 0x77)));
  if (k < 0) return out;
  pes = pes.slice(k);
  core.FS.writeFile("/in.a", concat(pes.flatMap((p) => p.data)));
  runFfmpeg(["-hide_banner", "-loglevel", "error", "-threads", "1", "-f", AFMT[m.acodec] || m.acodec, "-i", "/in.a",
    "-ac", "2", "-ar", String(sr), "-f", "f32le", "/out.pcm"]);
  let raw = null;
  try { raw = core.FS.readFile("/out.pcm"); } catch (_) { /* no output */ }
  try { core.FS.unlink("/in.a"); } catch (_) {}
  try { core.FS.unlink("/out.pcm"); } catch (_) {}
  if (!raw || raw.length < 8) return out;
  const f = new Float32Array(raw.buffer, raw.byteOffset, raw.length >> 2);
  const first = Math.round((rel(pes[0].pts) * 8) / 15);     // clip sample of the first decoded sample
  const n = f.length >> 1;
  for (let i = Math.max(0, first - s0), j = s0 + i - first; i < out[0].length && j < n; i++, j++) {
    out[0][i] = f[2 * j];
    out[1][i] = f[2 * j + 1];
  }
  return out;
}
