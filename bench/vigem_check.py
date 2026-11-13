#!/usr/bin/env python3
"""Windows-only end-to-end check of the vigem backend through XInput (issue #3). Stdlib only.

Starts the daemon with `--backend vigem` on loopback, drives it with a minimal WebSocket
client and reads the virtual pads back with XInput. Automates the docs/windows-setup.md
checks that need no person: button/trigger/stick mapping, neutral, unplug on PAD_GONE and on
close, reconnect, watchdog release, takeover, killed client, rumble (this script acts as
the game via XInputSetState), daemon Ctrl+Break and hard kill, four pads at once, and the
STATE-sent to XInput-visible latency on loopback.

  python bench/vigem_check.py [--python PATH | --exe PATH] [--trials 200] [--json]

Needs ViGEmBus, and `vgamepad` in the Python that runs the daemon (--python; default: this
one). Virtual pads take free XInput slots; leave real XInput controllers unplugged, or the
four-pad check cannot get four slots. Latency here is loopback WebSocket + daemon + ViGEm +
XInput readback: no Bluetooth, browser polling, LAN or display (docs/latency-testing.md).
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from ctypes import POINTER, Structure, byref, c_short, c_ubyte, c_uint32, c_ushort
from pathlib import Path

from bench import WSClient, _loop_factory, fine_timer, free_port, summarize

DAEMON = Path(__file__).resolve().parents[1] / "daemon"
_STATE = struct.Struct("<BBHHBBhhhhI")
_RUMBLE = struct.Struct("<BBBB")

# XInput wButtons bits as the daemon forwards them (0x0800 is unused and masked).
BUTTONS = {
    "DPAD_UP": 0x0001, "DPAD_DOWN": 0x0002, "DPAD_LEFT": 0x0004, "DPAD_RIGHT": 0x0008,
    "START": 0x0010, "BACK": 0x0020, "LEFT_THUMB": 0x0040, "RIGHT_THUMB": 0x0080,
    "LEFT_SHOULDER": 0x0100, "RIGHT_SHOULDER": 0x0200, "GUIDE": 0x0400,
    "A": 0x1000, "B": 0x2000, "X": 0x4000, "Y": 0x8000,
}
ZERO = (0, 0, 0, 0, 0, 0, 0)
HOLD_A = (0x1000, 0, 0, 0, 0, 0, 0)


# --- XInput -----------------------------------------------------------------------------

class _Gamepad(Structure):
    _fields_ = [("wButtons", c_ushort), ("bLeftTrigger", c_ubyte), ("bRightTrigger", c_ubyte),
                ("sThumbLX", c_short), ("sThumbLY", c_short), ("sThumbRX", c_short),
                ("sThumbRY", c_short)]


class _State(Structure):
    _fields_ = [("dwPacketNumber", c_uint32), ("Gamepad", _Gamepad)]


class _Vibration(Structure):
    _fields_ = [("wLeftMotorSpeed", c_ushort), ("wRightMotorSpeed", c_ushort)]


class XInput:
    def __init__(self) -> None:
        dll = ctypes.WinDLL("xinput1_4")
        try:
            self._get = dll[100]  # XInputGetStateEx (undocumented ordinal 100) also reports Guide
            self.guide = True
        except AttributeError:
            self._get = dll.XInputGetState
            self.guide = False
        self._get.argtypes = [c_uint32, POINTER(_State)]
        self._get.restype = c_uint32
        self._set = dll.XInputSetState
        self._set.argtypes = [c_uint32, POINTER(_Vibration)]
        self._set.restype = c_uint32

    def read(self, slot: int) -> tuple[int, ...] | None:
        st = _State()
        if self._get(slot, byref(st)) != 0:
            return None
        g = st.Gamepad
        return (g.wButtons, g.bLeftTrigger, g.bRightTrigger, g.sThumbLX, g.sThumbLY,
                g.sThumbRX, g.sThumbRY)

    def slots(self) -> set[int]:
        return {i for i in range(4) if self.read(i) is not None}

    def vibrate(self, slot: int, left: int, right: int) -> int:
        return self._set(slot, byref(_Vibration(left, right)))


def spin_until(pred, timeout_s: float) -> float | None:
    """Busy-wait (no sleep: loopback is sub-millisecond). Elapsed ms, or None on timeout."""
    t0 = time.perf_counter()
    end = t0 + timeout_s
    while True:
        if pred():
            return (time.perf_counter() - t0) * 1000
        if time.perf_counter() > end:
            return None


# --- daemon and client -------------------------------------------------------------------

class Daemon:
    def __init__(self, args: argparse.Namespace, port: int) -> None:
        opts = ["--backend", "vigem", "--bind", "127.0.0.1", "--port", str(port)]
        if args.exe:  # packaged build (console build; a --noconsole exe logs to a file instead)
            exe = Path(args.exe).resolve()
            cmd, cwd = [str(exe), *opts], exe.parent
        else:
            cmd, cwd = [args.python, "-m", "ghostpad", *opts], DAEMON
        self.p = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        self.port = port
        self.lines: list[str] = []
        self._ready = threading.Event()
        threading.Thread(target=self._pump, daemon=True).start()
        if not self._ready.wait(20) or "backend=vigem" not in self.listen_line:
            self.p.kill()
            raise RuntimeError("daemon did not start on the vigem backend:\n" + "\n".join(self.lines[-10:]))

    listen_line = ""

    def _pump(self) -> None:
        for line in self.p.stdout:  # type: ignore[union-attr]
            self.lines.append(line.rstrip())
            if "listening on" in line:
                self.listen_line = line
                self._ready.set()
        self._ready.set()


class Client:
    """Hello/welcome, STATE/PAD_GONE senders, and a reader that queues RUMBLE and records close."""

    def __init__(self, ws: WSClient) -> None:
        self.ws = ws
        self.seq = [0] * 4
        self.rumble: asyncio.Queue[tuple[int, int, int, float]] = asyncio.Queue()
        self.close_code: int | None = None
        self.closed = asyncio.Event()

    @classmethod
    async def open(cls, port: int, name: str) -> "Client":
        c = cls(await WSClient.connect("127.0.0.1", port, None))
        c.ws.send(0x1, json.dumps({"type": "hello", "version": 1, "client": name}).encode())
        await c.ws.w.drain()
        msg = await asyncio.wait_for(c._frame(), 5)
        if not msg or msg[0] != 0x1 or json.loads(msg[1])["type"] != "welcome":
            raise RuntimeError(f"no welcome: {msg!r}")
        c._task = asyncio.create_task(c._read())
        return c

    async def _frame(self) -> tuple[int, bytes] | None:
        r = self.ws.r
        try:
            b0, b1 = await r.readexactly(2)
            n = b1 & 0x7F
            if n == 126:
                (n,) = struct.unpack("!H", await r.readexactly(2))
            elif n == 127:
                (n,) = struct.unpack("!Q", await r.readexactly(8))
            return b0 & 0xF, await r.readexactly(n)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None

    async def _read(self) -> None:
        while True:
            f = await self._frame()
            if f is None:
                break
            op, data = f
            if op == 0x8:
                self.close_code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 1005
                break
            if op == 0x2 and data[:1] == b"\x05" and len(data) == 4:
                _, pad, large, small = _RUMBLE.unpack(data)
                self.rumble.put_nowait((pad, large, small, time.perf_counter()))
        self.closed.set()

    def state(self, pad: int, s: tuple[int, ...]) -> None:
        self.seq[pad] = (self.seq[pad] + 1) & 0xFFFF
        self.ws.send(0x2, _STATE.pack(0x01, pad, self.seq[pad], *s, 0))

    def pad_gone(self, pad: int) -> None:
        self.ws.send(0x2, bytes([0x04, pad]))

    def close(self) -> None:
        self.ws.close()

    async def abort(self) -> None:
        """Reset the connection without a close frame, like a killed client process."""
        sock = self.ws.w.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))  # RST
        self.ws.w.transport.abort()
        await asyncio.sleep(0)  # abort() closes the socket on the next loop iteration
        await asyncio.sleep(0)


# --- checks ------------------------------------------------------------------------------

class Run:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.x = XInput()
        self.results: list[dict] = []

    def record(self, name: str, ok: bool, detail: str, **data) -> None:
        self.results.append({"check": name, "ok": ok, "detail": detail, **data})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    async def plug(self, c: Client, pad: int, s: tuple[int, ...] = ZERO) -> tuple[int | None, float | None]:
        before = self.x.slots()
        c.state(pad, s)
        new: set[int] = set()

        def appeared() -> bool:
            new.update(self.x.slots() - before)
            return bool(new)
        ms = spin_until(appeared, 10)
        await asyncio.sleep(0.05)
        return (min(new) if new else None), ms

    def readback(self, c: Client, pad: int, slot: int, s: tuple[int, ...], expect=None,
                 timeout: float = 0.5) -> tuple[int, ...] | None:
        want = tuple(expect if expect is not None else s)
        c.state(pad, s)
        spin_until(lambda: self.x.read(slot) == want, timeout)
        return self.x.read(slot)

    async def main(self) -> None:
        if self.x.slots():
            print(f"note: XInput slots {sorted(self.x.slots())} already in use by other controllers")
        d = Daemon(self.args, free_port())
        try:
            await self.part_one(d)
        finally:
            if d.p.poll() is None:
                d.p.kill()
        d2 = Daemon(self.args, free_port())
        try:
            await self.hard_kill(d2)
        finally:
            if d2.p.poll() is None:
                d2.p.kill()

    async def part_one(self, d: Daemon) -> None:
        a = await Client.open(d.port, "check-a")
        slot, ms = await self.plug(a, 0)
        self.record("plug", slot is not None, f"pad 0 -> XInput slot {slot}, visible after {ms and round(ms)} ms")
        if slot is None:
            return

        bad = []
        for name, bit in BUTTONS.items():
            if name == "GUIDE" and not self.x.guide:
                continue
            got = self.readback(a, 0, slot, (bit, 0, 0, 0, 0, 0, 0))
            if got is None or got[0] != bit:
                bad.append(f"{name}: got {got and hex(got[0])}")
        got = self.readback(a, 0, slot, (0x0800 | 0x1000, 0, 0, 0, 0, 0, 0), expect=HOLD_A)
        if got != HOLD_A:
            bad.append(f"0x0800 not masked: {got}")
        self.record("buttons", not bad, "; ".join(bad) or
                    f"all {len(BUTTONS) - (0 if self.x.guide else 1)} bits map 1:1, 0x0800 masked"
                    + ("" if self.x.guide else " (Guide not readable)"))

        bad = []
        for v in (0, 1, 127, 128, 254, 255):
            s = (0, v, 255 - v, 0, 0, 0, 0)
            got = self.readback(a, 0, slot, s)
            if got != s:
                bad.append(f"lt={v} rt={255 - v}: got {got}")
        self.record("triggers", not bad, "; ".join(bad) or "LT/RT 0..255 exact and independent, no button bits")

        bad = []
        for axis in range(4):
            for v in (-32768, -32767, -1, 0, 1, 32767):
                s = [0, 0, 0, 0, 0, 0, 0]
                s[3 + axis] = v
                got = self.readback(a, 0, slot, tuple(s))
                if got != tuple(s):
                    bad.append(f"axis {axis} = {v}: got {got}")
        s = (0, 0, 0, 1234, -2345, 3456, -4567)
        if self.readback(a, 0, slot, s) != s:
            bad.append("combined sticks mismatch")
        self.record("sticks", not bad, "; ".join(bad) or "LX/LY/RX/RY pass through exactly (+Y up), extremes included")

        got = self.readback(a, 0, slot, ZERO)
        self.record("neutral", got == ZERO, f"readback {got}")

        lat = []
        for i in range(self.args.trials):
            s = (0x1000 if i % 2 else 0x2000, i % 256, 0, (i * 97) % 32767, 0, 0, 0)
            t0 = time.perf_counter()
            a.state(0, s)
            ms = spin_until(lambda: self.x.read(slot) == s, 0.5)
            if ms is not None:
                lat.append((time.perf_counter() - t0) * 1000)
            await asyncio.sleep(0.004)
        st = summarize(lat)
        self.record("latency", len(lat) == self.args.trials,
                    f"STATE sent -> XInput visible, n={len(lat)}/{self.args.trials}: min {st['min']:.3f} "
                    f"median {st['median']:.3f} p95 {st['p95']:.3f} p99 {st['p99']:.3f} max {st['max']:.3f} ms",
                    latency_ms=st)

        rel = []
        for _ in range(10):
            a.state(0, HOLD_A)
            spin_until(lambda: self.x.read(slot) == HOLD_A, 0.5)
            t0 = time.perf_counter()
            ms = spin_until(lambda: self.x.read(slot) == ZERO, 1.0)
            rel.append(ms if ms is not None else float("inf"))
        still = slot in self.x.slots()
        resumed = self.readback(a, 0, slot, (0x2000, 0, 0, 0, 0, 0, 0)) == (0x2000, 0, 0, 0, 0, 0, 0)
        st = summarize([r for r in rel if r != float("inf")])
        self.record("watchdog", max(rel) <= 250 and still and resumed,
                    f"held A, input stopped -> neutral in XInput: median {st['median']:.1f} max {max(rel):.1f} ms "
                    f"(10 trials, target 250); pad stayed plugged: {still}; resumed on next STATE: {resumed}",
                    release_ms=st)

        await self.rumble(a, slot)

        slot1, _ = await self.plug(a, 1, HOLD_A)
        a.state(0, ZERO)
        t0 = time.perf_counter()
        a.pad_gone(1)
        ms = spin_until(lambda: slot1 not in self.x.slots(), 3) if slot1 is not None else None
        self.record("pad_gone", slot1 is not None and ms is not None,
                    f"pad 1 (slot {slot1}) unplugged {ms and round(ms, 1)} ms after PAD_GONE; pad 0 kept: {slot in self.x.slots()}")

        await self.multipad(a, slot)

        a.state(0, HOLD_A)
        spin_until(lambda: self.x.read(slot) == HOLD_A, 0.5)
        a.close()
        ms = spin_until(lambda: not self.x.slots(), 3)
        self.record("unplug_on_close", ms is not None, f"all pads gone {ms and round(ms, 1)} ms after close")

        b = await Client.open(d.port, "check-b")
        slot_b, ms = await self.plug(b, 0, HOLD_A)
        self.record("reconnect", slot_b is not None, f"new client re-plugs pad 0 (slot {slot_b}) in {ms and round(ms)} ms")

        c = await Client.open(d.port, "check-c")
        await asyncio.wait_for(b.closed.wait(), 3)
        gone = spin_until(lambda: not self.x.slots(), 3) is not None
        slot_c, _ = await self.plug(c, 0, HOLD_A)
        self.record("takeover", b.close_code == 4001 and gone and slot_c is not None,
                    f"old client closed with {b.close_code}, its pad unplugged: {gone}, new owner plugs slot {slot_c}")

        c.state(0, HOLD_A)
        await c.abort()
        ms = spin_until(lambda: not self.x.slots(), 3)
        self.record("client_killed", ms is not None, f"connection reset with A held: pad gone after {ms and round(ms, 1)} ms")

        e = await Client.open(d.port, "check-e")
        slot_e, _ = await self.plug(e, 0, HOLD_A)
        os.kill(d.p.pid, signal.CTRL_BREAK_EVENT)
        ms = spin_until(lambda: not self.x.slots(), 5)
        try:
            await asyncio.wait_for(e.closed.wait(), 5)
        except asyncio.TimeoutError:
            pass
        rc = await asyncio.to_thread(d.p.wait, 10)
        released = any("exit stats" in ln for ln in d.lines)
        self.record("ctrl_break", ms is not None and e.close_code == 1000 and rc == 0 and released,
                    f"pad gone after {ms and round(ms, 1)} ms, client close {e.close_code}, exit code {rc}, exit stats logged: {released}")

    async def rumble(self, a: Client, slot: int) -> None:
        a.state(0, ZERO)
        got = []
        for left, right in ((0xC000, 0x4000), (0xFFFF, 0), (0, 0)):
            t0 = time.perf_counter()
            rc = self.x.vibrate(slot, left, right)
            try:
                pad, large, small, t1 = await asyncio.wait_for(a.rumble.get(), 1.0)
                got.append((left, right, rc, pad, large, small, round((t1 - t0) * 1000, 2)))
            except asyncio.TimeoutError:
                got.append((left, right, rc, None, None, None, None))
            a.state(0, ZERO)  # keep the watchdog quiet between steps
        ok = all(g[3] == 0 and g[4] == g[0] >> 8 and g[5] == g[1] >> 8 for g in got)
        self.record("rumble", ok, "; ".join(
            f"XInputSetState({l:#06x},{r:#06x}) rc={rc} -> RUMBLE pad={p} large={lg} small={sm} after {t} ms"
            for l, r, rc, p, lg, sm, t in got))

    async def multipad(self, a: Client, slot0: int) -> None:
        states = {p: (1 << (12 + p), 40 * p, 0, 1000 * p, 0, 0, 0) for p in range(4)}
        slots = {0: slot0}
        a.state(0, states[0])
        for p in (1, 2, 3):
            s, _ = await self.plug(a, p, states[p])
            slots[p] = s
        for p in range(4):
            a.state(p, states[p])
        ok = spin_until(lambda: all(slots[p] is not None and self.x.read(slots[p]) == states[p]
                                    for p in range(4)), 1.0) is not None
        for p in (1, 2, 3):
            a.pad_gone(p)
        gone = spin_until(lambda: self.x.slots() == {slot0}, 3) is not None
        self.record("multipad", ok and gone, f"pads 0-3 -> slots {slots}, each reads its own state: {ok}; "
                    f"pads 1-3 unplugged by PAD_GONE: {gone}")

    async def hard_kill(self, d: Daemon) -> None:
        c = await Client.open(d.port, "check-kill")
        slot, _ = await self.plug(c, 0, HOLD_A)
        t0 = time.perf_counter()
        d.p.kill()  # TerminateProcess: no release by the daemon
        ms = spin_until(lambda: not self.x.slots(), 10)
        self.record("hard_kill", slot is not None and ms is not None,
                    f"daemon killed with A held on slot {slot}: pad removed by the driver after "
                    f"{ms and round(ms, 1)} ms" if ms is not None else
                    f"daemon killed with A held on slot {slot}: pad still present after 10 s; held: {self.x.read(slot) if slot is not None else None}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--python", default=sys.executable, help="Python with vgamepad that runs the daemon")
    ap.add_argument("--exe", help="run this console ghostpad.exe (build_exe.py) instead of the source tree")
    ap.add_argument("--trials", type=int, default=200, help="latency samples")
    ap.add_argument("--json", action="store_true", help="also print all results as JSON")
    args = ap.parse_args(argv)
    if sys.platform != "win32":
        print("vigem_check needs Windows with ViGEmBus", file=sys.stderr)
        return 2
    run = Run(args)
    with fine_timer(), asyncio.Runner(loop_factory=_loop_factory) as runner:
        runner.run(run.main())
    failed = [r["check"] for r in run.results if not r["ok"]]
    print(f"\n{len(run.results) - len(failed)}/{len(run.results)} checks passed"
          + (f"; failed: {', '.join(failed)}" if failed else ""))
    if args.json:
        print(json.dumps(run.results, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
