import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from ghostpad import protocol as p
from ghostpad.backends.mock import MockBackend
from ghostpad.server import GhostpadServer
from tests.wsclient import WSClient, build_frame, http_get

S1 = p.PadState(0x1000, 10, 20, 100, -100, 5, -5)
S2 = p.PadState(0x2000, 0, 255, 32767, -32767, 0, 0)


class ServerTestBase(unittest.IsolatedAsyncioTestCase):
    token: str | None = None

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "web"
        root.mkdir()
        (root / "index.html").write_text("<h1>hi</h1>")
        (root / "app.js").write_text("export const x = 1;")
        (root / "style.css").write_text("a{}")
        (root / "sub").mkdir()
        (root / "sub" / "index.html").write_text("sub")
        (Path(self.tmp.name) / "secret.txt").write_text("SECRET")
        self.backend = MockBackend()
        self.server = GhostpadServer(self.backend, host="127.0.0.1", port=0, token=self.token,
                                     web_root=root)
        await self.server.start()
        self.port = self.server.port
        self.clients: list[WSClient] = []

    async def asyncTearDown(self):
        for c in self.clients:
            await c.aclose()
        await self.server.stop()
        self.tmp.cleanup()

    async def connect(self, **kw) -> WSClient:
        c = await WSClient.connect(self.port, **kw)
        self.clients.append(c)
        return c

    async def welcomed(self, **kw) -> WSClient:
        c = await self.connect(**kw)
        op, data = await c.recv()
        self.assertEqual(json.loads(data)["type"], "welcome")
        return c

    async def settle(self, t: float = 0.05):
        await asyncio.sleep(t)


