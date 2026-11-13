import asyncio
import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path

from ghostpad import protocol as p
from ghostpad.__main__ import parse_args
from tests.wsclient import WSClient

DAEMON = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_trace_flag(self):
        self.assertFalse(parse_args([]).trace)
        self.assertTrue(parse_args(["--trace"]).trace)

    def test_defaults(self):
        a = parse_args([])
        self.assertEqual((a.backend, a.port, a.token), ("auto", 47800, None))


@unittest.skipUnless(sys.platform == "win32", "Ctrl+Break is Windows-only")
class CtrlBreakTests(unittest.IsolatedAsyncioTestCase):
    async def test_ctrl_break_releases_pads_and_exits_0(self):
        proc = subprocess.Popen(
            [sys.executable, "-m", "ghostpad", "--backend", "mock", "--bind", "127.0.0.1",
             "--port", "0", "--trace"],
            cwd=DAEMON, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        self.addCleanup(proc.kill)
        port = None
        lines: list[str] = []
        while port is None:  # "listening on 127.0.0.1:<port>"
            line = await asyncio.to_thread(proc.stdout.readline)
            self.assertTrue(line, "daemon exited early:\n" + "".join(lines))
            lines.append(line)
            if "listening on" in line:
                port = int(line.split("listening on ")[1].split()[0].rsplit(":", 1)[1])
        c = await WSClient.connect(port)
        await c.recv()  # welcome
        await c.send_binary(p.encode_state(0, 1, p.PadState(0x1000, 0, 0, 0, 0, 0, 0)))
        await asyncio.sleep(0.1)
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        self.assertIsNone(await c.recv(5.0))
        self.assertEqual(c.close_code, 1000)
        await c.aclose()
        out, _ = await asyncio.to_thread(proc.communicate, timeout=10)
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("pad 0 neutral, unplug (release)", out)
        self.assertIn("exit stats:", out)


if __name__ == "__main__":
    unittest.main()
