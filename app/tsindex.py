"""GOP locator for MPEG-2 transport streams (tvheadend recordings), for the in-browser decoder.

A TS has no index, and the files are large (10-35 GB) and sit on CIFS, so nothing scans a whole file.
`open` reads the head and tail: PAT/PMT (first video and first audio PID), the first I-frame (frame 0)
and the last video pts (frame count). Frame n is the picture with pts pts0 + n * frame duration.
`gop` finds the I-frame at or before frame n by interpolation search over small reads; every video
PES seen on the way is kept as a (byte, pts) anchor, so later searches near it take one read or none.
Pictures are assumed to be one per PES (true for broadcast MPEG-2).

Off-air recordings carry reception errors, so the scanners resync after lost sync bytes, skip packets
flagged with transport_error_indicator, take a pts only when its marker bits are right, and drop a
picture whose pts does not fit between the I-frames around it.
"""
import bisect
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

PKT = 188
WRAP = 1 << 33                  # pts counter
HEAD = 4 << 20                  # bytes read at open (first I-frame)
TAIL = 2 << 20                  # bytes read at open (last pts)
PROBE = 384 << 10               # bytes per interpolation probe
CHUNK = 1536 << 10              # read straight through gaps up to twice this
FPS = {1: (24000, 1001), 2: (24, 1), 3: (25, 1), 4: (30000, 1001), 5: (30, 1), 6: (50, 1),
       7: (60000, 1001), 8: (60, 1)}
VIDEO_TYPES = {0x01: "mpeg1video", 0x02: "mpeg2video"}
SLACK = 64                      # frames a picture may display away from the I-frames around it
AMARGIN = 2880                  # one AC-3 frame (90 kHz) of audio around a GOP
AREACH = 768 << 10              # step when looking for a GOP's audio beyond its video bytes
AUDIO_TYPES = {0x03: "mp2", 0x04: "mp2", 0x0F: "aac", 0x81: "ac3", 0x87: "eac3"}
PIC_TYPE = {1: "I", 2: "P", 3: "B"}
TS_EXTS = (".ts", ".m2ts", ".mts")


class TsError(Exception):
    pass


def _pts(b: bytes, i: int) -> int:
    return (((b[i] >> 1) & 7) << 30) | (b[i + 1] << 22) | ((b[i + 2] >> 1) << 15) | (b[i + 3] << 7) | (b[i + 4] >> 1)


def _sync(buf: bytes, start: int = 0, end: int | None = None) -> int:
    """First packet start at or after start: three sync bytes 188 apart (two at the end of buf); -1 if none."""
    end = len(buf) - PKT if end is None else end
    i = buf.find(b"\x47", start)
    while 0 <= i < end:
        if buf[i + PKT] == 0x47 and (i + 2 * PKT >= len(buf) or buf[i + 2 * PKT] == 0x47):
            return i
        i = buf.find(b"\x47", i + 1)
    return -1


def _packets(buf: bytes):
    """Start of every whole, error-free packet in buf; finds the packet grid again after lost sync."""
    p = _sync(buf)
    while 0 <= p <= len(buf) - PKT:
        if buf[p] != 0x47:
            p = _sync(buf, p + 1)
            continue
        if not buf[p + 1] & 0x80:           # transport_error_indicator: the demodulator gave up on it
            yield p
        p += PKT


def _pts_ok(b: bytes, i: int) -> bool:
    """marker bits of a PES pts field (a damaged header that slipped through as error-free fails them)"""
    return b[i] >> 4 in (2, 3) and b[i] & b[i + 2] & b[i + 4] & 1 == 1


def _payload(buf: bytes, p: int) -> int:
    """Payload start of the packet at p (p + 188 when it has none)."""
    afc = (buf[p + 3] >> 4) & 3
    if not afc & 1:
        return p + PKT
    return p + 5 + buf[p + 4] if afc & 2 else p + 4


