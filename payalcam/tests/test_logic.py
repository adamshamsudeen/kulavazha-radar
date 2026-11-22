"""Unit tests (stdlib unittest). Run from the repo root:  python3 -m unittest

Infra (engineer 1): config, request validation, camera worker, zoom, encoder helpers, ops, HTTP conventions.
Room (engineer 2): clean_text and names, tags, token bucket, Seat state machine (fake clock),
chat limits, mutes, reactions, persistence, owner endpoints and the /state shape over HTTP.
"""
import json, os, sys, tempfile, threading, time, unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import tests  # noqa: F401  (sets the CAMLIVE_* env before config is imported)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import fake_ptz  # noqa: E402
import ptz_server as S  # noqa: E402

S._SECRET["key"] = b"k" * 32  # tests never create DATA_DIR/secret.key


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestConfig(unittest.TestCase):
    def write(self, text):
        d = tempfile.mkdtemp()
        p = Path(d) / "c.toml"
        p.write_text(text)
        return p

    def test_deep_merge_keeps_defaults(self):
        m = config.deep_merge(config.DEFAULTS, {"stream": {"crf": 30}})
        self.assertEqual(m["stream"]["crf"], 30)
        self.assertEqual(m["stream"]["fps"], 15)
        self.assertEqual(config.DEFAULTS["stream"]["crf"], 23)  # defaults untouched

    def test_env_overrides(self):
        p = self.write('[server]\nport = 9000\n')
        cfg = config.load(p, env={"CAMLIVE_PORT": "8766", "CAMLIVE_SOURCE": "test", "CAMLIVE_PTZ": "fake"})
        self.assertEqual((cfg["server"]["port"], cfg["server"]["source"], cfg["server"]["ptz"]), (8766, "test", "fake"))

    def test_camera_source_needs_camera_settings(self):
        with self.assertRaises(SystemExit):
            config.load(self.write(""), env={"CAMLIVE_PTZ": "fake"})
        with self.assertRaises(SystemExit):
            config.load(self.write(""), env={"CAMLIVE_SOURCE": "test"})  # ptz=onvif needs them too
        with self.assertRaises(SystemExit):
            config.load(self.write(""), env={"CAMLIVE_SOURCE": "nope", "CAMLIVE_PTZ": "fake"})

    def test_rtsp_url_quotes_credentials(self):
        url = config.rtsp_url({"user": "a@b", "password": "p/w:@x", "ip": "10.0.0.9", "rtsp_port": 554,
                               "rtsp_path": "abc/live/channel0"})
        self.assertEqual(url, "rtsp://a%40b:p%2Fw%3A%40x@10.0.0.9:554/abc/live/channel0")

    def test_dirs_are_per_port(self):
        self.assertIn("8799", str(config.HLS_DIR))
        self.assertIn("8799", str(config.DATA_DIR))
        self.assertIn("8799", config.LOG_FILE.name)


class TestValidation(unittest.TestCase):
    def test_velocity(self):
        self.assertEqual(S.velocity(0.5), 0.5)
        self.assertEqual(S.velocity(3), 1.0)
        self.assertEqual(S.velocity(-7.5), -1.0)
        self.assertEqual(S.velocity(0.123456), 0.12)
        self.assertEqual(str(S.velocity(-0.001)), "0.0")
        for bad in (True, False, None, "0.5", [1], float("nan"), float("inf"), 10 ** 400):
            with self.assertRaises(S.BadRequest, msg=repr(bad)):
                S.velocity(bad)

    def test_parse_body(self):
        self.assertEqual(S.parse_body(b""), {})
        self.assertEqual(S.parse_body(b'{"pan": 1}'), {"pan": 1})
        for bad in (b'{"pan": NaN}', b'{"pan": Infinity}', b'[1]', b'"x"', b'{', b'\xff\xfe\x00'):
            with self.assertRaises(S.BadRequest, msg=repr(bad)):
                S.parse_body(bad)
        self.assertEqual(S.parse_body(b'{"pan": 1e999}')["pan"], float("inf"))  # caught later by velocity()


class TestCameraWorker(unittest.TestCase):
    def setUp(self):
        S.CAM.update(desired=S.STOP, deadline=0.0, sent=None, last_call=float("-inf"), retry_at=0.0,
                     ok=None, last_ok=0.0, last_error=None, fail_since=None, last_log=0.0)
        S._ptz_token.clear()
        fake_ptz.CALLS.clear()
        self.clock = Clock()

    def step(self, ptz=fake_ptz):
        return S.camera_step(ptz=ptz, clock=self.clock)

    def test_first_iteration_sends_stop_then_nothing(self):
        self.assertIsNone(S.camera_public()["ok"])
        self.assertEqual(self.step(), S.STOP)
        self.clock.t += 1
        self.assertIsNone(self.step())
        self.assertEqual(fake_ptz.CALLS, [("stop",)])
        self.assertEqual(S.camera_public(), {"ok": True, "message": None})

    def test_hold_auto_stops_after_deadline(self):
        self.step()
        self.clock.t += 1
        S.set_motion(0.5, 0, now=self.clock.t)
        self.assertEqual(self.step(), (0.5, 0))
        self.clock.t += 1.0
        self.assertIsNone(self.step())  # still inside the 1.2 s hold
        self.clock.t += 0.3
        self.assertEqual(self.step(), S.STOP)  # dead-man stop, no /stop needed
        self.assertEqual(fake_ptz.CALLS, [("stop",), ("move", 0.5, 0.0), ("stop",)])

    def test_nudge_is_short(self):
        self.step()
        self.clock.t += 1
        S.set_motion(0, -1, nudge=True, now=self.clock.t)
        self.step()
        self.clock.t += 0.41
        self.assertEqual(self.step(), S.STOP)

    def test_latest_wins_and_rate_limit(self):
        self.step()
        self.clock.t += 1
        S.set_motion(0.2, 0, now=self.clock.t)
        self.step()
        self.clock.t += 0.05
        S.set_motion(-0.3, 0.1, now=self.clock.t)
        S.set_motion(0.4, 0.4, now=self.clock.t)
        self.assertIsNone(self.step())  # < 0.2 s since last call
        self.clock.t += 0.2
        self.assertEqual(self.step(), (0.4, 0.4))
        S.set_stop()
        self.clock.t += 0.2
        self.assertEqual(self.step(), S.STOP)
        self.assertEqual([c[0] for c in fake_ptz.CALLS], ["stop", "move", "move", "stop"])

    def test_errors_retry_after_2s_and_clear_token(self):
        class Broken:
            tokens = 0

            def profile_token(self):
                Broken.tokens += 1
                return "tok"

            def stop(self, token=None):
                raise OSError("timed out")

            def move(self, *a, **k):
                return "<s:Fault>no</s:Fault>"

        b = Broken()
        self.assertIsNone(self.step(b))
        self.assertEqual(S.camera_public(), {"ok": False, "message": S.CAMERA_DOWN_MESSAGE})
        self.assertEqual(S.CAM["last_error"], "camera unavailable")
        self.clock.t += 1.9
        self.assertIsNone(self.step(b))
        self.assertEqual(Broken.tokens, 1)  # still in the 2 s back-off
        self.clock.t += 0.2
        self.step(b)
        self.assertEqual(Broken.tokens, 1)  # a timeout keeps the cached token
        self.clock.t += 2.1
        S.set_motion(1, 1, now=self.clock.t)
        self.assertIsNone(self.step(b))  # a Fault reply is a failure too
        self.assertFalse(S.CAM["ok"])
        self.assertNotIn("t", S._ptz_token)  # and it drops the token (it may be stale)
        self.clock.t += 2.1
        self.assertEqual(self.step(), S.STOP)  # camera back (and the hold expired meanwhile)
        self.assertTrue(S.CAM["ok"])
        self.assertIsNone(S.CAM["fail_since"])