class WebSocketTests(ServerTestBase):
    async def test_hello_welcome(self):
        c = await self.connect()
        op, data = await c.recv()
        self.assertEqual(op, 1)
        self.assertEqual(json.loads(data),
                         {"type": "welcome", "version": 1, "backend": "mock", "max_pads": 4})

    async def test_version_mismatch_closes_4000(self):
        c = await self.connect(version=2)
        self.assertIsNone(await c.recv())
        self.assertEqual(c.close_code, 4000)

    async def test_state_applied_lazy_plug(self):
        c = await self.welcomed()
        self.assertEqual(self.backend.ops(), [])
        await c.send_binary(p.encode_state(1, 1, S1))
        await c.send_binary(p.encode_state(1, 2, S2))
        await self.settle()
        self.assertEqual(self.backend.ops(), [("connect", 1), ("update", 1), ("update", 1)])
        self.assertEqual(self.backend.applied(1), [S1, S2])
        self.assertEqual(self.server.stats.count, 2)

    async def test_binary_before_hello_dropped(self):
        c = await self.connect(hello=False)
        await c.send_binary(p.encode_state(0, 1, S1))
        await self.settle()
        self.assertEqual(self.backend.events, [])
        await c.send_text(p.encode_hello("late"))
        await c.recv()
        await c.send_binary(p.encode_state(0, 2, S1))
        await self.settle()
        self.assertEqual(self.backend.applied(0), [S1])

    async def test_stale_and_duplicate_seq_dropped(self):
        c = await self.welcomed()
        for seq in (10, 10, 9, 11, 5):
            await c.send_binary(p.encode_state(0, seq, p.PadState(seq, 0, 0, 0, 0, 0, 0)))
        await self.settle()
        self.assertEqual([s.buttons for s in self.backend.applied(0)], [10, 11])

    async def test_seq_wrap_accepted(self):
        c = await self.welcomed()
        for seq in (65534, 65535, 0, 1):
            await c.send_binary(p.encode_state(0, seq, p.PadState(seq, 0, 0, 0, 0, 0, 0)))
        await self.settle()
        self.assertEqual([s.buttons for s in self.backend.applied(0)], [65534, 65535, 0, 1])

    async def test_seq_is_per_pad(self):
        c = await self.welcomed()
        await c.send_binary(p.encode_state(0, 100, S1))
        await c.send_binary(p.encode_state(1, 5, S2))
        await self.settle()
        self.assertEqual(self.backend.applied(1), [S2])

    async def test_malformed_and_bad_pad_dropped_not_fatal(self):
        c = await self.welcomed()
        await c.send_binary(b"\x01\x00")
        await c.send_binary(p.encode_state(4, 1, S1))
        await c.send_binary(b"\x99" * 20)
        await c.send_binary(p.encode_state(0, 1, S1))
        await self.settle()
        self.assertEqual(self.backend.applied(), [S1])

    async def test_ping_pong(self):
        c = await self.welcomed()
        await c.send_binary(p.encode_ping(7, 12345.678))
        op, data = await c.recv()
        self.assertEqual(op, 2)
        pid, tc, proc = p.decode_pong(data)
        self.assertEqual((pid, tc), (7, 12345.678))
        self.assertLess(proc, 50_000)

    async def test_ws_ping_frame_answered(self):
        c = await self.welcomed()
        await c.send_raw(build_frame(0x9, b"xyz"))
        _, op, data = await c.read_frame()
        self.assertEqual((op, data), (0xA, b"xyz"))

    async def test_fragmented_binary_state(self):
        c = await self.welcomed()
        raw = p.encode_state(0, 1, S1)
        await c.send_raw(build_frame(0x2, raw[:7], fin=False) + build_frame(0x0, raw[7:]))
        await self.settle()
        self.assertEqual(self.backend.applied(0), [S1])

    async def test_pad_gone_unplugs(self):
        c = await self.welcomed()
        await c.send_binary(p.encode_state(2, 1, S1))
        await c.send_binary(p.encode_pad_gone(2))
        await self.settle()
        self.assertEqual(self.backend.ops(),
                         [("connect", 2), ("update", 2), ("neutral", 2), ("disconnect", 2)])
        # seq state reset: a replugged pad accepts seq 1 again
        await c.send_binary(p.encode_state(2, 1, S2))
        await self.settle()
        self.assertEqual(self.backend.applied(2), [S1, S2])

    async def test_close_neutral_then_unplug(self):
        c = await self.welcomed()
        await c.send_binary(p.encode_state(0, 1, S1))
        await c.send_binary(p.encode_state(3, 1, S2))
        await self.settle()
        await c.send_close()
        await self.settle()
        ops = self.backend.ops()
        self.assertEqual(ops[-4:], [("neutral", 0), ("disconnect", 0), ("neutral", 3), ("disconnect", 3)])
        self.assertEqual(self.backend.plugged, set())

    async def test_abrupt_disconnect_unplugs(self):
        c = await self.welcomed()
        await c.send_binary(p.encode_state(0, 1, S1))
        await self.settle()
        c.w.close()
        await self.settle(0.1)
        self.assertEqual(self.backend.plugged, set())
        self.assertEqual(self.backend.ops()[-2:], [("neutral", 0), ("disconnect", 0)])

    async def test_watchdog_neutralises_after_silence(self):
        c = await self.welcomed()
        await c.send_binary(p.encode_state(0, 1, S1))
        await self.settle(0.1)
        self.assertEqual(self.backend.states[0], S1)
        await asyncio.sleep(0.35)
        self.assertEqual(self.backend.states[0], p.NEUTRAL)
        self.assertEqual(self.backend.plugged, {0})  # still plugged in
        neutrals = [e for e in self.backend.events if e[0] == "neutral"]
        self.assertEqual(len(neutrals), 1)  # not repeated every tick
        # next STATE resumes
        await c.send_binary(p.encode_state(0, 2, S2))
        await self.settle()
        self.assertEqual(self.backend.states[0], S2)

    async def test_heartbeats_keep_watchdog_quiet(self):
        c = await self.welcomed()
        for seq in range(1, 13):
            await c.send_binary(p.encode_state(0, seq, S1))
            await asyncio.sleep(0.05)
        self.assertNotIn("neutral", [e[0] for e in self.backend.events])

    async def test_watchdog_release_within_budget(self):
        c = await self.welcomed()
        await c.send_binary(p.encode_state(0, 1, S1))
        loop = asyncio.get_running_loop()
        while self.backend.states.get(0) != S1:
            await asyncio.sleep(0.001)
        t0 = loop.time()
        while self.backend.states[0] != p.NEUTRAL:
            await asyncio.sleep(0.005)
            self.assertLess(loop.time() - t0, 0.26)
        self.assertGreaterEqual(loop.time() - t0, 0.15)

    async def test_takeover_closes_old_with_4001(self):
        a = await self.welcomed(name="a")
        await a.send_binary(p.encode_state(0, 1, S1))
        await self.settle()
        b = await self.welcomed(name="b")
        self.assertIsNone(await a.recv())
        self.assertEqual(a.close_code, 4001)
        self.assertEqual(self.backend.plugged, set())  # a's pad unplugged
        await b.send_binary(p.encode_state(0, 1, S2))
        await self.settle()
        self.assertEqual(self.backend.plugged, {0})
        self.assertEqual(self.backend.states[0], S2)
        self.assertFalse(b.closed)

    async def test_old_client_frames_ignored_after_takeover(self):
        a = await self.welcomed(name="a")
        b = await self.welcomed(name="b")
        await a.send_binary(p.encode_state(0, 1, S1))
        await self.settle()
        self.assertEqual(self.backend.plugged, set())

    async def test_failed_hello_does_not_kick_owner(self):
        a = await self.welcomed(name="a")
        bad = await self.connect(version=9)
        self.assertIsNone(await bad.recv())
        await a.send_binary(p.encode_state(0, 1, S1))
        await self.settle()
        self.assertEqual(self.backend.applied(0), [S1])

    async def test_shutdown_unplugs_everything(self):
        c = await self.welcomed()
        await c.send_binary(p.encode_state(0, 1, S1))
        await c.send_binary(p.encode_state(1, 1, S1))
        await self.settle()
        await self.server.stop()
        self.assertEqual(self.backend.plugged, set())
        self.assertTrue(self.backend.closed)
        self.assertEqual(self.backend.states, {})

    async def test_ws_path_without_upgrade_426(self):
        status, _, _ = await http_get(self.port, "/ws")
        self.assertEqual(status, 426)


