"""Protocol v1 encode/decode. Pure functions, no I/O. See DESIGN.md."""

from __future__ import annotations

import json
import struct
from typing import NamedTuple

PROTOCOL_VERSION = 1
MAX_PADS = 4

MSG_STATE = 0x01
MSG_PING = 0x02
MSG_PONG = 0x03
MSG_PAD_GONE = 0x04
MSG_RUMBLE = 0x05

_STATE = struct.Struct("<BBHHBBhhhhI")  # 20 bytes
_PING = struct.Struct("<BId")  # 13 bytes
_PONG = struct.Struct("<BIdI")  # 17 bytes
_PAD_GONE = struct.Struct("<BB")  # 2 bytes
_RUMBLE = struct.Struct("<BBBB")  # 4 bytes

STATE_SIZE = _STATE.size
PING_SIZE = _PING.size
PONG_SIZE = _PONG.size
PAD_GONE_SIZE = _PAD_GONE.size
RUMBLE_SIZE = _RUMBLE.size

_U32_MASK = 0xFFFFFFFF


class PadState(NamedTuple):
    buttons: int
    lt: int
    rt: int
    lx: int
    ly: int
    rx: int
    ry: int


NEUTRAL = PadState(0, 0, 0, 0, 0, 0, 0)


class StateFrame(NamedTuple):
    pad: int
    seq: int
    state: PadState
    t_client: int


class Ping(NamedTuple):
    id: int
    t_client: float


class PadGone(NamedTuple):
    pad: int


Message = StateFrame | Ping | PadGone


def seq_newer(seq: int, last: int) -> bool:
    """True if `seq` is newer than `last` (serial number arithmetic, 16 bit)."""
    return 0 < ((seq - last) & 0xFFFF) < 0x8000


def decode_binary(data: bytes) -> Message | None:
    """Decode a client-to-server binary frame; None if malformed/unknown/bad pad."""
    if not data:
        return None
    t = data[0]
    if t == MSG_STATE:
        if len(data) != STATE_SIZE:
            return None
        _, pad, seq, buttons, lt, rt, lx, ly, rx, ry, tc = _STATE.unpack(data)
        if pad >= MAX_PADS:
            return None
        return StateFrame(pad, seq, PadState(buttons, lt, rt, lx, ly, rx, ry), tc)
    if t == MSG_PING:
        if len(data) != PING_SIZE:
            return None
        _, pid, tc = _PING.unpack(data)
        return Ping(pid, tc)
    if t == MSG_PAD_GONE:
        if len(data) != PAD_GONE_SIZE:
            return None
        pad = data[1]
        if pad >= MAX_PADS:
            return None
        return PadGone(pad)
    return None


def encode_state(pad: int, seq: int, s: PadState, t_client: int = 0) -> bytes:
    return _STATE.pack(
        MSG_STATE, pad, seq & 0xFFFF, s.buttons, s.lt, s.rt,
        s.lx, s.ly, s.rx, s.ry, t_client & _U32_MASK,
    )


def encode_ping(pid: int, t_client: float) -> bytes:
    return _PING.pack(MSG_PING, pid & _U32_MASK, t_client)


def encode_pong(pid: int, t_client: float, proc_us: int) -> bytes:
    return _PONG.pack(MSG_PONG, pid & _U32_MASK, t_client, min(max(proc_us, 0), _U32_MASK))


def decode_pong(data: bytes) -> tuple[int, float, int] | None:
    if len(data) != PONG_SIZE or data[0] != MSG_PONG:
        return None
    _, pid, tc, proc = _PONG.unpack(data)
    return pid, tc, proc


def encode_pad_gone(pad: int) -> bytes:
    return _PAD_GONE.pack(MSG_PAD_GONE, pad)


def encode_rumble(pad: int, large: int, small: int) -> bytes:
    return _RUMBLE.pack(MSG_RUMBLE, pad, large, small)


def decode_rumble(data: bytes) -> tuple[int, int, int] | None:
    if len(data) != RUMBLE_SIZE or data[0] != MSG_RUMBLE:
        return None
    _, pad, large, small = _RUMBLE.unpack(data)
    return pad, large, small


# --- JSON control messages -------------------------------------------------


def encode_hello(client: str, version: int = PROTOCOL_VERSION) -> str:
    return json.dumps({"type": "hello", "version": version, "client": client})


def parse_hello(text: str) -> dict | None:
    """Return the hello dict, or None if `text` is not a hello object."""
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if isinstance(obj, dict) and obj.get("type") == "hello":
        return obj
    return None


def hello_version_ok(hello: dict) -> bool:
    v = hello.get("version")
    return type(v) is int and v == PROTOCOL_VERSION


def encode_welcome(backend: str, max_pads: int = MAX_PADS) -> str:
    return json.dumps(
        {"type": "welcome", "version": PROTOCOL_VERSION, "backend": backend, "max_pads": max_pads}
    )
