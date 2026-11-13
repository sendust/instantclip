"""Subclip encoder queue: cut [in, out) of the hires clip with the subclip codec settings.

Marks come from the proxy timeline; proxy and hires are produced by the same ffmpeg from the
same filter graph, so both start at 0 and input seek (-ss before -i) is frame accurate.
"""
import asyncio
import itertools
import json
import logging
import os
import re
import time
from pathlib import Path, PurePosixPath

from .config import ROOT, Config
from .ffutil import FFmpegProcess
from .media import Media, MediaError

log = logging.getLogger("encoder")

STATE_PATH = ROOT / "state.json"   # runtime state (subclip counters), not configuration
GROW_MARGIN = 3.0       # seconds of hires that must exist beyond the out point of a growing clip
HISTORY = 100
CONTAINER_FMT = {"mp4": "mp4", "mov": "mov", "mkv": "matroska", "ts": "mpegts"}


class Job:
    _ids = itertools.count(1)

    def __init__(self, source: str, tin: float, tout: float, folder: str, n: int, out_rel: str):
        self.id = next(self._ids)
        self.source = source
        self.tin, self.tout = tin, tout
        self.folder = folder
        self.n = n
        self.out_rel = out_rel
        self.state = "queued"     # queued | waiting | running | done | error | canceled
        self.progress = 0.0
        self.speed = ""
        self.error = ""
        self.created = time.time()
        self.started = self.finished = 0.0
        self.proc: FFmpegProcess | None = None

    def as_dict(self):
        return {"id": self.id, "source": self.source, "in": self.tin, "out": self.tout,
                "dur": self.tout - self.tin, "output": self.out_rel, "name": PurePosixPath(self.out_rel).name,
                "state": self.state, "progress": self.progress, "speed": self.speed, "error": self.error,
                "created": self.created, "started": self.started, "finished": self.finished}


