"""Tests for the review fixes (rate tables, presence, seat liveness, IPv6 /48 tier, persistence,
camera worker, stream state, owner key, names, logging and HTTP details). Run: python3 -m unittest
"""
import json, math, tempfile, threading, unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  (sets the CAMLIVE_* env before config is imported)
from tests import test_logic as T

S = T.S
idt = T.idt


def idt6(n, ip):
    d = idt(n, ip=ip)
    d["nk"] = S.net_key(ip)
    return d


class TestRateTables(unittest.TestCase):
    def setUp(self):
        T.reset_room()

    def test_prune_buckets_deletes_idle(self):
        table = {"a": S.Bucket(3, 3.0, 0.0), "b": S.Bucket(3, 3.0, 0.0)}
        table["b"].take(650.0)
        S.prune_buckets(table, 700.0)
        self.assertEqual(set(table), {"b"})
        S.prune_buckets(table, 650.0 + 601)
        self.assertEqual(table, {})

    def test_refused_reactions_store_nothing(self):
        # one IP with a fresh X-Client per request: the IP bucket (burst 24) refuses the rest
        codes = [S.react_post(idt(1000 + i, ip="192.0.2.99"), "🔥", now=0)[0] for i in range(200)]
        self.assertEqual(codes.count(202), 24)
        self.assertEqual(len(S.REACT_CLIENT), 24)
        self.assertEqual(len(S.REACT_IP), 1)
        S.prune_rate_tables(now=601)
        self.assertEqual((len(S.REACT_CLIENT), len(S.REACT_IP)), (0, 0))

    def test_refused_chat_stores_nothing_and_prunes(self):
        codes = [S.chat_post(idt(2000 + i, ip="192.0.2.98"), "Otter", f"m{i}", now=0)[0] for i in range(50)]
        self.assertEqual(codes.count(200), 6)
        self.assertEqual(len(S.CHAT_RATE), 6)
        S.prune_rate_tables(now=601)
        self.assertEqual((len(S.CHAT_RATE), len(S.CHAT_IP)), (0, 0))

    def test_make_room_caps_table(self):
        table = {i: S.Bucket(1, 1.0, float(i)) for i in range(10)}
        S.make_room(table, 10.0, cap=10)
        self.assertEqual(len(table), 9)
        self.assertNotIn(0, table)  # least recently used goes first

    def test_slow_mode_and_duplicate_per_network(self):
        S.CHAT["owner_slow"] = 60
        self.assertEqual(S.chat_post(idt(1, ip="192.0.2.50"), "Otter", "one", now=0)[0], 200)
        st, out = S.chat_post(idt(2, ip="192.0.2.50"), "Otter", "two", now=5)  # fresh X-Client, same IP
        self.assertEqual((st, out["error"]), (429, "slow_mode"))
        self.assertEqual(S.chat_post(idt(3, ip="192.0.2.51"), "Heron", "hi", now=5)[0], 200)  # other network
        S.CHAT["owner_slow"] = 0
        st, out = S.chat_post(idt(4, ip="192.0.2.51"), "Fox", "hi", now=10)
        self.assertEqual((st, out["error"]), (429, "duplicate"))
        self.assertEqual(out["message"], "Someone on your network just said that.")


