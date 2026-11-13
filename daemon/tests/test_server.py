import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path

from ghostpad import protocol as p
from ghostpad import server as server_mod
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

    async def test_ping_does_not_keep_watchdog_alive(self):
        c = await self.welcomed()
        await c.send_binary(p.encode_state(0, 1, S1))
        await self.settle(0.05)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        i = 0
        while self.backend.states[0] != p.NEUTRAL:
            i += 1
            await c.send_binary(p.encode_ping(i, 1.0))
            await c.send_raw(build_frame(0x9, b"x"))  # WS-level ping too
            await asyncio.sleep(0.02)
            self.assertLess(loop.time() - t0, 0.4)
        self.assertGreaterEqual(loop.time() - t0, 0.12)
        # PINGs are still answered after the trip
        await c.send_binary(p.encode_ping(999, 2.0))
        while True:
            op, data = await c.recv()
            if op == 2 and p.decode_pong(data)[0] == 999:
                break

    async def test_pad_gone_counts_as_input(self):
        c = await self.welcomed()
        await c.send_binary(p.encode_state(0, 1, S1))
        await c.send_binary(p.encode_state(1, 1, S1))
        for _ in range(8):  # 160 ms of re-sent PAD_GONE for a pad that is not plugged
            await asyncio.sleep(0.02)
            await c.send_binary(p.encode_pad_gone(3))
        await self.settle(0.05)
        self.assertEqual(self.backend.states[0], S1)  # PAD_GONE refreshed the timer

    async def test_no_pads_no_watchdog_trips(self):
        c = await self.welcomed()
        await c.send_binary(p.encode_ping(1, 1.0))
        await asyncio.sleep(0.5)
        self.assertEqual(self.server.stats.watchdog_trips, 0)
        self.assertEqual(self.backend.events, [])
        # also after the only pad is unplugged
        await c.send_binary(p.encode_state(0, 1, S1))
        await c.send_binary(p.encode_pad_gone(0))
        await asyncio.sleep(0.5)
        self.assertEqual(self.server.stats.watchdog_trips, 0)

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


class HelloTimeoutTests(ServerTestBase):
    hello_timeout = 0.15

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.server.stop()
        self.backend.events.clear()
        self.backend.closed = False
        self.server = GhostpadServer(self.backend, host="127.0.0.1", port=0,
                                     hello_timeout=self.hello_timeout)
        await self.server.start()
        self.port = self.server.port

    async def test_silent_client_closed_4003(self):
        c = await self.connect(hello=False)
        self.assertIsNone(await c.recv())
        self.assertEqual(c.close_code, 4003)

    async def test_binary_spam_does_not_extend_deadline(self):
        c = await self.connect(hello=False)
        for _ in range(5):
            await c.send_binary(p.encode_state(0, 1, S1))
            await asyncio.sleep(0.05)
        self.assertIsNone(await c.recv())
        self.assertEqual(c.close_code, 4003)
        self.assertEqual(self.backend.events, [])

    async def test_valid_hello_not_closed(self):
        c = await self.welcomed()
        await asyncio.sleep(0.4)
        self.assertFalse(c.closed)
        await c.send_binary(p.encode_state(0, 1, S1))
        await self.settle()
        self.assertEqual(self.backend.applied(0), [S1])

    async def test_silent_client_does_not_kick_owner(self):
        a = await self.welcomed(name="a")
        s = await self.connect(hello=False)
        self.assertIsNone(await s.recv())
        self.assertFalse(a.closed)