class Encoder:
    def __init__(self, cfg: Config, media: Media):
        self.cfg = cfg
        self.media = media
        self.jobs: list[Job] = []
        self.task: asyncio.Task | None = None
        self.counters: dict[str, int] = {}
        try:
            self.counters = json.loads(STATE_PATH.read_text()).get("counters", {})
        except (OSError, ValueError):
            pass

    def start(self):
        self.media.busy_outputs = lambda: {j.out_rel for j in self.jobs if j.state in ("queued", "waiting", "running")}
        self.task = asyncio.create_task(self._dispatch())

    def limit(self) -> int:
        """Concurrent encodes; read every pass so a UI change applies immediately."""
        return max(1, min(8, int(self.cfg.section("subclip").get("workers", 1))))

    # ---------- naming / counter ----------
    def _save_counters(self):
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"counters": self.counters}, indent=1, ensure_ascii=False))
        os.replace(tmp, STATE_PATH)

    def _disk_max(self, src_stem: str, out_dir: Path) -> int:
        """Highest N on disk ("stem_[N]_..." or legacy "stem_N_...") — seeds a source without a counter."""
        pat = re.compile(re.escape(src_stem) + r"_\[?(\d+)\]?_")
        return max((int(m.group(1)) for p in (out_dir.iterdir() if out_dir.exists() else [])
                    if (m := pat.match(p.name))), default=0)

    def peek_n(self, src_stem: str, folder: str = "") -> int:
        if src_stem in self.counters:
            return self.counters[src_stem] + 1
        return self._disk_max(src_stem, self.media.resolve("subclip", folder)) + 1

    def _take_n(self, src_stem: str, out_dir: Path) -> int:
        n = self.counters[src_stem] + 1 if src_stem in self.counters else self._disk_max(src_stem, out_dir) + 1
        self.counters[src_stem] = n
        self._save_counters()
        return n

    def reset_counter(self, source: str):
        stem = PurePosixPath(source).stem
        self.counters[stem] = 0
        self._save_counters()
        log.info("subclip counter reset: %s", stem)

    def submit(self, source: str, tin: float, tout: float, folder: str = "") -> Job:
        src = self.media.resolve("capture", source)
        if not src.is_file():
            raise MediaError("소스 클립이 없습니다")
        source = str(src.relative_to(self.media.root_dir("capture").resolve()))
        tin, tout = round(float(tin), 4), round(float(tout), 4)
        if tout - tin < 0.03:
            raise MediaError("mark out 이 mark in 보다 뒤에 있어야 합니다")
        out_dir = self.media.resolve("subclip", folder)
        out_dir.mkdir(parents=True, exist_ok=True)
        sc = self.cfg.section("subclip")
        stem = src.stem
        n = self._take_n(stem, out_dir)
        name = sc["name_template"].format(src=stem, n=n, start=tin, end=tout, dur=tout - tin)
        out = out_dir / f"{name}.{sc['container']}"
        k = 1   # after a counter reset the same name can come back: never overwrite
        while out.exists() or any(j.out_rel == str(out.relative_to(self.media.root_dir("subclip").resolve()))
                                  for j in self.jobs if j.state in ("queued", "waiting", "running")):
            k += 1
            out = out_dir / f"{name}-{k}.{sc['container']}"
        out_rel = str(out.relative_to(self.media.root_dir("subclip").resolve()))
        job = Job(source, tin, tout, folder, n, out_rel)
        self.jobs.append(job)
        self.jobs = [j for j in self.jobs if j.state in ("queued", "waiting", "running")] + \
            [j for j in self.jobs if j.state not in ("queued", "waiting", "running")][-HISTORY:]
        log.info("job %d queued: %s [%.3f - %.3f] -> %s", job.id, source, tin, tout, out_rel)
        return job

    def cancel(self, job_id: int):
        for j in self.jobs:
            if j.id == job_id:
                if j.state in ("queued", "waiting"):
                    j.state = "canceled"
                elif j.state == "running":
                    j.state = "canceled"
                    if j.proc:
                        asyncio.create_task(j.proc.stop(timeout=2))
                return
        raise MediaError("작업이 없습니다")

    def clear_finished(self):
        self.jobs = [j for j in self.jobs if j.state in ("queued", "waiting", "running")]

    # ---------- command ----------
    def build_args(self, src: Path, out: Path, tin: float, dur: float) -> list[str]:
        sc = self.cfg.section("subclip")
        ff = self.cfg.section("ffmpeg")
        codec = sc["vcodec"]
        nv = "nvenc" in codec
        a = [ff["ffmpeg"], "-hide_banner", "-nostdin", "-nostats", "-progress", "pipe:1",
             "-loglevel", "level+info", "-y"]
        if nv:
            a += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        # -t as an input option: the cut happens before fps/scale filters, so even a 1-2 frame
        # subclip keeps its video (an output -t with an fps filter could drop every frame)
        a += ["-ss", f"{tin:.4f}", "-t", f"{dur:.4f}", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?"]

        vf = []
        if sc.get("fps", "source") != "source":
            vf.append(f"fps={sc['fps']}")
        if sc.get("resolution", "source") != "source":
            w, h = sc["resolution"].split("x")
            vf.append(f"scale_cuda={w}:{h}" if nv else f"scale={w}:{h}")
        if vf:
            a += ["-vf", ",".join(vf)]

        a += ["-c:v", codec, "-preset", str(sc["preset"])]
        if sc.get("profile") and codec != "hevc_nvenc":
            a += ["-profile:v", sc["profile"]]
        rc = sc.get("rc", "vbr")
        if nv:
            if sc.get("tune"):
                a += ["-tune", sc["tune"]]
            if rc == "cq":
                a += ["-rc", "vbr", "-cq", str(sc.get("cq", 23)), "-b:v", "0"]
            else:
                a += ["-rc", rc, "-b:v", sc["bitrate"]]
            if sc.get("spatial_aq"):
                a += ["-spatial-aq", "1"]
        else:
            if sc.get("tune"):
                a += ["-tune", sc["tune"]]
            if rc == "cq":
                a += ["-crf", str(sc.get("cq", 23))]
            else:
                a += ["-b:v", sc["bitrate"]]
                if rc == "cbr":
                    a += ["-x264-params", "nal-hrd=cbr"]
            a += ["-pix_fmt", "yuv420p"]
        if rc != "cq" or not nv:
            if sc.get("maxrate"):
                a += ["-maxrate", sc["maxrate"]]
            if sc.get("bufsize"):
                a += ["-bufsize", sc["bufsize"]]
        a += ["-g", str(sc.get("gop", 60)), "-bf", str(sc.get("bframes", 2))]

        gain = float(sc.get("audio_gain", 1.0))
        if abs(gain - 1.0) > 1e-6:
            a += ["-af", f"volume={gain:.4f}"]
        a += ["-c:a", sc["acodec"]]
        if sc["acodec"] not in ("pcm_s16le", "pcm_s24le"):
            a += ["-b:a", sc["abitrate"]]
        fmt = CONTAINER_FMT.get(sc["container"], "mp4")
        if fmt in ("mp4", "mov") and sc.get("faststart"):
            a += ["-movflags", "+faststart"]
        a += ["-f", fmt, str(out)]
        return a

    # ---------- dispatcher ----------
    async def _dispatch(self):
        """Start queued jobs in order while fewer than limit() run. A job whose growing source
        does not hold the out point yet waits without taking a slot."""
        while True:
            running = sum(1 for j in self.jobs if j.state == "running")
            rec = self.media.recording_rels()
            for job in sorted(self.jobs, key=lambda j: j.id):
                if running >= self.limit():
                    break
                if job.state not in ("queued", "waiting"):
                    continue
                if job.source in rec and rec[job.source] < job.tout + GROW_MARGIN:
                    job.state = "waiting"
                    continue
                job.state, job.started = "running", time.time()
                running += 1
                asyncio.create_task(self._run_job(job))
            await asyncio.sleep(0.5)

    async def _run_job(self, job: Job):
        try:
            await self._run(job)
        except Exception as e:
            log.exception("job %d", job.id)
            job.state, job.error = "error", str(e)
        job.finished = time.time()
        self.media.bump()

    async def _run(self, job: Job):
        src = self.media.resolve("capture", job.source)
        out = self.media.resolve("subclip", job.out_rel)
        dur = job.tout - job.tin
        job.proc = FFmpegProcess(f"subclip:{job.id}", self.build_args(src, out, job.tin, dur))
        if job.state == "canceled":   # canceled between dispatch and start
            return
        await job.proc.start()
        waiter = asyncio.create_task(job.proc.wait())
        while not waiter.done():
            await asyncio.wait({waiter}, timeout=0.5)
            job.progress = min(100.0, job.proc.out_time() / dur * 100) if dur else 0
            job.speed = job.proc.progress.get("speed", "")
        rc = job.proc.returncode
        if job.state == "canceled":
            out.unlink(missing_ok=True)
            log.info("job %d canceled", job.id)
        elif rc == 0:
            job.state, job.progress = "done", 100.0
            log.info("job %d done -> %s (%.1fs)", job.id, job.out_rel, time.time() - job.started)
        else:
            job.state, job.error = "error", job.proc.error_text()
            out.unlink(missing_ok=True)
            log.error("job %d failed rc=%s: %s", job.id, rc, job.error)

    def status(self) -> list[dict]:
        return [j.as_dict() for j in self.jobs]

    async def stop_all(self):
        if self.task:
            self.task.cancel()
        await asyncio.gather(*(j.proc.stop(2) for j in self.jobs if j.proc and j.proc.running),
                             return_exceptions=True)
