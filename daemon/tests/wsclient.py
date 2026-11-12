"""Tiny stdlib asyncio WebSocket client for tests (independent of ghostpad.wsproto
framing code paths where practical)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def mask_bytes(data: bytes, mask: bytes) -> bytes:
    return bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def build_frame(opcode: int, payload: bytes = b"", fin: bool = True, masked: bool = True,
                rsv: int = 0) -> bytes:
    n = len(payload)
    out = bytearray([(0x80 if fin else 0) | (rsv << 4) | opcode])
    mbit = 0x80 if masked else 0
    if n < 126:
        out.append(mbit | n)
    elif n < 65536:
        out += bytes([mbit | 126]) + struct.pack("!H", n)
    else:
        out += bytes([mbit | 127]) + struct.pack("!Q", n)
    if masked:
        mask = os.urandom(4)
        out += mask + mask_bytes(payload, mask)
    else:
        out += payload
    return bytes(out)


async def http_get(port: int, path: str, method: str = "GET", host: str = "127.0.0.1"
                   ) -> tuple[int, dict[str, str], bytes]:
    r, w = await asyncio.open_connection(host, port)
    w.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
    await w.drain()
    raw = await r.read()
    w.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    return status, headers, body


class WSClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.r = reader
        self.w = writer
        self.close_code: int | None = None
        self.closed = False

    @classmethod
    async def connect(cls, port: int, path: str = "/ws", host: str = "127.0.0.1",
                      hello: bool = True, version: int = 1, name: str = "test") -> "WSClient":
        r, w = await asyncio.open_connection(host, port)
        key = base64.b64encode(os.urandom(16)).decode()
        w.write((f"GET {path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        await w.drain()
        head = await r.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        if " 101 " not in lines[0]:
            raise ConnectionError(lines[0])
        expect = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
        hdrs = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
        assert hdrs["sec-websocket-accept"] == expect, "bad accept key"
        c = cls(r, w)
        if hello:
            await c.send_text(f'{{"type":"hello","version":{version},"client":"{name}"}}')
        return c

    async def send_raw(self, data: bytes) -> None:
        self.w.write(data)
        await self.w.drain()

    async def send_text(self, text: str) -> None:
        await self.send_raw(build_frame(0x1, text.encode()))

    async def send_binary(self, data: bytes) -> None:
        await self.send_raw(build_frame(0x2, data))

    async def send_close(self, code: int = 1000) -> None:
        await self.send_raw(build_frame(0x8, struct.pack("!H", code)))

    async def read_frame(self) -> tuple[bool, int, bytes]:
        b0, b1 = await self.r.readexactly(2)
        assert not b1 & 0x80, "server frame must not be masked"
        n = b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack("!H", await self.r.readexactly(2))
        elif n == 127:
            (n,) = struct.unpack("!Q", await self.r.readexactly(8))
        return bool(b0 & 0x80), b0 & 0xF, await self.r.readexactly(n)

    async def recv(self, timeout: float = 2.0) -> tuple[int, bytes] | None:
        """Next data/pong frame as (opcode, payload); None after a close frame/EOF
        (the close code is stored in `close_code`)."""
        try:
            while True:
                _, op, data = await asyncio.wait_for(self.read_frame(), timeout)
                if op == 0x8:
                    self.closed = True
                    self.close_code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 1005
                    try:
                        await self.send_close(self.close_code)
                    except (ConnectionError, OSError):
                        pass
                    return None
                return op, data
        except (asyncio.IncompleteReadError, ConnectionError):
            self.closed = True
            return None

    async def aclose(self) -> None:
        self.w.close()
        try:
            await self.w.wait_closed()
        except (ConnectionError, OSError):
            pass