class TraceTests(ServerTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.server.stop()
        self.backend.events.clear()
        self.backend.closed = False
        self.server = GhostpadServer(self.backend, host="127.0.0.1", port=0, trace=True)
        await self.server.start()
        self.port = self.server.port

    async def test_trace_logs_pad_events(self):
        with self.assertLogs("ghostpad.server", level="DEBUG") as cm:
            c = await self.welcomed()
            await c.send_binary(p.encode_state(0, 1, S1))
            await c.send_binary(p.encode_state(1, 1, S1))
            await c.send_binary(p.encode_pad_gone(1))
            await self.settle(0.05)
            await c.send_close()
            await self.settle(0.05)
        text = "\n".join(r.getMessage() for r in cm.records if r.levelname == "DEBUG")
        for needle in ("pad 0 connect", "pad 0 update", "PAD_GONE", "pad 0 neutral, unplug"):
            self.assertIn(needle, text)

    async def test_trace_logs_watchdog_neutral(self):
        with self.assertLogs("ghostpad.server", level="DEBUG") as cm:
            c = await self.welcomed()
            await c.send_binary(p.encode_state(0, 1, S1))
            await asyncio.sleep(0.4)
        self.assertTrue(any("neutral (watchdog)" in r.getMessage() for r in cm.records))


class TraceOffTests(ServerTestBase):
    async def test_no_trace_records_by_default(self):
        with self.assertLogs("ghostpad.server", level="DEBUG") as cm:
            log_marker = __import__("logging").getLogger("ghostpad.server")
            c = await self.welcomed()
            await c.send_binary(p.encode_state(0, 1, S1))
            await self.settle()
            log_marker.debug("marker")
        self.assertFalse(any("trace" in r.getMessage() for r in cm.records))


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

    async def test_stats_requires_token(self):
        for path in ("/stats", "/stats?token=nope", "/stats?token="):
            status, _, body = await http_get(self.port, path)
            self.assertEqual(status, 403, path)
            self.assertNotIn(b"recv_apply", body)
        status, _, _ = await http_get(self.port, "/stats", method="HEAD")
        self.assertEqual(status, 403)

    async def test_stats_with_token_ok(self):
        status, h, body = await http_get(self.port, "/stats?token=s3cret")
        self.assertEqual(status, 200)
        self.assertIn("recv_apply", json.loads(body))
        status, _, _ = await http_get(self.port, "/stats?token=s3cret", method="HEAD")
        self.assertEqual(status, 200)

    async def test_static_not_token_protected(self):
        status, _, _ = await http_get(self.port, "/")
        self.assertEqual(status, 200)


class StatsOpenTests(ServerTestBase):
    async def test_stats_open_without_token_configured(self):
        status, _, _ = await http_get(self.port, "/stats")
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


class DisconnectLogTests(ServerTestBase):
    async def test_owner_disconnect_logged_once(self):
        with self.assertLogs("ghostpad.server", level="INFO") as cm:
            a = await self.welcomed(name="a")
            b = await self.welcomed(name="b")  # takes over: 'a' is released, not "disconnected"
            await a.send_close()
            await b.send_close()
            await self.settle(0.1)
        msgs = [r.getMessage() for r in cm.records]
        self.assertIn("client 'b' disconnected", msgs)
        self.assertNotIn("client 'a' disconnected", msgs)


class ExceptionHandlerTests(unittest.TestCase):
    def test_connection_resets_are_quiet_others_pass_through(self):
        from unittest import mock
        from ghostpad.server import _quiet_connection_resets
        loop = mock.Mock()
        for exc in (ConnectionResetError(10054, "reset"), ConnectionAbortedError()):
            _quiet_connection_resets(loop, {"message": "teardown", "exception": exc})
        loop.default_exception_handler.assert_not_called()
        ctx = {"message": "boom", "exception": ValueError("x")}
        _quiet_connection_resets(loop, ctx)
        loop.default_exception_handler.assert_called_once_with(ctx)

    def test_installed_on_start_unless_one_is_set(self):
        async def check():
            from ghostpad.server import _quiet_connection_resets
            loop = asyncio.get_running_loop()
            s = GhostpadServer(MockBackend(), host="127.0.0.1", port=0)
            await s.start()
            self.assertIs(loop.get_exception_handler(), _quiet_connection_resets)
            await s.stop()
            mine = lambda lp, ctx: None
            loop.set_exception_handler(mine)
            s = GhostpadServer(MockBackend(), host="127.0.0.1", port=0)
            await s.start()
            self.assertIs(loop.get_exception_handler(), mine)
            await s.stop()
        asyncio.run(check())


class ClientLogTests(ServerTestBase):
    """{"type":"log"} text after hello -> INFO on the ghostpad.client logger."""

    async def sync(self, c: WSClient) -> None:
        """Frames are handled in order: once the PONG is back, earlier frames are done."""
        await c.send_binary(p.encode_ping(1, 1.0))
        op, data = await c.recv()
        self.assertEqual(data[0], p.MSG_PONG)

    async def test_logged_with_client_name(self):
        c = await self.welcomed(name="tv")
        with self.assertLogs("ghostpad.client", level="INFO") as cm:
            await c.send_text(p.encode_client_log("gamepad connected: 0"))
            await c.send_text(p.encode_client_log("line\nforged"))
            await c.send_text('{"type":"other"}')  # ignored
            await c.send_text("not json")  # ignored
            await self.sync(c)
        self.assertEqual([r.getMessage() for r in cm.records],
                         ["tv: gamepad connected: 0", "tv: line\\nforged"])

    async def test_rate_limited_then_reports_drops(self):
        from unittest import mock
        c = await self.welcomed(name="tv")
        with self.assertLogs("ghostpad.client", level="INFO") as cm:
            with mock.patch("ghostpad.server.CLIENT_LOG_RATE", 0.0):
                for i in range(server_mod.CLIENT_LOG_BURST + 5):
                    await c.send_text(p.encode_client_log(f"n{i}"))
                await self.sync(c)
            with mock.patch("ghostpad.server.CLIENT_LOG_RATE", 1e9):
                await c.send_text(p.encode_client_log("after"))
                await self.sync(c)
        msgs = [r.getMessage() for r in cm.records]
        self.assertEqual(len(msgs), server_mod.CLIENT_LOG_BURST + 2)
        self.assertEqual(msgs[-2], "tv: 5 log lines dropped (rate limit)")
        self.assertEqual(cm.records[-2].levelname, "WARNING")
        self.assertEqual(msgs[-1], "tv: after")

    async def test_before_hello_is_not_logged(self):
        c = await self.connect(hello=False)
        with self.assertLogs("ghostpad", level="INFO") as cm:
            await c.send_text(p.encode_client_log("early"))
            await self.settle()
            __import__("logging").getLogger("ghostpad").info("marker")
        self.assertFalse(any("early" in r.getMessage() for r in cm.records))


class RumbleTests(ServerTestBase):
    """Backend force feedback -> RUMBLE (0x05) to the owning client."""

    async def plugged_client(self, pads=(0,), **kw) -> WSClient:
        c = await self.welcomed(**kw)
        for pad in pads:
            await c.send_binary(p.encode_state(pad, 1, S1))
        while not set(pads) <= self.backend.plugged:
            await asyncio.sleep(0.005)
        return c

    def emit_from_thread(self, pad: int, large: int, small: int) -> None:
        t = threading.Thread(target=self.backend.emit_rumble, args=(pad, large, small))
        t.start()
        t.join()

    async def next_rumble(self, c: WSClient, timeout: float = 1.0):
        """Next RUMBLE, skipping PONGs; None on close or timeout."""
        while True:
            try:
                msg = await c.recv(timeout)
            except asyncio.TimeoutError:
                return None
            if msg is None:
                return None
            op, data = msg
            if op == 2 and data[0] == p.MSG_RUMBLE:
                return p.decode_rumble(data)

    async def assert_no_rumble(self, c: WSClient) -> None:
        """A PING round trip proves nothing was queued before the PONG."""
        await c.send_binary(p.encode_ping(77, 1.0))
        op, data = await c.recv()
        self.assertEqual(data[0], p.MSG_PONG, f"unexpected frame {data!r}")

    async def test_forwarded_from_another_thread_on_change_only(self):
        c = await self.plugged_client()
        self.emit_from_thread(0, 200, 50)
        self.assertEqual(await self.next_rumble(c), (0, 200, 50))
        self.emit_from_thread(0, 200, 50)  # unchanged: not re-sent
        await self.assert_no_rumble(c)
        self.emit_from_thread(0, 0, 0)
        self.assertEqual(await self.next_rumble(c), (0, 0, 0))
        self.assertEqual(self.server.stats.rumble_sent, 2)

    async def test_unplugged_pad_and_no_owner_ignored(self):
        self.backend.emit_rumble(0, 9, 9)  # no client at all
        c = await self.plugged_client()
        self.backend.emit_rumble(1, 9, 9)  # pad 1 not plugged by the owner
        self.backend.emit_rumble(7, 9, 9)  # out of range
        await self.assert_no_rumble(c)
        self.assertEqual(self.server.stats.rumble_sent, 0)

    async def test_pad_gone_sends_off(self):
        c = await self.plugged_client(pads=(0, 1))
        self.backend.emit_rumble(1, 255, 255)
        self.assertEqual(await self.next_rumble(c), (1, 255, 255))
        await c.send_binary(p.encode_pad_gone(1))
        self.assertEqual(await self.next_rumble(c), (1, 0, 0))
        await c.send_binary(p.encode_pad_gone(0))  # pad 0 never rumbled: nothing sent
        await self.assert_no_rumble(c)

    async def test_watchdog_sends_off_and_mutes_until_input(self):
        c = await self.plugged_client()
        self.backend.emit_rumble(0, 100, 0)
        self.assertEqual(await self.next_rumble(c), (0, 100, 0))
        self.assertEqual(await self.next_rumble(c), (0, 0, 0))  # watchdog after ~200 ms
        self.assertEqual(self.server.stats.watchdog_trips, 1)
        self.backend.emit_rumble(0, 120, 0)  # client counts as stalled: not sent
        await self.assert_no_rumble(c)
        await c.send_binary(p.encode_state(0, 2, S1))
        await self.settle()
        self.backend.emit_rumble(0, 130, 0)
        self.assertEqual(await self.next_rumble(c), (0, 130, 0))

    async def test_takeover_sends_off_before_4001(self):
        a = await self.plugged_client(name="a")
        self.backend.emit_rumble(0, 50, 60)
        self.assertEqual(await self.next_rumble(a), (0, 50, 60))
        b = await self.welcomed(name="b")
        self.assertEqual(await self.next_rumble(a), (0, 0, 0))
        self.assertIsNone(await a.recv())
        self.assertEqual(a.close_code, 4001)
        self.backend.emit_rumble(0, 70, 70)  # b has not plugged pad 0
        await self.assert_no_rumble(b)

    async def test_stop_detaches_handler(self):
        await self.plugged_client()
        await self.server.stop()
        self.backend.emit_rumble(0, 1, 1)  # must not raise or reach a closed loop
        self.assertIsNone(self.backend._rumble_handler)


if __name__ == "__main__":
    unittest.main()