class TestIPv6Net(unittest.TestCase):
    def setUp(self):
        T.reset_room()

    def test_net_key(self):
        self.assertEqual(S.norm_net("2001:db8:aa:1::5"), "2001:db8:aa::/48")
        self.assertEqual(S.net_key("2001:db8:aa:1::5"), S.net_key("2001:db8:aa:ffff::9"))
        self.assertNotEqual(S.net_key("2001:db8:aa::1"), S.net_key("2001:db8:ab::1"))
        for ip in ("1.2.3.4", "::ffff:1.2.3.4", None, "", "junk"):
            self.assertIsNone(S.net_key(ip))

    def test_presence_and_queue_caps_per_48(self):
        now = 100.0
        with S.ROOM_LOCK:
            ids = [idt6(i, f"2001:db8:aa:{i}::1") for i in range(20)]  # 20 different /64s, one /48
            admitted = [S.touch_presence(d, "Bot", now) for d in ids]
            self.assertEqual(admitted.count(True), S.PER_NET_VIEWERS)
            S.SEAT.control(ids[0]["ck"], now)
            results = [S.SEAT.join(d["ck"], now) for d in ids[1:S.PER_NET_VIEWERS]]
        self.assertEqual(results.count("ok"), S.SEAT.queue_per_net)
        self.assertEqual(results.count("ip_limit"), S.PER_NET_VIEWERS - 1 - S.SEAT.queue_per_net)
        with S.ROOM_LOCK:  # a real viewer elsewhere still gets in and can queue
            real = idt(99, ip="198.51.100.77")
            self.assertTrue(S.touch_presence(real, "Real", now))
            self.assertEqual(S.SEAT.join(real["ck"], now), "ok")

    def test_mute_covers_the_48(self):
        a = idt6(1, "2001:db8:aa:1::1")
        S.MUTES[f"n:{a['nk']}"] = S.mono() + 600
        other64 = idt6(2, "2001:db8:aa:2::1")
        self.assertEqual(S.chat_post(other64, "Otter", "hi")[0], 403)
        self.assertEqual(S.chat_post(idt6(3, "2001:db8:bb::1"), "Otter", "hi")[0], 200)

    def test_chat_bucket_per_48(self):
        codes = [S.chat_post(idt6(i, f"2001:db8:aa:{i}::1"), "Bot", f"m{i}", now=0)[0] for i in range(30)]
        self.assertEqual(codes.count(200), 10)  # /48 burst, not 6 per /64


