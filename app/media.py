"""Host media storage: layout of hires / proxy / thumbnail, browser operations, proxy generation.

Layout for a hires clip  <record>/<dir>/<name>.<ext>   (rel = "<dir>/<name>.<ext>")
    proxy                <proxy>/<dir>/<name>/index.m3u8 (+ seg_NNNNNN.ts)
    thumbnails           <thumbnail>/<dir>/<name>/NNNNNN.jpg (+ meta.json)
"""
import asyncio
import contextlib
import json
import logging
import math
import os
import re
import shutil
import time
from pathlib import Path, PurePosixPath

from .config import Config
from .ffutil import MEDIA_EXTS, FFmpegProcess, close_playlist, hls_info, interlaced, probe, probe_cached, run_capture
from .tsindex import TS_EXTS, TsError, TsFile, TsIndex, is_ts

log = logging.getLogger("media")

ROOTS = ("capture", "subclip")
_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class MediaError(Exception):
    pass


def clean_name(name: str) -> str:
    name = name.strip()
    if not name or name in (".", "..") or _BAD_NAME.search(name):
        raise MediaError(f"사용할 수 없는 이름: {name!r}")
    return name


class Media:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rev = 0                     # bumped on every change -> clients refresh lists
        self.recording_rels = lambda: {}  # set by recorder: rel -> elapsed seconds
        self.busy_outputs = lambda: set()   # set by encoder: subclip rels still being written
        self.busy_sources = lambda: set()   # set by encoder: capture rels that pending jobs still read
        self.proxy_jobs: dict[str, FFmpegProcess] = {}
        self.proxy_progress: dict[str, dict] = {}
        self.proxy_errors: dict[str, str] = {}   # rel -> last generation failure
        self._timing: dict[tuple, tuple[float, float]] = {}   # (first segment, mtime) -> proxy_timing
        self.ts = TsIndex()              # GOP locators of TS clips the browser decodes
        self._probing: dict[Path, float] = {}   # file -> when a listing last probed it in the background

    # ---------- paths ----------
    def root_dir(self, root: str) -> Path:
        if root == "capture":
            return self.cfg.path_of("record")
        if root == "subclip":
            return self.cfg.path_of("subclip")
        raise MediaError(f"unknown root {root}")

    def resolve(self, root: str, rel: str) -> Path:
        # checked on the path as written, not on its target: a symlink placed under the root (e.g. to
        # a tvheadend share) is browsable, while ".." can still never leave the root
        base = self.root_dir(root).resolve()
        rel = (rel or "").strip("/")
        p = Path(os.path.normpath(base / rel)) if rel else base
        if p != base and base not in p.parents:
            raise MediaError("경로가 허용 범위를 벗어났습니다")
        return p

    def _hidden_dirs(self) -> set[Path]:
        """proxy/thumbnail live inside the capture dir by default: never list them as folders."""
        return {self.cfg.path_of("proxy").resolve(), self.cfg.path_of("thumbnail").resolve()}

    def proxy_dir(self, rel: str) -> Path:
        return self.cfg.path_of("proxy") / str(PurePosixPath(rel).with_suffix(""))

    def thumb_dir(self, rel: str) -> Path:
        return self.cfg.path_of("thumbnail") / str(PurePosixPath(rel).with_suffix(""))

    def proxy_url(self, rel: str) -> str:
        return "/media/proxy/" + str(PurePosixPath(rel).with_suffix("")) + "/index.m3u8"

    def thumb_url(self, rel: str) -> str:
        return "/media/thumb/" + str(PurePosixPath(rel).with_suffix("")) + "/"

    def bump(self):
        self.rev += 1

    def ensure_dirs(self):
        for k in ("record", "proxy", "subclip", "thumbnail"):
            self.cfg.path_of(k).mkdir(parents=True, exist_ok=True)

    def close_stale_playlists(self):
        """At startup nothing records: every EVENT playlist must be closed."""
        proxy = self.cfg.path_of("proxy")
        if proxy.exists():
            for m3u8 in proxy.rglob("index.m3u8"):
                close_playlist(m3u8)

    # ---------- listing ----------
    async def list_dir(self, root: str, rel: str) -> dict:
        d = self.resolve(root, rel)
        if not d.is_dir():
            raise MediaError("폴더가 없습니다")
        base = self.root_dir(root).resolve()
        hidden = self._hidden_dirs() if root == "capture" else set()
        recording = self.recording_rels() if root == "capture" else {}
        folders, files, later = [], [], []
        for e in sorted(os.scandir(d), key=lambda e: e.name.lower()):
            if e.name.startswith("."):
                continue
            p = Path(e.path)
            r = str(p.relative_to(base))
            try:
                st = e.stat()
            except OSError:
                continue
            if e.is_dir():
                if p.resolve() in hidden:
                    continue
                folders.append({"name": e.name, "path": r, "mtime": st.st_mtime})
                continue
            if p.suffix.lower() not in MEDIA_EXTS:
                continue
            item = {"name": e.name, "path": r, "size": st.st_size, "mtime": st.st_mtime}
            # a folder of hundreds of new files (e.g. a tvheadend share, ~0.1 s of ffprobe each) lists
            # at once; the durations follow from a background probe, which bumps rev when done
            info = probe_cached(p)
            if info is None and r not in recording:
                later.append(p)
            if root == "capture":
                item.update(self._clip_meta(r, recording, info or {}))
            else:
                item["duration"] = (info or {}).get("duration")
            files.append(item)
        if later:
            self._probe_later(later)
        files.sort(key=lambda f: f["mtime"], reverse=True)
        return {"root": root, "path": str(d.relative_to(base)) if d != base else "",
                "folders": folders, "files": files, "rev": self.rev}

    def _probe_later(self, paths: list[Path]):
        # at most once a minute per file: one still being copied in changes on every listing, and each
        # background probe ends in a bump that lists the folder again
        now = time.monotonic()
        self._probing = {p: t for p, t in self._probing.items() if now - t < 60}
        paths = [p for p in paths if p not in self._probing]
        if not paths:
            return
        self._probing.update(dict.fromkeys(paths, now))
        ffprobe = self.cfg.section("ffmpeg")["ffprobe"]

        async def run():
            sem = asyncio.Semaphore(8)

            async def one(p):
                async with sem:
                    await probe(ffprobe, p)
            try:
                await asyncio.gather(*(one(p) for p in paths))
            finally:
                self.bump()
        asyncio.create_task(run())

    def _clip_meta(self, rel, recording, info: dict) -> dict:
        m3u8 = self.proxy_dir(rel) / "index.m3u8"
        hls = hls_info(m3u8)
        meta = {"recording": rel in recording, "has_proxy": hls is not None and hls["segments"] > 0,
                "proxy_building": rel in self.proxy_jobs}
        meta["ts"] = not (meta["has_proxy"] or meta["proxy_building"] or meta["recording"]) and \
            self.browser_decode() and rel.lower().endswith(TS_EXTS)
        if rel in recording:
            meta["duration"] = recording[rel]
        elif hls and hls["ended"]:
            meta["duration"] = hls["duration"]
        else:
            meta["duration"] = info.get("duration")
        return meta

    async def clip_info(self, rel: str) -> dict:
        p = self.resolve("capture", rel)
        if not p.is_file():
            raise MediaError("파일이 없습니다")
        rel = str(p.relative_to(self.root_dir("capture").resolve()))
        recording = self.recording_rels()
        hls = hls_info(self.proxy_dir(rel) / "index.m3u8")
        meta = {"interval": self.cfg.section("thumbnail")["interval"]}
        try:
            meta.update(json.loads((self.thumb_dir(rel) / "meta.json").read_text()))
        except (OSError, ValueError):
            pass
        info = await probe(self.cfg.section("ffmpeg")["ffprobe"], p)
        if not (hls and hls["segments"] > 0) and rel not in self.proxy_jobs and rel not in recording:
            tf = await self.ts_file(p)
            if tf:   # no proxy: the browser decodes the TS itself (web/tsplayer.js), no thumbnails
                return {"path": rel, "name": p.name, "recording": False, "duration": tf.frames / self.editor_rate(),
                        "proxy": None, "proxy_ended": True, "proxy_building": False, "proxy_error": "",
                        "ts": {**tf.info(), "path": rel}, "thumb": None, "hires": info}
        return {
            "path": rel, "name": p.name, "recording": rel in recording,
            "duration": recording.get(rel) or (hls or {}).get("duration") or info.get("duration"),
            "proxy": self.proxy_url(rel) if hls and hls["segments"] > 0 else None,
            "proxy_ended": bool(hls and hls["ended"]),
            "proxy_building": rel in self.proxy_jobs,
            "proxy_error": self.proxy_errors.get(rel, ""),
            "thumb": {"base": self.thumb_url(rel), "interval": meta["interval"]},
            "hires": info,
        }

    def browser_decode(self) -> bool:
        return bool(self.cfg.section("editor").get("browser_decode", True))

    def editor_rate(self) -> float:
        n, _, d = str(self.cfg.section("record_proxy")["fps"]).partition("/")
        return float(n) / float(d or 1)

    async def ts_file(self, p: Path) -> TsFile | None:
        """GOP locator of a TS the browser can decode for the editor (MPEG-2 on the editor's frame rate)."""
        if not self.browser_decode() or not p.is_file() or not is_ts(p):
            return None
        try:
            tf = await asyncio.to_thread(self.ts.get, p)
        except (TsError, OSError) as e:
            log.warning("%s: no browser decode: %s", p.name, e)
            return None
        num, den = tf.seq["fps"]
        return tf if abs(num / den - self.editor_rate()) < 0.01 else None

    async def ts_clip(self, rel: str) -> TsFile | None:
        """ts_file of a capture clip that has no proxy (the one the editor decodes in the browser)."""
        if hls_info(self.proxy_dir(rel) / "index.m3u8"):
            return None
        return await self.ts_file(self.resolve("capture", rel))

    async def proxy_timing(self, rel: str) -> tuple[float, float] | None:
        """(player zero, proxy video start) as source pts, from the first proxy segment; None without one.

        The player's clock starts at the earliest stream of the proxy (audio, ~0.02 s before the video),
        and proxy frames carry the pts of the source frames they show (same filter graph / same input).
        """
        seg = self.proxy_dir(rel) / "seg_000000.ts"
        try:
            key = (str(seg), seg.stat().st_mtime)
        except OSError:
            tf = await self.ts_clip(rel)   # browser-decoded TS: editor frame k = source frame at pts0 + k
            return (tf.pts0 / 90000, tf.pts0 / 90000) if tf else None
        if key in self._timing:
            return self._timing[key]
        rc, out, _ = await run_capture([self.cfg.section("ffmpeg")["ffprobe"], "-v", "error", "-show_entries",
                                        "stream=codec_type,start_time", "-of", "csv=p=0", str(seg)], timeout=10)
        starts = {}
        for line in out.splitlines():
            kind, _, st = line.partition(",")
            with contextlib.suppress(ValueError):
                starts.setdefault(kind, float(st))
        if "video" not in starts:
            return None
        self._timing[key] = (min(starts.values()), starts["video"])
        return self._timing[key]

    async def scenes(self, rel: str, t: float, before: float, after: float) -> dict:
        """Scene-change score of every editor frame in [t - before, t + after].

        Read from the proxy, because that is exactly what the editor shows: editor frame k is the k-th
        frame of the proxy HLS, found by counting frames through the segment durations. (Decimating the
        59.94 hires to 29.97 after a seek can pick the other field-frame of a pair and land a cut one
        frame off.) Score of a frame = how different it is from the one before (ffmpeg "scene", 0..1):
        a cut shows as one high frame, the first of the new shot.
        """
        p = self.resolve("capture", rel)
        if not p.is_file():
            raise MediaError("파일이 없습니다")
        rel = str(p.relative_to(self.root_dir("capture").resolve()))
        fps = self.cfg.section("record_proxy")["fps"]
        n, _, d = str(fps).partition("/")
        rate = float(n) / float(d or 1)
        before, after = max(0.5, min(30.0, before)), max(0.5, min(30.0, after))
        f0 = max(1, round((t - before) * rate))   # frame 0 has no predecessor, so no score
        f1 = round((t + after) * rate)
        pdir = self.proxy_dir(rel)
        segs, start, dur = [], 0, None   # (file, first frame, frames)
        try:
            lines = (pdir / "index.m3u8").read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        if not lines:
            tf = await self.ts_clip(rel)
            if not tf:
                raise MediaError("프록시가 없습니다")
            return await self._ts_scenes(p, tf, f0, f1, rate, fps)
        for line in lines:
            if line.startswith("#EXTINF:"):
                dur = float(line[8:].split(",")[0])
            elif line and not line.startswith("#") and dur is not None:
                k = round(dur * rate)
                segs.append((pdir / line, start, k))
                start, dur = start + k, None
        # the segments holding f0 - 1 .. f1 (one frame before the range, to score its first frame)
        use = [sg for sg in segs if sg[1] + sg[2] > f0 - 1 and sg[1] <= f1]
        if not use:
            raise MediaError("그 구간의 프록시가 아직 없습니다")
        # editor frame k shows the proxy frame at (k + 0.5) / rate on the player clock, so proxy frame p
        # first shows at editor frame ceil(p + offset - 0.5), offset = video start on that clock in frames
        tm = await self.proxy_timing(rel)
        shift = max(0, math.ceil((tm[1] - tm[0]) * rate - 0.5 - 1e-6)) if tm else 0
        first = use[0][1] + shift
        args = [self.cfg.section("ffmpeg")["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error",
                "-i", "concat:" + "|".join(str(sg[0]) for sg in use), "-map", "0:v:0",
                "-vf", "scale=320:-2,select='gte(scene,0)',metadata=print:file=-", "-an", "-f", "null", "-"]
        rc, out, err = await run_capture(args, timeout=60)
        if rc != 0:
            raise MediaError(f"scene 검출 실패: {err.strip()[-200:]}")
        frames, k = [], None
        for line in out.splitlines():
            if line.startswith("frame:"):
                k = first + int(line.split()[0][6:])   # n-th decoded frame of the first used segment
            elif line.startswith("lavfi.scene_score=") and k is not None and f0 <= k <= f1:
                frames.append([k, round(float(line.split("=", 1)[1]), 4)])
        return {"fps": fps, "from": f0, "to": min(f1, start - 1), "frames": frames}

    async def _ts_scenes(self, p: Path, tf: TsFile, f0: int, f1: int, rate: float, fps) -> dict:
        """scenes() of a browser-decoded TS, read from the source itself: editor frame k is the source frame
        at pts0 + k frames, and of an interlaced frame the editor shows the first field, so that is scored."""
        info = await probe(self.cfg.section("ffmpeg")["ffprobe"], p)
        v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        v0, f1 = tf.pts0 / 90000, min(f1, tf.frames - 1)
        x = max(0.0, v0 + (f0 - 1) / rate - info.get("start_time", 0.0) - 3)
        field = ("field=type=bottom," if v.get("field_order") in ("bb", "bt") else "field=type=top,") \
            if interlaced(v) else ""
        args = [self.cfg.section("ffmpeg")["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error",
                "-ss", f"{x:.4f}", "-t", f"{(f1 - f0 + 2) / rate + 3.5:.4f}", "-i", str(p), "-copyts",
                "-map", "0:v:0", "-vf", f"{field}scale=320:-2,select='gte(scene,0)',metadata=print:file=-",
                "-an", "-f", "null", "-"]
        rc, out, err = await run_capture(args, timeout=60)
        if rc != 0:
            raise MediaError(f"scene 검출 실패: {err.strip()[-200:]}")
        frames, k = [], None
        for line in out.splitlines():
            if line.startswith("frame:"):
                k = round((float(line.rsplit("pts_time:", 1)[1]) - v0) * rate)
            elif line.startswith("lavfi.scene_score=") and k is not None and f0 <= k <= f1:
                frames.append([k, round(float(line.split("=", 1)[1]), 4)])
        return {"fps": fps, "from": f0, "to": f1, "frames": frames}

    # ---------- operations ----------
    def _check_not_recording(self, root, rel):
        if root != "capture":
            for r in self.busy_outputs():
                if r == rel or r.startswith(rel.rstrip("/") + "/"):
                    raise MediaError("인코딩 중인 subclip 은 변경할 수 없습니다")
            return
        under = lambda r: r == rel or r.startswith(rel.rstrip("/") + "/")
        if any(under(r) for r in self.recording_rels()):
            raise MediaError("녹화 중인 클립은 변경할 수 없습니다")
        if any(under(r) for r in self.busy_sources()):
            raise MediaError("subclip 작업(대기/인코딩)이 남아 있는 클립은 변경할 수 없습니다")
        if rel in self.proxy_jobs:
            raise MediaError("프록시 생성 중인 클립은 변경할 수 없습니다")

    def _check_local(self, root, p: Path):
        """Files reached through a symlink to another place (a tvheadend share...) are read-only here."""
        base, real = self.root_dir(root).resolve(), p.resolve()
        if real != base and base not in real.parents:
            raise MediaError("링크로 연결된 외부 폴더는 변경할 수 없습니다")

    def mkdir(self, root, rel, name):
        self._check_local(root, self.resolve(root, rel))
        d = self.resolve(root, rel) / clean_name(name)
        if d.exists():
            raise MediaError("이미 존재합니다")
        d.mkdir(parents=True)
        log.info("mkdir %s:%s", root, d)
        self.bump()

    def _move_side(self, src_rel: str, dst_rel: str):
        """Keep proxy / thumbnail trees in step with the hires file or folder."""
        for f in (self.proxy_dir, self.thumb_dir):
            s, t = f(src_rel), f(dst_rel)
            if s.exists():
                t.parent.mkdir(parents=True, exist_ok=True)
                os.replace(s, t)

    def rename(self, root, rel, new_name):
        if not rel:
            raise MediaError("루트는 변경할 수 없습니다")
        self._check_not_recording(root, rel)
        p = self.resolve(root, rel)
        if not p.exists():
            raise MediaError("파일이 없습니다")
        self._check_local(root, p)
        new_name = clean_name(new_name)
        if p.is_file() and not Path(new_name).suffix:
            new_name += p.suffix
        t = p.with_name(new_name)
        if t.exists():
            raise MediaError("같은 이름이 이미 있습니다")
        base = self.root_dir(root).resolve()
        os.replace(p, t)
        if root == "capture":
            self._move_side(rel, str(t.relative_to(base)))
        log.info("rename %s:%s -> %s", root, rel, t.name)
        self.bump()

    def move(self, root, rels: list[str], dest: str) -> int:
        """Move files/folders into folder dest (same root). Capture clips take proxy/thumbnail along."""
        d = self.resolve(root, dest)
        if not d.is_dir():
            raise MediaError("대상 폴더가 없습니다")
        self._check_local(root, d)
        base = self.root_dir(root).resolve()
        todo = []
        for rel in rels:
            if not rel:
                raise MediaError("루트는 이동할 수 없습니다")
            self._check_not_recording(root, rel)
            p = self.resolve(root, rel)
            if not p.exists():
                raise MediaError(f"파일이 없습니다: {rel}")
            self._check_local(root, p)
            if p.is_dir() and (d == p or p in d.parents):
                raise MediaError("폴더를 자기 자신 안으로 옮길 수 없습니다")
            t = d / p.name
            if t == p:
                continue
            if t.exists():
                raise MediaError(f"대상 폴더에 같은 이름이 있습니다: {p.name}")
            todo.append((rel, p, t))
        for rel, p, t in todo:   # all checks passed: nothing is half-moved on a name clash
            os.replace(p, t)
            if root == "capture":
                self._move_side(rel, str(t.relative_to(base)))
            log.info("move %s:%s -> %s", root, rel, t.relative_to(base))
        self.bump()
        return len(todo)

    def delete(self, root, rels: list[str]):
        for rel in rels:
            if not rel:
                raise MediaError("루트는 삭제할 수 없습니다")
            self._check_not_recording(root, rel)
            p = self.resolve(root, rel)
            self._check_local(root, p)
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
            if root == "capture":
                for side in (self.proxy_dir(rel), self.thumb_dir(rel)):
                    if side.exists():
                        shutil.rmtree(side, ignore_errors=True)
            log.info("delete %s:%s", root, rel)
        self.bump()

    # ---------- proxy generation for clips that have none ----------
    async def make_proxy(self, rel: str):
        p = self.resolve("capture", rel)
        if not p.is_file():
            raise MediaError("파일이 없습니다")
        rel = str(p.relative_to(self.root_dir("capture").resolve()))
        if rel in self.proxy_jobs:
            return
        if rel in self.recording_rels():
            raise MediaError("녹화 중인 클립입니다")
        ff = self.cfg.section("ffmpeg")
        px, tcfg = self.cfg.section("record_proxy"), self.cfg.section("thumbnail")
        pdir, tdir = self.proxy_dir(rel), self.thumb_dir(rel)
        for d in (pdir, tdir):
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True)
        (tdir / "meta.json").write_text(json.dumps({"interval": tcfg["interval"]}))
        w, h = px["width"], px["height"]
        fc = (f"[0:v:0]bwdif=mode=0:deint=interlaced,scale={w}:{h}:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={px['fps']},format=yuv420p,split=2[p][t0];"
              f"[t0]fps=1/{tcfg['interval']},scale={tcfg['width']}:{tcfg['height']}[t]")
        args = [ff["ffmpeg"], "-hide_banner", "-nostdin", "-nostats", "-progress", "pipe:1",
                "-loglevel", "level+info", "-i", str(p), "-filter_complex", fc,
                "-map", "[p]", "-map", "0:a:0?", *video_args(px, "v"), "-ac", "2",
                "-c:a", px["acodec"], "-b:a", px["abitrate"], *hls_args(px, pdir),
                "-map", "[t]", "-c:v", "mjpeg", "-q:v", str(tcfg["quality"]), "-f", "image2",
                str(tdir / "%06d.jpg")]
        info = await probe(ff["ffprobe"], p)
        job = FFmpegProcess(f"proxy:{p.name}", args)
        self.proxy_jobs[rel] = job
        self.proxy_progress[rel] = {"name": p.name, "duration": info.get("duration") or 0}
        self.bump()
        await job.start()
        asyncio.create_task(self._proxy_done(rel, job))

    async def _proxy_done(self, rel, job):
        rc = await job.wait()
        close_playlist(self.proxy_dir(rel) / "index.m3u8")
        if rc != 0:
            self.proxy_errors[rel] = job.error_text() or f"ffmpeg rc={rc}"
            log.error("proxy failed %s: %s", rel, self.proxy_errors[rel])
        else:
            self.proxy_errors.pop(rel, None)
        self.proxy_jobs.pop(rel, None)
        self.proxy_progress.pop(rel, None)
        self.bump()

    def proxy_status(self) -> list[dict]:
        out = []
        for rel, job in self.proxy_jobs.items():
            meta = self.proxy_progress.get(rel, {})
            dur = meta.get("duration") or 0
            t = job.out_time()
            out.append({"path": rel, "name": meta.get("name"), "time": t,
                        "progress": min(100.0, t / dur * 100) if dur else None,
                        "speed": job.progress.get("speed")})
        return out

    async def stop_all(self):
        await asyncio.gather(*(j.stop() for j in self.proxy_jobs.values()), return_exceptions=True)


def video_args(c: dict, stream: str = "v") -> list[str]:
    """Encoder args for h264_nvenc / hevc_nvenc / libx264 from a record_* section."""
    codec = c["vcodec"]
    a = [f"-c:{stream}", codec]
    if "nvenc" in codec:
        a += ["-preset", c.get("preset", "p4")]
        if c.get("profile") and codec == "h264_nvenc":
            a += ["-profile:v", c["profile"]]
        a += ["-rc", c.get("rc", "cbr")]
    elif codec == "libx264":
        a += ["-preset", c.get("preset", "veryfast")]
        if c.get("profile"):
            a += ["-profile:v", c["profile"]]
        if c.get("rc") == "cbr":
            a += ["-x264-params", "nal-hrd=cbr"]
    # mpeg2video (CPU): no preset/profile; CBR = minrate == maxrate == bitrate
    a += ["-b:v", c["bitrate"]]
    if c.get("minrate"):
        a += ["-minrate", c["minrate"]]
    if c.get("threads"):
        a += ["-threads", str(c["threads"])]
    if c.get("maxrate"):
        a += ["-maxrate", c["maxrate"]]
    if c.get("bufsize"):
        a += ["-bufsize", c["bufsize"]]
    a += ["-g", str(c.get("gop", 30)), "-bf", str(c.get("bframes", 0))]
    if "nvenc" not in codec:  # nvenc gets nv12 (4:2:0) cuda frames; a CPU pix_fmt would break the hw chain
        a += ["-pix_fmt", "yuv420p"]
    return a


def hls_args(px: dict, pdir: Path) -> list[str]:
    return ["-f", "hls", "-hls_time", str(px.get("hls_time", 2)), "-hls_list_size", "0",
            "-hls_playlist_type", "event", "-hls_flags", "independent_segments",
            "-hls_segment_filename", str(pdir / "seg_%06d.ts"), str(pdir / "index.m3u8")]

