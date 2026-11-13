"""Tray app: pure helpers and the daemon thread on any OS; the real tray process on Windows."""

import argparse
import asyncio
import os
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path

from ghostpad import protocol as p
from ghostpad import tray, trayicon
from ghostpad.__main__ import parse_args
from ghostpad.backends.mock import MockBackend
from ghostpad.server import GhostpadServer
from tests.wsclient import WSClient

DAEMON = Path(__file__).resolve().parents[1]
S1 = p.PadState(0x1000, 0, 0, 0, 0, 0, 0)


class HelperTests(unittest.TestCase):
    def test_icon_state(self):
        self.assertEqual(tray.icon_state(None, False), "error")
        self.assertEqual(tray.icon_state({"backend": "vigem", "client": None}, False), "error")
        self.assertEqual(tray.icon_state({"backend": "vigem", "client": None, "listening": False}, True), "error")
        self.assertEqual(tray.icon_state({"backend": "vigem", "client": None}, True), "idle")
        self.assertEqual(tray.icon_state({"backend": "vigem", "client": "tv"}, True), "connected")
        self.assertEqual(tray.icon_state({"backend": "mock", "client": "tv"}, True), "mock")

    def test_status_lines_and_tooltip(self):
        st = {"backend": "vigem", "port": 47800, "client": None, "pads": []}
        self.assertEqual(tray.status_lines(st, True), ["ghostpad on port 47800 (vigem)", "waiting for a client"])
        st.update(client="tizen", pads=[0])
        self.assertEqual(tray.status_lines(st, True)[1], "tizen: player 1")
        st.update(pads=[0, 2])
        self.assertEqual(tray.status_lines(st, True)[1], "tizen: players 1, 3")
        st.update(pads=[])
        self.assertEqual(tray.status_lines(st, True)[1], "tizen: no pads yet")
        st.update(backend="mock")
        self.assertIn("no virtual pad", tray.status_lines(st, True)[0])
        self.assertEqual(tray.status_lines(None, False)[0], "ghostpad stopped")
        st.update(client="x" * 300)
        tip = tray.tooltip(st, True)
        self.assertEqual(len(tip), tray.TIP_MAX)
        self.assertTrue(tip.endswith("\u2026"))

    def test_autostart_command_quotes(self):
        args = parse_args(["--port", "47801", "--token", "a b\"c"])
        cmd = tray.autostart_command(r"C:\Program Files\ghostpad\ghostpad.exe", args)
        self.assertTrue(cmd.startswith(r'"C:\Program Files\ghostpad\ghostpad.exe" --tray --port 47801'))
        self.assertIn('--bind 0.0.0.0 --backend auto --token "a b\\"c"', cmd)
        self.assertNotIn("--log-file", cmd)

    def test_web_menu_label_names_the_owner(self):
        self.assertEqual(tray.web_menu_label(None), "Open web test client")
        self.assertEqual(tray.web_menu_label({"client": None}), "Open web test client")
        self.assertEqual(tray.web_menu_label({"client": "tizen-spike"}),
                         "Open web test client (tizen-spike has the pads)")

    def test_web_client_url(self):
        self.assertEqual(tray.web_client_url(47800, None), "http://localhost:47800/")
        self.assertEqual(tray.web_client_url(1, "a&b c"), "http://localhost:1/?token=a%26b%20c")

    def test_tray_flags(self):
        a = parse_args([])
        self.assertFalse(a.tray)
        self.assertIsNone(a.log_file)
        self.assertTrue(parse_args(["--tray", "--log-file", "x.log"]).tray)


