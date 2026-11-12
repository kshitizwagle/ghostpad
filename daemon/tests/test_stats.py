import asyncio
import json
import unittest

from ghostpad import protocol as p
from ghostpad.backends.mock import MockBackend
from ghostpad.server import GhostpadServer
from ghostpad.stats import Stats
from tests.test_server import S1, ServerTestBase
from tests.wsclient import http_get


class StatsUnitTests(unittest.TestCase):
    def test_empty_snapshot(self):
        s = Stats().snapshot()
        self.assertEqual(s["recv_apply"]["count"], 0)
        self.assertIsNone(s["recv_apply"]["median_us"])
        self.assertEqual(s["frames_received"], 0)
        self.assertIn("no samples", Stats().summary())

    def test_percentiles_in_microseconds(self):
        st = Stats()
        for i in range(1, 101):  # 1..100 us
            st.record(i * 1000)
        r = st.snapshot()["recv_apply"]
        self.assertEqual(r["count"], 100)
        self.assertEqual(r["min_us"], 1.0)
        self.assertEqual(r["max_us"], 100.0)
        self.assertEqual(r["median_us"], 51.0)
        self.assertEqual(r["p95_us"], 96.0)
        self.assertEqual(r["p99_us"], 100.0)

    def test_window_bounded_but_count_total(self):
        st = Stats(capacity=10)
        for i in range(25):
            st.record(1000)
        r = st.snapshot()["recv_apply"]
        self.assertEqual((r["count"], r["window"]), (25, 10))

    def test_snapshot_json_roundtrip_and_copy(self):
        st = Stats()
        st.record(5000)
        samples = st.copy_samples()
        st.record(9000)  # not visible in the copy
        d = json.loads(st.snapshot_json(samples))
        self.assertEqual(d["recv_apply"]["window"], 1)
        self.assertGreaterEqual(d["uptime_s"], 0)


class StatsServerTests(ServerTestBase):
    async def get_stats(self) -> dict:
        status, headers, body = await http_get(self.port, "/stats")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("application/json"))
        return json.loads(body)

    async def test_counters_via_http(self):
        c = await self.welcomed()
        await c.send_binary(p.encode_state(0, 1, S1))
        await c.send_binary(p.encode_state(0, 2, S1))
        await c.send_binary(p.encode_state(0, 2, S1))  # stale
        await c.send_binary(b"\x01\x00")  # malformed
        await c.send_binary(p.encode_state(4, 1, S1))  # bad pad
        await c.send_binary(p.encode_ping(1, 1.0))
        await c.recv()
        await c.send_binary(p.encode_ping(2, 2.0))
        await c.recv()
        await self.settle()
        d = await self.get_stats()
        self.assertEqual(d["recv_apply"]["count"], 2)
        self.assertEqual(d["frames_received"], 7)
        self.assertEqual(d["stale_drops"], 1)
        self.assertEqual(d["malformed_drops"], 2)
        self.assertEqual(d["ping_count"], 2)
        self.assertEqual(d["watchdog_trips"], 0)
        self.assertGreater(d["uptime_s"], 0)
        self.assertIsNotNone(d["recv_apply"]["p99_us"])

    async def test_prehello_counted(self):
        c = await self.connect(hello=False)
        await c.send_binary(p.encode_state(0, 1, S1))
        await self.settle()
        d = await self.get_stats()
        self.assertEqual(d["prehello_drops"], 1)
        self.assertEqual(d["frames_received"], 0)

    async def test_head_and_post(self):
        status, _, body = await http_get(self.port, "/stats", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        status, _, _ = await http_get(self.port, "/stats", method="POST")
        self.assertEqual(status, 405)

    async def test_stats_not_shadowed_by_static(self):
        (self.server.web_root / "stats").write_text("static")
        d = await self.get_stats()
        self.assertIn("recv_apply", d)


class WatchdogStatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_watchdog_trip_counted(self):
        from tests.wsclient import WSClient
        srv = GhostpadServer(MockBackend(), host="127.0.0.1", port=0, watchdog_ms=60)
        await srv.start()
        c = await WSClient.connect(srv.port)
        await c.recv()
        await c.send_binary(p.encode_state(0, 1, S1))
        await asyncio.sleep(0.3)
        self.assertEqual(srv.stats.watchdog_trips, 1)
        await c.aclose()
        await srv.stop()

    async def test_exit_logs_json_snapshot(self):
        srv = GhostpadServer(MockBackend(), host="127.0.0.1", port=0)
        await srv.start()
        with self.assertLogs("ghostpad.server", "INFO") as cm:
            await srv.stop()
        line = next(m for m in cm.output if "exit stats:" in m)
        d = json.loads(line.split("exit stats: ", 1)[1])
        self.assertIn("recv_apply", d)
