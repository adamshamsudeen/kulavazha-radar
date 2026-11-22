"""PayalCam (camlive): one shared lake camera with a live view, PTZ control and chat.

Live:  python3 ptz_server.py            (settings in camlive.toml, served at http://localhost:8765)
Dev:   ./camlivectl dev                 (port 8766, synthetic test video, fake PTZ; never touches the camera)

Sections
  1. Config, logging, constants
  2. Request helpers (JSON bodies, validation)
  3. Camera worker (PTZ): ordered commands, dead-man auto-stop
  4. Zoom: target / applier model, optional runtime crop
  5. Encoder: ffmpeg, stderr tail, watchdog, janitor, stream state
  6. Ops: egress counter, caffeinate, tunnel watcher, alerts monitor
  7. Access control: owner detection
  8. Room: identity, presence, seat (turns), chat, reactions, mutes, stats, owner moderation
  9. HTTP handler and routes
 10. main()
"""
import atexit, base64, collections, hashlib, hmac, html, ipaddress, json, logging, math, os, re, shutil, signal
import subprocess, sys, unicodedata
import tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config
from config import CFG, DATA_DIR, HLS_DIR

if CFG["server"]["ptz"] == "fake":
    import fake_ptz as onvif
else:
    import onvif

# =====================================================================================
# 1. Config, logging, constants
# =====================================================================================
log = logging.getLogger("camlive")
PORT = config.PORT
SRV, STREAM, ROOM = CFG["server"], CFG["stream"], CFG["room"]
BOOT_EPOCH = int(time.time())  # HLS segment numbers start here, so names never repeat across restarts
BOOT = str(BOOT_EPOCH)
STARTED = time.monotonic()
PAGE_PATH = config.REPO / "ptz.html"
PLAYLIST = HLS_DIR / "live.m3u8"
PID_FILE = DATA_DIR / "ffmpeg.pid"
CURRENT_URL_FILE = config.LOG_DIR / "current_url.txt"
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
SHUTTING_DOWN = threading.Event()


def setup_logging():
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers = [RotatingFileHandler(config.LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5)]
    # Under launchd stderr is an unrotated file: copy the log there only for a terminal.
    # Crashes (uncaught tracebacks) still reach stderr either way.
    if sys.stderr is not None and sys.stderr.isatty():
        handlers.append(logging.StreamHandler(sys.stderr))
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False


def config_summary():
    """Startup summary. Never includes credentials, the RTSP URL or the ffmpeg argv."""
    return (f"port={PORT} bind={SRV['bind']} source={SRV['source']} ptz={SRV['ptz']} "
            f"zoom_live={bool(STREAM['zoom_live'])} levels={STREAM['zoom_levels']} crf={STREAM['crf']} "
            f"maxrate={STREAM['maxrate']} hls_dir={HLS_DIR} data_dir={DATA_DIR} "
            f"alerts={'on' if CFG['alerts']['ntfy_topic'] else 'off'} "
            f"owner_key={'set' if ROOM['owner_key'] else 'off'}")


def warn_weak_owner_key():
    key = ROOM["owner_key"] or ""
    if key and len(key) < 20:
        log.warning("room.owner_key is shorter than 20 characters; use a long random key "
                    "(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')")


# =====================================================================================
# 2. Request helpers
# =====================================================================================
class BadRequest(Exception):
    """Raised for malformed input; the handler answers 400 bad_request."""


def _reject_constant(name):
    raise ValueError(f"{name} is not allowed")


def parse_body(raw):
    """POST body -> dict. Empty means {}. NaN/Infinity, non-objects and bad JSON raise BadRequest."""
    if not raw or not raw.strip():
        return {}
    try:
        obj = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise BadRequest
    if not isinstance(obj, dict):
        raise BadRequest
    return obj


def velocity(v):
    """A pan/tilt speed: a finite int/float (not bool), clamped to [-1, 1], 2 decimals."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise BadRequest
    try:
        f = float(v)
    except OverflowError:
        raise BadRequest
    if not math.isfinite(f):
        raise BadRequest
    return round(max(-1.0, min(1.0, f)), 2) + 0.0  # + 0.0 turns -0.0 into 0.0


def ceil_s(x):
    return None if x is None else int(math.ceil(x))


# =====================================================================================
# 3. Camera worker (PTZ)
# HTTP handlers only record what they want (set_motion / set_stop) and return at once.
# One worker thread talks to the camera: commands stay ordered, the latest wins, at most
# 5 calls/s, and a move with no refresh stops by itself (dead-man), so a lost /stop is
# corrected within 1.2 s.
# =====================================================================================
STOP = "STOP"
HOLD_S, NUDGE_S, MIN_GAP_S, RETRY_S = 1.2, 0.4, 0.2, 2.0
CAMERA_DOWN_MESSAGE = "The camera is not responding right now."
cam_cond = threading.Condition()
CAM = {"desired": STOP, "deadline": 0.0, "sent": None, "last_call": float("-inf"), "retry_at": 0.0,
       "ok": None, "last_ok": 0.0, "last_error": None, "fail_since": None, "last_log": 0.0,
       "stop_urgent": False}
_ptz_token = {}


class CameraReplyError(RuntimeError):
    """The camera answered with a Fault or an HTTP error (the cached profile token may be stale)."""


def set_motion(pan, tilt, nudge=False, now=None):
    with cam_cond:
        CAM["desired"] = (pan, tilt)
        CAM["deadline"] = (time.monotonic() if now is None else now) + (NUDGE_S if nudge else HOLD_S)
        cam_cond.notify()


def set_stop():
    with cam_cond:
        CAM["desired"] = STOP
        cam_cond.notify()


def camera_step(ptz=None, clock=time.monotonic):
    """One worker iteration, no waiting. Returns the command sent successfully, else None."""
    ptz = ptz or onvif
    now = clock()
    with cam_cond:
        desired, deadline = CAM["desired"], CAM["deadline"]
        eff = desired if (desired != STOP and now < deadline) else STOP
        if eff == CAM["sent"]:
            return None
        # A move that failed may still have reached the camera (lost or late reply), and the
        # camera has no timeout of its own: the first STOP after it skips the gap and the back-off.
        urgent = eff == STOP and CAM["stop_urgent"]
        if not urgent and (now - CAM["last_call"] < MIN_GAP_S or now < CAM["retry_at"]):
            return None
        CAM["last_call"] = now
        if eff == STOP:
            CAM["stop_urgent"] = False
    try:
        if "t" not in _ptz_token:
            _ptz_token["t"] = ptz.profile_token()
        tok = _ptz_token["t"]
        r = ptz.stop(tok) if eff == STOP else ptz.move(eff[0], eff[1], tok)
        if not isinstance(r, str) or "Fault" in r or r.startswith("HTTP"):
            raise CameraReplyError(f"camera replied: {str(r)[:300]}")
    except Exception as e:
        if isinstance(e, CameraReplyError):
            _ptz_token.clear()  # timeouts and refused connections keep the cached token
        wall = time.time()
        with cam_cond:
            first = CAM["ok"] is not False
            CAM.update(sent=None, ok=False, last_error="camera unavailable", retry_at=clock() + RETRY_S)
            if eff != STOP:
                CAM["stop_urgent"] = True
            CAM["fail_since"] = CAM["fail_since"] or wall
            noisy = first or wall - CAM["last_log"] >= 60
            if noisy:
                CAM["last_log"] = wall
        if noisy:  # details go to the log only, never to clients
            log.warning("PTZ %s failed: %s", "stop" if eff == STOP else "move", str(e)[:400])
        return None
    with cam_cond:
        recovered = CAM["ok"] is False
        CAM.update(sent=eff, ok=True, last_ok=time.time(), last_error=None, fail_since=None)
    if recovered:
        log.info("PTZ working again")
    return eff


def camera_worker():
    while not SHUTTING_DOWN.is_set():
        with cam_cond:
            cam_cond.wait(0.1)
        try:
            camera_step()
        except Exception:
            log.exception("camera worker error")
            time.sleep(1)


def camera_public():
    with cam_cond:
        ok = CAM["ok"]
    return {"ok": ok, "message": CAMERA_DOWN_MESSAGE if ok is False else None}


def camera_health():
    with cam_cond:
        ok, last_ok, err = CAM["ok"], CAM["last_ok"], CAM["last_error"]
    return {"ok": ok, "last_ok_age_s": ceil_s(time.time() - last_ok) if last_ok else None, "last_error": err}


# =====================================================================================
# 4. Zoom
# /zoom only moves ZOOM["target"]. The encoder watchdog applies it once the target has been
# stable for 1.0 s and the cooldown has passed (1.5 s live mode, zoom_min_interval_s restart
# mode). Restart mode relaunches ffmpeg with a new crop; live mode sends ffmpeg a runtime
# crop command over stdin (no picture pause). ZOOM["level"] is what ffmpeg is producing.
# =====================================================================================
LEVELS = list(STREAM["zoom_levels"])
ZOOM_LIVE = bool(STREAM["zoom_live"])
SETTLE_S, LIVE_INTERVAL_S, RESEND_S = 1.0, 1.5, 5.0
view_lock = threading.Lock()
ZOOM = {"level": LEVELS[0], "target": LEVELS[0], "target_at": float("-inf"), "last_apply": float("-inf"),
        "by": None}
view = {"proc": None, "pid": None, "launched": 0.0, "launched_wall": 0.0, "running_zoom": None,
        "stop_reason": None}


def next_zoom(levels, current, step):
    i = levels.index(current) if current in levels else 0
    return levels[max(0, min(len(levels) - 1, i + (1 if step > 0 else -1)))]


def zoom_interval():
    return LIVE_INTERVAL_S if ZOOM_LIVE else float(STREAM["zoom_min_interval_s"])


def fmt_level(z):
    return f"{z:g}"


def parse_zoom_body(body):
    """{"step": 1|-1} -> ("step", s); {"level": x in LEVELS} -> ("level", x). Else BadRequest."""
    if "step" in body:
        s = body["step"]
        if isinstance(s, bool) or s not in (1, -1):
            raise BadRequest
        return "step", int(s)
    lv = body.get("level")
    if isinstance(lv, bool) or not isinstance(lv, (int, float)):
        raise BadRequest
    for l in LEVELS:
        if l == lv:
            return "level", l
    raise BadRequest


def set_zoom_target(kind, value, by, now=None):
    now = time.monotonic() if now is None else now
    with view_lock:
        ZOOM["target"] = next_zoom(LEVELS, ZOOM["target"], value) if kind == "step" else value
        ZOOM["target_at"] = now
        ZOOM["by"] = by


def zoom_public(now=None):
    now = time.monotonic() if now is None else now
    with view_lock:
        z = dict(ZOOM)
    applies = None
    if z["target"] != z["level"]:
        applies = round(max(0.0, max(z["target_at"] + SETTLE_S, z["last_apply"] + zoom_interval()) - now), 1)
    return {"level": z["level"], "target": z["target"], "levels": LEVELS, "applies_in_s": applies,
            "live": ZOOM_LIVE, "by": z["by"]}


def crop_command(z):
    return f"ccrop@z -1 w trunc(iw/{z}/2)*2\n".encode()


def live_crop(p, z):
    """ONE atomic write of the runtime crop command to ffmpeg's stdin. False if it failed."""
    cmd = crop_command(z)
    try:
        return os.write(p.stdin.fileno(), cmd) == len(cmd)
    except (BrokenPipeError, OSError, ValueError, AttributeError):
        return False


def zoom_tick(p, now=None, live=None):
    """Called by the encoder watchdog every 0.25 s. Returns True when a change was applied."""
    now = time.monotonic() if now is None else now
    live = ZOOM_LIVE if live is None else live
    with view_lock:
        z = ZOOM
        if z["target"] == z["level"] or now - z["target_at"] < SETTLE_S or now - z["last_apply"] < zoom_interval():
            return False
        target, by = z["target"], z["by"] or "Someone"
        z["last_apply"] = now
        restart = not (live and live_crop(p, target))
        z["level"] = target
        if restart:
            view["stop_reason"] = "zoom"
        else:
            view["running_zoom"] = target
    log.info("zoom applied: %s× by %s (%s)", fmt_level(target), by, "restart" if restart else "live")
    post_sys(f"🔍 {by} zoomed to {fmt_level(target)}×")
    if restart:
        stop_ffmpeg(p)
    return True


