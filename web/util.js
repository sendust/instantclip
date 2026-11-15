"use strict";
/* shared helpers: REST calls, toast, formatting */
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const PROXY_FPS = 30000 / 1001;   // editor timecode is counted in proxy (29.97p) frames
const FRAME = 1 / PROXY_FPS;

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(url, opt);
  let data = null;
  try { data = await r.json(); } catch (_) { /* empty body */ }
  if (!r.ok) throw new Error((data && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))) || r.statusText);
  return data;
}

let toastTimer = null;
function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("err", isErr);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isErr ? 5000 : 2500);
}

const pad = (n, w = 2) => String(n).padStart(w, "0");

/* "60000/1001" / "25" -> frames per second (0 when unknown) */
const rate = (r) => { const [n, d] = String(r || "").split("/").map(Number); return n / (d || 1) || 0; };
/* frame index of a time; the editor parks the playhead mid-frame */
const frameOf = (sec) => Math.floor(sec * PROXY_FPS + 1e-4);

/* HH:MM:SS:FF, non-drop count of 29.97p frames */
function tc(sec) {
  if (sec == null || !isFinite(sec)) return "--:--:--:--";
  const neg = sec < 0;
  sec = Math.abs(sec);
  let f = frameOf(sec);
  const fr = f % 30; f = Math.floor(f / 30);
  return `${neg ? "-" : ""}${pad(Math.floor(f / 3600))}:${pad(Math.floor(f / 60) % 60)}:${pad(f % 60)}:${pad(fr)}`;
}

function hms(sec) {
  if (sec == null || !isFinite(sec)) return "";
  sec = Math.floor(sec);
  return `${pad(Math.floor(sec / 3600))}:${pad(Math.floor(sec / 60) % 60)}:${pad(sec % 60)}`;
}

function bytes(n) {
  if (n == null) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${u[i]}`;
}

function bps(n) {
  if (n == null) return "";
  if (n >= 1e9) return (n / 1e9).toFixed(2) + " Gb/s";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + " Mb/s";
  return (n / 1e3).toFixed(0) + " kb/s";
}

function dateStr(t) {
  const d = new Date(t * 1000);
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

setInterval(() => {
  const d = new Date();
  $("#clock").textContent = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}, 500);
