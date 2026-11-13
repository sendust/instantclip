"""DeckLink capture channels.

Per device one ffmpeg at a time:
  preview  decklink -> live (640x360 h264 + opus) -> mediamtx RTSP          (WebRTC monitor)
  record   decklink -> bwdif_cuda -> hires 1080p59.94 4:2:0 (35M)          <record>/<name>.ts
                                  -> proxy 480p29.97 (4M) HLS EVENT          <proxy>/<name>/index.m3u8
                                  -> thumbnails every N s                    <thumbnail>/<name>/
                                  -> live -> mediamtx RTSP
Starting a recording replaces the preview process (the WebRTC reader reconnects by itself).
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path

from .config import Config
from .ffutil import FFmpegProcess, close_playlist, list_decklink
from .media import Media, clean_name, hls_args, video_args

log = logging.getLogger("recorder")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


class Channel:
    def __init__(self, mgr: "Recorder", device: str):
        self.mgr = mgr
        self.device = device
        self.proc: FFmpegProcess | None = None
        self.mode = "idle"            # idle | preview | recording
        self.want_preview = False
        self.rel: str | None = None   # recording clip (relative to record dir)
        self.rec_started = 0.0
        self.deadline = 0.0           # auto-stop time (epoch) of the running recording
        self.no_signal = False
        self.error = ""
        self._lock = asyncio.Lock()

    @property
    def live_path(self) -> str:
        return self.mgr.cfg.section("mediamtx")["path_prefix"] + slug(self.device)

    def elapsed(self) -> float:
        return time.time() - self.rec_started if self.mode == "recording" else 0.0

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.time()) if self.mode == "recording" else 0.0

    def status(self) -> dict:
        p = self.proc
        return {
            "device": self.device, "mode": self.mode, "preview": self.want_preview,
            "live_path": self.live_path, "clip": self.rel, "elapsed": self.elapsed(),
            "remaining": self.remaining(),
            "no_signal": self.no_signal, "error": self.error,
            "speed": p.progress.get("speed") if p else None,
            "drop": p.progress.get("drop_frames") if p else None,
            "dup": p.progress.get("dup_frames") if p else None,
        }

    def _on_line(self, line: str):
        if "No input signal detected" in line:
            self.no_signal = True
        elif "Input returned" in line:
            self.no_signal = False

    # ---------- command building ----------
    def _input_args(self) -> list[str]:
        cap = self.mgr.cfg.section("capture")
        ff = self.mgr.cfg.section("ffmpeg")
        return [ff["ffmpeg"], "-hide_banner", "-nostdin", "-nostats", "-progress", "pipe:1",
                "-stats_period", "1", "-loglevel", "level+info",
                "-f", "decklink", "-format_code", cap["format_code"],
                "-channels", str(cap["audio_channels"]), "-i", self.device]

    def _live_out(self, vlabel: str, alabel: str) -> list[str]:
        lv = self.mgr.cfg.section("live")
        url = self.mgr.cfg.section("mediamtx")["rtsp_url"].rstrip("/") + "/" + self.live_path
        return ["-map", vlabel, "-map", alabel, "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll",
                "-rc", "cbr", "-b:v", lv["bitrate"], "-maxrate", lv["bitrate"], "-bufsize", lv["bitrate"],
                "-g", str(lv["gop"]), "-bf", "0", "-c:a", "libopus", "-b:a", lv["abitrate"], "-ar", "48000",
                "-ac", "2", "-f", "rtsp", "-rtsp_transport", "tcp", url]

    def _preview_args(self) -> list[str]:
        cap, lv = self.mgr.cfg.section("capture"), self.mgr.cfg.section("live")
        fc = (f"[0:v]scale=interl=1,format=nv12,hwupload_cuda,{cap['deinterlace']},"
              f"fps=30000/1001,scale_cuda={lv['width']}:{lv['height']}[lv]")
        return self._input_args() + ["-filter_complex", fc] + self._live_out("[lv]", "0:a:0")

    def _record_args(self, hires: Path, pdir: Path, tdir: Path) -> list[str]:
        cfg = self.mgr.cfg
        cap, hi, px = cfg.section("capture"), cfg.section("record_hires"), cfg.section("record_proxy")
        lv, th = cfg.section("live"), cfg.section("thumbnail")
        # a CPU hires encoder (mpeg2video) needs the deinterlaced frames back in system memory
        hi_dl = "" if "nvenc" in hi["vcodec"] else "[hi0];[hi0]hwdownload,format=nv12,format=yuv420p"
        fc = (f"[0:v]scale=interl=1,format=nv12,hwupload_cuda,{cap['deinterlace']},split=2[l0]{hi_dl}[hi];"
              f"[l0]fps={px['fps']},scale_cuda={px['width']}:{px['height']},split=3[lo][lv0][th0];"
              f"[lv0]scale_cuda={lv['width']}:{lv['height']}[lv];"
              f"[th0]fps=1/{th['interval']},hwdownload,format=nv12,scale={th['width']}:{th['height']}[th];"
              f"[0:a]asplit=3[a1][a2][a3]")
        fmt = {"ts": "mpegts", "mp4": "mp4", "mov": "mov", "mkv": "matroska"}.get(hi["container"], "mpegts")
        hires_mux = ["-f", fmt]
        if fmt in ("mp4", "mov"):  # readable while growing
            hires_mux += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-frag_duration", "1000000"]
        return (self._input_args() + ["-filter_complex", fc]
                + ["-map", "[hi]", "-map", "[a1]", *video_args(hi), "-c:a", hi["acodec"],
                   "-b:a", hi["abitrate"], *hi.get("extra_args", []), *hires_mux, str(hires)]
                + ["-map", "[lo]", "-map", "[a2]", *video_args(px), "-c:a", px["acodec"],
                   "-b:a", px["abitrate"], *hls_args(px, pdir)]
                + self._live_out("[lv]", "[a3]")
                + ["-map", "[th]", "-c:v", "mjpeg", "-q:v", str(th["quality"]), "-f", "image2",
                   str(tdir / "%06d.jpg")])

    # ---------- process control ----------
    async def _launch(self, args, mode):
        self.error = ""
        self.no_signal = False
        proc = FFmpegProcess(f"{mode}:{slug(self.device)}", args, on_line=self._on_line)
        self.proc = proc
        self.mode = mode
        await proc.start()
        asyncio.create_task(self._watch(proc))

    async def _watch(self, proc: FFmpegProcess):
        rc = await proc.wait()
        if self.proc is not proc:   # superseded (preview replaced by record etc.)
            return
        was = self.mode
        if was == "recording":
            self._finish_recording()
        self.proc = None
        self.mode = "idle"
        if not proc.stop_requested:
            self.error = f"ffmpeg 종료(rc={rc}): {proc.error_text()}"
            log.error("[%s] %s unexpectedly ended: %s", self.device, was, self.error)
            if self.want_preview:   # retry the monitor after a pause (device busy / no signal)
                await asyncio.sleep(5)
                async with self._lock:
                    if self.want_preview and self.mode == "idle":
                        await self._launch(self._preview_args(), "preview")

    def _finish_recording(self):
        if self.rel:
            close_playlist(self.mgr.media.proxy_dir(self.rel) / "index.m3u8")
            log.info("[%s] recording finished %s (%.1fs)", self.device, self.rel, self.elapsed())
        self.rel = None
        self.mgr.media.bump()

    async def _stop_proc(self):
        if self.proc and self.proc.running:
            p = self.proc
            await p.stop()
        if self.mode == "recording":
            self._finish_recording()
        self.proc = None
        self.mode = "idle"

    async def set_preview(self, on: bool):
        async with self._lock:
            self.want_preview = on
            if on and self.mode == "idle":
                await self._launch(self._preview_args(), "preview")
            elif not on and self.mode == "preview":
                await self._stop_proc()

    async def start_record(self, prefix: str = "") -> str:
        async with self._lock:
            if self.mode == "recording":
                raise RuntimeError("이미 녹화 중입니다")
            cap = self.mgr.cfg.section("capture")
            prefix = clean_name(prefix or cap["default_prefix"])
            name = time.strftime(cap["name_template"].replace("{prefix}", prefix))
            ext = self.mgr.cfg.section("record_hires")["container"]
            media = self.mgr.media
            hires = media.cfg.path_of("record") / f"{name}.{ext}"
            n = 1
            while hires.exists():
                n += 1
                hires = hires.with_name(f"{name}-{n}.{ext}")
            rel = hires.name
            pdir, tdir = media.proxy_dir(rel), media.thumb_dir(rel)
            for d in (hires.parent, pdir, tdir):
                d.mkdir(parents=True, exist_ok=True)
            (tdir / "meta.json").write_text(json.dumps({"interval": self.mgr.cfg.section("thumbnail")["interval"]}))
            if self.mode == "preview":
                await self._stop_proc()
            self.rel = rel
            self.rec_started = time.time()
            self.deadline = self.rec_started + float(cap["auto_stop_sec"])
            await self._launch(self._record_args(hires, pdir, tdir), "recording")
            media.bump()
            log.info("[%s] recording start %s", self.device, rel)
            return rel

    def extend(self, sec: float) -> float:
        if self.mode != "recording":
            raise RuntimeError("녹화 중이 아닙니다")
        self.deadline += sec
        log.info("[%s] auto-stop extended +%ds, remaining %.0fs", self.device, sec, self.remaining())
        return self.remaining()

    async def stop_record(self):
        async with self._lock:
            if self.mode != "recording":
                return
            await self._stop_proc()
            if self.want_preview:
                await self._launch(self._preview_args(), "preview")


class Recorder:
    def __init__(self, cfg: Config, media: Media):
        self.cfg = cfg
        self.media = media
        self.channels: dict[str, Channel] = {}
        self.devices: list[str] = []
        media.recording_rels = self.recording_rels

    async def refresh_devices(self) -> list[str]:
        try:
            self.devices = await list_decklink(self.cfg.section("ffmpeg")["ffmpeg"])
        except Exception:
            log.exception("decklink listing failed")
        return self.devices

    def channel(self, device: str) -> Channel:
        if device not in self.channels:
            if self.devices and device not in self.devices:
                raise KeyError(device)
            self.channels[device] = Channel(self, device)
        return self.channels[device]

    async def watchdog(self):
        """Auto-stop recordings whose deadline passed (operator walked away)."""
        while True:
            await asyncio.sleep(1)
            for c in list(self.channels.values()):
                if c.mode == "recording" and time.time() >= c.deadline:
                    log.warning("[%s] auto-stop: recording time limit reached (%s)", c.device, c.rel)
                    try:
                        await c.stop_record()
                    except Exception:
                        log.exception("[%s] auto-stop", c.device)

    def recording_rels(self) -> dict[str, float]:
        return {c.rel: c.elapsed() for c in self.channels.values() if c.mode == "recording" and c.rel}

    def status(self) -> list[dict]:
        out = []
        for d in self.devices or list(self.channels):
            c = self.channels.get(d)
            out.append(c.status() if c else {"device": d, "mode": "idle", "preview": False,
                                             "live_path": self.cfg.section("mediamtx")["path_prefix"] + slug(d)})
        return out

    async def stop_all(self):
        for c in self.channels.values():
            c.want_preview = False
        await asyncio.gather(*(c._stop_proc() for c in self.channels.values()), return_exceptions=True)