class FakeProc:
    def __init__(self, fd=None):
        self.stdin = type("W", (), {"fileno": lambda s: fd})() if fd is not None else None

    def poll(self):
        return 0  # already exited: stop_ffmpeg() is a no-op


class TestZoom(unittest.TestCase):
    def setUp(self):
        S.ZOOM.update(level=1, target=1, target_at=float("-inf"), last_apply=float("-inf"), by=None)
        S.view["stop_reason"] = None

    def test_next_zoom(self):
        L = [1, 1.5, 2, 3, 4]
        self.assertEqual(S.next_zoom(L, 1, 1), 1.5)
        self.assertEqual(S.next_zoom(L, 1, -1), 1)
        self.assertEqual(S.next_zoom(L, 4, 1), 4)
        self.assertEqual(S.next_zoom(L, 3, -1), 2)
        self.assertEqual(S.next_zoom(L, 2.5, 1), 1.5)  # unknown current: start from the first level

    def test_parse_zoom_body(self):
        self.assertEqual(S.parse_zoom_body({"step": 1}), ("step", 1))
        self.assertEqual(S.parse_zoom_body({"step": -1}), ("step", -1))
        self.assertEqual(S.parse_zoom_body({"level": 1.5}), ("level", 1.5))
        self.assertEqual(S.parse_zoom_body({"level": 2.0}), ("level", 2))
        for bad in ({}, {"step": 2}, {"step": True}, {"step": 0}, {"level": 2.5}, {"level": True}, {"level": "2"}):
            with self.assertRaises(S.BadRequest, msg=repr(bad)):
                S.parse_zoom_body(bad)

    def test_target_steps_from_target_not_level(self):
        S.set_zoom_target("step", 1, "Ann", now=10)
        S.set_zoom_target("step", 1, "Ann", now=10.2)
        z = S.zoom_public(now=10.2)
        self.assertEqual((z["level"], z["target"], z["by"]), (1, 2, "Ann"))
        self.assertEqual(z["applies_in_s"], 1.0)

    def test_restart_mode_coalesces_and_cools_down(self):
        interval = S.zoom_interval()
        S.set_zoom_target("level", 2, "Ann", now=10)
        self.assertFalse(S.zoom_tick(FakeProc(), now=10.9, live=False))  # not settled for 1 s yet
        S.set_zoom_target("level", 3, "Bo", now=10.9)  # troll keeps moving it: nothing applies
        self.assertFalse(S.zoom_tick(FakeProc(), now=11.5, live=False))
        self.assertTrue(S.zoom_tick(FakeProc(), now=11.9, live=False))
        self.assertEqual((S.ZOOM["level"], S.view["stop_reason"]), (3, "zoom"))
        self.assertIsNone(S.zoom_public(now=12)["applies_in_s"])
        S.set_zoom_target("level", 1, "Bo", now=12)
        self.assertEqual(S.zoom_public(now=12)["applies_in_s"], round(11.9 + interval - 12, 1))
        self.assertFalse(S.zoom_tick(FakeProc(), now=11.9 + interval - 0.1, live=False))
        self.assertTrue(S.zoom_tick(FakeProc(), now=11.9 + interval + 0.01, live=False))

    def test_live_mode_writes_one_crop_command(self):
        r, w = os.pipe()
        try:
            self.assertEqual(S.zoom_tick(FakeProc(w), now=5, live=True), False)  # nothing pending
            S.set_zoom_target("level", 1.5, "Ann", now=5)
            self.assertTrue(S.zoom_tick(FakeProc(w), now=6.1, live=True))
            self.assertEqual(os.read(r, 512), b"ccrop@z -1 w trunc(iw/1.5/2)*2\n")
            self.assertEqual(S.ZOOM["level"], 1.5)
            self.assertIsNone(S.view["stop_reason"])  # no restart
        finally:
            os.close(r)
            os.close(w)

    def test_live_mode_falls_back_to_restart(self):
        r, w = os.pipe()
        os.close(r)
        os.close(w)
        S.set_zoom_target("level", 2, "Ann", now=5)
        self.assertTrue(S.zoom_tick(FakeProc(w), now=7, live=True))
        self.assertEqual((S.ZOOM["level"], S.view["stop_reason"]), (2, "zoom"))

    def test_filter_shape(self):
        self.assertEqual(S.video_filter(2), "crop@z=w=trunc(iw/2/2)*2:h=trunc(ow*9/16/2)*2,scale=1280:720")


class TestEncoderHelpers(unittest.TestCase):
    def test_args(self):
        a = S.ffmpeg_args(1)
        self.assertNotIn("-nostdin", a)
        self.assertEqual(a[a.index("-start_number") + 1], str(S.BOOT_EPOCH))
        self.assertEqual(a[a.index("-crf") + 1], "23")
        self.assertIn("testsrc2=size=2304x1296:rate=15", a)
        self.assertTrue(a[-2].endswith("seg%d.ts") and a[-1].endswith("live.m3u8"))
        self.assertIn("program_date_time", a[a.index("-hls_flags") + 1])

    def test_redact(self):
        self.assertEqual(S.redact("rtsp://admin:pw@1.2.3.4:554/x: refused"), "rtsp://***@1.2.3.4:554/x: refused")

    def test_tail_collapses_and_filters(self):
        t = S.Tail(maxlen=3)
        for line in ["a\n", "a\n", "a\n", "\n", "Enter command: <target>|all\n",
                     "Command reply for stream -1: ret:0 res:\n", "b\n", "c\n", "d\n"]:
            t.add(line)
        self.assertEqual(t.lines(), ["b", "c", "d"])
        t.add("d\n")
        self.assertEqual(t.lines(2), ["c", "d (x2)"])
        t.add("Command reply for stream -1: ret:-38 res:\n")
        self.assertIn("ret:-38", t.lines(1)[0])

    def test_janitor(self):
        d = Path(tempfile.mkdtemp())
        (d / "live.m3u8").write_text("#EXTM3U\nseg100.ts\nseg101.ts\n")
        old = time.time() - 120
        for n in ("seg99.ts", "seg100.ts", "seg101.ts", "seg98.ts.tmp"):
            (d / n).write_bytes(b"x")
            os.utime(d / n, (old, old))
        (d / "seg102.ts").write_bytes(b"x")  # fresh, not yet listed
        self.assertEqual(S.janitor(d), 2)
        self.assertEqual(sorted(f.name for f in d.iterdir()), ["live.m3u8", "seg100.ts", "seg101.ts", "seg102.ts"])

    def test_args_start_number(self):
        a = S.ffmpeg_args(1, 1795033200)
        self.assertEqual(a[a.index("-start_number") + 1], "1795033200")

    def test_next_segment_number(self):
        self.assertEqual(S.next_segment_number("#EXTM3U\nseg100.ts\nseg107.ts\nseg103.ts\n", 5), 108)
        self.assertEqual(S.next_segment_number("", 5), 5)
        self.assertEqual(S.next_segment_number("seg3.ts\n", 50), 50)   # never goes backwards

    def test_retire_stale_playlist(self):
        d = Path(tempfile.mkdtemp())
        saved = dict(S.SEQ)
        try:
            S.SEQ["next"] = 0
            (d / "live.m3u8").write_text("#EXTM3U\nseg100.ts\nseg101.ts\n")
            for n in ("seg100.ts", "seg101.ts", "seg102.ts"):
                (d / n).write_bytes(b"x")
            now = (d / "live.m3u8").stat().st_mtime
            # live playlist (younger than LIVE_AGE_S): kept for append_list, numbering still tracked
            self.assertFalse(S.retire_stale_playlist(d, now=now + 1))
            self.assertTrue((d / "live.m3u8").exists())
            self.assertEqual(S.SEQ["next"], 102)
            # after an outage: playlist and its segments go, numbering continues past them
            self.assertTrue(S.retire_stale_playlist(d, now=now + S.LIVE_AGE_S + 1))
            self.assertEqual(sorted(f.name for f in d.iterdir()), ["seg102.ts"])
            self.assertEqual(S.SEQ["next"], 102)
            self.assertFalse(S.retire_stale_playlist(d, now=now + 100))  # nothing left to retire
            self.assertEqual(S.SEQ["next"], 102)
        finally:
            S.SEQ.clear(); S.SEQ.update(saved)

    def test_remove_old_tmp_dirs_spares_dirs_in_use(self):
        tmp = Path(tempfile.mkdtemp())
        old = time.time() - 3600
        for name in ("camlive-hls-old", "camlive-hls-used", "camlive-hls-fresh"):
            (tmp / name).mkdir()
            (tmp / name / "seg1.ts").write_bytes(b"x")
            if name != "camlive-hls-fresh":
                for p in (tmp / name / "seg1.ts", tmp / name):
                    os.utime(p, (old, old))
        ps = "/opt/homebrew/bin/ffmpeg -i x " + str(tmp / "camlive-hls-used" / "seg%06d.ts")
        self.assertEqual(S.remove_old_tmp_dirs(tmp, ps), ["camlive-hls-old"])
        self.assertEqual(sorted(p.name for p in tmp.iterdir()), ["camlive-hls-fresh", "camlive-hls-used"])


