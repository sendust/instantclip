"""Live proxy: HLS for clips that have no proxy, transcoded segment by segment when the player asks.

For files recorded elsewhere (e.g. tvheadend 1080i MPEG-2 / AC-3, which no browser decodes): nothing is
built in advance. The playlist is computed from the file's length; segment n is the editor frames
[60n, 60n + 60), transcoded on request (deinterlaced, scaled like the recorded proxy) and cached.

Every segment is cut on the same source frame grid: frame j of the live proxy is the source frame at
v0 + j / 29.97, v0 = the first decodable video frame, and carries pts BASE + j / 29.97. Each segment
is a separate ffmpeg run, seeking early and cutting with trim / atrim exactly as the encoder does, so
the segments join without gaps and every one has the same audio lead (AAC priming) over its video.
"""
import asyncio
import contextlib
import hashlib
import logging
import os
import time
from pathlib import Path

from .config import Config
from .ffutil import probe, run_capture

log = logging.getLogger("live")

SEG_FRAMES = 60
PREROLL = 2.0   # seconds read before a segment, so its first frames decode (GOP 0.5 s on broadcast)
AHEAD = 2       # segments made in advance after each request
BASE = 10.0     # pts of frame 0: keeps the AAC priming of segment 0 off negative timestamps
INTERLACED = ("tt", "bb", "tb", "bt")


