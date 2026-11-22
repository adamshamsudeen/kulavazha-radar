"""Hyacinth Watch camera grabber.

One background ffmpeg process keeps the latest JPEG frame in memory, so every
browser polling /api/cam gets it instantly (no ffmpeg spawn per request).

CAM_URL can be:
  rtsp://user:pass@IP:554/cam/realmonitor?channel=1&subtype=1   (CP Plus / Dahua RTSP)
  http://user:pass@IP/cgi-bin/snapshot.cgi                       (Dahua snapshot, digest auth)
  http://127.0.0.1:8765/hls/live.m3u8                            (PayalCam's HLS restream of the CP Plus)
  /path/to/video.mp4                                             (looped, for rehearsal)
CAM_URL is re-read from .env when it changes, so no server restart is needed.
"""
import asyncio
import os
import shutil
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import config

STATE = {"url": "", "jpeg": b"", "ts": 0.0, "error": "", "task": None}


def env_value(key: str, default: str = "") -> str:
    """Read a key from .env on every call (so edits apply live), else the environment."""
    env = config.ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v:
                    return v
    return os.environ.get(key, default)


def current_url() -> str:
    return env_value("CAM_URL")


def redact(url: str) -> str:
    p = urlsplit(url)
    if p.password:
        return urlunsplit(p._replace(netloc=f"{p.username}:***@{p.hostname}" + (f":{p.port}" if p.port else "")))
    return url


async def _snapshot_loop(url: str) -> None:
    p = urlsplit(url)
    auth = httpx.DigestAuth(p.username or "", p.password or "") if p.username else None
    clean = urlunsplit(p._replace(netloc=p.hostname + (f":{p.port}" if p.port else "")))
    async with httpx.AsyncClient(timeout=6, auth=auth) as c:
        while STATE["url"] == url:
            try:
                r = await c.get(clean)
                if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
                    STATE.update(jpeg=r.content, ts=time.time(), error="")
                else:
                    STATE["error"] = f"snapshot HTTP {r.status_code}"
            except Exception as e:  # noqa: BLE001
                STATE["error"] = f"snapshot: {type(e).__name__}"
            await asyncio.sleep(1.5)


async def _ffmpeg_loop(url: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        STATE["error"] = "ffmpeg not installed"
        return
    while STATE["url"] == url:
        args = [ffmpeg, "-hide_banner", "-loglevel", "error"]
        if url.startswith("rtsp"):
            args += ["-rtsp_transport", "tcp", "-timeout", "5000000"]
        elif url.startswith("http"):
            args += ["-reconnect", "1", "-reconnect_streamed", "1"]  # live HLS / MJPEG over http
        else:
            args += ["-re", "-stream_loop", "-1"]  # local file: play in real time, loop
        args += ["-i", url, "-an", "-vf", "fps=1,scale=640:-2", "-q:v", "5", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"]
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        buf = b""
        try:
            while STATE["url"] == url:
                chunk = await asyncio.wait_for(proc.stdout.read(65536), timeout=10)
                if not chunk:
                    break
                buf += chunk
                # Keep only the newest complete JPEG (FFD8 ... FFD9).
                end = buf.rfind(b"\xff\xd9")
                if end != -1:
                    start = buf.rfind(b"\xff\xd8", 0, end)
                    if start != -1:
                        STATE.update(jpeg=buf[start:end + 2], ts=time.time(), error="")
                    buf = buf[end + 2:]
                if len(buf) > 4_000_000:
                    buf = b""
        except asyncio.TimeoutError:
            STATE["error"] = "camera stream stalled"
        finally:
            if proc.returncode is None:
                proc.kill()
            err = (await proc.stderr.read())[-300:].decode(errors="ignore").strip()
            if err:
                STATE["error"] = err.replace(url, redact(url))
        await asyncio.sleep(3)


def _is_stream(url: str) -> bool:
    """HLS / MJPEG / video URLs go through ffmpeg; plain http(s) URLs are treated as JPEG snapshots."""
    path = urlsplit(url).path.lower()
    return path.endswith((".m3u8", ".mjpg", ".mjpeg", ".mp4", ".ts"))


def ensure_running() -> str:
    """Start (or restart, if CAM_URL changed) the grabber. Returns the URL in use."""
    url = current_url()
    if url != STATE["url"]:
        STATE.update(url=url, jpeg=b"", ts=0.0, error="")
        if url:
            loop = _snapshot_loop if url.startswith("http") and not _is_stream(url) else _ffmpeg_loop
            STATE["task"] = asyncio.get_event_loop().create_task(loop(url))
    return url


def status() -> dict:
    url = ensure_running()
    return {
        "configured": bool(url),
        "url": redact(url),
        "age_s": round(time.time() - STATE["ts"], 1) if STATE["ts"] else None,
        "error": STATE["error"],
        "place": env_value("CAM_PLACE", config.CAM_PLACE) or config.CAM_PLACE,
        "lat": env_value("CAM_LAT") or None,
        "lng": env_value("CAM_LNG") or None,
    }
