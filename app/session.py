"""Per-client (IP) editor session: last loaded clip, playhead, play state, marks.

A browser reloaded or reopened from the same IP gets its editor back as it was.
Kept in memory, written to sessions.json at most every few seconds.
"""
import asyncio
import json
import logging
import os
import time

from .config import ROOT

log = logging.getLogger("session")

SESSIONS_PATH = ROOT / "sessions.json"
KEEP_DAYS = 30


class Sessions:
    def __init__(self):
        self.data: dict[str, dict] = {}
        self.dirty = False
        try:
            self.data = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        cutoff = time.time() - KEEP_DAYS * 86400
        self.data = {ip: s for ip, s in self.data.items() if s.get("updated", 0) > cutoff}

    def get(self, ip: str) -> dict:
        return self.data.get(ip, {})

    def put(self, ip: str, s: dict):
        clip = s.get("clip")
        if clip is not None and not (isinstance(clip, dict) and clip.get("root") in ("capture", "subclip")
                                     and isinstance(clip.get("path"), str)):
            raise ValueError("bad clip")
        num = lambda v: round(float(v), 4) if v is not None else None
        mark = lambda v: float(v) if v is not None else None   # unrounded: marks sit on exact frame starts
        rec = s.get("rec")   # last recording clip, for "녹화클립 로드" after previewing a subclip
        if rec is not None and not (isinstance(rec, dict) and isinstance(rec.get("path"), str)):
            raise ValueError("bad rec")
        self.data[ip] = {"clip": None if clip is None else {
            "root": clip["root"], "path": clip["path"], "t": num(clip.get("t")) or 0.0,
            "playing": bool(clip.get("playing")), "in": mark(clip.get("in")), "out": mark(clip.get("out"))},
            "rec": None if rec is None else {"path": rec["path"], "t": num(rec.get("t")) or 0.0},
            "updated": time.time()}
        self.dirty = True

    def flush(self):
        if not self.dirty:
            return
        self.dirty = False
        tmp = SESSIONS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, SESSIONS_PATH)

    async def run(self):
        try:
            while True:
                await asyncio.sleep(3)
                try:
                    self.flush()
                except OSError:
                    log.exception("sessions.json write")
        finally:
            self.flush()