class IconTests(unittest.TestCase):
    def test_render_alpha_and_holes(self):
        px = trayicon.render_rgba(32, trayicon.COLORS["connected"])
        self.assertEqual(len(px), 32 * 32 * 4)
        alpha = lambda x, y: px[(y * 32 + x) * 4 + 3]
        self.assertEqual(alpha(0, 0), 0)          # corner: transparent
        self.assertEqual(alpha(16, 12), 255)      # body: opaque
        self.assertEqual(alpha(9, 15), 0)         # d-pad: cut out
        self.assertEqual(px[(12 * 32 + 16) * 4:(12 * 32 + 16) * 4 + 3], bytes(trayicon.COLORS["connected"]))
        self.assertEqual(trayicon.rgba_to_bgra(b"\x01\x02\x03\x04"), b"\x03\x02\x01\x04")

    def test_ico_structure(self):
        ico = trayicon.ico_bytes("idle", sizes=(16, 256))
        reserved, kind, count = struct.unpack_from("<HHH", ico)
        self.assertEqual((reserved, kind, count), (0, 1, 2))
        for i, size in enumerate((16, 256)):
            w, h, _, _, planes, bpp, length, offset = struct.unpack_from("<BBBBHHII", ico, 6 + 16 * i)
            self.assertEqual((w, h, planes, bpp), (size % 256, size % 256, 1, 32))
            png = ico[offset:offset + length]
            self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(struct.unpack(">II", png[16:24]), (size, size))
            # IDAT inflates to size rows of (filter byte + RGBA)
            n = struct.unpack(">I", png[33:37])[0]
            self.assertEqual(len(zlib.decompress(png[41:41 + n])), size * (1 + size * 4))


class DaemonThreadTests(unittest.TestCase):
    def test_status_then_clean_stop(self):
        backend = MockBackend()
        t = tray.DaemonThread(lambda: GhostpadServer(backend, host="127.0.0.1", port=0))
        t.start()
        self.assertTrue(t.ready.wait(5))
        self.assertIsNone(t.error)
        st = t.status()
        self.assertEqual((st["client"], st["pads"], st["listening"]), (None, [], True))

        async def drive():
            c = await WSClient.connect(st["port"], name="tv")
            await c.recv()  # welcome
            await c.send_binary(p.encode_state(0, 1, S1))
            for _ in range(200):
                s = t.status()
                if s["pads"] == [0]:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual((s["client"], s["pads"]), ("tv", [0]))
            return c

        loop = asyncio.new_event_loop()  # kept open: the client stays connected through stop()
        try:
            c = loop.run_until_complete(drive())
            self.assertTrue(t.stop(5))
            self.assertIsNone(t.status())
            names = [e[0] for e in backend.events]
            # stop() releases the pad the normal way: neutral, then unplug, then backend close.
            self.assertEqual(names[-3:], ["neutral", "disconnect", "close"])
            loop.run_until_complete(c.aclose())
        finally:
            loop.close()

    def test_start_error_is_reported(self):
        def boom():
            raise OSError("port in use")
        t = tray.DaemonThread(boom)
        t.start()
        self.assertTrue(t.ready.wait(5))
        self.assertIsInstance(t.error, OSError)
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertTrue(t.stop(1))


@unittest.skipUnless(sys.platform == "win32", "Windows tray")
class TrayProcessTests(unittest.TestCase):
    """The real tray process: icon added, WM_CLOSE quits cleanly (server.stop runs)."""

    def test_icon_then_close_releases(self):
        import ctypes
        from ctypes import wintypes as w

        user32 = ctypes.WinDLL("user32")
        user32.FindWindowW.restype = w.HWND
        user32.FindWindowW.argtypes = [w.LPCWSTR, w.LPCWSTR]
        user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        with tempfile.TemporaryDirectory() as tmp:
            logf = Path(tmp) / "tray.log"
            proc = subprocess.Popen(
                [sys.executable, "-m", "ghostpad", "--tray", "--backend", "mock", "--bind", "127.0.0.1",
                 "--port", "0", "--log-file", str(logf)],
                cwd=DAEMON, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                hwnd = None
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not hwnd:
                    hwnd = user32.FindWindowW(tray.WINDOW_CLASS, f"{tray.WINDOW_CLASS} {proc.pid}")
                    time.sleep(0.05)
                self.assertTrue(hwnd, "tray window not found")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and "tray icon added" not in logf.read_text("utf-8"):
                    time.sleep(0.05)
                self.assertTrue(user32.PostMessageW(hwnd, tray.WM_CLOSE, 0, 0))
                self.assertEqual(proc.wait(10), 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
            text = logf.read_text("utf-8")
        for needle in ("listening on 127.0.0.1:", "tray icon added (mock)", "exit stats:", "tray exit"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
