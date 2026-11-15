"""instantclip backend: FastAPI REST + WebSocket status + static web UI and media."""
import asyncio
import contextlib
import json
import logging
import subprocess
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import ROOT, Config
from .encoder import Encoder
from .logger import setup_logging
from .media import ROOTS, Media, MediaError
from .recorder import Recorder
from .session import Sessions
from .sysmon import SysMon

cfg = Config()
setup_logging(cfg.section("log"))
log = logging.getLogger("main")

media = Media(cfg)
recorder = Recorder(cfg, media)
encoder = Encoder(cfg, media)
sysmon = SysMon(cfg)
sessions = Sessions()
WEB = ROOT / "web"


def _commit_time() -> str:
    """Date/time of the checked-out commit, shown next to the logo ("" outside a git checkout)."""
    try:
        return subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


COMMIT_TIME = _commit_time()

# subclip settings the UI may write (anything else in the section is file-only)
SUBCLIP_KEYS = {"vcodec", "preset", "tune", "profile", "rc", "bitrate", "maxrate", "bufsize", "cq",
                "gop", "bframes", "spatial_aq", "resolution", "fps", "container", "faststart",
                "acodec", "abitrate", "audio_gain", "workers"}


@contextlib.asynccontextmanager
async def lifespan(app):
    media.ensure_dirs()
    media.close_stale_playlists()
    await recorder.refresh_devices()
    log.info("decklink devices: %s", recorder.devices)
    encoder.start()
    tasks = [asyncio.create_task(sysmon.run()), asyncio.create_task(recorder.watchdog()),
             asyncio.create_task(sessions.run())]
    log.info("instantclip ready on port %s", cfg.section("http")["port"])
    yield
    log.info("shutting down")
    for t in tasks:
        t.cancel()
    await recorder.stop_all()
    await encoder.stop_all()
    await media.stop_all()


app = FastAPI(title="instantclip", lifespan=lifespan)


@app.exception_handler(MediaError)
async def media_error(_, e: MediaError):
    return JSONResponse({"detail": str(e)}, status_code=400)


@app.middleware("http")
async def no_cache_playlists(request: Request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if p.endswith(".m3u8") or p.startswith("/api/") or p.endswith((".js", ".css", ".html")) or p == "/":
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------------- config ----------------
@app.get("/api/config")
async def get_config():
    c = cfg.snapshot()
    return {"subclip": c["subclip"], "paths": c["paths"], "mediamtx": c["mediamtx"],
            "thumbnail": c["thumbnail"], "capture": c["capture"], "editor": c["editor"],
            "record_proxy": {k: c["record_proxy"][k] for k in ("width", "height", "fps")},
            "commit_time": COMMIT_TIME}


@app.put("/api/config/subclip")
async def put_subclip(values: dict):
    bad = set(values) - SUBCLIP_KEYS
    if bad:
        raise HTTPException(400, f"unknown keys: {sorted(bad)}")
    if "audio_gain" in values:
        values["audio_gain"] = max(0.0, min(10.0, float(values["audio_gain"])))
    if "workers" in values:
        values["workers"] = max(1, min(8, int(values["workers"])))
    for k in ("gop", "bframes", "cq"):
        if k in values:
            values[k] = int(values[k])
    sc = cfg.update_section("subclip", values)
    log.info("subclip config saved: %s", values)
    return sc


# ---------------- recording ----------------
class RecStart(BaseModel):
    device: str
    prefix: str = ""
    fps: str = ""   # 60000/1001 | 30000/1001, empty: capture.fps


class DeviceReq(BaseModel):
    device: str


class PreviewReq(BaseModel):
    device: str
    on: bool


def _channel(device):
    try:
        return recorder.channel(device)
    except KeyError:
        raise HTTPException(404, f"unknown device {device}")


@app.get("/api/devices")
async def devices(refresh: bool = False):
    if refresh or not recorder.devices:
        await recorder.refresh_devices()
    return recorder.status()


@app.post("/api/record/start")
async def record_start(r: RecStart):
    try:
        rel = await _channel(r.device).start_record(r.prefix, r.fps)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"clip": rel}


@app.post("/api/record/stop")
async def record_stop(r: DeviceReq):
    await _channel(r.device).stop_record()
    return {"ok": True}


