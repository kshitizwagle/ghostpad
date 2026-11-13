"""Minimal RFC 6455 server side over asyncio streams.

Supports: handshake helpers, masked client frames, text/binary/ping/pong/close,
fragmented messages (reassembled, bounded), 64 KiB max frame/message.
Server frames are never masked and never fragmented.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import socket
import struct
from time import perf_counter_ns

log = logging.getLogger(__name__)

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 64 * 1024

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

CLOSE_NORMAL = 1000
CLOSE_PROTOCOL = 1002
CLOSE_BAD_DATA = 1007
CLOSE_TOO_BIG = 1009


class WSError(Exception):
    """Protocol violation; `code` is the close code to send."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.encode("ascii") + GUID).digest()).decode("ascii")


def handshake_response(key: str) -> bytes:
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n"
    ).encode("ascii")


def encode_frame(opcode: int, payload: bytes = b"", fin: bool = True, mask: bytes | None = None) -> bytes:
    """Build a frame. Server frames use mask=None; mask is for test clients."""
    n = len(payload)
    b0 = (0x80 if fin else 0) | opcode
    mbit = 0x80 if mask else 0
    if n < 126:
        head = struct.pack("!BB", b0, mbit | n)
    elif n < 65536:
        head = struct.pack("!BBH", b0, mbit | 126, n)
    else:
        head = struct.pack("!BBQ", b0, mbit | 127, n)
    if mask:
        return head + mask + unmask(payload, mask)
    return head + payload


def unmask(payload: bytes, mask: bytes) -> bytes:
    n = len(payload)
    if not n:
        return payload
    m = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "little") ^ int.from_bytes(m, "little")).to_bytes(n, "little")


async def read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    """Read one client frame -> (fin, opcode, unmasked payload). Raises WSError."""
    b0, b1 = await reader.readexactly(2)
    if b0 & 0x70:
        raise WSError(CLOSE_PROTOCOL, "reserved bits set")
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    if not b1 & 0x80:
        raise WSError(CLOSE_PROTOCOL, "client frame not masked")
    n = b1 & 0x7F
    if n == 126:
        (n,) = struct.unpack("!H", await reader.readexactly(2))
    elif n == 127:
        (n,) = struct.unpack("!Q", await reader.readexactly(8))
    if opcode >= 0x8:
        if not fin or n > 125:
            raise WSError(CLOSE_PROTOCOL, "bad control frame")
        if opcode not in (OP_CLOSE, OP_PING, OP_PONG):
            raise WSError(CLOSE_PROTOCOL, "unknown opcode")
    elif opcode > OP_BIN:
        raise WSError(CLOSE_PROTOCOL, "unknown opcode")
    if n > MAX_FRAME:
        raise WSError(CLOSE_TOO_BIG, "frame too large")
    raw = await reader.readexactly(4 + n)
    return fin, opcode, unmask(raw[4:], raw[:4])


class WebSocket:
    """Server-side connection. `recv()` returns (opcode, data) or None once closed.

    Text data is returned as `str`, binary as `bytes`. Ping/pong/close are handled
    internally. `last_rx_ns` (perf_counter_ns) is stamped when each frame is read.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self.last_rx_ns = perf_counter_ns()
        self.close_sent = False
        self.close_code: int | None = None  # code received from the peer
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass

    # -- sending ------------------------------------------------------------

    def _write(self, data: bytes) -> None:
        if not self._writer.is_closing():
            self._writer.write(data)

    async def send_text(self, text: str) -> None:
        self._write(encode_frame(OP_TEXT, text.encode("utf-8")))
        await self._drain()

    async def send_binary(self, data: bytes) -> None:
        self._write(encode_frame(OP_BIN, data))
        await self._drain()

    def send_binary_nowait(self, data: bytes) -> None:
        """Queue a binary frame without draining (sync, loop thread). No-op after our close."""
        if not self.close_sent:
            self._write(encode_frame(OP_BIN, data))

    async def _drain(self) -> None:
        try:
            await self._writer.drain()
        except (ConnectionError, OSError):
            pass

    def send_close(self, code: int = CLOSE_NORMAL, reason: str = "") -> None:
        """Queue a close frame (sync, safe from any task). Transport is aborted
        after a grace period if the peer never echoes."""
        if self.close_sent:
            return
        self.close_sent = True
        payload = struct.pack("!H", code) + reason.encode("utf-8")[:120]
        self._write(encode_frame(OP_CLOSE, payload))
        asyncio.get_running_loop().call_later(2.0, self._writer.close)

    async def close(self, code: int = CLOSE_NORMAL, reason: str = "") -> None:
        """Send close and wait (bounded) for the peer's close / EOF."""
        self.send_close(code, reason)
        await self._drain()
        try:
            await asyncio.wait_for(self._wait_peer_close(), 2.0)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass

    async def _wait_peer_close(self) -> None:
        while await self.recv() is not None:
            pass

    def abort(self) -> None:
        self._writer.close()

    # -- receiving ----------------------------------------------------------

    async def recv(self) -> tuple[int, bytes | str] | None:
        frag_op = -1
        frag = bytearray()
        try:
            while True:
                fin, op, data = await read_frame(self._reader)
                self.last_rx_ns = perf_counter_ns()
                if op == OP_PING:
                    if not self.close_sent:
                        self._write(encode_frame(OP_PONG, data))
                    continue
                if op == OP_PONG:
                    continue
                if op == OP_CLOSE:
                    if len(data) == 1:
                        raise WSError(CLOSE_PROTOCOL, "bad close payload")
                    self.close_code = struct.unpack("!H", data[:2])[0] if data else 1005
                    self.send_close(self.close_code if self.close_code != 1005 else CLOSE_NORMAL)
                    return None
                if self.close_sent:
                    continue  # ignore data after we initiated close
                if op == OP_CONT:
                    if frag_op < 0:
                        raise WSError(CLOSE_PROTOCOL, "unexpected continuation")
                    frag += data
                    if len(frag) > MAX_FRAME:
                        raise WSError(CLOSE_TOO_BIG, "message too large")
                    if not fin:
                        continue
                    op, data = frag_op, bytes(frag)
                elif frag_op >= 0:
                    raise WSError(CLOSE_PROTOCOL, "new data frame inside fragmented message")
                elif not fin:
                    frag_op, frag = op, bytearray(data)
                    continue
                if op == OP_TEXT:
                    try:
                        return OP_TEXT, data.decode("utf-8")
                    except UnicodeDecodeError:
                        raise WSError(CLOSE_BAD_DATA, "invalid utf-8") from None
                return OP_BIN, data
        except WSError as e:
            log.debug("ws protocol error: %s", e.reason)
            self.send_close(e.code, e.reason)
            await self._drain()
            return None
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return None