def rate(r) -> float:
    try:
        n, _, d = str(r).partition("/")
        return float(n) / float(d or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def interlaced(stream: dict) -> bool:
    return stream.get("field_order") in INTERLACED


class LiveProxy:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.clips: dict[str, dict] = {}            # key -> clip entry
        self.tasks: dict[tuple[str, int], asyncio.Task] = {}
        self.sem = asyncio.Semaphore(4)
        self.made = 0

    def enabled(self) -> bool:
        return bool(self.cfg.section("editor").get("live_proxy", True))

    def cache_dir(self) -> Path:
        return self.cfg.path_of("proxy") / ".live"

    @staticmethod
    def url(key: str) -> str:
        return f"/media/live/{key}/index.m3u8"

    async def entry(self, rel: str, p: Path) -> dict | None:
        """Register a clip (by rel + size + mtime) and return its entry; None if it has no video."""
        st = p.stat()
        key = hashlib.sha1(f"{rel}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]
        if key in self.clips:
            return self.clips[key]
        ff = self.cfg.section("ffmpeg")
        info = await probe(ff["ffprobe"], p)
        v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
        if not v:
            return None
        # first decodable video frame (a broadcast recording starts mid-GOP)
        rc, out, _ = await run_capture([ff["ffprobe"], "-v", "error", "-select_streams", "v:0", "-read_intervals",
                                        "%+5", "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(p)],
                                       timeout=20)
        v0 = None
        for line in out.splitlines():
            with contextlib.suppress(ValueError):
                v0 = float(line.strip(",").split(",")[0])
                break
        start = info.get("start_time", 0.0)
        if v0 is None:
            v0 = start
        r = rate(self.cfg.section("record_proxy")["fps"])
        frames = max(1, int((start + (info.get("duration") or 0) - v0) * r))
        e = {"key": key, "rel": rel, "path": p, "start": start, "v0": v0, "frames": frames, "rate": r,
             "src_fps": rate(v.get("r_frame_rate")) or r, "interlaced": interlaced(v),
             "audio": any(s.get("codec_type") == "audio" for s in info["streams"])}
        self.clips[key] = e
        return e

    def duration(self, e: dict) -> float:
        return e["frames"] / e["rate"]

    def playlist(self, key: str) -> str | None:
        e = self.clips.get(key)
        if not e:
            return None
        n, d = e["frames"], SEG_FRAMES / e["rate"]
        segs = (n + SEG_FRAMES - 1) // SEG_FRAMES
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{int(d) + 1}", "#EXT-X-MEDIA-SEQUENCE:0",
                 "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-INDEPENDENT-SEGMENTS"]
        for i in range(segs):
            k = min(SEG_FRAMES, n - i * SEG_FRAMES)
            lines += [f"#EXTINF:{k / e['rate']:.6f},", f"seg_{i:06d}.ts"]
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    def segment_count(self, e: dict) -> int:
        return (e["frames"] + SEG_FRAMES - 1) // SEG_FRAMES

    async def segment(self, key: str, n: int) -> Path | None:
        """Path of segment n, transcoding it first unless cached. Concurrent requests share one run."""
        e = self.clips.get(key)
        if not e or not 0 <= n < self.segment_count(e):
            return None
        t = self._start(e, n)
        for i in range(n + 1, min(n + 1 + AHEAD, self.segment_count(e))):   # playback asks for these next
            self._start(e, i)
        if t:
            await asyncio.shield(t)
        out = self._path(e, n)
        return out if out.exists() else None

    def _path(self, e: dict, n: int) -> Path:
        return self.cache_dir() / e["key"] / f"seg_{n:06d}.ts"

    def _start(self, e: dict, n: int) -> asyncio.Task | None:
        """The run making segment n (started if needed); None when it is cached."""
        out, k = self._path(e, n), (e["key"], n)
        if out.exists():
            return None
        if k not in self.tasks:
            self.tasks[k] = asyncio.create_task(self._make(e, n, out))
            self.tasks[k].add_done_callback(lambda _: self.tasks.pop(k, None))
        return self.tasks[k]

    def _args(self, e: dict, n: int, out: Path) -> list[str]:
        ff, px = self.cfg.section("ffmpeg"), self.cfg.section("record_proxy")
        r = e["rate"]
        nv = "nvenc" in px["vcodec"]
        ta = e["v0"] + n * SEG_FRAMES / r
        tb = ta + min(SEG_FRAMES, e["frames"] - n * SEG_FRAMES) / r
        x = max(0.0, ta - e["start"] - PREROLL)
        rel = lambda p: p - e["start"] - x
        half = 0.5 / e["src_fps"]
        off = BASE + n * SEG_FRAMES / r
        vf = []
        if e["interlaced"]:   # one frame per source frame (keeps the first field): 29.97 from 1080i
            vf.append(("bwdif_cuda" if nv else "bwdif") + "=mode=0:deint=interlaced")
        vf += [f"trim=start={rel(ta) - half:.6f}:end={rel(tb) - half:.6f}", f"setpts=PTS-STARTPTS+{off:.6f}/TB"]
        if abs(e["src_fps"] - r) > 0.01:
            vf.append(f"fps={px['fps']}")
        w, h = px["width"], px["height"]
        vf.append(f"scale_cuda={w}:{h}" if nv else f"scale={w}:{h},format=yuv420p")
        a = [ff["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error", "-y"]
        if nv:
            a += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        a += ["-ss", f"{x:.4f}", "-t", f"{rel(tb) + 1:.4f}", "-i", str(e["path"]), "-map", "0:v:0",
              "-vf", ",".join(vf), "-c:v", px["vcodec"], "-b:v", px["bitrate"], "-g", str(SEG_FRAMES), "-bf", "0"]
        a += ["-preset", px.get("preset", "p4")] if nv else ["-preset", "veryfast"]
        if e["audio"]:   # first audio stream only
            a += ["-map", "0:a:0", "-af", f"atrim=start={rel(ta):.6f}:end={rel(tb):.6f},"
                                         f"asetpts=PTS-STARTPTS+{off:.6f}/TB",
                  "-ac", "2", "-c:a", "aac", "-b:a", px.get("abitrate", "128k")]
        return a + ["-f", "mpegts", str(out)]

    async def _make(self, e: dict, n: int, out: Path):
        async with self.sem:
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tmp")
            t = time.monotonic()
            rc, _, err = await run_capture(self._args(e, n, tmp), timeout=60)
            if rc != 0 or not tmp.exists():
                tmp.unlink(missing_ok=True)
                log.error("live segment %s #%d failed: %s", e["rel"], n, err.strip()[-300:])
                return
            os.replace(tmp, out)
            log.debug("live segment %s #%d %.2fs", e["rel"], n, time.monotonic() - t)
        self.made += 1
        if self.made % 100 == 0:
            await asyncio.to_thread(self.prune)

    async def timing(self, e: dict) -> tuple[float, float] | None:
        """(player zero, video start) as source pts, like Media.proxy_timing for a built proxy:
        frame 0 of segment 0 is the source frame at v0, the player clock starts at its audio."""
        if "timing" in e:
            return e["timing"]
        seg = await self.segment(e["key"], 0)
        if not seg:
            return None
        rc, out, _ = await run_capture([self.cfg.section("ffmpeg")["ffprobe"], "-v", "error", "-show_entries",
                                        "stream=codec_type,start_time", "-of", "csv=p=0", str(seg)], timeout=10)
        starts = {}
        for line in out.splitlines():
            kind, _, st = line.partition(",")
            with contextlib.suppress(ValueError):
                starts.setdefault(kind, float(st))
        if "video" not in starts:
            return None
        e["timing"] = (e["v0"] + min(starts.values()) - starts["video"], e["v0"])
        return e["timing"]

    def prune(self):
        """Keep the segment cache under editor.live_cache_gb, dropping the oldest segments first."""
        limit = float(self.cfg.section("editor").get("live_cache_gb", 20)) * 1e9
        files = []
        for f in self.cache_dir().rglob("seg_*.ts"):
            with contextlib.suppress(OSError):
                st = f.stat()
                files.append((st.st_mtime, st.st_size, f))
        total = sum(s for _, s, _ in files)
        for _, s, f in sorted(files):
            if total <= limit:
                break
            f.unlink(missing_ok=True)
            total -= s
        for d in self.cache_dir().iterdir():
            with contextlib.suppress(OSError):
                d.rmdir()   # only succeeds once empty
