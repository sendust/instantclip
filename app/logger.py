"""Logging: rotating files under log.dir.

instantclip.log  application events (all loggers)
ffmpeg.log       raw ffmpeg stderr lines (logger "ffmpeg.*"), kept out of the main log
"""
import logging
import logging.handlers
from pathlib import Path

from .config import ROOT

FMT = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(name)s] %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


class _NotFfmpeg(logging.Filter):
    def filter(self, record):
        return not record.name.startswith("ffmpeg")


def setup_logging(cfg: dict) -> Path:
    log_dir = Path(cfg.get("dir", "logs"))
    if not log_dir.is_absolute():
        log_dir = ROOT / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, str(cfg.get("level", "INFO")).upper(), logging.INFO)
    fmt = logging.Formatter(FMT, DATEFMT)

    def rotating(name):
        h = logging.handlers.RotatingFileHandler(
            log_dir / name, maxBytes=int(cfg.get("max_bytes", 10 << 20)),
            backupCount=int(cfg.get("backup_count", 10)), encoding="utf-8")
        h.setFormatter(fmt)
        return h

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    main = rotating("instantclip.log")
    main.addFilter(_NotFfmpeg())
    root.addHandler(main)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(_NotFfmpeg())
    root.addHandler(console)

    ff = logging.getLogger("ffmpeg")
    ff.propagate = False
    ff.setLevel(logging.DEBUG)
    ff.addHandler(rotating("ffmpeg.log"))

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    return log_dir


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
