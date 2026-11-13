#!/usr/bin/env python3
"""ghostpad synthetic load/latency client. Stdlib only.

Connects to a daemon, sends STATE frames at --rate Hz (values change every frame) plus
PINGs, and reports RTT / network RTT / jitter / loss, the generator's own send-interval
accuracy, and the daemon's GET /stats snapshot.

  python bench/bench.py --spawn                         # loopback against a local mock daemon
  python bench/bench.py --host 192.168.1.20 --token X   # against a real daemon on the LAN
  python bench/bench.py --spawn --json > bench/results/run1.json

Loopback numbers exclude Bluetooth, browser polling quantisation, real LAN/Wi-Fi and
display lag; see docs/latency-testing.md.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LOSS_TIMEOUT_S = 1.0
ROOT = Path(__file__).resolve().parents[1]

_STATE = struct.Struct("<BBHHBBhhhhI")  # type pad seq buttons lt rt lx ly rx ry t_client
_PING = struct.Struct("<BId")
_PONG = struct.Struct("<BIdI")


# --- minimal WebSocket client (derived from daemon/tests/wsclient.py) -----------------

def _mask(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    if n == 0:
        return data
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "little") ^ int.from_bytes(key, "little")).to_bytes(n, "little")


def build_frame(opcode: int, payload: bytes = b"") -> bytes:
    n = len(payload)
    out = bytearray([0x80 | opcode])
    if n < 126:
        out.append(0x80 | n)
    elif n < 65536:
        out += bytes([0x80 | 126]) + struct.pack("!H", n)
    else:
        out += bytes([0x80 | 127]) + struct.pack("!Q", n)
    mask = os.urandom(4)
    return bytes(out) + mask + _mask(payload, mask)


class WSClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.r, self.w = reader, writer

    @classmethod
    async def connect(cls, host: str, port: int, token: str | None) -> "WSClient":
        r, w = await asyncio.open_connection(host, port)
        sock = w.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        key = base64.b64encode(os.urandom(16)).decode()
        path = "/ws" + (f"?token={quote(token, safe='')}" if token else "")
        w.write((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                 f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        await w.drain()
        head = (await r.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
        if " 101 " not in head[0]:
            raise ConnectionError(f"handshake failed: {head[0]}")
        hdrs = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in head[1:])}
        expect = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
        if hdrs.get("sec-websocket-accept") != expect:
            raise ConnectionError("bad Sec-WebSocket-Accept")
        return cls(r, w)

    def send(self, opcode: int, payload: bytes) -> None:
        self.w.write(build_frame(opcode, payload))

    async def recv(self) -> tuple[int, bytes] | None:
        """Next data frame (opcode, payload); None on close/EOF. Answers pings."""
        try:
            while True:
                b0, b1 = await self.r.readexactly(2)
                n = b1 & 0x7F
                if n == 126:
                    (n,) = struct.unpack("!H", await self.r.readexactly(2))
                elif n == 127:
                    (n,) = struct.unpack("!Q", await self.r.readexactly(8))
                data = await self.r.readexactly(n)
                op = b0 & 0xF
                if op == 0x8:
                    return None
                if op == 0x9:
                    self.send(0xA, data)
                    continue
                if op == 0xA:
                    continue
                return op, data
        except (asyncio.IncompleteReadError, ConnectionError):
            return None

    def close(self) -> None:
        try:
            self.send(0x8, struct.pack("!H", 1000))
        except Exception:
            pass
        self.w.close()


def stats_path(token: str | None) -> str:
    """/stats URL path; the daemon requires ?token= when started with --token."""
    return "/stats" + (f"?token={quote(token, safe='')}" if token else "")


async def http_get_json(host: str, port: int, path: str) -> dict:
    r, w = await asyncio.open_connection(host, port)
    w.write(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    await w.drain()
    raw = await r.read()
    w.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    if status != 200:
        raise RuntimeError(f"GET {path} -> HTTP {status}")
    return json.loads(body)


# --- statistics ---------------------------------------------------------------------

def summarize(values: list[float]) -> dict:
    """Nearest-rank percentiles (same rule as daemon stats.py and the web HUD)."""
    if not values:
        return {"n": 0, "min": None, "median": None, "p95": None, "p99": None, "max": None}
    s = sorted(values)
    n = len(s)
    pick = lambda p: s[min(n - 1, int(p * n))]  # noqa: E731
    return {"n": n, "min": s[0], "median": s[n // 2], "p95": pick(0.95), "p99": pick(0.99), "max": s[-1]}


def jitter(values: list[float]) -> float | None:
    """Mean absolute difference of consecutive values."""
    if len(values) < 2:
        return None
    return sum(abs(b - a) for a, b in zip(values, values[1:])) / (len(values) - 1)


# --- the run ------------------------------------------------------------------------

def make_state(i: int, seq: int, t_ms: int) -> bytes:
    """STATE whose values change every frame (buttons cycle, sticks sweep, triggers ramp)."""
    ph = i * 0.05
    return _STATE.pack(
        0x01, 0, seq & 0xFFFF, (1 << (i % 16)) & 0xFFFF, i % 256, (255 - i) % 256,
        int(32767 * math.sin(ph)), int(32767 * math.cos(ph)),
        int(32767 * math.sin(ph * 0.5)), int(32767 * math.cos(ph * 0.5)), t_ms & 0xFFFFFFFF,
    )


async def run_bench(args: argparse.Namespace) -> dict:
    ws = await WSClient.connect(args.host, args.port, args.token)
    ws.send(0x1, json.dumps({"type": "hello", "version": 1, "client": "ghostpad-bench"}).encode())
    await ws.w.drain()
    welcome = await ws.recv()
    if welcome is None or welcome[0] != 0x1 or json.loads(welcome[1]).get("type") != "welcome":
        raise ConnectionError("no welcome from daemon (bad token?)")

    pending: dict[int, float] = {}
    rtt: list[float] = []
    net: list[float] = []
    proc_us: list[float] = []
    state: dict = {"sent_pings": 0, "late": 0, "lost": 0, "frames": 0}
    send_intervals: list[float] = []
    done = asyncio.Event()

    async def reader() -> None:
        while True:
            msg = await ws.recv()
            if msg is None:
                done.set()
                return
            op, data = msg
            if op == 0x2 and len(data) == _PONG.size and data[0] == 0x03:
                _, pid, t_client, p_us = _PONG.unpack(data)
                now = time.perf_counter() * 1000
                if pid not in pending:
                    state["late"] += 1
                    continue
                del pending[pid]
                r = now - t_client
                rtt.append(r)
                net.append(max(0.0, r - p_us / 1000))
                proc_us.append(float(p_us))

    async def pinger() -> None:
        pid = 0
        period = args.ping_interval_ms / 1000
        nxt = time.perf_counter()
        while True:
            nxt += period
            await asyncio.sleep(max(0, nxt - time.perf_counter()))
            pid += 1
            t = time.perf_counter() * 1000
            pending[pid] = time.perf_counter()
            state["sent_pings"] += 1
            ws.send(0x2, _PING.pack(0x02, pid, t))
            await ws.w.drain()
            now = time.perf_counter()
            for k in [k for k, v in pending.items() if now - v >= LOSS_TIMEOUT_S]:
                del pending[k]
                state["lost"] += 1

    rtask = asyncio.create_task(reader())
    ptask = asyncio.create_task(pinger())
    period = 1.0 / args.rate
    t_start = time.perf_counter()
    nxt = t_start
    last = None
    i = 0
    while nxt - t_start < args.duration and not done.is_set():
        await asyncio.sleep(max(0, nxt - time.perf_counter()))
        now = time.perf_counter()
        if last is not None:
            send_intervals.append((now - last) * 1000)
        last = now
        i += 1
        ws.send(0x2, make_state(i, i, int(now * 1000)))
        await ws.w.drain()
        nxt += period
    ptask.cancel()
    elapsed = time.perf_counter() - t_start
    # Snapshot the daemon before the link goes quiet (the 200 ms watchdog would count a trip),
    # then wait out the loss timeout for outstanding PONGs.
    await asyncio.sleep(0.1)
    stats = await http_get_json(args.host, args.port, stats_path(args.token))
    await asyncio.sleep(LOSS_TIMEOUT_S)
    state["lost"] += len(pending)
    pending.clear()
    rtask.cancel()
    ws.close()
    state["frames"] = i

    return {
        "config": {"host": args.host, "port": args.port, "rate_hz": args.rate, "duration_s": args.duration,
                   "ping_interval_ms": args.ping_interval_ms, "spawned": bool(args.spawn)},
        "rtt_ms": summarize(rtt),
        "net_rtt_ms": summarize(net),
        "one_way_ms_est": {k: (v / 2 if isinstance(v, float) else v) for k, v in summarize(net).items()},
        "jitter_ms": jitter(rtt),
        "server_proc_us": summarize(proc_us),
        "pings": {"sent": state["sent_pings"], "received": len(rtt), "lost": state["lost"], "late": state["late"],
                  "loss_pct": 100 * state["lost"] / state["sent_pings"] if state["sent_pings"] else None},
        "states": {"sent": state["frames"], "elapsed_s": round(elapsed, 3),
                   "achieved_hz": round(state["frames"] / elapsed, 1) if elapsed else None,
                   "send_interval_ms": summarize(send_intervals)},
        "daemon_stats": stats,
    }


# --- spawn --------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def spawn_daemon(token: str | None) -> tuple[subprocess.Popen, int]:
    port = free_port()
    cmd = [sys.executable, "-m", "ghostpad", "--backend", "mock", "--bind", "127.0.0.1", "--port", str(port)]
    if token:
        cmd += ["--token", token]
    proc = subprocess.Popen(cmd, cwd=ROOT / "daemon", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if proc.poll() is not None:
            raise RuntimeError("spawned daemon exited early")
        try:
            await http_get_json("127.0.0.1", port, stats_path(token))
            return proc, port
        except (OSError, RuntimeError, ValueError):
            await asyncio.sleep(0.05)
    proc.terminate()
    raise RuntimeError("spawned daemon did not come up")


# --- output -------------------------------------------------------------------------

def fmt_row(label: str, s: dict, scale: float = 1.0, unit: str = "ms") -> str:
    if not s["n"]:
        return f"  {label:<10} no samples"
    f = lambda v: f"{v * scale:8.3f}"  # noqa: E731
    return (f"  {label:<10} min{f(s['min'])}  median{f(s['median'])}  p95{f(s['p95'])}  "
            f"p99{f(s['p99'])}  max{f(s['max'])} {unit}   (n={s['n']})")


def print_report(r: dict) -> None:
    c = r["config"]
    print(f"ghostpad bench: {c['host']}:{c['port']}  rate={c['rate_hz']} Hz  duration={c['duration_s']} s"
          f"  ping every {c['ping_interval_ms']} ms{'  [spawned mock daemon]' if c['spawned'] else ''}")
    print("Client-side (RTT = now - t_client; net RTT = RTT - proc):")
    print(fmt_row("RTT", r["rtt_ms"]))
    print(fmt_row("net RTT", r["net_rtt_ms"]))
    print(fmt_row("one-way~", r["one_way_ms_est"]))
    print(fmt_row("proc", r["server_proc_us"], unit="us"))
    j = r["jitter_ms"]
    p = r["pings"]
    print(f"  jitter {'-' if j is None else f'{j:.3f} ms'}   pings sent={p['sent']} received={p['received']} "
          f"lost={p['lost']} ({p['loss_pct'] or 0:.1f}%) late={p['late']}")
    s = r["states"]
    print(f"Generator: {s['sent']} STATE in {s['elapsed_s']} s = {s['achieved_hz']} Hz achieved; "
          f"send interval {fmt_row('', s['send_interval_ms']).strip()}")
    d = r["daemon_stats"]
    ra = d["recv_apply"]
    print("Daemon GET /stats:")
    if ra["window"]:
        print(f"  recv->apply count={ra['count']} min={ra['min_us']} median={ra['median_us']} "
              f"p95={ra['p95_us']} p99={ra['p99_us']} max={ra['max_us']} us")
    print(f"  frames={d['frames_received']} stale={d['stale_drops']} malformed={d['malformed_drops']} "
          f"watchdog_trips={d['watchdog_trips']} pings={d['ping_count']} uptime={d['uptime_s']} s")
    print("Note: loopback excludes Bluetooth, browser polling, LAN/Wi-Fi and display lag.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ghostpad synthetic latency client")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=47800)
    p.add_argument("--token", default=None)
    p.add_argument("--rate", type=float, default=250.0, help="STATE frames per second")
    p.add_argument("--duration", type=float, default=10.0, help="seconds")
    p.add_argument("--ping-interval-ms", type=float, default=250.0,
                   help="PING period (HUD uses 250; lower it for more RTT samples)")
    p.add_argument("--json", action="store_true", help="print one JSON document instead of a report")
    p.add_argument("--spawn", action="store_true", help="start a local mock daemon on a free port")
    a = p.parse_args(argv)
    if a.rate <= 0 or a.duration <= 0 or a.ping_interval_ms <= 0:
        p.error("--rate, --duration and --ping-interval-ms must be positive")
    return a


async def amain(args: argparse.Namespace) -> dict:
    proc = None
    try:
        if args.spawn:
            proc, args.port = await spawn_daemon(args.token)
            args.host = "127.0.0.1"
        return await run_bench(args)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(amain(args))
    except (OSError, ConnectionError, RuntimeError) as e:
        print(f"bench: {e}", file=sys.stderr)
        return 1
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
    else:
        print_report(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
