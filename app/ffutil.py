"""ffmpeg helpers: async process supervisor, device listing, probing."""
import asyncio
import json
import logging
import os
import re
import shlex
import signal
import time
from pathlib import Path

log = logging.getLogger("ffutil")

MEDIA_EXTS = {".ts", ".m2ts", ".mts", ".mp4", ".mov", ".mxf", ".mkv", ".m4v", ".avi", ".mpg"}


class FFmpegProcess:
    """One ffmpeg run. stdout carries -progress key=value lines, stderr goes to ffmpeg.log.

    on_line(line) is called for every stderr line (signal detection etc).
    """

    def __init__(self, name: str, args: list[str], on_line=None):
        self.name = name
        self.args = args
        self.on_line = on_line
        self.proc: asyncio.subprocess.Process | None = None
        self.progress: dict[str, str] = {}
        self.tail: list[str] = []
        self.started = 0.0
        self.returncode: int | None = None
        self.stop_requested = False
        self._log = logging.getLogger(f"ffmpeg.{name}")
        self._tasks: list[asyncio.Task] = []

    @property
    def running(self) -> bool:
        return self.proc is not None and self.returncode is None

    async def start(self):
        self._log.info("START %s", shlex.join(self.args))
        log.info("[%s] ffmpeg start", self.name)
        self.started = time.time()
        self.proc = await asyncio.create_subprocess_exec(
            *self.args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        self._tasks = [asyncio.create_task(self._read_progress()),
                       asyncio.create_task(self._read_stderr())]

    async def _read_progress(self):
        block = {}
        async for raw in self.proc.stdout:
            line = raw.decode(errors="replace").strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            block[k] = v
            if k == "progress":
                self.progress = block
                block = {}

    async def _read_stderr(self):
        async for raw in self.proc.stderr:
            for line in raw.decode(errors="replace").replace("\r", "\n").splitlines():
                line = line.rstrip()
                if not line:
                    continue
                self._log.info(line)
                self.tail = (self.tail + [line])[-30:]
                if self.on_line:
                    try:
                        self.on_line(line)
                    except Exception:  # never let a callback kill the reader
                        log.exception("[%s] on_line", self.name)

    async def wait(self) -> int:
        rc = await self.proc.wait()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.returncode = rc
        self._log.info("EXIT rc=%s (%.1fs)", rc, time.time() - self.started)
        log.info("[%s] ffmpeg exit rc=%s", self.name, rc)
        return rc

    async def stop(self, timeout: float = 10.0):
        """Graceful: SIGINT (muxers finalize: HLS ENDLIST, mp4 moov), then SIGTERM, SIGKILL.

        ('q' on stdin is not usable: every command runs with -nostdin.)
        """
        if not self.proc or self.proc.returncode is not None:
            return
        self.stop_requested = True
        for sig, wait in ((signal.SIGINT, timeout), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
            try:
                self.proc.send_signal(sig)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(asyncio.shield(self.proc.wait()), wait)
                return
            except asyncio.TimeoutError:
                log.warning("[%s] still alive, escalating", self.name)

    def out_time(self) -> float:
        try:
            return int(self.progress.get("out_time_us", "0")) / 1e6
        except ValueError:
            return 0.0

    def error_text(self) -> str:
        errs = [l for l in self.tail if re.search(r"error|fail|invalid|cannot|no such", l, re.I)]
        return (errs or self.tail or [""])[-1][:300]


async def run_capture(args: list[str], timeout: float = 15) -> tuple[int, str, str]:
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        out, err = await p.communicate()
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def list_decklink(ffmpeg: str) -> list[str]:
    rc, out, err = await run_capture([ffmpeg, "-hide_banner", "-sources", "decklink"])
    names = re.findall(r"\[([^\]]+)\]", out + err)
    return [n for n in names if n.lower().startswith("decklink") or "intensity" in n.lower()] or names


def decklink_holders() -> dict[str, set[int]]:
    """/dev/blackmagic/ioN -> pids that have it open. Only processes whose fd table we may read
    (same user) are seen; the root-owned Blackmagic helpers are skipped that way too."""
    out: dict[str, set[int]] = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if link.startswith("/dev/blackmagic/"):
                out.setdefault(link, set()).add(int(pid))
    return out


def describe_pid(pid: int) -> str:
    """'ffmpeg (pid 123, mediarouter)': the command plus the working dir name of its parent."""
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return f"pid {pid}"
    owner = ""
    for p in (ppid_of(pid), pid):   # who launched it says more than "ffmpeg"
        try:
            owner = os.path.basename(os.readlink(f"/proc/{p}/cwd"))
        except OSError:
            continue
        if owner:
            break
    return f"{comm} (pid {pid}{', ' + owner if owner else ''})"


def ppid_of(pid: int) -> int:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


_probe_cache: dict[tuple, dict] = {}


async def probe(ffprobe: str, path: Path) -> dict:
    """Format/stream summary, cached on (path, size, mtime)."""
    try:
        st = path.stat()
    except OSError:
        return {}
    key = (str(path), st.st_size, int(st.st_mtime))
    if key in _probe_cache:
        return _probe_cache[key]
    rc, out, _ = await run_capture([ffprobe, "-v", "error", "-print_format", "json", "-show_format",
                                    "-show_streams", str(path)], timeout=20)
    info = {}
    if rc == 0:
        try:
            j = json.loads(out)
            fmt = j.get("format", {})
            info = {"duration": float(fmt.get("duration") or 0), "start_time": float(fmt.get("start_time") or 0),
                    "streams": [{k: s.get(k) for k in ("codec_type", "codec_name", "width", "height",
                                                         "r_frame_rate", "field_order", "channels", "pix_fmt")}
                                for s in j.get("streams", [])]}
        except (ValueError, TypeError):
            pass
    if len(_probe_cache) > 5000:
        _probe_cache.clear()
    _probe_cache[key] = info
    return info


def hls_info(m3u8: Path) -> dict | None:
    """Duration (sum of EXTINF), segment count and whether the playlist is closed."""
    try:
        text = m3u8.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    dur = 0.0
    segs = 0
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            try:
                dur += float(line[8:].split(",")[0])
                segs += 1
            except ValueError:
                pass
    return {"duration": dur, "segments": segs, "ended": "#EXT-X-ENDLIST" in text}


def close_playlist(m3u8: Path):
    """Append ENDLIST to an EVENT playlist left open by a crashed ffmpeg."""
    info = hls_info(m3u8)
    if info and not info["ended"]:
        with open(m3u8, "a", encoding="utf-8") as f:
            f.write("#EXT-X-ENDLIST\n")
        log.info("closed playlist %s", m3u8)