def _sections(buf: bytes, pid: int) -> bytes | None:
    """First complete PSI section on pid (PAT/PMT fit in one or two packets)."""
    data = None
    if _sync(buf) < 0:
        raise TsError("TS 동기 바이트를 찾을 수 없습니다")
    for p in _packets(buf):
        if ((buf[p + 1] & 0x1F) << 8 | buf[p + 2]) != pid:
            continue
        q = _payload(buf, p)
        if buf[p + 1] & 0x40:
            q += 1 + buf[q]                 # pointer field
            data = bytearray(buf[q:p + PKT])
        elif data is not None:
            data += buf[q:p + PKT]
        if data is not None and len(data) >= 3:
            n = 3 + ((data[1] & 0x0F) << 8 | data[2])
            if len(data) >= n:
                return bytes(data[:n])
    return None


def _streams(buf: bytes) -> tuple[dict, dict | None]:
    pat = _sections(buf, 0)
    if not pat:
        raise TsError("PAT 가 없습니다")
    pmt_pid = next((((pat[i + 2] & 0x1F) << 8) | pat[i + 3] for i in range(8, len(pat) - 4, 4)
                    if (pat[i] << 8 | pat[i + 1]) != 0), None)
    pmt = _sections(buf, pmt_pid) if pmt_pid is not None else None
    if not pmt:
        raise TsError("PMT 가 없습니다")
    video = audio = None
    i = 12 + ((pmt[10] & 0x0F) << 8 | pmt[11])
    while i + 5 <= len(pmt) - 4:
        st, pid, n = pmt[i], (pmt[i + 1] & 0x1F) << 8 | pmt[i + 2], (pmt[i + 3] & 0x0F) << 8 | pmt[i + 4]
        desc = pmt[i + 5:i + 5 + n]
        codec = AUDIO_TYPES.get(st)
        if st == 0x06:                      # DVB private data: AC-3 / E-AC-3 descriptor
            d = 0
            while d + 2 <= len(desc):
                codec = {0x6A: "ac3", 0x7A: "eac3"}.get(desc[d], codec)
                d += 2 + desc[d + 1]
        if st in VIDEO_TYPES and not video:
            video = {"pid": pid, "codec": VIDEO_TYPES[st]}
        elif codec and not audio:
            audio = {"pid": pid, "codec": codec}
        i += 5 + n
    if not video:
        raise TsError("MPEG-2 비디오 스트림이 없습니다")
    return video, audio


def _pictures(buf: bytes, base: int, pid: int, seq: dict | None = None):
    """Video PES starts in buf as (file byte, pts, picture type or '').

    The picture header can sit a packet or two into the PES (after sequence and GOP headers), so the
    first 2 KB of each PES payload is collected. With seq given, the first sequence header fills it in.
    """
    out, cur, es = [], None, b""
    for p in _packets(buf):
        if ((buf[p + 1] & 0x1F) << 8 | buf[p + 2]) != pid:
            continue
        q = _payload(buf, p)
        if buf[p + 1] & 0x40:
            if cur:
                out.append(cur + [""])
            cur, es = None, b""
            if q + 14 <= p + PKT and buf[q:q + 3] == b"\0\0\1" and buf[q + 7] & 0x80 and _pts_ok(buf, q + 9):
                cur, es = [base + p, _pts(buf, q + 9)], buf[q + 9 + buf[q + 8]:p + PKT]
        elif cur:
            es += buf[q:p + PKT]
        if not cur or len(es) < 8:
            continue
        k = es.find(b"\0\0\1\0")
        if k >= 0 and k + 6 <= len(es):
            if seq is not None and not seq:
                s = es.find(b"\0\0\1\xb3", 0, k)
                if s >= 0:
                    seq.update(_seq_header(es, s))
            out.append(cur + [PIC_TYPE.get((es[k + 5] >> 3) & 7, "?")])
            cur = None
        elif len(es) > 2048:
            out.append(cur + [""])
            cur = None
    if cur:
        out.append(cur + [""])
    return out


def _pes(buf: bytes, base: int, pid: int) -> list:
    """PES starts with a pts on pid as (file byte, pts)."""
    out = []
    for p in _packets(buf):
        if buf[p + 1] & 0x40 and ((buf[p + 1] & 0x1F) << 8 | buf[p + 2]) == pid:
            q = _payload(buf, p)
            if q + 14 <= p + PKT and buf[q:q + 3] == b"\0\0\1" and buf[q + 7] & 0x80 and _pts_ok(buf, q + 9):
                out.append((base + p, _pts(buf, q + 9)))
    return out