# =====================================================================================
# 5. Encoder: one shared ffmpeg turns the camera into HLS files every viewer downloads
# =====================================================================================
restarts = {"zoom": 0, "stall": 0, "exit": 0, "error": 0}
ENC = {"last_exit_code": None}
COMMAND_OK = re.compile(r"Command reply for stream -?\d+: ret:0\b")
_RTSP_PATH = CFG["camera"]["rtsp_path"]


def redact(line):
    line = re.sub(r"//[^@/\s]+@", "//***@", line)
    if _RTSP_PATH:
        line = line.replace(_RTSP_PATH, "<path>")
    return line


class Tail:
    """Last N ffmpeg stderr lines, redacted, with consecutive repeats collapsed into 'line (xN)'."""

    def __init__(self, maxlen=40):
        self._lines = collections.deque(maxlen=maxlen)
        self._last, self._n = None, 0
        self._lock = threading.Lock()

    def add(self, raw):
        line = redact(raw.rstrip())
        # ffmpeg echoes every stdin command; successful zoom replies are noise
        if not line.strip() or line.startswith("Enter command:") or COMMAND_OK.match(line):
            return
        with self._lock:
            if line == self._last and self._lines:
                self._n += 1
                self._lines[-1] = f"{line} (x{self._n})"
            else:
                self._last, self._n = line, 1
                self._lines.append(line)

    def lines(self, n=None):
        with self._lock:
            out = list(self._lines)
        return out[-n:] if n else out


TAIL = Tail()


def input_args():
    if SRV["source"] == "test":
        return ["-re", "-f", "lavfi", "-i", "testsrc2=size=2304x1296:rate=15"]
    # main stream: 2304x1296 HEVC. It only streams reliably over UDP on this camera.
    # The URL carries the camera credentials and ends up in ffmpeg's argv, visible to local `ps`
    # (plain ffmpeg has no other way to take RTSP credentials): keep the Mac single-user.
    return ["-rtsp_transport", "udp", "-buffer_size", "4194304", "-timeout", "5000000", "-i", config.RTSP_URL]


def video_filter(z):
    # centred crop of 1/z of the width at 16:9 (z=1 is the full frame), always scaled to the same size.
    # Up to ~1.8x shows real extra detail (2304 / 1280); beyond that it is enlarged.
    return f"crop@z=w=trunc(iw/{z}/2)*2:h=trunc(ow*9/16/2)*2,scale={STREAM['width']}:{STREAM['height']}"


def ffmpeg_args(z, start=None):
    """The full argv. It contains the RTSP URL: never log or print it."""
    fps = str(STREAM["fps"])
    return [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostats", *input_args(),
            "-an", "-vf", video_filter(z), "-r", fps,
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-profile:v", "main",
            "-pix_fmt", "yuv420p", "-crf", str(STREAM["crf"]), "-maxrate", STREAM["maxrate"],
            "-bufsize", STREAM["bufsize"], "-g", fps, "-keyint_min", fps, "-sc_threshold", "0",
            "-f", "hls", "-hls_time", "1", "-hls_list_size", "6", "-hls_delete_threshold", "6",
            "-start_number", str(BOOT_EPOCH if start is None else start),
            "-hls_flags", "delete_segments+append_list+discont_start+omit_endlist+program_date_time+temp_file",
            "-hls_segment_filename", str(HLS_DIR / "seg%d.ts"), str(PLAYLIST)]


def stop_ffmpeg(p):
    """Ask ffmpeg to quit ('q') so it can send RTSP TEARDOWN; escalate if it does not."""
    if p is None or p.poll() is not None:
        return
    try:
        p.stdin.write(b"q")
        p.stdin.flush()
        p.wait(3)
        return
    except Exception:
        pass
    try:
        p.terminate()
        p.wait(2)
        return
    except Exception:
        pass
    try:
        p.kill()
        p.wait(2)
    except Exception:
        pass


def read_stderr(p):
    try:
        for raw in iter(p.stderr.readline, b""):
            line = raw.decode(errors="replace")
            if line.startswith("Command reply") and not COMMAND_OK.match(line):
                log.warning("ffmpeg rejected a zoom command: %s", redact(line.strip())[:200])
            TAIL.add(line)
    except (OSError, ValueError):
        pass
    finally:
        try:
            p.stderr.close()
        except OSError:
            pass


LIVE_AGE_S = 6  # the stream counts as live while the playlist is younger than this
SEQ = {"next": BOOT_EPOCH}  # first segment number for the next ffmpeg run


def next_segment_number(text, fallback):
    """One past the highest segment listed in a playlist, so names never repeat (segments are cached)."""
    return max([fallback, *(int(n) + 1 for n in re.findall(r"seg(\d+)\.ts", text))])


def retire_stale_playlist(hls_dir=None, now=None):
    """Before a relaunch: if no frames have arrived for LIVE_AGE_S (an outage), delete the old
    playlist and its segments, so the new run's playlist lists only fresh video. Otherwise a viewer
    joining just after the camera comes back starts a second or two into pre-outage frames.
    A live playlist is kept: ffmpeg's append_list continues it seamlessly."""
    hls_dir = HLS_DIR if hls_dir is None else hls_dir
    now = time.time() if now is None else now
    playlist = hls_dir / "live.m3u8"
    try:
        text, age = playlist.read_text(), now - playlist.stat().st_mtime
    except OSError:
        return False
    SEQ["next"] = next_segment_number(text, SEQ["next"])
    if age < LIVE_AGE_S:
        return False
    for name in set(re.findall(r"seg\d+\.ts", text)):
        (hls_dir / name).unlink(missing_ok=True)
    playlist.unlink(missing_ok=True)
    return True


def launch_ffmpeg():
    # view_lock is held from reading the level through Popen to recording the process,
    # so a zoom apply can never race a launch and leave a stale crop.
    with view_lock:
        z = ZOOM["level"]
        if retire_stale_playlist():
            log.info("dropped the pre-outage playlist; next segment %d", SEQ["next"])
        p = subprocess.Popen(ffmpeg_args(z, SEQ["next"]), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE)
        os.set_blocking(p.stdin.fileno(), False)  # a stuck ffmpeg can never block the watchdog
        view.update(proc=p, pid=p.pid, launched=time.monotonic(), launched_wall=time.time(),
                    running_zoom=z, stop_reason=None)
    try:
        PID_FILE.write_text(str(p.pid))
    except OSError:
        log.warning("could not write %s", PID_FILE)
    threading.Thread(target=read_stderr, args=(p,), daemon=True, name="ffmpeg-stderr").start()
    return p


def playlist_mtime():
    try:
        return PLAYLIST.stat().st_mtime
    except OSError:
        return None


def janitor(hls_dir=HLS_DIR, now=None):
    """Delete orphan segments (not in the playlist, older than 60 s) and stale .tmp files."""
    now = time.time() if now is None else now
    try:
        listed = set(re.findall(r"seg\d+\.ts", (hls_dir / "live.m3u8").read_text()))
    except OSError:
        listed = set()
    removed = 0
    for f in list(hls_dir.glob("seg*.ts")) + list(hls_dir.glob("*.tmp")):
        try:
            if f.name not in listed and now - f.stat().st_mtime > 60:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def watch(p):
    """Watchdog for one ffmpeg run, ticking every 0.25 s. Returns the exit reason."""
    grace, stall = float(STREAM["first_segment_grace_s"]), float(STREAM["stall_seconds"])
    last_resend = last_janitor = time.monotonic()
    while p.poll() is None:
        time.sleep(0.25)
        now = time.monotonic()
        if zoom_tick(p, now):
            last_resend = now
        elif ZOOM_LIVE and now - last_resend >= RESEND_S:
            # idempotent; heals a command lost while ffmpeg was still connecting
            with view_lock:
                live_crop(p, ZOOM["level"])
            last_resend = now
        if now - last_janitor >= 30:
            last_janitor = now
            janitor()
        mtime = playlist_mtime()
        if mtime is not None and mtime >= view["launched_wall"]:
            stalled = time.time() - mtime > stall
        else:
            stalled = now - view["launched"] > grace  # first segment after launch gets more time
        if stalled and p.poll() is None and not SHUTTING_DOWN.is_set():
            log.warning("live view stalled, restarting ffmpeg")
            with view_lock:
                view["stop_reason"] = "stall"
            stop_ffmpeg(p)
    return view["stop_reason"] or "exit"


def relaunch_delay(fails):
    """Seconds to wait before relaunching after `fails` quick failures in a row: 3, 6, 12, 24, 30..."""
    return min(30, 3 * 2 ** max(0, min(fails - 1, 4)))


def encoder():
    fails = 0
    while not SHUTTING_DOWN.is_set():
        p, reason = None, "error"
        try:
            p = launch_ffmpeg()
            reason = watch(p)
        except Exception:
            log.exception("encoder error")
            if p is not None:
                stop_ffmpeg(p)
        code = p.poll() if p is not None else None
        ran = time.monotonic() - view["launched"] if p is not None else 0
        if p is not None:
            try:
                p.stdin.close()
            except Exception:
                pass
        if SHUTTING_DOWN.is_set():
            break
        ENC["last_exit_code"] = code
        restarts[reason] += 1
        log.log(logging.INFO if reason == "zoom" else logging.WARNING,
                "ffmpeg exited code=%s after %ds reason=%s%s", code, int(ran), reason,
                "".join("\n  | " + l for l in TAIL.lines(5)) if reason != "zoom" else "")
        if reason != "zoom":
            # camera offline or stream dropped: retry after 3 s, backing off to 30 s while it keeps failing
            fails = 0 if ran >= 60 else fails + 1
            SHUTTING_DOWN.wait(relaunch_delay(fails))


STREAM_STATE = {"down_since": None}
stream_lock = threading.Lock()


def segment_age(now=None):
    m = playlist_mtime()
    return None if m is None else max(0.0, (time.time() if now is None else now) - m)


def stream_public(now=None):
    """'starting' only within first_segment_grace_s of the stream going down (not of the latest
    relaunch, which happens every few seconds while the camera is offline), then 'offline'."""
    now = time.time() if now is None else now
    age = segment_age(now)
    live = age is not None and age < LIVE_AGE_S
    with stream_lock:
        if live:
            STREAM_STATE["down_since"] = None
        elif STREAM_STATE["down_since"] is None:
            STREAM_STATE["down_since"] = now
        down = STREAM_STATE["down_since"]
    if live:
        state = "live"
    elif view["proc"] is not None and now - down < float(STREAM["first_segment_grace_s"]):
        state = "starting"
    else:
        state = "offline"
    return {"state": state, "down_since": down, "segment_age_s": None if age is None else round(age, 1)}


def kill_stale_ffmpeg():
    """Terminate an ffmpeg left behind by a previous run of THIS instance (same HLS_DIR)."""
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return
    cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    if "ffmpeg" in cmd and str(HLS_DIR) in cmd:  # cmd holds the RTSP URL: never log it
        log.warning("stopping leftover ffmpeg pid %s", pid)
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(30):
                time.sleep(0.1)
                os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            log.warning("could not stop leftover ffmpeg pid %s", pid)
    PID_FILE.unlink(missing_ok=True)


def remove_old_tmp_dirs(tmp=None, ps_output=None, now=None):
    """Delete $TMPDIR/camlive-hls-* dirs from the old version. Skips any dir a running process
    names on its command line or that changed in the last 10 minutes (an old server may still use it)."""
    tmp = Path(tmp or tempfile.gettempdir())
    now = time.time() if now is None else now
    if ps_output is None:
        ps_output = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
    removed = []
    for d in tmp.glob("camlive-hls-*"):
        try:
            if not d.is_dir() or d.name in ps_output:
                continue
            newest = max([d.stat().st_mtime] + [f.stat().st_mtime for f in d.iterdir()])
            if now - newest < 600:
                continue
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
        except OSError:
            continue
    return removed


def prepare_dirs():
    HLS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    kill_stale_ffmpeg()
    for pattern in ("*.ts", "*.tmp", "*.m3u8"):
        for f in HLS_DIR.glob(pattern):
            f.unlink(missing_ok=True)
    if SRV["source"] == "camera":  # the dev instance never touches other instances' files
        for name in remove_old_tmp_dirs():
            log.info("removed old HLS dir %s", name)


def hls_dir_bytes():
    total = 0
    try:
        files = list(HLS_DIR.iterdir())
    except OSError:
        return 0
    for f in files:
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


# =====================================================================================
# 6. Ops: egress counter, caffeinate, tunnel URL watcher, alerts
# =====================================================================================
_egress = collections.deque()
_egress_lock = threading.Lock()
EGRESS_WINDOW_S = 10.0