class TestPageCache(unittest.TestCase):
    def test_reread_on_mtime_change(self):
        f = Path(tempfile.mkdtemp()) / "ptz.html"
        f.write_text("<title>{{NAME}}</title><p>{{TAGLINE}}</p>")
        orig, S.PAGE_PATH = S.PAGE_PATH, f
        orig_room = dict(S.ROOM)
        try:
            S.ROOM["name"] = "A <b>&"
            S._page["mtime"] = None
            self.assertEqual(S.page_bytes().decode().split("</title>")[0], "<title>A &lt;b&gt;&amp;")
            f.write_text("v2")
            os.utime(f, ns=(time.time_ns() + 10**9,) * 2)
            self.assertEqual(S.page_bytes(), b"v2")
        finally:
            S.PAGE_PATH = orig
            S.ROOM.clear()
            S.ROOM.update(orig_room)
            S._page["mtime"] = None


class TestOps(unittest.TestCase):
    def test_egress(self):
        S._egress.clear()
        S.egress_add(1_000_000, now=100)
        S.egress_add(250_000, now=105)
        self.assertEqual(S.egress_mbps(now=106), 1.0)
        self.assertEqual(S.egress_mbps(now=111), 0.2)

    def test_monitor_alerts_on_changes_only(self):
        sent = []
        S.MON.update(video_down_since=None, ptz_alerted=False)
        S.CAM.update(ok=True, fail_since=None)
        down = {"state": "offline", "down_since": 1000.0, "segment_age_s": None}
        S.monitor_check(now=1100, stream=down, alert=sent.append)
        self.assertEqual(sent, [])
        S.monitor_check(now=1121, stream=down, alert=sent.append)
        S.monitor_check(now=1131, stream=down, alert=sent.append)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("camlive: video down since "))
        S.monitor_check(now=1300, stream={"state": "live", "down_since": None, "segment_age_s": 1.0}, alert=sent.append)
        self.assertEqual(sent[-1], "camlive: video back (down 5m)")
        live = {"state": "live", "down_since": None, "segment_age_s": 1.0}
        S.CAM.update(ok=False, fail_since=2000.0)
        S.monitor_check(now=2299, stream=live, alert=sent.append)
        S.monitor_check(now=2300, stream=live, alert=sent.append)
        S.monitor_check(now=2310, stream=live, alert=sent.append)
        self.assertEqual(sent[-1], "camlive: PTZ failing for 5 min")
        self.assertEqual(len(sent), 3)
        S.CAM.update(ok=None, fail_since=None)

    def test_tunnel_check(self):
        f = Path(tempfile.mkdtemp()) / "current_url.txt"
        S.TUNNEL["url"] = None
        self.assertEqual(S.tunnel_check(lambda: "abc-def.trycloudflare.com", f), "https://abc-def.trycloudflare.com")
        self.assertEqual(f.read_text(), "https://abc-def.trycloudflare.com\n")
        self.assertIsNone(S.tunnel_check(lambda: "abc-def.trycloudflare.com", f))  # unchanged
        self.assertIsNone(S.tunnel_check(lambda: "bad host/<x>", f))
        S.TUNNEL["url"] = None  # restart with the same URL on disk: no alert
        self.assertIsNone(S.tunnel_check(lambda: "abc-def.trycloudflare.com", f))
        self.assertEqual(S.TUNNEL["url"], "https://abc-def.trycloudflare.com")
        S.TUNNEL["url"] = None

    def test_owner_key_lockout(self):
        S._owner_fails.clear()
        self.assertTrue(S.check_owner_key("1.1.1.1", "secret", "secret", now=0))
        for i in range(5):
            self.assertFalse(S.check_owner_key("1.1.1.1", "wrong", "secret", now=i))
        self.assertFalse(S.check_owner_key("1.1.1.1", "secret", "secret", now=10))  # locked out
        self.assertTrue(S.check_owner_key("2.2.2.2", "secret", "secret", now=10))
        self.assertTrue(S.check_owner_key("1.1.1.1", "secret", "secret", now=4 + 601))
        self.assertFalse(S.check_owner_key("3.3.3.3", "x", "", now=0))  # owner_key off


class TestOnvif(unittest.TestCase):
    def test_move_formats_and_escapes(self):
        import onvif
        seen = []
        orig = onvif.call
        onvif.call = lambda service, body: seen.append(body) or "<ok/>"
        try:
            onvif.move(0.5, -1, token='a"<b>&')
            self.assertIn('x="0.50" y="-1.00"', seen[0])
            self.assertIn("a\"&lt;b&gt;&amp;", seen[0])
            self.assertNotIn("Zoom", seen[0])
            with self.assertRaises(ValueError):
                onvif.move('1" injected="', 0, token="t")
            onvif.call = lambda service, body: "<nothing/>"
            with self.assertRaises(RuntimeError):
                onvif.profile_token()
        finally:
            onvif.call = orig