def _seq_header(es: bytes, s: int) -> dict:
    w, h = es[s + 4] << 4 | es[s + 5] >> 4, (es[s + 5] & 0x0F) << 8 | es[s + 6]
    info = {"width": w, "height": h, "fps": FPS.get(es[s + 7] & 0x0F, (30000, 1001)), "progressive": False,
            "chroma": "yuv420p"}
    e = es.find(b"\0\0\1\xb5", s + 12, s + 400)
    if e >= 0 and es[e + 4] >> 4 == 1:
        info["progressive"] = bool(es[e + 5] & 0x08)
        info["chroma"] = {2: "yuv422p", 3: "yuv444p"}.get((es[e + 5] >> 1) & 3, "yuv420p")
    return info


class TsFile:
    """Pictures are kept as anchors in byte (decode) order with their pts relative to frame 0; `covered`
    lists the byte ranges read so far, inside which every picture start is known.

    With B-frames a GOP's pictures are not in display order, and in an open GOP the B-frames right after
    the next I-frame display before it. So the bytes to decode display frames [I_k, I_k+1) run from I_k to
    the first picture after I_k+1 that displays after it; the decoder (worker) drops the leading B-frames
    of I_k, which reference the GOP before, and keeps the first `frames` pictures it outputs.
    """

    def __init__(self, path: Path):
        self.path = path
        self.size = path.stat().st_size
        self.lock = threading.Lock()
        self.reads = 0
        self.rejected = 0                   # pictures dropped as damaged (pts out of place)
        with open(path, "rb") as f:
            head = f.read(min(self.size, HEAD))
            f.seek(max(0, self.size - TAIL))
            tail = f.read()
        self.video, self.audio = _streams(head)
        pid, seq = self.video["pid"], {}
        pics = _pictures(head, 0, pid, seq)
        first = next((p for p in pics if p[2] == "I"), None)
        if not first or not seq:
            raise TsError("첫 I-프레임을 찾을 수 없습니다")
        num, den = seq["fps"]
        self.seq = seq
        self.fd = 90000 * den // num        # 3003 at 29.97
        self.pts0 = first[1]
        tail_start = max(0, self.size - TAIL)
        last = _pictures(tail, tail_start, pid)
        if not last:
            raise TsError("마지막 비디오 PTS 를 찾을 수 없습니다")
        rels = sorted(r for r in ((p[1] - self.pts0) % WRAP for p in last) if r < WRAP // 2)
        while len(rels) > 1 and rels[-1] - rels[-2] != self.fd:   # B-frames cut off by the end of the file
            rels.pop()
        self.frames = rels[-1] // self.fd + 1
        self.bytes, self.rels, self.types, self.covered = [], [], [], []
        self.abytes, self.arels = [], []    # audio PES starts, pts relative to frame 0 (signed)
        self._add(0, len(head), pics, self._audio_pes(head, 0))
        self._add(tail_start, self.size, last, self._audio_pes(tail, tail_start))

    def rel(self, pts: int) -> int:
        return (pts - self.pts0) % WRAP

    def _audio_pes(self, buf: bytes, base: int) -> list:
        return _pes(buf, base, self.audio["pid"]) if self.audio else []

    def _add(self, start: int, end: int, pics: list, apes: list = ()):
        for b, pts in apes:
            i = bisect.bisect_left(self.abytes, b)
            if i == len(self.abytes) or self.abytes[i] != b:
                self.abytes.insert(i, b)
                self.arels.insert(i, (pts - self.pts0 + WRAP // 2) % WRAP - WRAP // 2)
        for b, pts, ty in pics:
            r = self.rel(pts)
            if r >= self.frames * self.fd:      # before frame 0 (wrapped) or past the end
                continue
            i = bisect.bisect_left(self.bytes, b)
            if i < len(self.bytes) and self.bytes[i] == b:
                self.types[i] = self.types[i] or ty
                continue
            if not self._fits(i, r):
                self.rejected += 1
                if self.rejected <= 5:
                    log.warning("%s: picture at byte %d with pts out of place (frame %d), ignored",
                                self.path.name, b, r // self.fd)
                continue
            self.bytes.insert(i, b)
            self.rels.insert(i, r)
            self.types.insert(i, ty)
        if pics and not pics[-1][2] and end < self.size:
            end = pics[-1][0]                   # cut off before its picture header: not known yet
        rs = sorted(self.covered + [[start, end]])
        self.covered = [rs[0]]
        for s, e in rs[1:]:
            if s <= self.covered[-1][1]:
                self.covered[-1][1] = max(self.covered[-1][1], e)
            else:
                self.covered.append([s, e])

    def _read(self, f, start: int, n: int):
        """Read about n bytes from start; picture starts in [start, end of read) become known."""
        pos = max(0, start - 2 * PKT)       # the packet straddling start is read whole
        f.seek(pos)
        buf = f.read(min(max(n, 8 * PKT) + 2 * PKT, self.size - pos))
        self.reads += 1
        ok = len(buf) >= 4 * PKT
        pics = _pictures(buf, pos, self.video["pid"]) if ok else []
        apes = self._audio_pes(buf, pos) if ok else []
        self._add(start, pos + len(buf), [p for p in pics if p[0] >= start], [a for a in apes if a[0] >= start])

    def _fits(self, i: int, r: int) -> bool:
        """a picture going in at index i displays between the I-frames known around it (within SLACK)"""
        lo = next((self.rels[k] for k in range(i - 1, max(-1, i - 65), -1) if self.types[k] == "I"), None)
        hi = next((self.rels[k] for k in range(i, min(len(self.bytes), i + 64)) if self.types[k] == "I"), None)
        return (lo is None or r >= lo - SLACK * self.fd) and (hi is None or r <= hi + SLACK * self.fd)

    def _gap(self, a: int, b: int) -> tuple[int, int] | None:
        """First byte range in [a, b) not read yet."""
        for s, e in self.covered:
            if e <= a:
                continue
            if s > a:
                return a, min(s, b)
            a = e
            if a >= b:
                return None
        return (a, b) if a < b else None

    def info(self) -> dict:
        return {"size": self.size, "frames": self.frames, "fps": list(self.seq["fps"]), "fd": self.fd, "pts0": self.pts0,
                "start": self.pts0 / 90000, "width": self.seq["width"], "height": self.seq["height"],
                "progressive": self.seq["progressive"], "chroma": self.seq["chroma"],
                "video": self.video, "audio": self.audio}

    def gop(self, n: int) -> dict:
        """The GOP holding display frame n: frames [frame, frame + frames) decode from bytes [start, end)."""
        n = max(0, min(self.frames - 1, n))
        target = n * self.fd
        with self.lock, open(self.path, "rb") as f:
            span = None
            for _ in range(60):
                keys = [i for i, t in enumerate(self.types) if t == "I"]
                a = max((i for i in keys if self.rels[i] <= target), key=lambda i: self.rels[i])
                b = next((i for i in keys if self.bytes[i] > self.bytes[a]), None)
                gap = self._gaps(self.bytes[a], self.bytes[b] if b is not None else self.size, target)
                if gap:
                    # interpolation crawls when one side's pts is stale (e.g. a probe landed inside a big
                    # I-frame and found no picture start): halve the gap whenever it shrank by less than half
                    halve = span is not None and gap[1] - gap[0] > span // 2
                    span = gap[1] - gap[0]
                    self._fill(f, gap, target, halve)
                    continue
                if b is None:                   # the file's last GOP
                    return self._with_audio(f, self._result(a, self.frames * self.fd, self.size))
                # first picture after I_k+1 that displays after it
                e = next((i for i in range(b + 1, len(self.bytes)) if self.rels[i] > self.rels[b]), None)
                if e is None or self._gap(self.bytes[b], self.bytes[e]):
                    self._read(f, self.bytes[b], CHUNK)
                    continue
                return self._with_audio(f, self._result(a, self.rels[b], self.bytes[e]))
        log.warning("%s: GOP of frame %d not found after 60 steps (%d reads, %d anchors)",
                    self.path.name, n, self.reads, len(self.bytes))
        raise TsError(f"프레임 {n} 의 GOP 를 찾지 못했습니다")

    def _result(self, a: int, next_rel: int, end: int) -> dict:
        r = self.rels[a]
        return {"frame": r // self.fd, "frames": (next_rel - r) // self.fd, "start": self.bytes[a], "end": end,
                "pts": (self.pts0 + r) % WRAP}

    def _with_audio(self, f, g: dict) -> dict:
        """Add the bytes [astart, aend) of the audio PES that cover the GOP's frames (plus AMARGIN each side).

        Audio is muxed near the video of the same time (in these files 0.2-0.7 MB after it), so the search
        widens the GOP's own byte range step by step until audio before and after its time span is known.
        """
        if not self.audio:
            return g
        t0 = g["frame"] * self.fd - AMARGIN
        t1 = (g["frame"] + g["frames"]) * self.fd + AMARGIN
        lo, hi = g["start"], g["end"]
        for _ in range(40):
            while (gap := self._gap(lo, hi)):
                self._read(f, gap[0], gap[1] - gap[0])
            i0, i1 = bisect.bisect_left(self.abytes, lo), bisect.bisect_left(self.abytes, hi)
            rels = self.arels[i0:i1]
            before = lo == 0 or any(r <= t0 for r in rels)
            after = hi == self.size or any(r >= t1 for r in rels)
            if before and after:
                a = max((k for k in range(i0, i1) if self.arels[k] <= t0), default=i0)
                b = next((k for k in range(i0, i1) if self.arels[k] >= t1), None)
                if a < i1:
                    g.update(astart=self.abytes[a], aend=self.abytes[b] if b is not None else hi,
                             apts=(self.pts0 + self.arels[a]) % WRAP)
                return g
            if not before:
                lo = max(0, lo - AREACH)
            if not after:
                hi = min(self.size, hi + AREACH)
        log.warning("%s: no audio around frame %d", self.path.name, g["frame"])
        return g

    def _gaps(self, a: int, b: int, target: int) -> tuple[int, int, int, int] | None:
        """The unread byte range in [a, b) nearest before the target: (g0, g1, rel before, rel after)."""
        best = None
        while (g := self._gap(a, b)):
            i = bisect.bisect_left(self.bytes, g[0]) - 1
            j = bisect.bisect_left(self.bytes, g[1])
            r0 = self.rels[i] if i >= 0 else 0
            r1 = self.rels[j] if j < len(self.bytes) else self.frames * self.fd
            if r0 > target and best:
                break
            best, a = (*g, r0, r1), g[1]
        return best

    def _fill(self, f, gap: tuple[int, int, int, int], target: int, halve: bool = False):
        g0, g1, r0, r1 = gap
        if target >= r1:                        # no I-frame between the gap's end and the target: look back
            self._read(f, max(g0, g1 - CHUNK), CHUNK)
        elif g1 - g0 <= 2 * CHUNK:
            self._read(f, g0, g1 - g0)
        else:                                   # interpolate, aiming a few frames early to land before the target
            x = (g0 + g1) // 2 if halve else g0 + (g1 - g0) * max(0, target - r0 - 8 * self.fd) // max(1, r1 - r0)
            self._read(f, max(g0, min(g1 - PROBE, x)), PROBE)


class TsIndex:
    """TsFile per (path, size, mtime), most recent few kept."""
    KEEP = 16

    def __init__(self):
        self.files: dict[tuple, TsFile] = {}
        self.lock = threading.Lock()

    def get(self, path: Path) -> TsFile:
        st = path.stat()
        key = (str(path), st.st_size, int(st.st_mtime))
        with self.lock:
            tf = self.files.pop(key, None)
        if tf is None:
            tf = TsFile(path)
            log.info("ts index %s: %d frames, video %s audio %s", path.name, tf.frames, tf.video, tf.audio)
        with self.lock:
            self.files[key] = tf
            while len(self.files) > self.KEEP:
                self.files.pop(next(iter(self.files)))
        return tf


def is_ts(path: Path) -> bool:
    return path.suffix.lower() in TS_EXTS and os.path.getsize(path) > 4 * PKT
