"""Subclip encoder queue: cut [in, out) of the hires clip with the subclip codec settings.

Marks are editor frames on the proxy timeline. Proxy frames carry the pts of the hires frames they show
(same filter graph while recording, same input when built later), so a mark is turned into the source pts
of the frame the editor showed there. The cut itself is done on those timestamps with trim/atrim after an
early input seek: an input -ss lands on the next decodable picture (up to a GOP after the seek point) and
an input -t counts from there, which shifted whole subclips by up to ~0.5 s.
"""
import asyncio
import itertools
import json
import logging
import math
import os
import re
import time
from pathlib import Path, PurePosixPath

from .config import ROOT, Config
from .ffutil import FFmpegProcess, probe, run_capture
from .ffutil import interlaced as is_interlaced
from .media import Media, MediaError

log = logging.getLogger("encoder")

STATE_PATH = ROOT / "state.json"   # runtime state (subclip counters), not configuration
GROW_MARGIN = 3.0       # seconds of hires that must exist beyond the out point of a growing clip
HISTORY = 100
CONTAINER_FMT = {"mp4": "mp4", "mov": "mov", "mkv": "matroska", "ts": "mpegts"}
PREROLL = 5.0   # seconds decoded before the in point, so the input seek lands before it (GOP < PREROLL)
FIELD_CUT = 0.3  # scene score that counts as a cut inside the in frame (see in_after_field_cut)