def egress_add(n, now=None):
    now = time.monotonic() if now is None else now
    with _egress_lock:
        _egress.append((now, n))
        while _egress and now - _egress[0][0] > EGRESS_WINDOW_S:
            _egress.popleft()


def egress_mbps(now=None):
    now = time.monotonic() if now is None else now
    with _egress_lock:
        while _egress and now - _egress[0][0] > EGRESS_WINDOW_S:
            _egress.popleft()
        total = sum(n for _, n in _egress)
    return round(total * 8 / EGRESS_WINDOW_S / 1e6, 2)


def start_caffeinate():
    """Hold idle sleep off for exactly this server's lifetime."""
    try:
        subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        log.warning("caffeinate not available; the Mac may sleep")


def send_alert(text):
    """ntfy push, only if alerts.ntfy_topic is set. Errors are swallowed."""
    log.info("alert: %s", text)
    topic = CFG["alerts"]["ntfy_topic"]
    if not topic:
        return
    try:
        req = urllib.request.Request(f"{CFG['alerts']['ntfy_server'].rstrip('/')}/{topic}",
                                     data=text.encode(), method="POST")
        urllib.request.urlopen(req, timeout=5).close()
    except Exception:
        pass


TUNNEL = {"url": None}
HOSTNAME_RE = re.compile(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?")


def fetch_tunnel_host():
    with urllib.request.urlopen(CFG["tunnel"]["metrics_url"], timeout=2) as r:
        return json.loads(r.read(4096)).get("hostname")


def tunnel_check(fetch=fetch_tunnel_host, url_file=None):
    url_file = url_file or CURRENT_URL_FILE
    try:
        host = (fetch() or "").strip().lower()
    except Exception:
        return None
    if not HOSTNAME_RE.fullmatch(host):
        return None
    url = f"https://{host}"
    if url == TUNNEL["url"]:
        return None
    TUNNEL["url"] = url
    try:
        previous = url_file.read_text().strip()
    except OSError:
        previous = None
    if previous == url:
        return None  # same URL as before this server started: nothing to tell anyone
    log.info("tunnel URL is now %s", url)
    try:
        url_file.parent.mkdir(parents=True, exist_ok=True)
        url_file.write_text(url + "\n")
    except OSError:
        log.warning("could not write %s", url_file)
    send_alert(f"camlive: new URL {url}")
    return url


def tunnel_watcher():
    while not SHUTTING_DOWN.is_set():
        tunnel_check()
        SHUTTING_DOWN.wait(30)


VIDEO_DOWN_ALERT_S, PTZ_DOWN_ALERT_S = 120, 300
MON = {"video_down_since": None, "ptz_alerted": False}


def monitor_check(now=None, stream=None, alert=send_alert):
    """Alerts on state changes only: video down/back, PTZ failing."""
    now = time.time() if now is None else now
    st = stream or stream_public()
    if st["state"] != "live":
        down = st["down_since"]
        if MON["video_down_since"] is None and down is not None and now - down >= VIDEO_DOWN_ALERT_S:
            MON["video_down_since"] = down
            alert(f"camlive: video down since {time.strftime('%H:%M', time.localtime(down))}")
    elif MON["video_down_since"] is not None:
        mins = max(1, round((now - MON["video_down_since"]) / 60))
        MON["video_down_since"] = None
        alert(f"camlive: video back (down {mins}m)")
    with cam_cond:
        ok, fail_since = CAM["ok"], CAM["fail_since"]
    if ok is False and fail_since and now - fail_since >= PTZ_DOWN_ALERT_S and not MON["ptz_alerted"]:
        MON["ptz_alerted"] = True
        alert("camlive: PTZ failing for 5 min")
    elif ok and MON["ptz_alerted"]:
        MON["ptz_alerted"] = False


def monitor():
    n = 0
    while not SHUTTING_DOWN.wait(10):
        n += 1
        try:
            monitor_check()
            if n % 6 == 0:
                flush_client_errors()
        except Exception:
            log.exception("monitor error")


# =====================================================================================
# 7. Access control: owner detection
# Owner = local request (no CF-Connecting-IP and Host is 127.0.0.1:{port} or localhost:{port}),
# or X-Owner-Key equal to room.owner_key. 5 wrong keys per network (IPv4 address, IPv6 /48) in
# 10 min -> header ignored 10 min.
# =====================================================================================
OWNER_FAIL_LIMIT, OWNER_WINDOW_S, OWNER_TABLE_MAX = 5, 600, 5000
_owner_fails = {}  # lockout key -> {"fails": [monotonic...], "locked_until": monotonic}
_owner_lock = threading.Lock()


def owner_lockout_key(ip):
    """IPv6 callers rotate freely inside their /64 (and often a /48), so lock out the /48."""
    return norm_net(ip) or norm_ip(ip)


def _prune_owner_fails(now):
    """Caller holds _owner_lock. Drop stale failures and empty records; hard cap on the table."""
    for k in list(_owner_fails):
        r = _owner_fails[k]
        r["fails"] = [t for t in r["fails"] if now - t < OWNER_WINDOW_S]
        if not r["fails"] and r["locked_until"] <= now:
            del _owner_fails[k]
    if len(_owner_fails) > OWNER_TABLE_MAX:
        def newest(k):
            r = _owner_fails[k]
            return max(r["fails"] + [r["locked_until"] - OWNER_WINDOW_S])
        for k in sorted(_owner_fails, key=newest)[:len(_owner_fails) - OWNER_TABLE_MAX]:
            del _owner_fails[k]


def client_ip(handler):
    return handler.headers.get("CF-Connecting-IP") or handler.client_address[0]


def is_local(handler):
    if handler.headers.get("CF-Connecting-IP"):
        return False
    return (handler.headers.get("Host") or "").lower() in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")


def check_owner_key(ip, key, configured, now=None):
    """ip: the raw client address; failures are counted per owner_lockout_key(ip)."""
    now = time.monotonic() if now is None else now
    if not key or not configured:
        return False
    lk = owner_lockout_key(ip)
    with _owner_lock:
        rec = _owner_fails.get(lk)
        if rec is not None and rec["locked_until"] > now:
            return False
        if hmac.compare_digest(key.encode(), configured.encode()):
            return True
        if rec is None:
            if len(_owner_fails) >= 1000:  # keep the table small
                _prune_owner_fails(now)
            rec = _owner_fails[lk] = {"fails": [], "locked_until": 0.0}
        rec["fails"] = [t for t in rec["fails"] if now - t < OWNER_WINDOW_S] + [now]
        if len(rec["fails"]) >= OWNER_FAIL_LIMIT:
            rec["fails"], rec["locked_until"] = [], now + OWNER_WINDOW_S
            log.warning("owner key: too many wrong keys from one network, ignoring it for 10 min")
    return False


def is_owner(handler):
    if not hasattr(handler, "_is_owner"):  # evaluated once per request, so a wrong key counts once
        handler._is_owner = is_local(handler) or check_owner_key(
            client_ip(handler), handler.headers.get("X-Owner-Key"), ROOM["owner_key"])
    return handler._is_owner


# =====================================================================================
# 8. Room: identity, presence, seat (turns), chat, reactions, mutes, stats, owner moderation
# The infra above calls only the hooks can_control / control_denied / on_driver_change /
# caller_name / post_sys / seat_public / seat_summary / viewer_stats / named_viewers /
# room_public / state_payload. Locks: ROOM_LOCK (presence, seat, mutes, stats) may be held
# while taking CHAT_LOCK (post_sys), never the other way round. REACT_LOCK is never nested.
# =====================================================================================
ROOM_LOCK = threading.RLock()
seat_lock = ROOM_LOCK  # the roadmap's name for it
CHAT_LOCK = threading.Lock()
REACT_LOCK = threading.Lock()
SECRET_FILE = DATA_DIR / "secret.key"
CHAT_FILE = DATA_DIR / "chat.json"
STATS_FILE = DATA_DIR / "stats.json"
MOD_FILE = DATA_DIR / "moderation.json"


def mono():
    return time.monotonic()


# ---- identity: client_key, ip_key and the 3-emoji tag, all keyed by a per-install secret ----
# 64 single-code-point emoji with default emoji presentation (no ZWJ, no VS16).
EMOJI64 = tuple("🐶🐱🐭🐹🐰🦊🐻🐼🐨🐯🦁🐮🐷🐸🐵🐔🐧🐤🦆🦉🐺🐴🦄🐝🐛🦋🐌🐞🐢🐙🦀🐠"
                "🐬🐳🦒🐘🦔🦓🍎🍐🍊🍋🍌🍉🍇🍓🍒🍑🥝🍍🥥🥑🌽🥕🍄🍩🍪🌵🌻🌷🍀🌈🎈🚀")
_SECRET = {"key": None}


def load_secret(path=None):
    """32 random bytes, created on first boot (mode 600) so tags stay stable across restarts."""
    path = Path(path or SECRET_FILE)
    try:
        data = path.read_bytes()
        if len(data) == 32:
            return data
    except OSError:
        pass
    data = os.urandom(32)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)
    return data


def secret():
    if _SECRET["key"] is None:
        _SECRET["key"] = load_secret()
    return _SECRET["key"]


def _mac(msg):
    return hmac.new(secret(), msg, hashlib.sha256)


def client_key(cid):
    return _mac(b"cid:" + cid.encode()).hexdigest()[:16]


def norm_ip(ip):
    """'local' when absent; IPv6 reduced to its /64 prefix. Never stored raw (only hashed)."""
    ip = (ip or "").strip()
    if not ip:
        return "local"
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip[:64]
    if a.version == 6:
        if a.ipv4_mapped:
            return str(a.ipv4_mapped)
        return str(ipaddress.ip_network(f"{a}/64", strict=False))
    return str(a)


def ip_key(ip):
    return _mac(b"ip:" + norm_ip(ip).encode()).hexdigest()[:16]


def norm_net(ip):
    """The IPv6 /48 a client sits in (one site: a free tunnel broker hands out a whole /48, so
    the /64 alone is too fine for limits). None for IPv4 and local requests: there the ip_key tier
    is the network already."""
    try:
        a = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return None
    if a.version != 6 or a.ipv4_mapped:
        return None
    return str(ipaddress.ip_network(f"{a}/48", strict=False))


def net_key(ip):
    n = norm_net(ip)
    return _mac(b"net:" + n.encode()).hexdigest()[:16] if n else None


def tag_for(ck):
    return "".join(EMOJI64[b % 64] for b in _mac(b"tag:" + ck.encode()).digest()[:3])


CC_RE = re.compile(r"[A-Z][A-Z0-9]")


def country_of(handler):
    """ISO-2 from Cloudflare, 'LOCAL' for genuinely local requests (see is_local), else ''."""
    if not handler.headers.get("CF-Connecting-IP"):
        return "LOCAL" if is_local(handler) else ""
    cc = (handler.headers.get("CF-IPCountry") or "").strip().upper()
    return cc if CC_RE.fullmatch(cc) else ""


def real_country(cc):
    return bool(re.fullmatch(r"[A-Z]{2}", cc or "")) and cc != "XX"


def ident(handler):
    """{"ck", "ik", "nk", "tag", "country", "owner"} for this request, computed once.
    nk is the IPv6 /48 key (None for IPv4 and local requests)."""
    if getattr(handler, "_ident", None) is None:
        cid = handler.client_id
        ck = client_key(cid) if cid else None
        ip = handler.headers.get("CF-Connecting-IP")
        handler._ident = {"ck": ck, "ik": ip_key(ip), "nk": net_key(ip),
                          "tag": tag_for(ck) if ck else "", "country": country_of(handler),
                          "owner": is_owner(handler)}
    return handler._ident


# ---- text cleaning ----
STRIP_CHARS = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200B, 0x2060, 0xFEFF))
RESERVED = frozenset({"owner", "admin", "mod", "moderator", "host", "camera", "system", "official"})
_LEET = str.maketrans("013457", "oleast")


