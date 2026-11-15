"""JSON configuration: load, defaults, thread-safe update with atomic save."""
import copy
import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("INSTANTCLIP_CONFIG", ROOT / "config.json"))

# values used when a key is missing from config.json (the file itself is the reference)
DEFAULTS = {
    "http": {"host": "0.0.0.0", "port": 8090},
    "paths": {
        "record": "/mnt/raid0_old/capture",
        "proxy": "/mnt/raid0_old/capture/proxy",
        "subclip": "/mnt/raid0_old/output",
        "thumbnail": "/mnt/raid0_old/capture/thumbnail",
    },
    "log": {"dir": "logs", "level": "INFO", "max_bytes": 10 * 1024 * 1024, "backup_count": 10},
    "ffmpeg": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
    "mediamtx": {"rtsp_url": "rtsp://127.0.0.1:8554", "webrtc_url": "", "webrtc_port": 8889,
                 "path_prefix": "instantclip_", "api_url": "http://127.0.0.1:9997"},
    "capture": {"format_code": "Hi59", "audio_channels": 2, "deinterlace": "bwdif_cuda=mode=1",
                "fps": "60000/1001",   # default recording rate; 60000/1001 or 30000/1001, chosen per REC in the UI
                "name_template": "{prefix}_%Y%m%d_%H%M%S", "default_prefix": "REC",
                "auto_stop_sec": 7200, "extend_sec": 600,
                # device name -> /dev/blackmagic/ioN, for "in use by another process" (learned while capturing)
                "device_nodes": {}},
    "record_hires": {"container": "ts", "vcodec": "mpeg2video", "bitrate": "35M", "minrate": "35M",
                     "maxrate": "35M", "bufsize": "9781k", "gop": 30, "bframes": 2, "threads": 16,
                     "acodec": "aac", "abitrate": "256k",
                     "extra_args": ["-intra_vlc", "1", "-non_linear_quant", "1", "-qmin", "1", "-qmax", "28"]},
    "record_proxy": {"width": 854, "height": 480, "fps": "30000/1001", "vcodec": "h264_nvenc",
                     "preset": "p4", "rc": "cbr", "bitrate": "4M", "maxrate": "4M", "bufsize": "8M",
                     "gop": 30, "acodec": "aac", "abitrate": "128k", "hls_time": 2},
    "live": {"width": 640, "height": 360, "bitrate": "1500k", "gop": 30, "abitrate": "128k"},
    "editor": {"seek_lr": 10, "seek_ud": 60},   # arrow-key seek seconds: ←/→ and ↓/↑
    "thumbnail": {"interval": 2, "width": 160, "height": 90, "quality": 5},
    "subclip": {
        "vcodec": "h264_nvenc", "preset": "p5", "tune": "hq", "profile": "high", "rc": "vbr",
        "bitrate": "20M", "maxrate": "25M", "bufsize": "40M", "cq": 23, "gop": 60, "bframes": 2,
        "spatial_aq": True, "resolution": "source", "fps": "source", "container": "mp4",
        "faststart": True, "acodec": "aac", "abitrate": "192k", "audio_gain": 1.0,
        "name_template": "{src}_[{n:03d}]_{start:.2f}_{end:.2f}_{dur:.2f}", "workers": 1,
    },
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = {}
        self.load()

    def load(self):
        with self._lock:
            raw = {}
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = _merge(DEFAULTS, raw)
            if raw != self._data:
                self._save()

    def _save(self):
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def section(self, name: str) -> dict:
        with self._lock:
            return copy.deepcopy(self._data.get(name, {}))

    def update_section(self, name: str, values: dict) -> dict:
        """Merge values into a section and write config.json immediately."""
        with self._lock:
            self._data[name] = _merge(self._data.get(name, {}), values)
            self._save()
            return copy.deepcopy(self._data[name])

    def path_of(self, key: str) -> Path:
        return Path(self.section("paths")[key])