class TestPresence(unittest.TestCase):
    def setUp(self):
        T.reset_room()

    def test_shared_ip_never_evicts_the_driver(self):
        a, b, c, d = (idt(i, ip="203.0.113.7") for i in range(1, 5))
        with S.ROOM_LOCK:
            for t in range(3):
                for x in (a, b, c, d):
                    S.touch_presence(x, "N", 100.0 + t)
                if t == 0:
                    S.SEAT.control(a["ck"], 100.0)
                S.room_tick(100.0 + t + 0.5)
            self.assertEqual(S.SEAT.driver, a["ck"])
            self.assertEqual(set(S.PRESENCE), {a["ck"], b["ck"], c["ck"]})
            self.assertIn(d["ck"], S.OVERFLOW)
            self.assertEqual(S.SEAT.control(d["ck"], 103.0), "not_driver")  # overflow cannot take the seat
        self.assertNotIn("left the wheel", " ".join(m["text"] for m in S.CHAT["msgs"]))

    def test_overflow_is_bounded(self):
        with S.ROOM_LOCK:
            for i in range(50):
                S.touch_presence(idt(100 + i, ip="192.0.2.7"), None, 100.0)
        self.assertEqual(len(S.PRESENCE), S.PER_IP_VIEWERS)
        self.assertEqual(len(S.OVERFLOW), S.PER_GROUP_OVERFLOW)

    def test_hidden_queued_tab_keeps_its_place(self):
        a, b = idt(1, ip="198.51.100.1"), idt(2, ip="198.51.100.2")
        t = 100.0
        with S.ROOM_LOCK:
            S.touch_presence(a, "Alice", t)
            S.touch_presence(b, "Bob", t)
            S.SEAT.control(a["ck"], t)
            S.SEAT.join(b["ck"], t)
            for i in range(1, 31):  # A keeps driving; B is in the background with a 5 s heartbeat
                t += 1
                S.touch_presence(a, None, t)
                S.SEAT.control(a["ck"], t)
                if i % 5 == 0:
                    S.touch_presence(b, None, t, hidden=True)
                S.room_tick(t)
            self.assertNotIn(b["ck"], S.PRESENCE)  # not counted as watching
            self.assertIn(b["ck"], S.AWAY)
            self.assertEqual(S.viewers_public(t)["count"], 1)
            self.assertIn(b["ck"], S.SEAT.queue)
            S.SEAT.leave(a["ck"], t)  # the turn comes up while B is away: a longer offer window
            self.assertEqual(S.SEAT.offered, b["ck"])
            self.assertEqual(S.SEAT.offer_until - t, S.SEAT.ready_away_s)
            S.touch_presence(b, None, t + 20)  # back to the tab
            self.assertIn(b["ck"], S.PRESENCE)
            self.assertEqual(S.SEAT.claim(b["ck"], t + 20), "ok")

    def test_hidden_tab_without_heartbeat_is_dropped(self):
        a, b = idt(1, ip="local", owner=True), idt(2, ip="198.51.100.2")  # owner: no turn limit
        with S.ROOM_LOCK:
            S.touch_presence(a, "Alice", 0.0)
            S.touch_presence(b, "Bob", 0.0)
            S.SEAT.control(a["ck"], 0.0)
            S.SEAT.join(b["ck"], 0.0)
            for t in range(1, 125):
                S.touch_presence(a, None, float(t))
                S.SEAT.control(a["ck"], float(t))
                S.room_tick(float(t))
                if t == 60:
                    self.assertIn(b["ck"], S.SEAT.queue)
            self.assertNotIn(b["ck"], S.SEAT.queue)
            self.assertNotIn(b["ck"], S.AWAY)

    def test_hidden_heartbeat_never_creates_presence(self):
        with S.ROOM_LOCK:
            self.assertFalse(S.touch_presence(idt(5), "X", 0.0, hidden=True))
        self.assertEqual((S.PRESENCE, S.AWAY), ({}, {}))

    def test_leave_beacon_grace(self):
        a, b = idt(1, ip="198.51.100.1"), idt(2, ip="198.51.100.2")
        with S.ROOM_LOCK:
            S.touch_presence(a, "Alice", 0.0)
            S.touch_presence(b, "Bob", 0.0)
            S.SEAT.control(a["ck"], 0.0)
            S.presence_leave(a["ck"], 1.0)  # reload, or a duplicate tab closed
            S.touch_presence(a, None, 1.5)  # the page polls again
            S.touch_presence(b, None, 1.5)
            S.room_tick(6.0)
            self.assertEqual(S.SEAT.driver, a["ck"])
            S.SEAT.join(b["ck"], 6.0)
            S.presence_leave(b["ck"], 7.0)  # queued tab really closed
            S.touch_presence(a, None, 7.0)
            S.SEAT.control(a["ck"], 7.0)
            S.room_tick(7.0 + S.LEAVE_GRACE_S + 0.5)
            self.assertNotIn(b["ck"], S.SEAT.queue)
            self.assertNotIn(b["ck"], S.AWAY)
            S.presence_leave(a["ck"], 11.0)
            S.room_tick(11.0 + S.LEAVE_GRACE_S + 0.5)
            self.assertIsNone(S.SEAT.driver)
        self.assertIn("👋 Alice left the wheel", [m["text"] for m in S.CHAT["msgs"]])