class TokenTests(ServerTestBase):
    token = "s3cret"

    async def test_missing_token_4002(self):
        c = await self.connect()
        self.assertIsNone(await c.recv())
        self.assertEqual(c.close_code, 4002)

    async def test_wrong_token_4002(self):
        c = await self.connect(path="/ws?token=nope")
        self.assertIsNone(await c.recv())
        self.assertEqual(c.close_code, 4002)
        self.assertEqual(self.backend.events, [])

    async def test_right_token_ok(self):
        c = await self.welcomed(path="/ws?token=s3cret")
        await c.send_binary(p.encode_state(0, 1, S1))
        await self.settle()
        self.assertEqual(self.backend.applied(0), [S1])

    async def test_bad_token_does_not_take_over(self):
        a = await self.welcomed(path="/ws?token=s3cret")
        bad = await self.connect(path="/ws?token=x")
        self.assertIsNone(await bad.recv())
        self.assertFalse(a.closed)

    async def test_static_not_token_protected(self):
        status, _, _ = await http_get(self.port, "/")
        self.assertEqual(status, 200)


class StaticTests(ServerTestBase):
    async def test_index_and_types(self):
        status, h, body = await http_get(self.port, "/")
        self.assertEqual((status, body), (200, b"<h1>hi</h1>"))
        self.assertEqual(h["content-type"], "text/html; charset=utf-8")
        self.assertEqual(h["content-length"], str(len(body)))
        _, h, _ = await http_get(self.port, "/app.js")
        self.assertEqual(h["content-type"], "text/javascript; charset=utf-8")
        _, h, _ = await http_get(self.port, "/style.css")
        self.assertEqual(h["content-type"], "text/css; charset=utf-8")
        status, _, body = await http_get(self.port, "/sub/")
        self.assertEqual(body, b"sub")
        status, _, body = await http_get(self.port, "/app.js?v=3")
        self.assertEqual(status, 200)

    async def test_head(self):
        status, h, body = await http_get(self.port, "/app.js", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(h["content-length"], "19")

    async def test_missing_404(self):
        status, _, _ = await http_get(self.port, "/nope.txt")
        self.assertEqual(status, 404)

    async def test_post_405(self):
        status, _, _ = await http_get(self.port, "/", method="POST")
        self.assertEqual(status, 405)

    async def test_path_traversal_blocked(self):
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/%2E%2E%2Fsecret.txt",
                     "/sub/../../secret.txt", "/..%5csecret.txt", "//etc/passwd",
                     "/%00", "/C:/Windows/win.ini"):
            status, _, body = await http_get(self.port, path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"SECRET", body, path)

    async def test_symlink_escape_blocked(self):
        link = Path(self.tmp.name) / "web" / "link.txt"
        try:
            link.symlink_to(Path(self.tmp.name) / "secret.txt")
        except OSError:
            self.skipTest("symlinks unavailable")
        status, _, body = await http_get(self.port, "/link.txt")
        self.assertEqual(status, 404)

    async def test_garbage_request_400(self):
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        w.write(b"GARBAGE\r\n\r\n")
        await w.drain()
        raw = await r.read()
        self.assertIn(b" 400 ", raw.split(b"\r\n")[0])
        w.close()


if __name__ == "__main__":
    unittest.main()
