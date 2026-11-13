"""Tray icon artwork: a controller silhouette, drawn in code (no image files, any OS).

Pure functions, used by tray.py (live icon) and packaging/build_exe.py (the exe's .ico).
Shapes are signed distance functions in a 32x32 design space, anti-aliased by coverage.
"""

from __future__ import annotations

import math
import struct
import zlib

# State colours (RGB). Chosen to read on both light and dark taskbars.
COLORS: dict[str, tuple[int, int, int]] = {
    "idle": (138, 143, 152),       # listening, no client
    "connected": (46, 160, 67),    # a client owns the pads
    "mock": (210, 153, 34),        # mock backend: no real virtual pad
    "error": (215, 58, 73),        # daemon stopped
}


def _round_rect(x: float, y: float, cx: float, cy: float, hw: float, hh: float, r: float) -> float:
    qx, qy = abs(x - cx) - hw + r, abs(y - cy) - hh + r
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outside + min(max(qx, qy), 0.0) - r


def _circle(x: float, y: float, cx: float, cy: float, r: float) -> float:
    return math.hypot(x - cx, y - cy) - r


def _shape(x: float, y: float) -> float:
    """Signed distance (design units) to the controller; negative inside."""
    body = _round_rect(x, y, 16.0, 14.5, 12.5, 6.5, 6.0)
    grips = min(_circle(x, y, 9.0, 21.0, 5.5), _circle(x, y, 23.0, 21.0, 5.5))
    solid = min(body, grips)
    dpad = min(_round_rect(x, y, 9.5, 15.0, 3.6, 1.2, 0.6), _round_rect(x, y, 9.5, 15.0, 1.2, 3.6, 0.6))
    buttons = min(_circle(x, y, 21.5, 13.0, 1.7), _circle(x, y, 24.5, 16.5, 1.7))
    return max(solid, -min(dpad, buttons))


def render_rgba(size: int, rgb: tuple[int, int, int]) -> bytes:
    """size x size RGBA pixels, top-down rows, straight (non-premultiplied) alpha."""
    unit = 32.0 / size  # design units per pixel
    r, g, b = rgb
    out = bytearray(size * size * 4)
    i = 0
    for py in range(size):
        y = (py + 0.5) * unit
        for px in range(size):
            d = _shape((px + 0.5) * unit, y) / unit  # in pixels
            a = 0.5 - d
            alpha = 0 if a <= 0 else 255 if a >= 1 else int(a * 255 + 0.5)
            if alpha:
                out[i:i + 4] = bytes((r, g, b, alpha))
            i += 4
    return bytes(out)


def rgba_to_bgra(rgba: bytes) -> bytes:
    """Swap channels for a Windows 32-bit DIB."""
    out = bytearray(rgba)
    out[0::4], out[2::4] = rgba[2::4], rgba[0::4]
    return bytes(out)


def png_bytes(size: int, rgba: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    row = size * 4
    raw = b"".join(b"\x00" + rgba[y * row:(y + 1) * row] for y in range(size))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def ico_bytes(state: str = "connected", sizes: tuple[int, ...] = (16, 20, 24, 32, 48, 64, 256)) -> bytes:
    """A .ico with PNG-compressed images (supported since Windows Vista)."""
    images = [png_bytes(s, render_rgba(s, COLORS[state])) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = len(head) + 16 * len(images)
    entries, blobs = b"", b""
    for s, img in zip(sizes, images):
        dim = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
        blobs += img
    return head + entries + blobs
