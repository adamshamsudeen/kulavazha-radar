"""Report weed: rate limits and the item sent to Kulavazha Radar."""
import json, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tests  # noqa: F401
import ptz_server as S  # noqa: E402


class ReportWeed(unittest.TestCase):
    def setUp(self):
        S.REPORT_IP.clear()
        S.REPORT_BUCKET["b"] = None

    def test_one_per_ip_per_minute(self):
        self.assertEqual(S.report_allowed("1.1.1.1", now=100), (True, 0))
        ok, retry = S.report_allowed("1.1.1.1", now=110)
        self.assertFalse(ok)
        self.assertEqual(retry, 50)
        self.assertTrue(S.report_allowed("1.1.1.1", now=161)[0])

    def test_shared_bucket_caps_a_crowd(self):
        oks = [S.report_allowed(f"10.0.0.{i}", now=100)[0] for i in range(8)]
        self.assertEqual(oks.count(True), 5)

    def test_item_shape(self):
        it = S.radar_item("Fuzzy Lynx", b"\xff\xd8jpeg")
        self.assertEqual(it["source"], "payalcam")
        self.assertEqual(it["extraction"]["place_id"], "punnamada")
        self.assertTrue(it["photo"].startswith("data:image/jpeg;base64,"))
        self.assertIn("Fuzzy Lynx", it["reporter"])
        self.assertIsNone(S.radar_item("x", None)["photo"])

    def test_posts_to_radar_feed(self):
        got = []

        class Radar(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                b = b'{"ok":true,"seq":7}'
                self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Radar)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old = dict(S.RADAR)
        S.RADAR["feed_url"] = f"http://127.0.0.1:{srv.server_address[1]}/api/feed"
        sent = {}

        class H:
            headers = {}
            client_address = ("9.9.9.9", 1)
            def send_json(self, obj, code=200): sent.update(obj=obj, code=code)
            def send_err(self, code, error, message, **kw): sent.update(obj={"error": error}, code=code)

        orig = (S.caller_name, S.latest_frame_jpeg, S.post_sys)
        S.caller_name, S.latest_frame_jpeg, S.post_sys = (lambda h: "Swift Kite"), (lambda: None), (lambda *a, **k: None)
        try:
            S.h_report_weed(H(), {})
        finally:
            S.caller_name, S.latest_frame_jpeg, S.post_sys = orig
            S.RADAR.clear(); S.RADAR.update(old); srv.shutdown()
        self.assertEqual(sent["code"], 200)
        self.assertEqual(sent["obj"]["seq"], 7)
        self.assertEqual(got[0]["reporter"], "PayalCam viewer Swift Kite")


if __name__ == "__main__":
    unittest.main()