@app.post("/api/record/extend")
async def record_extend(r: DeviceReq):
    try:
        remaining = _channel(r.device).extend(float(cfg.section("capture")["extend_sec"]))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"remaining": remaining}


@app.post("/api/record/preview")
async def record_preview(r: PreviewReq):
    try:
        await _channel(r.device).set_preview(r.on)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


# ---------------- media browser ----------------
class MkdirReq(BaseModel):
    root: str
    path: str = ""
    name: str


class RenameReq(BaseModel):
    root: str
    path: str
    name: str


class MoveReq(BaseModel):
    root: str
    paths: list[str]
    dest: str = ""


class DeleteReq(BaseModel):
    root: str
    paths: list[str]


def _root(root):
    if root not in ROOTS:
        raise HTTPException(400, "bad root")
    return root


@app.get("/api/files")
async def files(root: str, path: str = ""):
    return await media.list_dir(_root(root), path)


@app.post("/api/files/mkdir")
async def files_mkdir(r: MkdirReq):
    media.mkdir(_root(r.root), r.path, r.name)
    return {"ok": True}


@app.post("/api/files/rename")
async def files_rename(r: RenameReq):
    media.rename(_root(r.root), r.path, r.name)
    return {"ok": True}


@app.post("/api/files/move")
async def files_move(r: MoveReq):
    return {"moved": media.move(_root(r.root), r.paths, r.dest)}


@app.post("/api/files/delete")
async def files_delete(r: DeleteReq):
    media.delete(_root(r.root), r.paths)
    return {"ok": True}


@app.get("/api/files/download")
async def files_download(root: str, path: str, inline: bool = False):
    p = media.resolve(_root(root), path)
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, filename=p.name, content_disposition_type="inline" if inline else "attachment")


@app.get("/api/clip")
async def clip(path: str):
    return await media.clip_info(path)


class ClipReq(BaseModel):
    path: str


@app.post("/api/clip/proxy")
async def clip_proxy(r: ClipReq):
    await media.make_proxy(r.path)
    return {"ok": True}


# ---------------- subclip jobs ----------------
class JobReq(BaseModel):
    source: str
    tin: float
    tout: float
    folder: str = ""


@app.post("/api/jobs")
async def job_submit(r: JobReq):
    return encoder.submit(r.source, r.tin, r.tout, r.folder).as_dict()


@app.get("/api/counter")
async def counter(source: str, folder: str = ""):
    return {"next": encoder.peek_n(Path(source).stem, folder)}


@app.post("/api/counter/reset")
async def counter_reset(r: ClipReq):
    encoder.reset_counter(r.path)
    return {"next": 1}


@app.post("/api/jobs/{job_id}/cancel")
async def job_cancel(job_id: int):
    encoder.cancel(job_id)
    return {"ok": True}


@app.post("/api/jobs/clear")
async def job_clear():
    encoder.clear_finished()
    return {"ok": True}


# ---------------- client session (per IP) ----------------
@app.get("/api/session")
async def session_get(request: Request):
    return sessions.get(request.client.host)


@app.post("/api/session")   # POST, so navigator.sendBeacon can save on page unload
async def session_put(request: Request):
    try:
        sessions.put(request.client.host, await request.json())
    except (ValueError, TypeError, KeyError):
        raise HTTPException(400, "bad session")
    return {"ok": True}


# ---------------- status ----------------
def status() -> dict:
    return {"recorders": recorder.status(), "jobs": encoder.status(), "proxies": media.proxy_status(),
            "sys": sysmon.data, "rev": media.rev,
            "counter_rev": encoder.counter_rev}


@app.get("/api/status")
async def get_status():
    return status()


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    try:
        while True:
            await sock.send_text(json.dumps(status(), default=str))
            await asyncio.sleep(1)
    except (WebSocketDisconnect, RuntimeError):
        pass


# ---------------- static ----------------
@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


media.ensure_dirs()
app.mount("/media/proxy", StaticFiles(directory=cfg.path_of("proxy")), name="proxy")
app.mount("/media/thumb", StaticFiles(directory=cfg.path_of("thumbnail")), name="thumb")
app.mount("/static", StaticFiles(directory=WEB), name="static")


def main():
    h = cfg.section("http")
    uvicorn.run(app, host=h["host"], port=int(h["port"]), log_config=None, ws_ping_interval=20)


if __name__ == "__main__":
    main()
