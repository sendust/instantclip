# Excerpt of instantclip app/media.py (Media methods): proxy_timing, scenes, _ts_scenes, _peaks
# Depends on: run_capture (asyncio subprocess -> rc, stdout, stderr), probe (ffprobe json), interlaced(stream), TsFile (pts0, frames).

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
        Also the audio peak of every frame ("peaks", see _peaks), to see where a shot's sound starts and ends.
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
        src = "concat:" + "|".join(str(sg[0]) for sg in use)
        args = [self.cfg.section("ffmpeg")["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error",
                "-i", src, "-map", "0:v:0",
                "-vf", "scale=320:-2,select='gte(scene,0)',metadata=print:file=-", "-an", "-f", "null", "-"]
        f1 = min(f1, start - 1)
        (rc, out, err), peaks = await asyncio.gather(
            run_capture(args, timeout=60),
            self._peaks(["-i", src], tm[0], f0, f1, rate) if tm else asyncio.sleep(0, []))
        if rc != 0:
            raise MediaError(f"scene 검출 실패: {err.strip()[-200:]}")
        frames, k = [], None
        for line in out.splitlines():
            if line.startswith("frame:"):
                k = first + int(line.split()[0][6:])   # n-th decoded frame of the first used segment
            elif line.startswith("lavfi.scene_score=") and k is not None and f0 <= k <= f1:
                frames.append([k, round(float(line.split("=", 1)[1]), 4)])
        return {"fps": fps, "from": f0, "to": f1, "frames": frames, "peaks": peaks}

    async def _ts_scenes(self, p: Path, tf: TsFile, f0: int, f1: int, rate: float, fps) -> dict:
        """scenes() of a browser-decoded TS, read from the source itself: editor frame k is the source frame
        at pts0 + k frames, and of an interlaced frame the editor shows the first field, so that is scored."""
        info = await probe(self.cfg.section("ffmpeg")["ffprobe"], p)
        v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        v0, f1 = tf.pts0 / 90000, min(f1, tf.frames - 1)
        x = max(0.0, v0 + (f0 - 1) / rate - info.get("start_time", 0.0) - 3)
        field = ("field=type=bottom," if v.get("field_order") in ("bb", "bt") else "field=type=top,") \
            if interlaced(v) else ""
        inp = ["-ss", f"{x:.4f}", "-t", f"{(f1 - f0 + 2) / rate + 3.5:.4f}", "-i", str(p)]
        args = [self.cfg.section("ffmpeg")["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error", *inp, "-copyts",
                "-map", "0:v:0", "-vf", f"{field}scale=320:-2,select='gte(scene,0)',metadata=print:file=-",
                "-an", "-f", "null", "-"]
        # the browser plays audio sample s at frame-0 time + s / 48000 (web/tsworker.js): same clock
        (rc, out, err), peaks = await asyncio.gather(run_capture(args, timeout=60), self._peaks(inp, v0, f0, f1, rate))
        if rc != 0:
            raise MediaError(f"scene 검출 실패: {err.strip()[-200:]}")
        frames, k = [], None
        for line in out.splitlines():
            if line.startswith("frame:"):
                k = round((float(line.rsplit("pts_time:", 1)[1]) - v0) * rate)
            elif line.startswith("lavfi.scene_score=") and k is not None and f0 <= k <= f1:
                frames.append([k, round(float(line.split("=", 1)[1]), 4)])
        return {"fps": fps, "from": f0, "to": f1, "frames": frames, "peaks": peaks}

    async def _peaks(self, inp: list[str], zero: float, f0: int, f1: int, rate: float) -> list:
        """Audio peak (dBFS, L and R together) of editor frames f0..f1: [dB or None, ...] (None: no audio there,
        -90 for silence). The first audio stream of inp is measured in short chunks; a chunk at source pts t is
        at t - zero on the player clock and so in editor frame floor((t - zero) * rate)."""
        args = [self.cfg.section("ffmpeg")["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error", *inp, "-copyts",
                "-map", "0:a:0", "-af", "asetnsamples=n=160:p=0,astats=metadata=1:reset=1:measure_perchannel=none:"
                "measure_overall=Peak_level,ametadata=print:key=lavfi.astats.Overall.Peak_level:file=-",
                "-vn", "-f", "null", "-"]
        rc, out, _ = await run_capture(args, timeout=60)
        peaks: list = [None] * (f1 - f0 + 1)
        if rc != 0:
            return peaks   # no audio stream: an empty graph, the scenes still count
        k = None
        for line in out.splitlines():
            if line.startswith("frame:"):
                k = math.floor((float(line.rsplit("pts_time:", 1)[1]) - zero) * rate + 1e-6) - f0
            elif line.startswith("lavfi.astats.Overall.Peak_level=") and k is not None and 0 <= k < len(peaks):
                with contextlib.suppress(ValueError):
                    db = max(-90.0, float(line.split("=", 1)[1]))   # -inf = digital silence
                    peaks[k] = round(db if peaks[k] is None else max(peaks[k], db), 1)
        return peaks