def clean_text(s, multiline=False):
    """NFKC; drop control chars (keep '\\n' when multiline), bidi overrides and zero-width
    spaces (U+200C/U+200D are kept); at most 3 combining marks in a row; max 2 blank-line run, 8 lines."""
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize("NFKC", s).replace("\r\n", "\n")
    if not multiline:
        s = re.sub(r"[\t\n\r\v\f]", " ", s)
    out, marks = [], 0
    for ch in s:
        if ch in STRIP_CHARS:
            continue
        cat = unicodedata.category(ch)
        if cat == "Cc" and not (multiline and ch == "\n"):
            continue
        if cat == "Mn":
            marks += 1
            if marks > 3:
                continue
        else:
            marks = 0
        out.append(ch)
    s = "".join(out)
    if multiline:
        s = "\n".join(line.rstrip() for line in s.split("\n"))
        s = re.sub(r"\n{3,}", "\n\n", s.strip())
        s = "\n".join(s.split("\n")[:8])
    return s.strip()


# Characters that render as nothing (or as a blank) but are letters or symbols to Unicode.
NAME_FILLERS = frozenset("ᅟᅠㅤﾠ⠀᠎͏឴឵")
# Cyrillic and Greek lookalikes of Latin letters (after casefold), for the reserved-name check only.
_CONFUSABLE = str.maketrans({
    "а": "a", "в": "b", "с": "c", "ԁ": "d", "е": "e", "һ": "h", "н": "h", "і": "i", "ј": "j",
    "к": "k", "м": "m", "о": "o", "р": "p", "ԛ": "q", "ѕ": "s", "т": "t", "у": "y", "х": "x",
    "ԝ": "w", "ү": "y", "ѵ": "v", "ӏ": "l",
    "α": "a", "β": "b", "ε": "e", "η": "n", "ι": "i", "κ": "k", "μ": "u", "ν": "v", "ο": "o",
    "ρ": "p", "τ": "t", "υ": "u", "χ": "x", "ω": "w", "γ": "y", "ϲ": "c", "ϳ": "j",
    "ı": "i", "ɩ": "i", "ɑ": "a", "ɡ": "g", "ʀ": "r", "ᴏ": "o", "ꞵ": "b",
})


def clean_name(s):
    s = "".join(ch for ch in clean_text(s, False)
                if unicodedata.category(ch) != "Cf" and ch not in NAME_FILLERS)
    return re.sub(r"\s+", " ", s).strip()


def visible_chars(s):
    return sum(1 for ch in s if unicodedata.category(ch)[0] in "LNSP")


def fold_name(s):
    s = s.casefold().translate(_CONFUSABLE).translate(_LEET)
    return "".join(ch for ch in s if ch.isalnum() and ch not in NAME_FILLERS)


def is_reserved(name):
    f = fold_name(name)
    return f in RESERVED or (bool(f) and f == fold_name(ROOM["owner_name"] or "Owner"))


def name_check(raw, owner=False):
    """-> (cleaned name or '', None | 'invalid' | 'reserved')."""
    if owner:
        return ROOM["owner_name"] or "Owner", None
    if not isinstance(raw, str) or not raw.strip():
        return "", None
    n = clean_name(raw)
    if not 2 <= len(n) <= 24 or visible_chars(n) < 2:
        return "", "invalid"
    if is_reserved(n):
        return "", "reserved"
    return n, None


# ---- token bucket ----
class Bucket:
    def __init__(self, burst, every_s, now):
        self.burst, self.every, self.tokens, self.t = float(burst), float(every_s), float(burst), now

    def _fill(self, now):
        self.tokens = min(self.burst, self.tokens + max(0.0, now - self.t) / self.every)
        self.t = now

    def wait(self, now):
        """Seconds until one token is available (0.0 = now)."""
        self._fill(now)
        return 0.0 if self.tokens >= 1 else (1 - self.tokens) * self.every

    def take(self, now):
        self._fill(now)
        self.tokens -= 1

    def idle(self, now):
        self._fill(now)
        return self.tokens >= self.burst


def _bucket_of(v):
    return v if isinstance(v, Bucket) else v.get("bucket") if isinstance(v, dict) else None


def prune_buckets(table, now, idle_s=600):
    """Drop buckets untouched for idle_s. Every refill period here is far below idle_s, so such a
    bucket is full again. (Reads b.t before anything refills it: idle() would move b.t to now.)"""
    for k in [k for k, v in table.items() if (b := _bucket_of(v)) is not None and now - b.t > idle_s]:
        del table[k]


RATE_TABLE_MAX = 20000


