"""camlive configuration: camlive.toml deep-merged over DEFAULTS, plus three dev env overrides.

Only this module builds RTSP_URL and ONVIF_HOST. Never log or print them (they hold credentials).
"""
import copy, os, sys, tomllib
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent

DEFAULTS = {
    "camera": {"ip": "", "onvif_port": 8000, "rtsp_port": 5543, "rtsp_path": "", "user": "", "password": ""},
    "server": {"port": 8765, "bind": "127.0.0.1", "source": "camera", "ptz": "onvif",
               "hls_dir": "~/Library/Caches/camlive/hls-{port}",
               "data_dir": "~/Library/Application Support/camlive/{port}",
               "log_file": "~/Library/Logs/camlive/camlive.log"},
    "stream": {"width": 1280, "height": 720, "fps": 15, "crf": 23, "maxrate": "1500k", "bufsize": "3000k",
               "zoom_levels": [1, 1.5, 2, 3, 4], "zoom_live": False, "zoom_min_interval_s": 15,
               "stall_seconds": 10, "first_segment_grace_s": 20},
    "room": {"name": "PayalCam", "tagline": "Live view of Punnamada Lake, keeping an eye on the water hyacinth (kulavazha).", "place": "Punnamada Lake, Alappuzha",
             "owner_name": "Owner", "owner_key": "", "reactions": ["😍", "🌅", "😂", "🐦", "🔥", "👋"],
             "turn_s": 45, "idle_s": 20, "ready_s": 10, "grace_s": 20, "max_queue": 30, "queue_per_ip": 2,
             "viewer_timeout_s": 15, "chat_keep": 200},
    "alerts": {"ntfy_server": "https://ntfy.sh", "ntfy_topic": ""},
    "tunnel": {"metrics_url": "http://127.0.0.1:20241/quicktunnel"},
    # Kulavazha Radar: viewers' "Report weed" goes to this feed (empty = button hidden).
    "radar": {"feed_url": "http://127.0.0.1:3000/api/feed", "place_id": "punnamada",
              "per_ip_s": 60, "burst": 5, "every_s": 60},
}


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def load(path=None, env=os.environ):
    path = Path(path or env.get("CAMLIVE_CONFIG") or REPO / "camlive.toml")
    cfg = DEFAULTS
    if path.exists():
        with open(path, "rb") as f:
            cfg = deep_merge(DEFAULTS, tomllib.load(f))
    cfg = copy.deepcopy(cfg)
    srv = cfg["server"]
    if env.get("CAMLIVE_PORT"):
        try:
            srv["port"] = int(env["CAMLIVE_PORT"])
        except ValueError:
            sys.exit("camlive: CAMLIVE_PORT must be an integer")
    srv["source"] = env.get("CAMLIVE_SOURCE") or srv["source"]
    srv["ptz"] = env.get("CAMLIVE_PTZ") or srv["ptz"]
    if srv["source"] not in ("camera", "test") or srv["ptz"] not in ("onvif", "fake"):
        sys.exit("camlive: server.source must be camera|test and server.ptz must be onvif|fake")
    cam = cfg["camera"]
    if srv["source"] == "camera" and not all(cam[k] for k in ("ip", "user", "password", "rtsp_path")):
        sys.exit("camlive: set [camera] in camlive.toml")
    if srv["ptz"] == "onvif" and not all(cam[k] for k in ("ip", "user", "password")):
        sys.exit("camlive: set [camera] in camlive.toml")
    return cfg


def rtsp_url(cam):
    return (f"rtsp://{quote(cam['user'], safe='')}:{quote(cam['password'], safe='')}"
            f"@{cam['ip']}:{cam['rtsp_port']}/{cam['rtsp_path']}")


def _dir(template, port):
    return Path(os.path.expanduser(template.format(port=port)))


CFG = load()
PORT = CFG["server"]["port"]
RTSP_URL = rtsp_url(CFG["camera"])
ONVIF_HOST = f"http://{CFG['camera']['ip']}:{CFG['camera']['onvif_port']}"
HLS_DIR = _dir(CFG["server"]["hls_dir"], PORT)
DATA_DIR = _dir(CFG["server"]["data_dir"], PORT)
LOG_FILE = _dir(CFG["server"]["log_file"], PORT)
if PORT != DEFAULTS["server"]["port"] and "{port}" not in CFG["server"]["log_file"]:
    LOG_FILE = LOG_FILE.with_name(f"{LOG_FILE.stem}-{PORT}{LOG_FILE.suffix}")  # dev never shares the live log
LOG_DIR = LOG_FILE.parent