class TestPersistence(unittest.TestCase):
    def setUp(self):
        T.reset_room()
        self.d = Path(tempfile.mkdtemp())

    def test_concurrent_saves_never_corrupt(self):
        path = self.d / "chat.json"
        errors = []

        def writer(n):
            try:
                for i in range(40):
                    S.atomic_write_json(path, {"next_id": n, "messages": [{"x": "y" * (n * 50 + i)}]})
            except Exception as e:  # pragma: no cover
                errors.append(e)
        ts = [threading.Thread(target=writer, args=(n,)) for n in range(1, 5)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        json.loads(path.read_text())
        self.assertEqual([p.name for p in self.d.iterdir()], ["chat.json"])  # no temp files left
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")

    def test_damaged_files_never_stop_startup(self):
        for text in ("[1, 2]", "{\"next_id\": [], \"messages\": 5}", "not json", "{\"messages\": [{\"id\": NaN}]}"):
            (self.d / "chat.json").write_text(text)
            S.load_chat(self.d / "chat.json")
        self.assertEqual(S.CHAT["next_id"], 1)
        for text in ("[]", "{\"peak_today\": NaN}", "{\"countries_all\": {\"US\": Infinity}}", "{{"):
            (self.d / "stats.json").write_text(text)
            S.load_stats(self.d / "stats.json")

    def test_mutes_and_slow_mode_survive_restart(self):
        now = S.mono()
        S.MUTES.update({"c:" + "a" * 16: now + 3600, "i:" + "b" * 16: now - 5})
        S.CHAT["owner_slow"] = 30
        S.save_moderation(self.d / "moderation.json")
        T.reset_room()
        S.load_moderation(self.d / "moderation.json")
        self.assertEqual(set(S.MUTES), {"c:" + "a" * 16})  # expired one dropped
        self.assertAlmostEqual(S.MUTES["c:" + "a" * 16] - S.mono(), 3600, delta=2)
        self.assertEqual(S.slow_s(), 30)
        (self.d / "moderation.json").write_text('{"mutes": {"x:zz": 9e99, "c:' + "c" * 16 + '": "soon"}}')
        T.reset_room()
        S.load_moderation(self.d / "moderation.json")
        self.assertEqual(S.MUTES, {})
        self.assertFalse(S.SEAT.locked)

    def test_control_lock_survives_restart(self):
        S.SEAT.set_lock(True)
        S.save_moderation(self.d / "moderation.json")
        T.reset_room()
        self.assertFalse(S.SEAT.locked)
        S.load_moderation(self.d / "moderation.json")
        self.assertTrue(S.SEAT.locked)
        S.SEAT.set_lock(False)
        S.save_moderation(self.d / "moderation.json")
        T.reset_room()
        S.load_moderation(self.d / "moderation.json")
        self.assertFalse(S.SEAT.locked)
        (self.d / "moderation.json").write_text('{"locked": "yes"}')
        T.reset_room()
        S.load_moderation(self.d / "moderation.json")
        self.assertFalse(S.SEAT.locked)  # only a real true locks


class TestControlUnderLock(unittest.TestCase):
    def test_then_runs_under_room_lock_only_when_allowed(self):
        T.reset_room()
        calls = []

        class Hd:
            path = "/move"
            client_id = "1" * 32
            client_address = ("127.0.0.1", 1)
            headers = {"CF-Connecting-IP": "198.51.100.9", "CF-IPCountry": "DE"}

        def then():
            calls.append(S.ROOM_LOCK._is_owned())
        self.assertTrue(S.can_control(Hd(), then=then))
        self.assertEqual(calls, [True])
        with S.ROOM_LOCK:
            S.SEAT.set_lock(True)
        h = Hd()
        self.assertFalse(S.can_control(h, then=then))
        self.assertEqual(calls, [True])  # a refused /move never reaches the camera
        self.assertEqual(h._deny[0], "locked")
        T.reset_room()


class TestCameraStop(unittest.TestCase):
    def setUp(self):
        T.TestCameraWorker.setUp(self)

    def step(self, ptz):
        return S.camera_step(ptz=ptz, clock=self.clock)

    def test_stop_after_failed_move_goes_out_at_once(self):
        calls = []

        class Lossy:  # the camera starts moving but the reply is lost
            def profile_token(self):
                calls.append("token")
                return "tok"

            def move(self, *a):
                calls.append("move")
                raise TimeoutError("timed out")

            def stop(self, token=None):
                calls.append("stop")
                return "<ok/>"
        S.CAM["sent"] = S.STOP
        S.set_motion(1, 0, nudge=True, now=self.clock.t)
        self.assertIsNone(self.step(Lossy()))
        self.clock.t += 0.5  # nudge over: STOP is due, still inside the 2 s back-off
        self.assertEqual(self.step(Lossy()), S.STOP)
        self.assertEqual(calls, ["token", "move", "stop"])  # no back-off and no token re-fetch

    def test_failed_stop_keeps_back_off(self):
        class Down:
            n = 0

            def profile_token(self):
                return "tok"

            def stop(self, token=None):
                Down.n += 1
                raise ConnectionRefusedError()
        self.step(Down())
        self.clock.t += 0.5
        self.step(Down())
        self.assertEqual(Down.n, 1)


class TestStreamState(unittest.TestCase):
    def tearDown(self):
        S.STREAM_STATE["down_since"] = None
        S.view["proc"] = None

    def test_offline_after_grace_even_while_relaunching(self):
        grace = float(S.STREAM["first_segment_grace_s"])
        S.STREAM_STATE["down_since"] = None
        S.view["proc"] = object()
        with mock.patch.object(S, "segment_age", return_value=None):
            self.assertEqual(S.stream_public(now=1000.0)["state"], "starting")
            S.view["launched"] = S.time.monotonic()  # a relaunch a moment ago
            st = S.stream_public(now=1000.0 + grace + 1)
            self.assertEqual((st["state"], st["down_since"]), ("offline", 1000.0))
        with mock.patch.object(S, "segment_age", return_value=1.0):
            self.assertEqual(S.stream_public(now=2000.0)["state"], "live")
        self.assertIsNone(S.STREAM_STATE["down_since"])

    def test_relaunch_back_off(self):
        self.assertEqual([S.relaunch_delay(n) for n in range(7)], [3, 3, 6, 12, 24, 30, 30])


class TestOwnerKeyNetworks(unittest.TestCase):
    def setUp(self):
        S._owner_fails.clear()

    tearDown = setUp

    def test_ipv6_rotation_does_not_reset_the_lockout(self):
        for i in range(5):
            self.assertFalse(S.check_owner_key(f"2001:db8:1:{i}::{i + 1}", "wrong", "secret", now=i))
        self.assertFalse(S.check_owner_key("2001:db8:1:2::77", "secret", "secret", now=10))
        self.assertTrue(S.check_owner_key("2001:db8:2::1", "secret", "secret", now=10))  # another /48

    def test_stale_records_are_pruned(self):
        for i in range(1000):
            S.check_owner_key(f"10.0.{i // 250}.{i % 250}", "wrong", "secret", now=0)
        self.assertEqual(len(S._owner_fails), 1000)
        S.check_owner_key("10.9.9.9", "wrong", "secret", now=S.OWNER_WINDOW_S + 1)
        self.assertEqual(len(S._owner_fails), 1)


class TestNames(unittest.TestCase):
    def test_confusables_and_invisible_names(self):
        for n in ("Оwner", "Οwner", "Ownerㅤ", "cаmera", "Admın", "ΑDMIN", "ѕуѕtеm"):
            self.assertEqual(S.name_check(n), ("", "reserved"), n)
        for n in ("ㅤㅤㅤ", "⠀⠀", "ᅟᅠx", "a­", "​​ab"[:3]):
            self.assertEqual(S.name_check(n)[1], "invalid", repr(n))
        self.assertEqual(S.name_check("Анна"), ("Анна", None))  # ordinary Cyrillic names still work
        self.assertEqual(S.name_check("🦊🦊"), ("🦊🦊", None))
        self.assertEqual(S.name_check("Curious Otter"), ("Curious Otter", None))


class TestLoggingAndHTTP(unittest.TestCase):
    """Same in-process server and helpers as TestRoomHTTP (without re-running its tests)."""
    setUpClass = classmethod(T.TestHTTP.setUpClass.__func__)
    tearDownClass = classmethod(T.TestHTTP.tearDownClass.__func__)
    req, post = T.TestHTTP.req, T.TestHTTP.post
    setUp, p = T.TestRoomHTTP.setUp, T.TestRoomHTTP.p
    LOCAL, A, B, O, RA, RB = (T.TestRoomHTTP.LOCAL, T.TestRoomHTTP.A, T.TestRoomHTTP.B, T.TestRoomHTTP.O,
                              T.TestRoomHTTP.RA, T.TestRoomHTTP.RB)

    def test_client_errors_are_counted_not_logged(self):
        S.flush_client_errors()
        with self.assertLogs("camlive", level="INFO") as cm:
            S.log.info("marker")
            for _ in range(5):
                self.req("GET", "/nope-" + "x" * 5)
            self.post("/move", {"pan": 1, "tilt": 0}, cid=None)
            items = dict(S.flush_client_errors())
        self.assertEqual(items[("GET", "*", 404)], 5)
        self.assertEqual(items[("POST", "/move", 409)], 1)
        self.assertEqual(sum(1 for line in cm.output if "->" in line), 0)

    def test_server_backlog(self):
        self.assertGreaterEqual(S.Server.request_queue_size, 128)
        self.assertTrue(S.Server.daemon_threads)

    def test_healthz_details_only_for_local_host(self):
        st, _, b = self.req("GET", "/healthz", headers={"Host": "rebind.attacker.example:8799"})
        self.assertEqual(set(json.loads(b)), {"ok", "segment_age_s", "viewers"})
        st, _, b = self.req("GET", "/healthz", headers=self.LOCAL)
        self.assertIn("ffmpeg", json.loads(b))

    def test_country_local_only_for_local_host(self):
        _, s = self.p("/state", {"name": "Rebound"}, self.A, {"Host": "rebind.attacker.example:8799"})
        self.assertEqual(S.PRESENCE[S.client_key(self.A)]["country"], "")

    def test_hidden_state_poll_and_leave_over_http(self):
        self.p("/state", {"name": "Na"}, self.A, self.RA)
        self.p("/state", {"name": "Nb"}, self.B, self.RB)
        self.p("/move", {"pan": 0.5, "tilt": 0}, self.A, self.RA)
        self.p("/seat/join", {}, self.B, self.RB)
        st, s = self.p("/state", {"hidden": True}, self.B, self.RB)
        self.assertEqual((st, s["seat"]["you"]), (200, "queued"))
        self.assertTrue(S.PRESENCE[S.client_key(self.B)]["hidden"])
        # the driver's duplicate tab closes; the driver keeps the turn
        self.assertEqual(self.p("/presence/leave", {"cid": self.A}), (200, {"ok": True}))
        self.p("/state", {}, self.A, self.RA)
        S.room_tick(S.mono() + S.LEAVE_GRACE_S + 1)
        self.assertEqual(S.SEAT.driver, S.client_key(self.A))

    def test_mute_by_ambiguous_tag(self):
        self.p("/state", {"name": "Na"}, self.A, self.RA)
        self.p("/state", {"name": "Nb"}, self.B, self.RB)
        tag = S.PRESENCE[S.client_key(self.A)]["tag"]
        S.PRESENCE[S.client_key(self.B)]["tag"] = tag  # a cloned tag
        st, out = self.p("/mod/mute", {"tag": tag, "minutes": 10}, self.O, self.LOCAL)
        self.assertEqual((st, out["error"]), (409, "ambiguous"))
        self.assertEqual(S.MUTES, {})

    def test_mute_saved(self):
        with mock.patch.object(S, "save_moderation") as save:
            self.p("/state", {"name": "Na"}, self.A, self.RA)
            tag = S.PRESENCE[S.client_key(self.A)]["tag"]
            self.assertEqual(self.p("/mod/mute", {"tag": tag, "minutes": 10}, self.O, self.LOCAL)[0], 200)
            self.assertEqual(self.p("/mod/slowmode", {"seconds": 5}, self.O, self.LOCAL)[0], 200)
        self.assertEqual(save.call_count, 2)
        S.CHAT["owner_slow"] = 0


if __name__ == "__main__":
    unittest.main()