class TestHTTP(unittest.TestCase):
    """The request conventions of the API contract, against an in-process server (no ffmpeg)."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.H)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def req(self, method, path, body=None, headers=None, raw=None):
        c = HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=headers or {})
        r = c.getresponse()
        out = r.status, dict(r.getheaders()), r.read()
        c.close()
        return out

    def post(self, path, body=None, cid="0" * 32, **kw):
        h = {"Content-Type": "application/json"}
        if cid:
            h["X-Client"] = cid
        return self.req("POST", path, body if body is not None else {}, h, **kw)

    def test_security_headers_and_404(self):
        st, h, b = self.req("GET", "/nope")
        self.assertEqual(st, 404)
        self.assertEqual(json.loads(b), {"error": "not_found", "message": "Not found."})
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertEqual(h["Content-Security-Policy"], "frame-ancestors 'none'")
        self.assertEqual(h["Content-Type"], "application/json; charset=utf-8")

    def test_x_client_required(self):
        st, _, b = self.post("/move", {"pan": 1, "tilt": 0}, cid=None)
        self.assertEqual((st, json.loads(b)["error"]), (409, "reload"))
        st, _, _ = self.post("/move", {"pan": 1, "tilt": 0}, cid="ABC")
        self.assertEqual(st, 409)
        st, _, b = self.post("/presence/leave", {"cid": "x"}, cid=None)
        self.assertEqual((st, json.loads(b)), (200, {"ok": True}))

    def test_bad_bodies(self):
        st, _, b = self.post("/move", raw=b'{"pan": NaN, "tilt": 0}')
        self.assertEqual((st, json.loads(b)["error"]), (400, "bad_request"))
        st, _, _ = self.post("/stop", raw=b"[]")
        self.assertEqual(st, 400)
        st, _, b = self.post("/move", {"pan": True, "tilt": 0})
        self.assertEqual(json.loads(b)["message"], "pan and tilt must be numbers.")
        st, _, b = self.post("/zoom", {"level": 7})
        self.assertEqual((st, json.loads(b)["message"]), (400, "Unknown zoom level."))

    def test_move_stop_zoom(self):
        st, _, b = self.post("/move", {"pan": 0.5, "tilt": 0, "nudge": True})
        self.assertEqual(st, 202)
        self.assertEqual(set(json.loads(b)), {"ok", "seat", "camera"})
        self.assertEqual(S.CAM["desired"], (0.5, 0.0))
        st, _, b = self.post("/stop")
        self.assertEqual((st, json.loads(b)), (202, {"ok": True}))
        self.assertEqual(S.CAM["desired"], S.STOP)
        S.ZOOM.update(level=1, target=1)
        st, _, b = self.post("/zoom", {"step": 1})
        z = json.loads(b)["zoom"]
        self.assertEqual((st, z["target"], z["level"]), (202, 1.5, 1))
        S.ZOOM.update(level=1, target=1)

    def test_legacy(self):
        st, _, b = self.req("GET", "/chat?since=5")
        self.assertEqual(json.loads(b)[0]["id"], 1000000000)
        st, _, b = self.req("GET", "/chat?since=1000000000")
        self.assertEqual(json.loads(b), [])
        st, _, b = self.req("GET", "/zoom")
        self.assertEqual(set(json.loads(b)), {"zoom", "levels"})


# ===================================================================================
# Room (engineer 2)
# ===================================================================================
def reset_room():
    with S.ROOM_LOCK:
        S.PRESENCE.clear()
        S.AWAY.clear()
        S.OVERFLOW.clear()
        S.MUTES.clear()
        S.SEAT.__init__(**{k: getattr(S.SEAT, k) for k in (
            "turn_s", "idle_s", "ready_s", "grace_s", "max_queue", "queue_per_ip", "present", "muted",
            "post", "on_change", "queue_per_net", "away", "ready_away_s")})
        S.STATS.update(date=S.today(), countries_today={}, countries_all={}, peak_today=0, turns_today=0)
    with S.CHAT_LOCK:
        S.CHAT.update(msgs=[], next_id=1, deleted=[], dirty=False, last_sys={}, owner_slow=0,
                      auto_slow_until=float("-inf"), recent=S.collections.deque())
        S.CHAT_RATE.clear()
        S.CHAT_IP.clear()
        S.CHAT_NET.clear()
    with S.REACT_LOCK:
        S.REACT.update(counter=S.collections.Counter(), batches=S.collections.deque(maxlen=40), last=0,
                       recent=S.collections.deque(), crowd_at=float("-inf"))
        S.REACT_CLIENT.clear()
        S.REACT_IP.clear()
        S.REACT_NET.clear()


def idt(n, ip="1.2.3.4", owner=False, country="DE"):
    ck = S.client_key(f"{n:032x}")
    return {"ck": ck, "ik": S.ip_key(ip), "tag": S.tag_for(ck), "country": country, "owner": owner}


class TestText(unittest.TestCase):
    def test_clean_text(self):
        self.assertEqual(S.clean_text("ｈｅｌｌｏ"), "hello")  # NFKC
        self.assertEqual(S.clean_text("a‮b⁦c​d⁠e﻿f"), "abcdef")
        self.assertEqual(S.clean_text("\U0001F468\u200d\U0001F469 a\u200cb"), "\U0001F468\u200d\U0001F469 a\u200cb")  # ZWJ/ZWNJ kept
        self.assertEqual(S.clean_text("a\x00b\x07c"), "abc")
        self.assertEqual(S.clean_text("x" + "́" * 10), "x" + "́" * 3)  # zalgo capped
        self.assertEqual(S.clean_text("a\n\n\n\n\nb", True), "a\n\nb")
        self.assertEqual(S.clean_text("a\nb", False), "a b")
        self.assertEqual(S.clean_text("\n".join("abcdefghij"), True), "\n".join("abcdefgh"))
        self.assertEqual(S.clean_text("  x  \r\n", True), "x")
        self.assertEqual(S.clean_text(None), "")
        self.assertEqual(S.clean_text(5, True), "")

    def test_names(self):
        self.assertEqual(S.clean_name("  Curious \t  Otter "), "Curious Otter")
        self.assertEqual(S.name_check("Curious Otter"), ("Curious Otter", None))
        self.assertEqual(S.name_check(""), ("", None))
        self.assertEqual(S.name_check("x"), ("", "invalid"))
        self.assertEqual(S.name_check("x" * 25), ("", "invalid"))
        self.assertEqual(S.name_check("‮​"), ("", "invalid"))
        for n in ("Owner", "0wn3r", "o.w.n.e.r", "ADMIN", "Camera", "M0D", "SYSTEM!!", "Official"):
            self.assertEqual(S.name_check(n), ("", "reserved"), n)
        self.assertEqual(S.name_check("Owner", owner=True), ("Owner", None))
        self.assertEqual(S.name_check("Ownerz"), ("Ownerz", None))
        self.assertEqual(S.fold_name("5y5t3m"), "system")


class TestIdentity(unittest.TestCase):
    def test_tag_derivation(self):
        self.assertEqual(len(S.EMOJI64), 64)
        self.assertEqual(len(set(S.EMOJI64)), 64)
        for e in S.EMOJI64:
            self.assertEqual(len(e), 1)
            self.assertGreaterEqual(ord(e), 0x1F300)
        ck = S.client_key("a" * 32)
        self.assertEqual(len(ck), 16)
        t = S.tag_for(ck)
        self.assertEqual(len(t), 3)
        self.assertTrue(all(c in S.EMOJI64 for c in t))
        self.assertEqual(t, S.tag_for(S.client_key("a" * 32)))  # deterministic
        self.assertNotEqual(ck, S.client_key("b" * 32))
        old = S._SECRET["key"]
        try:
            S._SECRET["key"] = b"z" * 32
            self.assertNotEqual(S.client_key("a" * 32), ck)  # keyed by the secret
        finally:
            S._SECRET["key"] = old

    def test_ip_key(self):
        self.assertEqual(S.norm_ip(None), "local")
        self.assertEqual(S.norm_ip("2001:db8:1:2:aaaa::1"), "2001:db8:1:2::/64")
        self.assertEqual(S.ip_key("2001:db8:1:2::5"), S.ip_key("2001:db8:1:2:ffff::9"))
        self.assertNotEqual(S.ip_key("2001:db8:1:3::5"), S.ip_key("2001:db8:1:2::5"))
        self.assertEqual(S.ip_key("::ffff:1.2.3.4"), S.ip_key("1.2.3.4"))
        self.assertEqual(len(S.ip_key("1.2.3.4")), 16)

    def test_secret_file(self):
        d = Path(tempfile.mkdtemp()) / "sub"
        k1 = S.load_secret(d / "secret.key")
        self.assertEqual(len(k1), 32)
        self.assertEqual(oct((d / "secret.key").stat().st_mode & 0o777), "0o600")
        self.assertEqual(S.load_secret(d / "secret.key"), k1)


class TestBucket(unittest.TestCase):
    def test_burst_and_refill(self):
        b = S.Bucket(3, 3.0, 0.0)
        for _ in range(3):
            self.assertEqual(b.wait(0.0), 0.0)
            b.take(0.0)
        self.assertAlmostEqual(b.wait(0.0), 3.0)
        self.assertAlmostEqual(b.wait(1.5), 1.5)
        self.assertEqual(b.wait(3.0), 0.0)
        b.take(3.0)
        self.assertFalse(b.idle(3.0))
        self.assertTrue(b.idle(100.0))
        self.assertAlmostEqual(b.tokens, 3.0)  # never above burst


class SeatHarness:
    def __init__(self, **kw):
        self.clock = Clock(0.0)
        self.present, self.muted_set, self.posts, self.changes = set(), set(), [], []
        self.seat = S.Seat(clock=self.clock, present=lambda k: k in self.present,
                           muted=lambda k: k in self.muted_set, post=self.posts.append,
                           on_change=self.changes.append, **kw)

    def add(self, key, ip="ip1", owner=False):
        self.present.add(key)
        self.seat.note(key, {"name": key.upper(), "country": "DE", "tag": "🐶🐱🐭", "owner": owner, "ip_key": ip})

    def at(self, t):
        self.clock.t = t
        self.seat.tick()


class TestSeat(unittest.TestCase):
    def setUp(self):
        self.h = SeatHarness()
        self.s = self.h.seat
        for k, ip in (("a", "ip1"), ("b", "ip2"), ("c", "ip3"), ("d", "ip4")):
            self.h.add(k, ip)

    def test_auto_claim_and_no_limit_while_alone(self):
        self.assertEqual(self.s.mode, "free")
        self.assertEqual(self.s.control("a"), "ok")
        self.assertEqual((self.s.mode, self.s.driver, self.s.until), ("driving", "a", None))
        self.assertEqual(self.h.posts, ["🎮 A took the wheel"])
        self.assertEqual(self.h.changes, ["a"])
        self.assertEqual(self.s.control("b"), "not_driver")
        self.assertEqual(self.s.control("b", claim=False), "not_driver")
        self.h.at(100)  # no queue: no time limit, but idle still applies
        self.assertIsNone(self.s.driver)
        self.assertEqual(self.h.posts[-1], "💤 A let go of the wheel")
        self.assertEqual(self.h.changes[-1], None)
        self.assertEqual(self.s.mode, "free")

    def test_stop_never_claims(self):
        self.assertEqual(self.s.control("a", claim=False), "not_driver")
        self.assertEqual(self.s.mode, "free")

    def test_queue_turn_over_offer_claim(self):
        self.s.control("a")
        self.h.clock.t = 30
        self.assertEqual(self.s.join("b"), "ok")
        self.assertEqual(self.s.until, 50)  # max(started+45, now+20)
        self.assertEqual(self.s.join("b"), "ok")  # idempotent
        self.assertEqual(self.s.queue, ["b"])
        pub = self.s.public("b")
        self.assertEqual((pub["you"], pub["your_pos"], pub["left_s"], pub["eta_s"]), ("queued", 1, 20, 20))
        for t in range(31, 50, 2):  # keeps steering
            self.h.clock.t = t
            self.s.control("a")
            self.s.tick()
        self.assertEqual(self.s.driver, "a")
        self.h.at(50)
        self.assertEqual(self.h.posts[-2:], ["⏱ A's turn is over", "🎮 It's B's turn!"])
        self.assertEqual((self.s.mode, self.s.offered), ("offered", "b"))
        pub = self.s.public("b")
        self.assertEqual((pub["you"], pub["idle_left_s"], pub["driver"]["name"]), ("offered", 10, "B"))
        self.assertEqual(self.s.claim("c"), "not_offered")
        self.assertEqual(self.s.control("c"), "not_driver")
        self.h.clock.t = 52
        self.assertEqual(self.s.claim("b"), "ok")
        self.assertEqual((self.s.driver, self.s.until), ("b", None))
        self.assertEqual(self.h.posts[-1], "🎮 B took the wheel")

    def test_start_with_queue_sets_turn_limit(self):
        self.s.control("a")
        self.s.join("b")
        self.s.join("c")
        self.s.leave("a")
        self.assertEqual(self.h.posts[-2:], ["👋 A left the wheel", "🎮 It's B's turn!"])
        self.h.clock.t = 3
        self.assertEqual(self.s.control("b"), "ok")  # an arrow press claims an offered turn
        self.assertEqual(self.s.until, 48)
        self.s.leave("c")
        self.s.tick()
        self.assertIsNone(self.s.until)  # queue empty: no limit

    def test_short_driving_gets_full_turn_when_queue_appears(self):
        self.s.control("a")
        self.h.clock.t = 5
        self.s.join("b")
        self.assertEqual(self.s.until, 45)

    def test_offer_timeout_offers_next(self):
        self.s.control("a")
        self.s.join("b")
        self.s.join("c")
        self.s.leave("a")
        self.h.at(10)
        self.assertEqual(self.h.posts[-2:], ["⏭ B missed their turn", "🎮 It's C's turn!"])
        self.assertNotIn("b", self.s.queue)
        self.h.at(21)
        self.assertEqual(self.s.mode, "free")

    def test_presence_expiry_and_dead_queue_entries(self):
        self.s.control("a")
        self.s.join("b")
        self.s.join("c")
        self.h.present.discard("b")
        self.h.present.discard("a")
        self.h.at(1)
        self.assertIn("👋 A left the wheel", self.h.posts)
        self.assertEqual(self.s.offered, "c")

    def test_limits(self):
        self.s.control("a")
        self.assertEqual(self.s.join("a"), "already_driving")
        self.h.add("b2", "ip2")
        self.h.add("b3", "ip2")
        self.assertEqual(self.s.join("b"), "ok")
        self.assertEqual(self.s.join("b2"), "ok")
        self.assertEqual(self.s.join("b3"), "ip_limit")
        h = SeatHarness(max_queue=2)
        for k in "xyzw":
            h.add(k, "ip" + k)
        h.seat.control("x")
        self.assertEqual([h.seat.join(k) for k in "yzw"], ["ok", "ok", "queue_full"])

    def test_lock_unlock(self):
        self.s.control("a")
        self.s.join("b")
        self.s.set_lock(True)
        self.assertEqual((self.s.mode, self.s.driver, self.s.queue), ("locked", None, ["b"]))
        self.assertEqual(self.h.posts[-1], "🔒 The owner paused the controls")
        self.assertEqual(self.h.changes[-1], None)
        self.assertEqual(self.s.control("a"), "locked")
        self.assertEqual(self.s.join("c"), "locked")
        self.assertEqual(self.s.control("o", owner=True), "ok")
        self.h.at(100)
        self.assertEqual(self.s.mode, "locked")
        self.s.set_lock(False)
        self.assertEqual(self.h.posts[-2:], ["🔓 Controls are open again", "🎮 It's B's turn!"])

    def test_lock_keeps_offered_in_line(self):
        self.s.control("a")
        self.s.join("b")
        self.s.leave("a")
        self.s.set_lock(True)
        self.assertEqual((self.s.offered, self.s.queue), (None, ["b"]))

    def test_kick(self):
        self.s.control("a")
        self.s.join("b")
        self.s.kick()
        self.assertEqual(self.h.posts[-2:], ["⏭ The owner ended A's turn", "🎮 It's B's turn!"])
        self.assertNotIn("a", self.s.queue)
        self.s.kick()  # kicks the offer too
        self.assertEqual(self.h.posts[-1], "⏭ The owner ended B's turn")
        self.assertEqual(self.s.mode, "free")

    def test_owner_take_and_leave(self):
        self.h.add("o", "local", owner=True)
        self.s.control("a")
        self.s.join("b")
        self.h.clock.t = 5
        self.s.take("o")
        self.assertEqual((self.s.driver, self.s.until, self.s.queue), ("o", None, ["a", "b"]))
        self.assertEqual(self.h.posts[-1], "🎮 O took the wheel")
        self.h.at(500)  # no time limit, no idle release for the owner
        self.assertEqual(self.s.driver, "o")
        pub = self.s.public("a")
        self.assertEqual((pub["left_s"], pub["idle_left_s"], pub["driver"]["owner"]), (None, None, True))
        self.s.leave("o")
        self.assertEqual(self.s.offered, "a")

    def test_muted_driver_released_silently(self):
        self.s.control("a")
        self.s.join("b")
        n = len(self.h.posts)
        self.h.muted_set.add("a")
        self.s.remove("a")
        self.assertEqual(self.h.posts[n:], ["🎮 It's B's turn!"])
        self.h.muted_set.add("b")
        self.h.at(1)
        self.assertEqual(self.s.mode, "free")
        self.assertEqual(self.s.control("b"), "not_driver")  # muted cannot auto-claim

    def test_public_shape(self):
        self.s.control("a")
        for k in "bcd":
            self.s.join(k)
        p = self.s.public("d")
        self.assertEqual(set(p), {"mode", "driver", "left_s", "idle_left_s", "turn_s", "queue_len", "queue", "you",
                                  "your_pos", "eta_s"})
        self.assertEqual(p["driver"], {"name": "A", "country": "DE", "tag": "🐶🐱🐭", "owner": False})
        self.assertEqual(p["queue"][0], {"name": "B", "country": "DE", "tag": "🐶🐱🐭"})
        self.assertEqual((p["queue_len"], p["your_pos"], p["eta_s"], p["idle_left_s"]), (3, 3, 2 * 45 + 45, 20))
        self.assertEqual(self.s.public(None)["you"], "none")


class TestChat(unittest.TestCase):
    def setUp(self):
        reset_room()
        self.a, self.b = idt(1), idt(2, ip="5.6.7.8")

    def test_post_and_limits(self):
        st, out = S.chat_post(self.a, "Curious Otter", "hello\n\n\n\nworld", now=0)
        self.assertEqual(st, 200)
        m = out["message"]
        self.assertEqual(set(m), {"id", "kind", "name", "country", "tag", "owner", "text", "t"})
        self.assertEqual((m["id"], m["kind"], m["text"], m["tag"], m["owner"]),
                         (1, "chat", "hello\n\nworld", self.a["tag"], False))
        self.assertEqual(S.chat_post(self.a, "Curious Otter", "hello\n\nworld", now=1)[1]["error"], "duplicate")
        self.assertEqual(S.chat_post(self.a, "C", "x", now=1)[1]["error"], "name_invalid")
        self.assertEqual(S.chat_post(self.a, None, "x", now=1)[1]["error"], "name_invalid")
        self.assertEqual(S.chat_post(self.a, "Admin", "x", now=1)[1]["error"], "name_reserved")
        self.assertEqual(S.chat_post(self.a, "Otter", "​ ", now=1)[1]["error"], "text_invalid")
        self.assertEqual(S.chat_post(self.a, "Otter", "y" * 301, now=1)[1]["error"], "text_invalid")
        self.assertEqual(S.chat_post(self.a, "Otter", "two", now=1)[0], 200)
        self.assertEqual(S.chat_post(self.a, "Otter", "three", now=1)[0], 200)
        st, out = S.chat_post(self.a, "Otter", "four", now=1)  # burst 3 used up
        self.assertEqual((st, out["error"], out["retry_in"]), (429, "rate_limited", 2))
        self.assertEqual(S.chat_post(self.a, "Otter", "four", now=4)[0], 200)  # 1 token per 3 s
        self.assertEqual(S.chat_post(self.b, "Heron", "hi", now=3)[0], 200)  # other client unaffected

    def test_ip_bucket(self):
        same_ip = [idt(10 + i, ip="9.9.9.9") for i in range(3)]
        codes = [S.chat_post(c, "Fox", f"m{c['ck']}{j}", now=0)[0] for c in same_ip for j in range(3)]
        self.assertEqual(codes.count(200), 6)
        self.assertEqual(codes.count(429), 3)

    def test_owner_exempt_and_name(self):
        o = idt(3, ip="local", owner=True, country="LOCAL")
        for i in range(10):
            st, out = S.chat_post(o, "whatever", "same", now=0)
            self.assertEqual(st, 200)
        self.assertEqual((out["message"]["name"], out["message"]["owner"]), ("Owner", True))

    def test_slow_modes(self):
        S.CHAT["owner_slow"] = 10
        self.assertEqual(S.chat_post(self.a, "Otter", "one", now=100)[0], 200)
        st, out = S.chat_post(self.a, "Otter", "two", now=104)
        self.assertEqual((st, out["error"], out["retry_in"], out["message"]),
                         (429, "slow_mode", 6, "Slow mode: one message every 10 s."))
        self.assertEqual(S.chat_post(self.a, "Otter", "two", now=110)[0], 200)
        S.CHAT["owner_slow"] = 0
        o = idt(3, ip="local", owner=True)
        for i in range(31):
            S.chat_post(o, "", f"flood {i}", now=200)
        self.assertEqual(S.slow_s(200), 5)  # auto slow mode
        self.assertEqual(S.chat_delta(0, 200)["slow_s"], 5)
        self.assertEqual(S.slow_s(200 + 121), 0)

    def test_sys_collapse_and_cc(self):
        self.assertIsNotNone(S.post_sys("🔥 The crowd is loving this!", now=0))
        self.assertIsNone(S.post_sys("🔥 The crowd is loving this!", now=10))
        self.assertIsNotNone(S.post_sys("🔥 The crowd is loving this!", now=31))
        m = S.post_sys("🎉 First visitor today from {cc}", cc="IN", now=0)
        self.assertEqual((m["cc"], m["name"], m["tag"], m["kind"], m["country"]), ("IN", "camera", "", "sys", ""))
        self.assertIsNotNone(S.post_sys("🎉 First visitor today from {cc}", cc="US", now=1))
        self.assertNotIn("cc", S.post_sys("plain", cc="IN", now=0))

    def test_delta_and_delete(self):
        for i in range(120):
            S.post_sys(f"line {i}", now=0)
        d = S.chat_delta(0, 0)
        self.assertEqual([m["id"] for m in d["messages"]], list(range(71, 121)))
        self.assertEqual(len(S.chat_delta(5, 0)["messages"]), 100)
        self.assertEqual(S.chat_delta(5, 0)["messages"][0]["id"], 6)
        self.assertEqual(len(S.chat_delta(999, 0)["messages"]), 50)
        self.assertEqual(S.chat_delta(120, 0)["messages"], [])
        self.assertTrue(S.delete_message(120, now=0))
        self.assertFalse(S.delete_message(120, now=0))
        self.assertEqual(S.chat_delta(119, 1)["deleted"], [120])
        self.assertEqual(S.chat_delta(119, 601)["deleted"], [])
        # a page whose cursor is the deleted newest id gets nothing new, not the last 50 again
        self.assertEqual(S.chat_delta(120, 1)["messages"], [])

    def test_persistence(self):
        S.chat_post(self.a, "Otter", "kept", now=0)
        S.post_sys("hello", now=0)
        d = Path(tempfile.mkdtemp())
        self.assertTrue(S.save_chat(d / "chat.json"))
        self.assertFalse(S.save_chat(d / "chat.json"))  # not dirty
        saved = json.loads((d / "chat.json").read_text())
        self.assertEqual(saved["messages"][0]["_ck"], self.a["ck"])  # author keys stay server-side
        reset_room()
        self.assertEqual(S.load_chat(d / "chat.json"), 2)
        self.assertEqual(S.CHAT["next_id"], 3)
        self.assertEqual(S.message_author(1), (self.a["ck"], self.a["ik"], None))
        self.assertNotIn("_ck", S.chat_delta(0)["messages"][0])
        S.post_sys("after restart", now=0)
        self.assertEqual(S.CHAT["msgs"][-1]["id"], 3)
        S.STATS["countries_today"] = {"IN": 5, "US": 9}
        S.save_stats(d / "stats.json")
        S.STATS["countries_today"] = {}
        S.load_stats(d / "stats.json")
        self.assertEqual(S.flags_public()["today"], ["IN", "US"])


class TestMutesAndReactions(unittest.TestCase):
    def setUp(self):
        reset_room()
        self.a = idt(1)

    def test_mute(self):
        S.MUTES[f"i:{self.a['ik']}"] = S.mono() + 600
        same_ip = idt(2)  # muting the ip key mutes every client behind it
        st, out = S.chat_post(same_ip, "Otter", "hi")
        self.assertEqual((st, out["error"], out["muted_s"], out["message"]), (403, "muted", 600, "You're muted for 10 min."))
        self.assertIsNone(S.muted_s_of(idt(3, ip="local", owner=True)))
        self.assertEqual(S.react_post(same_ip, "🔥"), (202, {"ok": True}))
        self.assertFalse(S.REACT["counter"])  # dropped

    def test_reactions(self):
        self.assertEqual(S.react_post(self.a, "🍕")[0], 400)
        self.assertEqual(S.react_post(self.a, None)[0], 400)
        codes = [S.react_post(self.a, "🔥", now=0)[0] for _ in range(10)]
        self.assertEqual(codes, [202] * 8 + [429] * 2)
        self.assertEqual(S.react_post(self.a, "🔥", now=0)[1], {"error": "rate_limited", "message": "Easy there!",
                                                                 "retry_in": 1})
        S.flush_reactions(now=0)
        self.assertEqual(S.reactions_delta(0), {"last": 1, "batches": []})
        self.assertEqual(S.reactions_delta(1), {"last": 1, "batches": []})
        self.assertEqual(S.reactions_delta(-5)["batches"], [])
        S.flush_reactions(now=0.5)  # nothing new: no empty batch
        self.assertEqual(S.REACT["last"], 1)
        for i in range(4):
            S.react_post(idt(20 + i, ip=f"10.0.0.{i}"), "🔥", now=1)
        S.flush_reactions(now=1)
        self.assertEqual(S.reactions_delta(0 + 1)["batches"], [{"id": 2, "c": {"🔥": 4}}])
        self.assertEqual(S.CHAT["msgs"][-1]["text"], "🔥 The crowd is loving this!")  # 12 within 3 s
        n = len(S.CHAT["msgs"])
        for i in range(12):
            S.react_post(idt(40 + i, ip=f"10.0.1.{i}"), "😂", now=2)
        S.flush_reactions(now=2)
        self.assertEqual(len(S.CHAT["msgs"]), n)  # at most once per 120 s


class TestRoomHTTP(TestHTTP):
    """Three clients plus a remote one, over HTTP, against the contract shapes."""
    LOCAL = {"Host": "127.0.0.1:8799"}  # what a request from the owner's Mac looks like

    def setUp(self):
        reset_room()

    def p(self, path, body=None, cid=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        if cid:
            h["X-Client"] = cid
        st, hd, b = self.req("POST", path, body if body is not None else {}, h)
        return st, json.loads(b)

    A, B, C, O = "a" * 32, "b" * 32, "c" * 32, "0123456789abcdef" * 2
    RA = {"CF-Connecting-IP": "203.0.113.5", "CF-IPCountry": "in"}
    RB = {"CF-Connecting-IP": "2001:db8::1", "CF-IPCountry": "US"}
    RC = {"CF-Connecting-IP": "198.51.100.7", "CF-IPCountry": "XX"}

    def test_state_shape(self):
        st, s = self.p("/state", {"name": "Curious Otter", "chat_since": "x", "react_since": -1}, self.A, self.RA)
        self.assertEqual(st, 200)
        self.assertEqual(list(s), ["boot", "now", "room", "stream", "zoom", "camera", "seat", "viewers", "chat",
                                   "reactions", "flags", "me"])
        self.assertEqual(set(s["room"]), {"name", "tagline", "place", "reactions", "tz_offset_s", "tz_name"})
        self.assertEqual(set(s["viewers"]), {"count", "countries", "named", "others"})
        self.assertEqual(s["viewers"]["named"], [{"name": "Curious Otter", "country": "IN",
                                                  "tag": S.tag_for(S.client_key(self.A)), "driver": False,
                                                  "owner": False}])
        self.assertEqual(s["viewers"]["countries"], {"IN": 1})
        self.assertEqual(s["chat"]["messages"][-1]["text"], "🎉 First visitor today from {cc}")
        self.assertEqual(s["chat"]["messages"][-1]["cc"], "IN")
        self.assertEqual(s["flags"], {"today": ["IN"], "today_count": 1, "all_time_count": 1, "peak_today": 1})
        self.assertEqual(s["me"], {"tag": S.tag_for(S.client_key(self.A)), "name": "Curious Otter",
                                   "name_error": None, "is_owner": False, "muted_s": None})
        self.assertEqual(s["reactions"], {"last": 0, "batches": []})
        _, s = self.p("/state", {"name": "admin"}, self.B, self.RB)
        self.assertEqual((s["me"]["name"], s["me"]["name_error"]), ("", "reserved"))
        _, s = self.p("/state", {"name": "x"}, self.C, self.RC)
        self.assertEqual((s["me"]["name"], s["me"]["name_error"]), ("", "invalid"))
        self.assertEqual(s["viewers"]["count"], 3)
        self.assertEqual(len(s["viewers"]["named"]), 1)
        _, s = self.p("/state", {}, self.O, self.LOCAL)
        self.assertEqual((s["me"]["is_owner"], s["me"]["name"]), (True, "Owner"))
        self.assertEqual(s["viewers"]["named"][0]["name"], "Owner")  # owner sorts first

    def test_seat_flow(self):
        for cid, h in ((self.A, self.RA), (self.B, self.RB), (self.C, self.RC)):
            self.p("/state", {"name": f"N{cid[0]}"}, cid, h)
        st, out = self.p("/move", {"pan": 0.5, "tilt": 0}, self.A, self.RA)
        self.assertEqual((st, out["seat"]["you"], out["seat"]["mode"]), (202, "driver", "driving"))
        st, out = self.p("/move", {"pan": 0.5, "tilt": 0}, self.B, self.RB)
        self.assertEqual((st, out["error"], out["message"]), (409, "not_driver", "Na is driving. Join the line to take a turn."))
        self.assertEqual(self.p("/stop", {}, self.B, self.RB)[0], 409)
        self.assertEqual(self.p("/zoom", {"step": 1}, self.B, self.RB)[0], 409)
        self.assertEqual(self.p("/seat/join", {}, self.A, self.RA)[1]["error"], "already_driving")
        st, out = self.p("/seat/join", {}, self.B, self.RB)
        self.assertEqual((st, out["seat"]["you"], out["seat"]["your_pos"]), (200, "queued", 1))
        self.assertEqual(self.p("/seat/claim", {}, self.B, self.RB)[1]["error"], "not_offered")
        st, out = self.p("/seat/leave", {}, self.A, self.RA)
        self.assertEqual((st, out["seat"]["mode"], out["seat"]["driver"]["name"]), (200, "offered", "Nb"))
        st, out = self.p("/seat/claim", {}, self.B, self.RB)
        self.assertEqual((st, out["seat"]["you"]), (200, "driver"))
        self.assertEqual(self.p("/stop", {}, self.B, self.RB), (202, {"ok": True}))
        # owner powers
        self.assertEqual(self.p("/mod/lock", {"locked": True}, self.A, self.RA),
                         (403, {"error": "not_owner", "message": "Owner only."}))
        self.p("/state", {}, self.O, self.LOCAL)
        st, out = self.p("/mod/lock", {"locked": True}, self.O, self.LOCAL)
        self.assertEqual((st, out["seat"]["mode"], out["seat"]["driver"]), (200, "locked", None))
        st, out = self.p("/move", {"pan": 0.1, "tilt": 0}, self.B, self.RB)
        self.assertEqual((st, out["error"], out["message"]), (409, "locked", "The owner has paused the controls."))
        self.assertEqual(self.p("/seat/join", {}, self.C, self.RC)[1]["error"], "locked")
        self.assertEqual(self.p("/move", {"pan": 0.1, "tilt": 0}, self.O, self.LOCAL)[0], 202)
        self.assertEqual(self.p("/mod/lock", {"locked": "no"}, self.O, self.LOCAL)[0], 400)
        self.p("/mod/lock", {"locked": False}, self.O, self.LOCAL)
        self.p("/seat/join", {}, self.C, self.RC)
        st, out = self.p("/mod/take", {}, self.O, self.LOCAL)
        self.assertEqual((out["seat"]["driver"]["owner"], out["seat"]["left_s"]), (True, None))
        self.assertEqual(out["seat"]["queue"][0]["name"], "Nc")
        st, out = self.p("/mod/kick", {}, self.O, self.LOCAL)
        self.assertEqual((st, out["seat"]["mode"]), (200, "offered"))
        texts = [m["text"] for m in S.CHAT["msgs"]]
        for line in ("🎮 Na took the wheel", "👋 Na left the wheel", "🎮 It's Nb's turn!", "🎮 Nb took the wheel",
                     "🔒 The owner paused the controls", "🔓 Controls are open again", "🎮 Owner took the wheel",
                     "⏭ The owner ended Owner's turn", "🎮 It's Nc's turn!"):
            self.assertIn(line, texts)
        # leave beacon of the offered client: gone after the short grace (a reload would poll again first)
        self.assertEqual(self.p("/presence/leave", {"cid": self.C}), (200, {"ok": True}))
        self.assertEqual(self.p("/presence/leave", {"cid": "nope"}), (200, {"ok": True}))
        self.assertEqual(S.SEAT.mode, "offered")
        S.room_tick(S.mono() + S.LEAVE_GRACE_S + 0.5)
        self.assertEqual(S.SEAT.mode, "free")

    def test_chat_mod(self):
        self.p("/state", {"name": "Nb"}, self.B, self.RB)
        st, out = self.p("/chat", {"name": "Nb", "text": "hi"}, self.B, self.RB)
        self.assertEqual((st, out["message"]["country"], out["message"]["kind"]), (200, "US", "chat"))
        mid = out["message"]["id"]
        self.assertEqual(self.p("/chat", {"name": "Nb", "text": "hi"}, self.B, self.RB)[1]["error"], "duplicate")
        self.assertEqual(self.p("/mod/delete", {"id": mid}, self.B, self.RB)[0], 403)
        self.assertEqual(self.p("/mod/delete", {"id": mid}, self.O, self.LOCAL), (200, {"ok": True}))
        self.assertEqual(self.p("/mod/delete", {"id": mid}, self.O, self.LOCAL)[1]["error"], "not_found")
        self.assertEqual(self.p("/mod/delete", {"id": "1"}, self.O, self.LOCAL)[0], 400)
        _, s = self.p("/state", {"name": "Nb", "chat_since": mid - 1}, self.B, self.RB)
        self.assertEqual(s["chat"]["deleted"], [mid])
        self.assertEqual(self.p("/mod/mute", {"tag": s["me"]["tag"], "minutes": 5}, self.O, self.LOCAL)[0], 400)
        self.assertEqual(self.p("/mod/mute", {"tag": "nobody", "minutes": 10}, self.O, self.LOCAL)[0], 404)
        self.assertEqual(self.p("/mod/mute", {"tag": s["me"]["tag"], "minutes": 10}, self.O, self.LOCAL),
                         (200, {"ok": True, "muted_s": 600}))
        _, s = self.p("/state", {"name": "Nb"}, self.B, self.RB)
        self.assertEqual((s["me"]["muted_s"], s["me"]["name"], s["viewers"]["named"]), (600, "", []))
        st, out = self.p("/move", {"pan": 1, "tilt": 0}, self.B, self.RB)
        self.assertEqual((st, out["error"]), (403, "muted"))
        self.assertEqual(self.p("/seat/join", {}, self.B, self.RB)[0], 403)
        self.assertEqual(self.p("/zoom", {"step": 1}, self.B, self.RB)[0], 403)
        self.assertEqual(self.p("/chat", {"name": "Nb", "text": "x"}, self.B, self.RB)[0], 403)
        self.assertEqual(self.p("/react", {"e": "🔥"}, self.B, self.RB), (202, {"ok": True}))
        self.assertEqual(self.p("/mod/mute", {"tag": s["me"]["tag"], "minutes": 0}, self.O, self.LOCAL)[1]["muted_s"], 0)
        self.assertEqual(self.p("/chat", {"name": "Nb", "text": "back"}, self.B, self.RB)[0], 200)
        st, out = self.p("/mod/slowmode", {"seconds": 30}, self.O, self.LOCAL)
        self.assertEqual((st, out), (200, {"ok": True, "slow_s": 30}))
        self.assertEqual(self.p("/mod/slowmode", {"seconds": 61}, self.O, self.LOCAL)[0], 400)
        self.assertEqual(S.CHAT["msgs"][-1]["text"], "🐢 Slow mode: one message every 30 s")
        st, out = self.p("/chat", {"name": "Owner", "text": "hello all"}, self.O, self.LOCAL)
        self.assertEqual((out["message"]["owner"], out["message"]["name"]), (True, "Owner"))
        self.assertEqual(self.p("/mod/mute", {"id": out["message"]["id"] - 100, "minutes": 10}, self.O, self.LOCAL)[0], 404)
        self.assertEqual(self.p("/react", {"e": "nope"}, self.B, self.RB)[0], 400)

    def test_owner_key_header(self):
        old = S.ROOM["owner_key"]
        S.ROOM["owner_key"] = "s3cret-key"
        try:
            self.assertEqual(self.p("/mod/kick", {}, self.A, {**self.RA, "X-Owner-Key": "s3cret-key"})[0], 200)
            self.assertEqual(self.p("/mod/kick", {}, self.A, {**self.RA, "X-Owner-Key": "wrong"})[0], 403)
            self.assertEqual(self.p("/mod/kick", {}, self.A, self.RA)[0], 403)
            # a spoofed Host through the tunnel is not local
            self.assertEqual(self.p("/mod/kick", {}, self.A, {**self.RA, **self.LOCAL})[0], 403)
        finally:
            S.ROOM["owner_key"] = old
            S._owner_fails.clear()

    def test_presence_caps(self):
        cids = [f"{i:032x}" for i in range(5)]
        for c in cids:
            self.p("/state", {"name": "Same Net"}, c, self.RA)
        # 3 per ip; live entries are never evicted, the rest are counted as 'others'
        self.assertEqual(set(S.PRESENCE), {S.client_key(c) for c in cids[:3]})
        old = S.MAX_VIEWERS
        S.MAX_VIEWERS = 3
        try:
            _, s = self.p("/state", {}, "f" * 32, self.RB)
            self.assertEqual((s["viewers"]["count"], s["viewers"]["others"]), (6, 3))
        finally:
            S.MAX_VIEWERS = old

    def test_legacy_presence(self):
        self.p("/state", {"name": "Nb"}, self.B, self.RB)
        st, out = self.p("/presence", {"name": "old", "id": "x"})
        self.assertEqual((st, out), (200, [{"name": "Nb", "country": "US"}]))


if __name__ == "__main__":
    unittest.main()