def make_room(table, now, cap=RATE_TABLE_MAX):
    """Called before inserting into a rate table: prune idle entries, then evict the least
    recently used tenth if it is still full, so a flood can never grow the table without bound."""
    if len(table) < cap:
        return
    prune_buckets(table, now)
    if len(table) >= cap:
        for k in sorted(table, key=lambda k: _bucket_of(table[k]).t)[:max(1, cap // 10)]:
            del table[k]


# ---- mutes: {"c:<client_key>" | "i:<ip_key>" | "n:<net_key>": monotonic until} ----
# Saved to moderation.json as wall-clock times (with the owner's slow mode), so a restart keeps them.
MUTES = {}


def mute_left(ck, ik, now=None, nk=None):
    now = mono() if now is None else now
    until = max(MUTES.get(f"c:{ck}", 0.0) if ck else 0.0, MUTES.get(f"i:{ik}", 0.0) if ik else 0.0,
                MUTES.get(f"n:{nk}", 0.0) if nk else 0.0)
    return until - now if until > now else 0.0


def muted_s_of(idt, now=None):
    """Seconds of mute left (int, rounded up) or None. The owner is never muted."""
    if idt["owner"]:
        return None
    left = mute_left(idt["ck"], idt["ik"], now, idt.get("nk"))
    return ceil_s(left) if left > 0 else None


def muted_body(secs):
    return {"error": "muted", "message": f"You're muted for {max(1, math.ceil(secs / 60))} min.", "muted_s": secs}


# ---- the seat: a pure state machine (clock and callbacks injected), used under seat_lock ----
class Seat:
    """Turn-taking for the camera. Modes: free, driving, offered, locked (see the roadmap)."""

    def __init__(self, clock=time.monotonic, turn_s=45, idle_s=20, ready_s=10, grace_s=20, max_queue=30,
                 queue_per_ip=2, present=lambda k: True, muted=lambda k: False, post=lambda text: None,
                 on_change=lambda key: None, queue_per_net=3, away=lambda k: False, ready_away_s=30):
        self.clock = clock
        self.turn_s, self.idle_s, self.ready_s, self.grace_s = turn_s, idle_s, ready_s, grace_s
        self.max_queue, self.queue_per_ip, self.queue_per_net = max_queue, queue_per_ip, queue_per_net
        self.present, self.muted, self.post, self.on_change = present, muted, post, on_change
        # away(k): k's tab is in the background (only a slow heartbeat): its offer lasts ready_away_s
        self.away, self.ready_away_s = away, ready_away_s
        self.locked = False
        self.driver = self.started = self.until = self.last_input = None
        self.offered = self.offer_until = None
        self.queue = []
        self.info = {}  # key -> {"name", "country", "tag", "owner", "ip_key", "net_key"}

    # -- helpers --
    @property
    def mode(self):
        if self.locked:
            return "locked"
        return "driving" if self.driver else "offered" if self.offered else "free"

    def note(self, key, info):
        self.info[key] = info

    def name(self, key):
        return (self.info.get(key) or {}).get("name") or "Someone"

    def _owner(self, key):
        return bool((self.info.get(key) or {}).get("owner"))

    def _alive(self, key):
        return self.present(key) and not self.muted(key)

    def _now(self, now):
        return self.clock() if now is None else now

    def _start(self, key, now):
        self.driver, self.offered, self.offer_until = key, None, None
        self.started = self.last_input = now
        if key in self.queue:
            self.queue.remove(key)
        self.until = now + self.turn_s if (self.queue and not self._owner(key)) else None
        self.post(f"🎮 {self.name(key)} took the wheel")
        self.on_change(key)

    def _clear_driver(self):
        self.driver = self.started = self.until = self.last_input = None

    def _release(self, now, text=None):
        self._clear_driver()
        if text:
            self.post(text)
        self.on_change(None)
        self._advance(now)

    def _drop_offer(self, now, text=None):
        self.offered = self.offer_until = None
        if text:
            self.post(text)
        self._advance(now)

    def _advance(self, now):
        """Seat empty: offer it to the next live entry in the queue."""
        if self.locked or self.driver or self.offered:
            return
        while self.queue:
            k = self.queue.pop(0)
            if self._alive(k):
                ready = max(self.ready_s, self.ready_away_s) if self.away(k) else self.ready_s
                self.offered, self.offer_until = k, now + ready
                self.post(f"🎮 It's {self.name(k)}'s turn!")
                return

    def _fix_until(self, now):
        if not self.driver or self._owner(self.driver):
            return
        if not self.queue:
            self.until = None  # nobody waiting: no time limit
        elif self.until is None:
            self.until = max(self.started + self.turn_s, now + self.grace_s)

    # -- events --
    def tick(self, now=None):
        now = self._now(now)
        self.queue = [k for k in self.queue if self._alive(k)]
        if self.driver:
            k = self.driver
            if not self.present(k):
                self._release(now, f"👋 {self.name(k)} left the wheel")
            elif self.muted(k):
                self._release(now)  # mutes never produce a sys line
            elif not self._owner(k):
                if now - self.last_input > self.idle_s:
                    self._release(now, f"💤 {self.name(k)} let go of the wheel")
                elif self.until is not None and self.queue and now >= self.until:
                    self._release(now, f"⏱ {self.name(k)}'s turn is over")
        elif self.offered:
            k = self.offered
            if not self._alive(k):
                self._drop_offer(now)
            elif now >= self.offer_until:
                self._drop_offer(now, f"⏭ {self.name(k)} missed their turn")
        else:
            self._advance(now)
        self._fix_until(now)

    def control(self, key, now=None, claim=True, owner=False):
        """/move, /zoom (claim=True) and /stop (claim=False). -> 'ok' | 'locked' | 'not_driver'."""
        now = self._now(now)
        if owner:
            return "ok"
        if self.locked:
            return "locked"
        if key == self.driver:
            self.last_input = now
            return "ok"
        if claim and self._alive(key):
            if key == self.offered or (not self.driver and not self.offered and not self.queue):
                self._start(key, now)
                return "ok"
        return "not_driver"

    def join(self, key, now=None):
        """-> 'ok' | 'locked' | 'already_driving' | 'queue_full' | 'ip_limit'."""
        now = self._now(now)
        if self.locked:
            return "locked"
        if key == self.driver:
            return "already_driving"
        if key == self.offered or key in self.queue:
            return "ok"
        if not self.driver and not self.offered and not self.queue:
            self._start(key, now)
            return "ok"
        if len(self.queue) >= self.max_queue:
            return "queue_full"
        for field, cap in (("ip_key", self.queue_per_ip), ("net_key", self.queue_per_net)):
            v = (self.info.get(key) or {}).get(field)
            if v and sum(1 for q in self.queue if (self.info.get(q) or {}).get(field) == v) >= cap:
                return "ip_limit"
        self.queue.append(key)
        self._fix_until(now)
        self._advance(now)
        return "ok"

    def leave(self, key, now=None):
        now = self._now(now)
        if key == self.driver:
            self._release(now, f"👋 {self.name(key)} left the wheel")
        elif key == self.offered:
            self._drop_offer(now)
        elif key in self.queue:
            self.queue.remove(key)
            self._fix_until(now)

    def claim(self, key, now=None):
        now = self._now(now)
        if key is not None and key == self.offered and not self.locked and self._alive(key):
            self._start(key, now)
            return "ok"
        return "not_offered"

    def remove(self, key, now=None):
        """Muted: out of the seat and the queue, silently."""
        now = self._now(now)
        if key == self.driver:
            self._release(now)
        elif key == self.offered:
            self._drop_offer(now)
        elif key in self.queue:
            self.queue.remove(key)
            self._fix_until(now)

    def kick(self, now=None):
        now = self._now(now)
        k = self.driver or self.offered
        if k is None:
            return
        text = f"⏭ The owner ended {self.name(k)}'s turn"
        if self.driver:
            self._release(now, text)  # not re-queued
        else:
            self._drop_offer(now, text)

    def take(self, key, now=None):
        """Owner takes the wheel now (no time limit); the previous driver goes to the front of the queue."""
        now = self._now(now)
        if key == self.driver:
            return
        front = [k for k in (self.driver, self.offered) if k and k != key]
        self.queue = front + [k for k in self.queue if k != key and k not in front]
        self._clear_driver()
        self.offered = self.offer_until = None
        self._start(key, now)

    def set_lock(self, locked, now=None):
        now = self._now(now)
        if locked == self.locked:
            return
        if locked:
            self.locked = True
            if self.offered:
                self.queue.insert(0, self.offered)
                self.offered = self.offer_until = None
            if self.driver and not self._owner(self.driver):
                self._clear_driver()
            self.post("🔒 The owner paused the controls")
            self.on_change(None)  # the camera stops
        else:
            self.locked = False
            self.post("🔓 Controls are open again")
            self._advance(now)
            self._fix_until(now)

    def prune_info(self, keep):
        used = set(self.queue) | {self.driver, self.offered}
        for k in [k for k in self.info if k not in used and k not in keep]:
            del self.info[k]

    # -- views --
    def _pub(self, key, full=True):
        i = self.info.get(key) or {}
        d = {"name": i.get("name") or "Someone", "country": i.get("country") or "", "tag": i.get("tag") or ""}
        if full:
            d["owner"] = bool(i.get("owner"))
        return d

    def public(self, key, now=None):
        now = self._now(now)
        who = self.driver or self.offered
        left = idle = None
        if self.driver:
            if self.until is not None:
                left = max(0, math.ceil(self.until - now))
            if not self._owner(self.driver):
                idle = max(0, math.ceil(self.idle_s - (now - self.last_input)))
        elif self.offered:
            idle = max(0, math.ceil(self.offer_until - now))
        if key is not None and key == self.driver:
            you = "driver"
        elif key is not None and key == self.offered:
            you = "offered"
        elif key is not None and key in self.queue:
            you = "queued"
        else:
            you = "none"
        pos = self.queue.index(key) + 1 if you == "queued" else None
        eta = (pos - 1) * self.turn_s + (left if left is not None else self.turn_s) if pos else None
        return {"mode": self.mode, "driver": self._pub(who) if who else None, "left_s": left, "idle_left_s": idle,
                "turn_s": self.turn_s, "queue_len": len(self.queue),
                "queue": [self._pub(k, full=False) for k in self.queue[:5]],
                "you": you, "your_pos": pos, "eta_s": eta}


# ---- presence: POST /state is the heartbeat ----
# A visible tab polls every second. A tab in the background polls only while it is queued or offered
# the turn, every few seconds and with hidden=true: that keeps its place in line (AWAY) but never
# counts as watching. A closed tab (/presence/leave) is dropped after a short grace, so a reload or a
# second tab of the same browser (same cid) does not give up the turn.
PRESENCE = {}   # client_key -> {"name", "country", "tag", "ip_key", "net_key", "owner", "seen", "held", "hidden"}
AWAY = {}       # client_key -> the same entry, for queued/offered clients whose tab is in the background
OVERFLOW = {}   # client_key -> {"seen", "group"}, for viewers beyond the caps ("others")
MAX_VIEWERS, PER_IP_VIEWERS, PER_NET_VIEWERS, NAMED_MAX = 300, 3, 8, 40
PER_GROUP_OVERFLOW, OVERFLOW_MAX = 5, 500
VIEWER_TIMEOUT_S = float(ROOM["viewer_timeout_s"])
AWAY_TIMEOUT_S, LEAVE_GRACE_S = 120.0, 3.0


def seat_muted(key):
    i = SEAT.info.get(key) or {}
    return not i.get("owner") and mute_left(key, i.get("ip_key"), None, i.get("net_key")) > 0


def seat_present(key):
    return key in PRESENCE or key in AWAY


def seat_away(key):
    e = PRESENCE.get(key)
    return key in AWAY or bool(e and e.get("hidden"))


def driver_changed(key):
    on_driver_change()
    if key is not None:
        stats_roll()
        STATS["turns_today"] += 1


SEAT = Seat(turn_s=int(ROOM["turn_s"]), idle_s=int(ROOM["idle_s"]), ready_s=int(ROOM["ready_s"]),
            grace_s=int(ROOM["grace_s"]), max_queue=int(ROOM["max_queue"]), queue_per_ip=int(ROOM["queue_per_ip"]),
            present=seat_present, muted=seat_muted, post=lambda text: post_sys(text),
            on_change=driver_changed, away=seat_away)


def viewer_count():
    return len(PRESENCE) + len(OVERFLOW)


def _in_seat(key):
    return key == SEAT.offered or key in SEAT.queue


def _presence_slot_ok(idt):
    """Room for a new (non-owner) entry: global cap, 3 per ip_key, 8 per IPv6 /48. Live entries are
    never evicted to make room (that would throw a driver or a queued viewer out of the seat)."""
    if len(PRESENCE) >= MAX_VIEWERS:
        return False
    for field, v, cap in (("ip_key", idt["ik"], PER_IP_VIEWERS), ("net_key", idt.get("nk"), PER_NET_VIEWERS)):
        if v and sum(1 for e in PRESENCE.values() if e[field] == v and not e["owner"]) >= cap:
            return False
    return True


def _overflow_add(ck, group, now):
    """Bounded: at most 5 per network and 500 in all (oldest dropped). Overflow holds nothing."""
    if ck not in OVERFLOW:
        same = [k for k, v in OVERFLOW.items() if v["group"] == group]
        if len(same) >= PER_GROUP_OVERFLOW:
            del OVERFLOW[min(same, key=lambda k: OVERFLOW[k]["seen"])]
        if len(OVERFLOW) >= OVERFLOW_MAX:
            del OVERFLOW[min(OVERFLOW, key=lambda k: OVERFLOW[k]["seen"])]
    OVERFLOW[ck] = {"seen": now, "group": group}


def touch_presence(idt, name=None, now=None, hidden=False):
    """Upsert this client's presence (caller holds ROOM_LOCK). name=None keeps the stored name.
    hidden=True (background heartbeat) only refreshes a queued/offered client's place.
    Returns False when the client is over the cap (counted in 'others') or not refreshed."""
    now = mono() if now is None else now
    ck = idt["ck"]
    if not ck:
        return False
    if hidden:
        e = PRESENCE.get(ck) or AWAY.get(ck)
        if e is None or not _in_seat(ck):
            return False
        e.update(held=now, hidden=True)
        return True
    e = PRESENCE.get(ck)
    if e is None and ck in AWAY:
        e = PRESENCE[ck] = AWAY.pop(ck)  # back from the background: it was already admitted
    if e is None:
        if not idt["owner"] and not _presence_slot_ok(idt):
            _overflow_add(ck, idt.get("nk") or idt["ik"], now)
            return False
        OVERFLOW.pop(ck, None)
        e = PRESENCE[ck] = {"name": "", "country": idt["country"], "tag": idt["tag"], "ip_key": idt["ik"],
                            "net_key": idt.get("nk"), "owner": idt["owner"], "seen": now, "held": now,
                            "hidden": False}
        note_country(idt["country"])
    e.update(country=idt["country"], tag=idt["tag"], ip_key=idt["ik"], net_key=idt.get("nk"),
             owner=idt["owner"], seen=now, held=now, hidden=False)
    if idt["owner"]:
        e["name"] = ROOM["owner_name"] or "Owner"
    elif name is not None:
        e["name"] = name
    SEAT.note(ck, {"name": e["name"], "country": e["country"], "tag": e["tag"], "owner": e["owner"],
                   "ip_key": e["ip_key"], "net_key": e["net_key"]})
    note_peak()
    return True


def presence_leave(ck, now=None):
    """/presence/leave: let the entry expire LEAVE_GRACE_S from now unless the client polls again
    (a reload, or another tab of the same browser). The seat then frees itself through the tick."""
    now = mono() if now is None else now
    for e in (PRESENCE.get(ck), AWAY.get(ck)):
        if e is not None:
            e["seen"] = min(e["seen"], now - VIEWER_TIMEOUT_S + LEAVE_GRACE_S)
            e["held"] = min(e["held"], now - AWAY_TIMEOUT_S + LEAVE_GRACE_S)
    OVERFLOW.pop(ck, None)


def expire_presence(now=None):
    now = mono() if now is None else now
    for k, e in list(PRESENCE.items()):
        if now - e["seen"] > VIEWER_TIMEOUT_S:
            del PRESENCE[k]
            if now - e["held"] <= AWAY_TIMEOUT_S and _in_seat(k):
                AWAY[k] = e  # keeps its place in line while the tab is in the background
    for k, e in list(AWAY.items()):
        if now - e["held"] > AWAY_TIMEOUT_S or not _in_seat(k):
            del AWAY[k]
    for k in [k for k, v in OVERFLOW.items() if now - v["seen"] > VIEWER_TIMEOUT_S]:
        del OVERFLOW[k]


def is_muted_entry(ck, e, now=None):
    return not e["owner"] and mute_left(ck, e["ip_key"], now, e.get("net_key")) > 0


def viewers_public(now=None):
    named, countries = [], collections.Counter()
    for ck, e in PRESENCE.items():
        if real_country(e["country"]):
            countries[e["country"]] += 1
        if e["name"] and not is_muted_entry(ck, e, now):
            named.append({"name": e["name"], "country": e["country"], "tag": e["tag"],
                          "driver": ck == SEAT.driver, "owner": e["owner"]})
    named.sort(key=lambda v: (not v["driver"], not v["owner"], v["name"].casefold()))
    return {"count": viewer_count(), "countries": dict(countries.most_common()), "named": named[:NAMED_MAX],
            "others": len(OVERFLOW)}


# ---- stats (countries today, peak, turns), saved to stats.json ----
def today():
    return time.strftime("%Y-%m-%d")  # the server sits next to the camera: local = camera-local


STATS = {"date": today(), "countries_today": {}, "countries_all": {}, "peak_today": 0, "turns_today": 0}


def stats_roll(date=None):
    date = date or today()
    if STATS["date"] != date:
        STATS.update(date=date, countries_today={}, peak_today=viewer_count(), turns_today=0)


def note_country(cc):
    if not real_country(cc):
        return
    stats_roll()
    STATS["countries_all"].setdefault(cc, int(time.time()))
    if cc not in STATS["countries_today"]:
        STATS["countries_today"][cc] = int(time.time())
        post_sys("🎉 First visitor today from {cc}", cc=cc)


def note_peak():
    n = viewer_count()
    if n > STATS["peak_today"]:
        STATS["peak_today"] = n


def flags_public():
    today_ = sorted(STATS["countries_today"], key=lambda c: STATS["countries_today"][c])
    return {"today": today_, "today_count": len(today_), "all_time_count": len(STATS["countries_all"]),
            "peak_today": STATS["peak_today"]}


# ---- chat ----
CHAT_KEEP = int(ROOM["chat_keep"])
SYS_REPEAT_S, DUP_S, DELETED_KEEP_S = 30.0, 30.0, 600.0
AUTO_SLOW_N, AUTO_SLOW_WINDOW_S, AUTO_SLOW_S, AUTO_SLOW_FOR_S = 30, 10.0, 5, 120.0
CHAT = {"msgs": [], "next_id": 1, "deleted": [], "dirty": False, "last_sys": {}, "owner_slow": 0,
        "auto_slow_until": float("-inf"), "recent": collections.deque()}
CHAT_RATE = {}  # client_key -> {"bucket", "last_t", "texts": {text: t}}
CHAT_IP = {}    # ip_key -> the same record shape
CHAT_NET = {}   # IPv6 /48 net_key -> the same record shape


def public_msg(m):
    return {k: v for k, v in m.items() if not k.startswith("_")}


def _append_msg(msg):
    """Caller holds CHAT_LOCK. Assigns the id (strictly increasing, also across restarts)."""
    msg["id"] = CHAT["next_id"]
    CHAT["next_id"] += 1
    CHAT["msgs"].append(msg)
    del CHAT["msgs"][:-CHAT_KEEP]
    CHAT["dirty"] = True
    return msg


def post_sys(text, cc=None, now=None):
    """System chat line (kind 'sys', name 'camera'). Identical text within 30 s is skipped."""
    now = mono() if now is None else now
    key = (text, cc)
    with CHAT_LOCK:
        last = CHAT["last_sys"].get(key)
        if last is not None and now - last < SYS_REPEAT_S:
            return None
        for k in [k for k, t in CHAT["last_sys"].items() if now - t >= SYS_REPEAT_S]:
            del CHAT["last_sys"][k]
        CHAT["last_sys"][key] = now
        msg = {"id": 0, "kind": "sys", "name": "camera", "country": "", "tag": "", "owner": False, "text": text,
               "t": int(time.time())}
        if cc and "{cc}" in text:
            msg["cc"] = cc
        _append_msg(msg)
    log.info("sys: %s", text if not cc else text.replace("{cc}", cc))
    return public_msg(msg)


def slow_s(now=None):
    now = mono() if now is None else now
    return max(AUTO_SLOW_S if now < CHAT["auto_slow_until"] else 0, int(CHAT["owner_slow"]))


def chat_delta(since, now=None):
    now = mono() if now is None else now
    with CHAT_LOCK:
        msgs = CHAT["msgs"]
        last = CHAT["next_id"] - 1  # last id ever assigned: deleting the newest message must not reset cursors
        if since <= 0 or since > last:
            out = msgs[-50:]
        else:
            out = [m for m in msgs if m["id"] > since][:100]
        return {"messages": [public_msg(m) for m in out],
                "deleted": [i for i, t in CHAT["deleted"] if now - t < DELETED_KEEP_S],
                "slow_s": slow_s(now)}


def _chat_rec(table, key, burst, every_s, now):
    r = table.get(key)
    return r if r is not None else {"bucket": Bucket(burst, every_s, now), "last_t": float("-inf"), "texts": {}}


def _chat_limits(ck, ik, text, now, nk=None):
    """Caller holds CHAT_LOCK. -> None (allowed, tokens taken) or (429 body).
    Slow mode, the duplicate check and the token bucket apply per client AND per network (ip_key,
    plus the IPv6 /48), so a fresh X-Client per message gets around none of them. A record is only
    stored once a message passes, so refused requests leave nothing behind."""
    tiers = [(CHAT_RATE, ck, _chat_rec(CHAT_RATE, ck, 3, 3.0, now), "You just said that."),
             (CHAT_IP, ik, _chat_rec(CHAT_IP, ik, 6, 2.0, now), "Someone on your network just said that.")]
    if nk:
        tiers.append((CHAT_NET, nk, _chat_rec(CHAT_NET, nk, 10, 1.5, now), "Someone on your network just said that."))
    slow = slow_s(now)
    for _, _, r, dup_message in tiers:
        r["texts"] = {t: at for t, at in r["texts"].items() if now - at < DUP_S}
        if slow and now - r["last_t"] < slow:
            return {"error": "slow_mode", "message": f"Slow mode: one message every {slow} s.",
                    "retry_in": max(1, math.ceil(slow - (now - r["last_t"])))}
        if text in r["texts"]:
            return {"error": "duplicate", "message": dup_message,
                    "retry_in": max(1, math.ceil(DUP_S - (now - r["texts"][text])))}
    wait = max(r["bucket"].wait(now) for _, _, r, _ in tiers)
    if wait > 0:
        return {"error": "rate_limited", "message": "Slow down a little.", "retry_in": max(1, math.ceil(wait))}
    for table, key, r, _ in tiers:
        r["bucket"].take(now)
        r["last_t"] = now
        r["texts"][text] = now
        if key not in table:
            make_room(table, now)
            table[key] = r
    return None


def chat_post(idt, raw_name, raw_text, now=None):
    """POST /chat. -> (status, body)."""
    now = mono() if now is None else now
    owner = idt["owner"]
    with ROOM_LOCK:
        m = muted_s_of(idt, now)
    if m:
        return 403, muted_body(m)
    if owner:
        name = ROOM["owner_name"] or "Owner"
    else:
        name, err = name_check(raw_name)
        if err == "reserved":
            return 400, {"error": "name_reserved", "message": "That name is reserved. Pick another."}
        if not name:
            return 400, {"error": "name_invalid", "message": "Names need 2–24 characters."}
    text = clean_text(raw_text, True)
    if not 1 <= len(text) <= 300:
        return 400, {"error": "text_invalid", "message": "Messages need 1–300 characters."}
    with CHAT_LOCK:
        if not owner:
            limited = _chat_limits(idt["ck"], idt["ik"], text, now, idt.get("nk"))
            if limited:
                return 429, limited
        recent = CHAT["recent"]
        recent.append(now)
        while recent and now - recent[0] > AUTO_SLOW_WINDOW_S:
            recent.popleft()
        if len(recent) > AUTO_SLOW_N:
            CHAT["auto_slow_until"] = now + AUTO_SLOW_FOR_S
        msg = _append_msg({"id": 0, "kind": "chat", "name": name, "country": idt["country"], "tag": idt["tag"],
                           "owner": owner, "text": text, "t": int(time.time()),
                           "_ck": idt["ck"], "_ik": idt["ik"], "_nk": idt.get("nk")})
        return 200, {"ok": True, "message": public_msg(msg)}


def delete_message(mid, now=None):
    now = mono() if now is None else now
    with CHAT_LOCK:
        for i, m in enumerate(CHAT["msgs"]):
            if m["id"] == mid:
                del CHAT["msgs"][i]
                CHAT["deleted"].append((mid, now))
                CHAT["dirty"] = True
                return True
    return False


def message_author(mid):
    """-> (client_key, ip_key, net_key or None) of a chat message's author, or None."""
    with CHAT_LOCK:
        for m in CHAT["msgs"]:
            if m["id"] == mid and m.get("_ck"):
                return m["_ck"], m.get("_ik"), m.get("_nk")
    return None


SAVE_LOCK = threading.Lock()  # the saver thread and the exit save never write at the same time


def atomic_write_json(path, obj):
    """Write to a unique temp file in the same directory, fsync, then rename over the target."""
    path = Path(path)
    with SAVE_LOCK:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def save_chat(path=None, force=False):
    with CHAT_LOCK:
        if not CHAT["dirty"] and not force:
            return False
        snap = {"next_id": CHAT["next_id"], "messages": list(CHAT["msgs"])}
        CHAT["dirty"] = False
    try:
        atomic_write_json(path or CHAT_FILE, snap)
    except OSError:
        log.warning("could not save chat")
        with CHAT_LOCK:
            CHAT["dirty"] = True
        return False
    return True


def load_chat(path=None):
    """Restore chat.json. A missing or damaged file means an empty chat, never a failed start."""
    path = Path(path or CHAT_FILE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except OSError:
        return 0
    except (ValueError, RecursionError):
        log.warning("%s is damaged; starting with an empty chat", path.name)
        return 0
    if not isinstance(data, dict):
        log.warning("%s is not a chat file; starting with an empty chat", path.name)
        return 0
    msgs = []
    raw = data.get("messages")
    for m in raw if isinstance(raw, list) else []:
        if (isinstance(m, dict) and isinstance(m.get("id"), int) and not isinstance(m.get("id"), bool)
                and m.get("kind") in ("chat", "sys") and isinstance(m.get("text"), str)):
            msgs.append(m)
    msgs.sort(key=lambda m: m["id"])
    nid = data.get("next_id")
    nid = nid if isinstance(nid, int) and not isinstance(nid, bool) else 1
    with CHAT_LOCK:
        CHAT["msgs"] = msgs[-CHAT_KEEP:]
        CHAT["next_id"] = max(nid, (msgs[-1]["id"] + 1) if msgs else 1, CHAT["next_id"])
    return len(msgs)


def save_stats(path=None):
    with ROOM_LOCK:
        snap = json.loads(json.dumps(STATS))
    try:
        atomic_write_json(path or STATS_FILE, snap)
    except OSError:
        log.warning("could not save stats")


def _whole(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def load_stats(path=None):
    """Restore stats.json. A missing or damaged file means empty stats, never a failed start."""
    path = Path(path or STATS_FILE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except OSError:
        return
    except (ValueError, RecursionError):
        log.warning("%s is damaged; starting with empty stats", path.name)
        return
    if not isinstance(data, dict):
        return
    with ROOM_LOCK:
        for k in ("countries_today", "countries_all"):
            if isinstance(data.get(k), dict):
                STATS[k] = {c: int(t) for c, t in data[k].items() if real_country(c) and _whole(t)}
        for k in ("peak_today", "turns_today"):
            if isinstance(data.get(k), int) and not isinstance(data.get(k), bool):
                STATS[k] = data[k]
        STATS["date"] = data.get("date") if isinstance(data.get("date"), str) else STATS["date"]
        stats_roll()


# ---- moderation state (mutes, owner slow mode), saved to moderation.json on every change ----
def save_moderation(path=None):
    now, wall = mono(), time.time()
    with ROOM_LOCK:
        mutes = {k: round(wall + (u - now), 1) for k, u in MUTES.items() if u > now}
        locked = bool(SEAT.locked)
    with CHAT_LOCK:
        slow = int(CHAT["owner_slow"])
    try:
        atomic_write_json(path or MOD_FILE, {"mutes": mutes, "owner_slow": slow, "locked": locked})
    except OSError:
        log.warning("could not save moderation state")


def load_moderation(path=None):
    path = Path(path or MOD_FILE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except OSError:
        return
    except (ValueError, RecursionError):
        log.warning("%s is damaged; no mutes restored", path.name)
        return
    if not isinstance(data, dict):
        return
    now, wall = mono(), time.time()
    mutes = data.get("mutes")
    with ROOM_LOCK:
        for k, until in (mutes.items() if isinstance(mutes, dict) else ()):
            if (isinstance(k, str) and re.fullmatch(r"[cin]:[0-9a-f]{16}", k) and _whole(until)
                    and until > wall):
                MUTES[k] = now + min(until - wall, 1440 * 60)
    slow = data.get("owner_slow")
    if isinstance(slow, int) and not isinstance(slow, bool) and 0 <= slow <= 60:
        with CHAT_LOCK:
            CHAT["owner_slow"] = slow
    if data.get("locked") is True:  # paused controls stay paused across a restart
        with ROOM_LOCK:
            SEAT.locked = True


# ---- reactions: counted per 0.5 s batch ----
REACTIONS = list(ROOM["reactions"])
CROWD_N, CROWD_WINDOW_S, CROWD_EVERY_S = 12, 3.0, 120.0
REACT = {"counter": collections.Counter(), "batches": collections.deque(maxlen=40), "last": 0,
         "recent": collections.deque(), "crowd_at": float("-inf")}
REACT_CLIENT, REACT_IP, REACT_NET = {}, {}, {}


def react_post(idt, e, now=None):
    """POST /react. -> (status, body)."""
    now = mono() if now is None else now
    if not isinstance(e, str) or e not in REACTIONS:
        return 400, {"error": "bad_request", "message": "Bad request."}
    with ROOM_LOCK:
        m = muted_s_of(idt, now)
    tiers = [(REACT_IP, idt["ik"], 24, 1 / 12)]
    if idt.get("nk"):
        tiers.append((REACT_NET, idt["nk"], 48, 1 / 24))
    tiers.append((REACT_CLIENT, idt["ck"], 8, 0.25))
    with REACT_LOCK:
        buckets = []
        for table, key, burst, every in tiers:  # network first: a refused request stores nothing
            b = table.get(key) or Bucket(burst, every, now)
            if b.wait(now) > 0:
                return 429, {"error": "rate_limited", "message": "Easy there!", "retry_in": 1}
            buckets.append((table, key, b))
        for table, key, b in buckets:
            b.take(now)
            if key not in table:
                make_room(table, now)
                table[key] = b
        if not m:  # muted callers get 202 too; their reactions are dropped
            REACT["counter"][e] += 1
    return 202, {"ok": True}


def flush_reactions(now=None):
    now = mono() if now is None else now
    crowd = False
    with REACT_LOCK:
        if REACT["counter"]:
            REACT["last"] += 1
            c = dict(REACT["counter"])
            REACT["batches"].append({"id": REACT["last"], "c": c})
            REACT["recent"].append((now, c))
            REACT["counter"].clear()
        recent = REACT["recent"]
        while recent and now - recent[0][0] > CROWD_WINDOW_S:
            recent.popleft()
        totals = collections.Counter()
        for _, c in recent:
            totals.update(c)
        if totals and max(totals.values()) >= CROWD_N and now - REACT["crowd_at"] >= CROWD_EVERY_S:
            REACT["crowd_at"] = now
            crowd = True
    if crowd:
        post_sys("🔥 The crowd is loving this!")


def reactions_delta(since):
    with REACT_LOCK:
        batches = [] if since <= 0 else [b for b in REACT["batches"] if b["id"] > since][-20:]
        return {"last": REACT["last"], "batches": [{"id": b["id"], "c": dict(b["c"])} for b in batches]}


# ---- background: ticker (0.5 s) and saver (2 s chat, 60 s stats) ----
def room_tick(now=None):
    now = mono() if now is None else now
    with ROOM_LOCK:
        expire_presence(now)
        for k in [k for k, u in MUTES.items() if u <= now]:
            del MUTES[k]
        stats_roll()
        note_peak()
        SEAT.tick(now)
        SEAT.prune_info(PRESENCE)
    flush_reactions(now)
    with CHAT_LOCK:
        CHAT["deleted"] = [(i, t) for i, t in CHAT["deleted"] if now - t < DELETED_KEEP_S]


def prune_rate_tables(now=None):
    now = mono() if now is None else now
    with CHAT_LOCK:
        for table in (CHAT_RATE, CHAT_IP, CHAT_NET):
            prune_buckets(table, now)
    with REACT_LOCK:
        for table in (REACT_CLIENT, REACT_IP, REACT_NET):
            prune_buckets(table, now)


def room_ticker():
    n = 0
    while not SHUTTING_DOWN.wait(0.5):
        try:
            room_tick()
            n += 1
            if n % 120 == 0:
                prune_rate_tables()
        except Exception:
            log.exception("room tick error")


def room_saver():
    last_stats = mono()
    while not SHUTTING_DOWN.wait(2):
        try:
            save_chat()
            if mono() - last_stats >= 60:
                last_stats = mono()
                save_stats()
        except Exception:
            log.exception("room saver error")


def room_save_all():
    SHUTTING_DOWN.set()  # the saver thread stops before the final save (SAVE_LOCK covers a save in flight)
    save_chat(force=True)
    save_stats()
    save_moderation()


def room_start():
    """Called by main() after the data dir exists: secret, chat and stats, background threads."""
    secret()
    n = load_chat()
    load_stats()
    load_moderation()
    log.info("room: %d chat messages restored, next id %d, %d mutes", n, CHAT["next_id"], len(MUTES))
    atexit.register(room_save_all)
    for target in (room_ticker, room_saver):
        threading.Thread(target=target, daemon=True, name=target.__name__).start()


# ---- hooks called by the infra ----
def _deny(handler, reason, secs=None):
    handler._deny = (reason, secs)
    return False


def can_control(handler, then=None) -> bool:
    """Seat rules for /move and /zoom (may auto-claim a free or offered seat) and /stop (driver only).
    The owner always may. The refusal reason is kept for control_denied().
    then(): run while ROOM_LOCK is still held when allowed, so a camera command can never land after
    the turn ended or the controls were locked (lock order ROOM_LOCK -> cam_cond, as on_change)."""
    idt = ident(handler)
    is_stop = urlparse(handler.path).path == "/stop"
    now = mono()
    with ROOM_LOCK:
        if idt["owner"]:
            r = "ok"
        else:
            if not is_stop:
                m = muted_s_of(idt, now)
                if m:
                    return _deny(handler, "muted", m)
            touch_presence(idt, None, now)
            r = SEAT.control(idt["ck"], now, claim=not is_stop)
        if r == "ok" and then is not None:
            then()
    return True if r == "ok" else _deny(handler, r)


def seat_error(handler, code, error, message):
    handler.send_err(code, error, message, seat=seat_public(handler))


def send_locked(handler):
    seat_error(handler, 409, "locked", "The owner has paused the controls.")


def send_not_driver(handler):
    seat = seat_public(handler)
    name = (seat.get("driver") or {}).get("name") or "Someone"
    handler.send_err(409, "not_driver", f"{name} is driving. Join the line to take a turn.", seat=seat)


def control_denied(handler):
    reason, secs = getattr(handler, "_deny", None) or ("not_driver", None)
    if reason == "muted":
        return handler.send_json(muted_body(secs), 403)
    if reason == "locked":
        return send_locked(handler)
    return send_not_driver(handler)


def on_driver_change():
    """Seat code calls this whenever the driver changes: the camera stops."""
    set_stop()


def caller_name(handler) -> str:
    """Display name for sys lines such as '🔍 {name} zoomed to 2×'."""
    idt = ident(handler)
    if idt["owner"]:
        return ROOM["owner_name"] or "Owner"
    with ROOM_LOCK:
        e = PRESENCE.get(idt["ck"]) if idt["ck"] else None
        return (e and e["name"]) or "Someone"


def seat_public(handler):
    ck = ident(handler)["ck"] if handler is not None else None
    with ROOM_LOCK:
        return SEAT.public(ck, mono())


def seat_summary():
    with ROOM_LOCK:
        return {"mode": SEAT.mode, "queue_len": len(SEAT.queue)}


def viewer_stats():
    """(viewers now, peak viewers today)."""
    with ROOM_LOCK:
        return viewer_count(), STATS["peak_today"]


def named_viewers():
    """Legacy POST /presence answer: [{"name", "country"}]."""
    with ROOM_LOCK:
        return [{"name": v["name"], "country": v["country"]} for v in viewers_public()["named"]]


def room_public():
    lt = time.localtime()
    return {"name": ROOM["name"], "tagline": ROOM["tagline"], "place": ROOM["place"],
            "reactions": list(ROOM["reactions"]), "tz_offset_s": lt.tm_gmtoff, "tz_name": lt.tm_zone}


def _cursor(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def state_payload(handler, body):
    now = mono()
    idt = ident(handler)
    hidden = body.get("hidden") is True and not idt["owner"]
    with ROOM_LOCK:
        msec = muted_s_of(idt, now)
        name, err = name_check(body.get("name"), idt["owner"])
        if msec:
            name = ""
        touch_presence(idt, name, now, hidden=hidden)
        seat = SEAT.public(idt["ck"], now)
        viewers = viewers_public(now)
        flags = flags_public()
    return {"boot": BOOT, "now": time.time(), "room": room_public(), "stream": stream_public(),
            "zoom": zoom_public(), "camera": camera_public(), "seat": seat, "viewers": viewers,
            "chat": chat_delta(_cursor(body.get("chat_since")), now),
            "reactions": reactions_delta(_cursor(body.get("react_since"))),
            "flags": flags,
            "me": {"tag": idt["tag"], "name": name, "name_error": err, "is_owner": idt["owner"], "muted_s": msec}}


def h_state(handler, body):
    handler.send_json(state_payload(handler, body))


def h_presence_leave(handler, body):
    cid = body.get("cid")
    if isinstance(cid, str) and CLIENT_RE.fullmatch(cid):
        with ROOM_LOCK:
            presence_leave(client_key(cid), mono())
    handler.send_json({"ok": True})


# ---- seat endpoints ----
def h_seat_join(handler, body):
    idt, now = ident(handler), mono()
    with ROOM_LOCK:
        m = muted_s_of(idt, now)
        if not m:
            touch_presence(idt, None, now)
            r = SEAT.join(idt["ck"], now)
    if m:
        return handler.send_json(muted_body(m), 403)
    if r == "ok":
        return handler.send_json({"ok": True, "seat": seat_public(handler)})
    if r == "already_driving":
        return seat_error(handler, 409, "already_driving", "You're already driving.")
    if r == "locked":
        return send_locked(handler)
    if r == "queue_full":
        return handler.send_err(429, "queue_full", "The line is full right now. Try again in a minute.")
    return handler.send_err(429, "ip_limit", "Someone on your network is already in line.")


def h_seat_leave(handler, body):
    with ROOM_LOCK:
        SEAT.leave(ident(handler)["ck"], mono())
    handler.send_json({"ok": True, "seat": seat_public(handler)})


def h_seat_claim(handler, body):
    idt, now = ident(handler), mono()
    with ROOM_LOCK:
        touch_presence(idt, None, now)
        r = SEAT.claim(idt["ck"], now)
    if r == "ok":
        return handler.send_json({"ok": True, "seat": seat_public(handler)})
    seat_error(handler, 409, "not_offered", "It isn't your turn yet.")


# ---- chat and reactions ----
def h_chat(handler, body):
    code, out = chat_post(ident(handler), body.get("name"), body.get("text"))
    handler.send_json(out, code)


def h_react(handler, body):
    code, out = react_post(ident(handler), body.get("e"))
    handler.send_json(out, code)


# ---- owner moderation ----
MUTE_MINUTES = (0, 10, 60, 1440)


def owner_only(fn):
    def wrapped(handler, body):
        if not is_owner(handler):
            return handler.send_err(403, "not_owner", "Owner only.")
        return fn(handler, body)
    wrapped.__name__ = fn.__name__
    return wrapped


def _int_field(body, key):
    v = body.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise BadRequest
    return v


@owner_only
def h_mod_delete(handler, body):
    if not delete_message(_int_field(body, "id")):
        return handler.send_err(404, "not_found", "Not found.")
    handler.send_json({"ok": True})


@owner_only
def h_mod_mute(handler, body):
    minutes = _int_field(body, "minutes")
    if minutes not in MUTE_MINUTES:
        raise BadRequest
    now = mono()
    if "id" in body:
        keys = message_author(_int_field(body, "id"))
    elif isinstance(body.get("tag"), str):
        with ROOM_LOCK:
            found = [(ck, e["ip_key"], e.get("net_key")) for table in (PRESENCE, AWAY)
                     for ck, e in table.items() if e["tag"] == body["tag"]]
        if len(found) > 1:  # tags can collide (or be cloned): never guess who is meant
            return handler.send_err(409, "ambiguous", "Two viewers have that tag. Mute them from a chat message.")
        keys = found[0] if found else None
    else:
        raise BadRequest
    if keys is None:
        return handler.send_err(404, "not_found", "Not found.")
    ck, ik, nk = keys
    with ROOM_LOCK:
        for k in (f"c:{ck}", f"i:{ik}" if ik else None, f"n:{nk}" if nk else None):
            if not k:
                continue
            if minutes:
                MUTES[k] = now + minutes * 60
            else:
                MUTES.pop(k, None)
        if minutes:  # out of the seat and the queue, with no sys line
            hit = {ck} | {k for table in (PRESENCE, AWAY) for k, e in table.items() if not e["owner"] and (
                (ik and e["ip_key"] == ik) or (nk and e.get("net_key") == nk))}
            for k in hit:
                if not SEAT._owner(k):
                    SEAT.remove(k, now)
    save_moderation()
    handler.send_json({"ok": True, "muted_s": minutes * 60})


@owner_only
def h_mod_slowmode(handler, body):
    n = _int_field(body, "seconds")
    if not 0 <= n <= 60:
        raise BadRequest
    with CHAT_LOCK:
        CHAT["owner_slow"] = n
    save_moderation()
    post_sys(f"🐢 Slow mode: one message every {n} s" if n else "🐇 Slow mode is off")
    handler.send_json({"ok": True, "slow_s": slow_s()})


@owner_only
def h_mod_lock(handler, body):
    locked = body.get("locked")
    if not isinstance(locked, bool):
        raise BadRequest
    with ROOM_LOCK:
        SEAT.set_lock(locked, mono())
    save_moderation()  # right away: a crash must not quietly reopen the controls
    handler.send_json({"ok": True, "seat": seat_public(handler)})


@owner_only
def h_mod_kick(handler, body):
    with ROOM_LOCK:
        SEAT.kick(mono())
    handler.send_json({"ok": True, "seat": seat_public(handler)})


@owner_only
def h_mod_take(handler, body):
    idt, now = ident(handler), mono()
    with ROOM_LOCK:
        touch_presence(idt, None, now)
        SEAT.take(idt["ck"], now)
    handler.send_json({"ok": True, "seat": seat_public(handler)})


# =====================================================================================
# 9. HTTP handler and routes
# =====================================================================================
CLIENT_RE = re.compile(r"[0-9a-f]{32}")
HLS_RE = re.compile(r"/hls/(live\.m3u8|seg\d{1,12}\.ts)")
MAX_BODY = 10000
SECURITY_HEADERS = (("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                    ("X-Frame-Options", "DENY"), ("Content-Security-Policy", "frame-ancestors 'none'"))
_page = {"mtime": None, "body": b""}
_page_lock = threading.Lock()


def page_bytes():
    """ptz.html, re-read whenever its mtime changes, with {{NAME}} and {{TAGLINE}} filled in."""
    mtime = PAGE_PATH.stat().st_mtime_ns
    with _page_lock:
        if mtime != _page["mtime"]:
            text = PAGE_PATH.read_text(encoding="utf-8")
            text = text.replace("{{NAME}}", html.escape(ROOM["name"])).replace("{{TAGLINE}}", html.escape(ROOM["tagline"]))
            _page.update(mtime=mtime, body=text.encode("utf-8"))
        return _page["body"]


def h_move(handler, body):
    try:
        pan, tilt = velocity(body.get("pan")), velocity(body.get("tilt"))
    except BadRequest:
        return handler.send_err(400, "bad_request", "pan and tilt must be numbers.")
    nudge = body.get("nudge") is True
    if not can_control(handler, then=lambda: set_stop() if pan == 0 and tilt == 0 else set_motion(pan, tilt, nudge)):
        return control_denied(handler)
    handler.send_json({"ok": True, "seat": seat_public(handler), "camera": camera_public()}, 202)


def h_stop(handler, body):
    if not can_control(handler, then=set_stop):
        return control_denied(handler)
    handler.send_json({"ok": True}, 202)


def h_zoom(handler, body):
    try:
        kind, value = parse_zoom_body(body)
    except BadRequest:
        return handler.send_err(400, "bad_request", "Unknown zoom level.")
    if not can_control(handler):
        return control_denied(handler)
    set_zoom_target(kind, value, caller_name(handler))
    handler.send_json({"ok": True, "zoom": zoom_public()}, 202)


def h_legacy_presence(handler, body):
    handler.send_json(named_viewers())


# ---- Kulavazha Radar: "Report weed" from a viewer ----
RADAR = CFG.get("radar") or {}
REPORT_IP = {}                       # ip -> monotonic time of the last report
REPORT_BUCKET = {"b": None}          # one shared bucket so a crowd can't flood the radar
REPORT_LOCK = threading.Lock()


def report_allowed(ip, now=None):
    """(ok, retry_in): one report per IP every per_ip_s, plus a shared burst/every_s bucket."""
    now = time.monotonic() if now is None else now
    with REPORT_LOCK:
        last = REPORT_IP.get(ip)
        gap = float(RADAR.get("per_ip_s", 60))
        if last is not None and now - last < gap:
            return False, max(1, math.ceil(gap - (now - last)))
        b = REPORT_BUCKET["b"] or Bucket(RADAR.get("burst", 5), RADAR.get("every_s", 60), now)
        REPORT_BUCKET["b"] = b
        w = b.wait(now)
        if w > 0:
            return False, max(1, math.ceil(w))
        b.take(now)
        if len(REPORT_IP) > 5000:
            REPORT_IP.clear()
        REPORT_IP[ip] = now
        return True, 0


def latest_frame_jpeg(width=480):
    """A JPEG of the first frame of the newest finished HLS segment (about 1 s old) (the server's own view, so no upload)."""
    segs = sorted(HLS_DIR.glob("seg*.ts"), key=lambda q: q.stat().st_mtime)
    if len(segs) < 2:
        return None
    seg = segs[-2]                   # the newest one may still be being written
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(seg),
                              "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "6", "-f", "image2", "pipe:1"],
                             capture_output=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.stdout[:2] == b"\xff\xd8" else None


def radar_item(name, jpeg):
    place = ROOM.get("place") or "Punnamada Lake"
    return {
        "reporter": f"PayalCam viewer {name}"[:60], "source": "payalcam",
        "transcript": f"പായൽക്യാം: {place} ഭാഗത്ത് കുളവാഴക്കൂട്ടം കണ്ടു",
        "translation": f"PayalCam viewer {name} spotted a hyacinth mat on the live camera at {place}.",
        "extraction": {"place_id": RADAR.get("place_id") or "punnamada", "size": "medium", "blocking": False},
        "photo": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode() if jpeg else None,
    }


def h_report_weed(handler, body):
    url = RADAR.get("feed_url")
    if not url:
        return handler.send_err(404, "not_found", "Reporting is off.")
    ok, retry = report_allowed(client_ip(handler))
    if not ok:
        return handler.send_err(429, "rate_limited", "Thanks! You can report again in a minute.", retry_in=retry)
    name = caller_name(handler)
    item = radar_item(name, latest_frame_jpeg())
    try:
        req = urllib.request.Request(url, data=json.dumps(item).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=6) as r:
            seq = (json.loads(r.read() or b"{}") or {}).get("seq")
    except Exception as e:  # radar down: say so, don't crash the cam
        log.warning("report-weed: radar unreachable: %s", type(e).__name__)
        return handler.send_err(502, "radar_down", "Kulavazha Radar isn't reachable right now.")
    post_sys(f"🌿 {name} reported a weed mat to Kulavazha Radar")
    handler.send_json({"ok": True, "seq": seq, "photo": bool(item["photo"])})


# path -> (function(handler, body), requires X-Client)
POST_ROUTES = {
    "/state": (h_state, True),
    "/move": (h_move, True),
    "/stop": (h_stop, True),
    "/zoom": (h_zoom, True),
    "/seat/join": (h_seat_join, True),
    "/seat/leave": (h_seat_leave, True),
    "/seat/claim": (h_seat_claim, True),
    "/chat": (h_chat, True),
    "/react": (h_react, True),
    "/report-weed": (h_report_weed, True),
    "/mod/delete": (h_mod_delete, True),
    "/mod/mute": (h_mod_mute, True),
    "/mod/slowmode": (h_mod_slowmode, True),
    "/mod/lock": (h_mod_lock, True),
    "/mod/kick": (h_mod_kick, True),
    "/mod/take": (h_mod_take, True),
    "/presence/leave": (h_presence_leave, False),
    "/presence": (h_legacy_presence, False),  # LEGACY: delete next iteration
}


KNOWN_PATHS = frozenset(POST_ROUTES) | {"/", "/healthz", "/zoom", "/chat"}
_client_errors = collections.Counter()
_client_errors_lock = threading.Lock()


def count_client_error(method, path, code):
    key = (method if method in ("GET", "POST", "HEAD") else "other", path if path in KNOWN_PATHS else "*", code)
    with _client_errors_lock:
        _client_errors[key] += 1


def flush_client_errors():
    """One log line with the 4xx counts since the last call (nothing when there were none)."""
    with _client_errors_lock:
        items = sorted(_client_errors.items())
        _client_errors.clear()
    if items:
        log.info("client errors in the last minute: %s",
                 ", ".join(f"{m} {p} {c} x{n}" for (m, p, c), n in items))
    return items


class Server(ThreadingHTTPServer):
    request_queue_size = 128  # the default listen backlog of 5 resets bursts of tunnel connections
    daemon_threads = True


class H(BaseHTTPRequestHandler):
    server_version = "camlive"
    sys_version = ""
    timeout = 30  # a stalled client cannot hold a thread forever
    client_id = None

    # ---- output ----
    def end_headers(self):
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        super().end_headers()

    def _write(self, data):
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self._write(data)
        self._responded = True

    def send_err(self, code, error, message, **extra):
        self.send_json({"error": error, "message": message, **extra}, code)

    def send_body(self, data, ctype, cache):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self._write(data)
        self._responded = True

    # ---- logging: /mod/* calls and server errors (never IPs, query strings or bodies).
    # Client errors (4xx) anyone can provoke are only counted, and summarised once a minute. ----
    def log_request(self, code="-", size="-"):
        try:
            c = int(code)
        except (TypeError, ValueError):
            c = 0
        path = urlparse(self.path).path[:120]
        if 400 <= c < 500:
            if not path.startswith("/hls/"):
                count_client_error(self.command, path, c)
        elif path.startswith("/mod/") or c >= 500:
            log.info("%s %s -> %s", self.command, path, c)

    def log_message(self, fmt, *args):
        pass

    def _internal_error(self):
        log.exception("unhandled error on %s %s", self.command, urlparse(self.path).path[:120])
        if not getattr(self, "_responded", False):
            try:
                self.send_err(500, "internal", "Something went wrong.")
            except Exception:
                pass

    # ---- GET ----
    def do_GET(self):
        try:
            url = urlparse(self.path)
            path = url.path
            if path == "/":
                return self.send_body(page_bytes(), "text/html; charset=utf-8", "no-cache")
            if m := HLS_RE.fullmatch(path):
                return self.send_hls(m.group(1))
            if path == "/healthz":
                return self.send_healthz()
            if path == "/zoom":  # LEGACY: delete next iteration
                with view_lock:
                    level = ZOOM["level"]
                return self.send_json({"zoom": level, "levels": LEVELS})
            if path == "/chat":  # LEGACY: tells old tabs to reload
                try:
                    since = int((parse_qs(url.query).get("since") or ["0"])[0])
                except ValueError:
                    since = 0
                if since < 1000000000:
                    return self.send_json([{"id": 1000000000, "name": "📷 camera", "t": int(time.time()),
                                            "text": "This page was updated. Please reload to keep chatting and steering."}])
                return self.send_json([])
            self.send_err(404, "not_found", "Not found.")
        except Exception:
            self._internal_error()

    def send_hls(self, name):
        f = HLS_DIR / name
        try:
            data = f.read_bytes()
        except OSError:
            return self.send_error(404)
        playlist = name.endswith(".m3u8")
        egress_add(len(data))
        # playlist changes every second; segment names never repeat, so they can be cached
        self.send_body(data, "application/vnd.apple.mpegurl" if playlist else "video/mp2t",
                       "no-store" if playlist else "public, max-age=60")

    def send_healthz(self):
        st = stream_public()
        age = st["segment_age_s"]
        ok = age is not None and age < 15
        count, peak = viewer_stats()
        if not is_local(self):  # the details are for the owner's Mac only (also against DNS rebinding)
            return self.send_json({"ok": ok, "segment_age_s": age, "viewers": count}, 200 if ok else 503)
        now = time.monotonic()
        with view_lock:
            z = {"level": ZOOM["level"], "target": ZOOM["target"], "live": ZOOM_LIVE}
            p = view["proc"]
            running = p is not None and p.poll() is None
        self.send_json({
            "ok": ok, "boot": BOOT, "uptime_s": ceil_s(now - STARTED), "segment_age_s": age,
            "stream_state": st["state"],
            "ffmpeg": {"pid": view["pid"] if running else None,
                       "uptime_s": ceil_s(now - view["launched"]) if running else None,
                       "restarts": dict(restarts), "last_exit_code": ENC["last_exit_code"],
                       "stderr_tail": TAIL.lines()},
            "ptz": camera_health(),
            "viewers": count, "peak_viewers_today": peak,
            "zoom": z, "seat": seat_summary(),
            "egress_mbps": egress_mbps(), "hls_dir_bytes": hls_dir_bytes(),
            "tunnel_url": TUNNEL["url"],
        }, 200 if ok else 503)

    # ---- POST ----
    def do_POST(self):
        try:
            route = POST_ROUTES.get(urlparse(self.path).path)
            if route is None:
                return self.send_err(404, "not_found", "Not found.")
            fn, needs_client = route
            cid = self.headers.get("X-Client") or ""
            if CLIENT_RE.fullmatch(cid):
                self.client_id = cid
            elif needs_client:
                return self.send_err(409, "reload", "This page was updated. Please reload.")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 <= n <= MAX_BODY:
                    raise BadRequest
                body = parse_body(self.rfile.read(n) if n else b"")
            except (ValueError, BadRequest):
                return self.send_err(400, "bad_request", "Bad request.")
            try:
                fn(self, body)
            except BadRequest:
                self.send_err(400, "bad_request", "Bad request.")
        except Exception:
            self._internal_error()


# =====================================================================================
# 10. main
# =====================================================================================
def cleanup():
    SHUTTING_DOWN.set()
    stop_ffmpeg(view["proc"])
    PID_FILE.unlink(missing_ok=True)
    try:  # best effort: leave the camera still
        onvif.stop(_ptz_token.get("t") or onvif.profile_token())
    except Exception:
        pass
    log.info("camlive stopped")


def main():
    if not Path(FFMPEG).exists():
        sys.exit("camlive: ffmpeg not found (brew install ffmpeg)")
    setup_logging()
    try:  # bind first: a second copy on a busy port exits before touching anything
        httpd = Server((SRV["bind"], PORT), H)
    except OSError as e:
        log.error("cannot listen on %s:%s: %s", SRV["bind"], PORT, e)
        sys.exit(1)
    log.info("starting camlive: %s", config_summary())
    warn_weak_owner_key()
    prepare_dirs()
    start_caffeinate()
    atexit.register(cleanup)
    room_start()  # secret, chat.json, stats.json, ticker and saver threads (saved again at exit)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *a: sys.exit(0))  # so atexit stops ffmpeg and the camera
    for target in (encoder, camera_worker, monitor):
        threading.Thread(target=target, daemon=True, name=target.__name__).start()
    if SRV["source"] == "camera" and CFG["tunnel"]["metrics_url"]:
        threading.Thread(target=tunnel_watcher, daemon=True, name="tunnel_watcher").start()
    log.info("PTZ panel: http://localhost:%s", PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