def rate(r) -> float:
    """Frame rate string ("60000/1001", "25") -> frames per second; 0 when unknown."""
    try:
        n, _, d = str(r).partition("/")
        return float(n) / float(d or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


DEINTERLACERS = ("bwdif", "yadif")   # subclip "deinterlace": the filter for an interlaced source (CUDA twin with NVENC)


def deinterlacer(sc: dict) -> str:
    """configured filter; older configs ("auto" / "off") get bwdif: an interlaced source is always deinterlaced"""
    return sc.get("deinterlace") if sc.get("deinterlace") in DEINTERLACERS else "bwdif"


def per_field(fps: str, src_fps: float) -> bool:
    """Interlaced source: deinterlace to one frame per field (59.94p from 1080i) unless a lower rate is set."""
    return fps == "source" or rate(fps) > src_fps + 0.01


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
        self.counter_rev = 0
        self.no_bframes: dict[str, bool] = {}   # codec -> the GPU's encoder takes no B-frames
        try:
            self.counters = json.loads(STATE_PATH.read_text()).get("counters", {})
        except (OSError, ValueError):
            pass

    def start(self):
        self.media.busy_outputs = lambda: {j.out_rel for j in self.jobs if j.state in ("queued", "waiting", "running")}
        self.media.busy_sources = lambda: {j.source for j in self.jobs if j.state in ("queued", "waiting", "running")}
        self.task = asyncio.create_task(self._dispatch())

    def limit(self) -> int:
        """Concurrent encodes; read every pass so a UI change applies immediately."""
        return max(1, min(8, int(self.cfg.section("subclip").get("workers", 1))))

    # ---------- naming / counter ----------
    def _save_counters(self):
        self.counter_rev += 1   # pushed in /ws status so every browser re-reads its "next" number
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
    def source_pts(self, t: float, src_start: float, timing: tuple[float, float] | None) -> float:
        """Editor time of a frame start (k / proxy fps) -> pts of the source frame the editor shows there.

        The editor shows frame k by seeking the player to (k + 0.5) frames; the player clock starts at
        timing[0], proxy frames sit at timing[1] + j frames, and the last one not after that point is shown.
        """
        r = rate(self.cfg.section("record_proxy")["fps"])
        k = round(t * r)
        if not timing:
            return src_start + k / r
        zero, v0 = timing
        return v0 + math.floor((zero + (k + 0.5) / r - v0) * r + 1e-6) / r

    async def _scores(self, src: Path, t: float, src_start: float, h: float, deint: str = "") -> list[float]:
        """Scene scores of the three frames t - h, t, t + h (h = one frame, after deint if given)."""
        x = max(0.0, t - src_start - 3)
        rel = t - src_start - x
        rc, out, _ = await run_capture([
            self.cfg.section("ffmpeg")["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error",
            "-ss", f"{x:.4f}", "-t", f"{rel + 1:.4f}", "-i", str(src), "-map", "0:v:0",
            "-vf", f"{deint}trim=start={rel - 1.5 * h:.6f}:end={rel + 1.5 * h:.6f},scale=320:-2,"
                   "select='gte(scene,0)',metadata=print:file=-", "-an", "-f", "null", "-"], timeout=30)
        scores = [float(l.split("=", 1)[1]) for l in out.splitlines() if l.startswith("lavfi.scene_score=")]
        return scores if rc == 0 and len(scores) == 3 else []

    async def in_after_field_cut(self, src: Path, t0: float, src_start: float, src_fps: float) -> float:
        """Move the in point one source frame on when a cut falls inside the in frame.

        With a 59.94 source each editor (proxy) frame stands for two source frames, t0 and t0 + 1/59.94,
        and shows the second. A cut on a field boundary falls between them: the editor shows the new shot
        but the source frame at t0 is still the old one, which would open the subclip with a one-frame
        flash of the previous shot. (The out side needs nothing: the subclip ends before the pair.)
        """
        r = rate(self.cfg.section("record_proxy")["fps"])
        if not src_fps or src_fps < r * 1.5:
            return t0
        h = 1 / src_fps
        scores = await self._scores(src, t0, src_start, h)
        # frames t0 - h, t0, t0 + h: the cut is between t0 and t0 + h, not before t0
        if scores and scores[2] >= FIELD_CUT > scores[1]:
            log.info("in point on a field cut: starting one source frame later (%.4f)", t0 + h)
            return t0 + h
        return t0

    async def out_before_field_cut(self, src: Path, t1: float, src_start: float, src_fps: float) -> float:
        """Interlaced source encoded one frame per field: drop the last field when a cut falls inside
        the last frame.

        The editor shows the first field of each frame of a 1080i clip (a TS decoded in the browser). A cut
        between the two fields of the last frame is not visible in the editor (that frame still shows the
        old shot), but its second field already is the new shot and would end the subclip with a one-field
        flash.
        """
        h = 0.5 / src_fps
        scores = await self._scores(src, t1 - h, src_start, h, "bwdif=mode=1:deint=interlaced,")
        # fields t1 - 2h, t1 - h, t1: the cut is between the two fields of the last frame, not at t1
        if scores and scores[1] >= FIELD_CUT > scores[2]:
            log.info("out point on a field cut: ending one field earlier (%.4f)", t1 - h)
            return t1 - h
        return t1

    def build_args(self, src: Path, out: Path, t0: float, t1: float, src_fps: float = 0,
                   src_start: float = 0, audio: bool = True, interlaced: bool = False) -> list[str]:
        """Encode source pts [t0, t1): every frame from the one at t0 up to the one before t1."""
        sc = self.cfg.section("subclip")
        fps = sc.get("fps", "source")
        deint = None
        if interlaced and src_fps:
            # 1080i: one frame per field (mode=1, 59.94p) unless 29.97 is asked for, then one per frame
            # (mode=0, keeping the first field). From here on the source counts as that progressive rate.
            field = per_field(fps, src_fps)
            deint = f"mode={1 if field else 0}:deint=interlaced"
            src_fps = src_fps * 2 if field else src_fps
        ff = self.cfg.section("ffmpeg")
        codec = sc["vcodec"]
        nv = "nvenc" in codec
        a = [ff["ffmpeg"], "-hide_banner", "-nostdin", "-nostats", "-progress", "pipe:1",
             "-loglevel", "level+info", "-y"]
        if nv:
            a += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        # seek early, then cut exactly on timestamps. After an input seek to x, timestamps start at
        # x (+ the file's start time) = 0. The video bounds sit half a source frame early so the frames
        # at t0 / t1 are on the right side of them; trim comes before fps/scale, so even a 1-2 frame
        # subclip keeps its video. The input -t only bounds reading (it counts from the first decoded frame).
        x = max(0.0, t0 - src_start - PREROLL)
        rel = lambda p: p - src_start - x
        half = 0.5 / (src_fps or rate(self.cfg.section("record_proxy")["fps"]))
        a += ["-ss", f"{x:.4f}", "-t", f"{rel(t1) + 1:.4f}", "-i", str(src), "-map", "0:v:0"]
        if audio:
            a += ["-map", "0:a:0"]

        vf = [f"trim=start={rel(t0) - half:.6f}:end={rel(t1) - half:.6f}", "setpts=PTS-STARTPTS"]
        if deint:   # before trim: the deinterlacer needs the neighbouring frames, the pre-roll has them
            vf.insert(0, deinterlacer(sc) + ("_cuda=" if nv else "=") + deint)
        # never up-convert: a 29.97 recording gives a 29.97 subclip whatever the setting says
        if fps != "source" and not (src_fps and rate(fps) > src_fps + 0.01):
            vf.append(f"fps={fps}")
        if sc.get("resolution", "source") != "source":
            w, h = sc["resolution"].split("x")
            vf.append(f"scale_cuda={w}:{h}" if nv else f"scale={w}:{h}")
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
        bf = 0 if self.no_bframes.get(codec) else sc.get("bframes", 2)
        a += ["-g", str(sc.get("gop", 60)), "-bf", str(bf)]

        if audio:
            # audio from the first video frame's pts, so both streams start together at 0
            af = [f"atrim=start={rel(t0):.6f}:end={rel(t1):.6f}", "asetpts=PTS-STARTPTS"]
            gain = float(sc.get("audio_gain", 1.0))
            if abs(gain - 1.0) > 1e-6:
                af.append(f"volume={gain:.4f}")
            a += ["-af", ",".join(af), "-c:a", sc["acodec"]]
            if sc["acodec"] not in ("pcm_s16le", "pcm_s24le"):
                a += ["-b:a", sc["abitrate"]]
        fmt = CONTAINER_FMT.get(sc["container"], "mp4")
        if fmt in ("mp4", "mov") and sc.get("faststart"):
            a += ["-movflags", "+faststart"]
        if fmt in ("mp4", "mov") and "hevc" in codec:
            a += ["-tag:v", "hvc1"]   # ffmpeg writes hev1; Safari, QuickTime and some browsers only play hvc1
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

    async def _check_bframes(self, codec: str, bframes: int):
        """HEVC NVENC before Turing (e.g. Quadro P2200) has no B-frames and refuses to open with any:
        found out once per codec with 3-frame test encodes, then encoded with -bf 0 (on the GPU still,
        never another encoder). Only a GPU that encodes without B-frames but not with them counts as
        lacking them; when both tests fail (GPU busy, driver...) nothing is decided and the job runs as set."""
        if codec in self.no_bframes or "nvenc" not in codec or bframes <= 0:
            return

        async def test(bf):
            rc, _, err = await run_capture([self.cfg.section("ffmpeg")["ffmpeg"], "-v", "error", "-f", "lavfi", "-i",
                                            "testsrc2=s=1280x720", "-frames:v", "3", "-c:v", codec, "-bf", str(bf),
                                            "-f", "null", "-"], timeout=20)
            return rc == 0, err.strip()[-200:]
        ok, err = await test(bframes)
        if ok:
            self.no_bframes[codec] = False
        elif (await test(0))[0]:
            self.no_bframes[codec] = True
            log.info("%s takes no B-frames on this GPU: encoding with -bf 0 (%s)", codec, err)
        else:
            log.warning("%s test encode failed with and without B-frames, left undecided: %s", codec, err)

    async def _run(self, job: Job):
        src = self.media.resolve("capture", job.source)
        out = self.media.resolve("subclip", job.out_rel)
        dur = job.tout - job.tin
        info = await probe(self.cfg.section("ffmpeg")["ffprobe"], src)
        streams = info.get("streams", [])
        v = next((s for s in streams if s.get("codec_type") == "video"), {})
        start = info.get("start_time", 0.0)
        timing = await self.media.proxy_timing(job.source)
        await self._check_bframes(self.cfg.section("subclip")["vcodec"], int(self.cfg.section("subclip").get("bframes", 2)))
        t0, t1 = self.source_pts(job.tin, start, timing), self.source_pts(job.tout, start, timing)
        t0 = await self.in_after_field_cut(src, t0, start, rate(v.get("r_frame_rate")))
        sc = self.cfg.section("subclip")
        if is_interlaced(v) and per_field(sc.get("fps", "source"), rate(v.get("r_frame_rate"))):
            t1 = await self.out_before_field_cut(src, t1, start, rate(v.get("r_frame_rate")))
        job.proc = FFmpegProcess(f"subclip:{job.id}", self.build_args(
            src, out, t0, t1, rate(v.get("r_frame_rate")), start, any(s.get("codec_type") == "audio" for s in streams),
            is_interlaced(v)))
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
