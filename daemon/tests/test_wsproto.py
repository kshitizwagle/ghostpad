import asyncio
import unittest

from ghostpad import wsproto as ws
from tests.wsclient import build_frame


class FakeWriter:
    def __init__(self):
        self.buf = b""
        self.closing = False

    def write(self, d):
        self.buf += d

    async def drain(self):
        pass

    def is_closing(self):
        return self.closing

    def close(self):
        self.closing = True

    def get_extra_info(self, name):
        return None


def reader_with(data: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


class WSProtoTests(unittest.IsolatedAsyncioTestCase):
    def test_accept_key_rfc_example(self):
        self.assertEqual(ws.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_encode_frame_lengths(self):
        self.assertEqual(ws.encode_frame(ws.OP_BIN, b"abc"), b"\x82\x03abc")
        f = ws.encode_frame(ws.OP_BIN, b"x" * 200)
        self.assertEqual(f[:4], b"\x82\x7e\x00\xc8")
        f = ws.encode_frame(ws.OP_BIN, b"x" * 70000)
        self.assertEqual(f[1], 127)

    def test_unmask_roundtrip(self):
        mask = b"\x01\x02\x03\x04"
        for n in (0, 1, 3, 4, 5, 100):
            data = bytes(range(n))
            self.assertEqual(ws.unmask(ws.unmask(data, mask), mask), data)

    async def test_read_masked_frame(self):
        r = reader_with(build_frame(0x2, b"hello"))
        self.assertEqual(await ws.read_frame(r), (True, 2, b"hello"))

    async def test_extended_lengths(self):
        for n in (126, 65535, 65536):
            r = reader_with(build_frame(0x2, b"a" * n))
            self.assertEqual((await ws.read_frame(r))[2], b"a" * n)

    async def test_unmasked_rejected(self):
        with self.assertRaises(ws.WSError) as cm:
            await ws.read_frame(reader_with(build_frame(0x2, b"x", masked=False)))
        self.assertEqual(cm.exception.code, 1002)

    async def test_too_large_rejected(self):
        with self.assertRaises(ws.WSError) as cm:
            await ws.read_frame(reader_with(build_frame(0x2, b"a" * (ws.MAX_FRAME + 1))))
        self.assertEqual(cm.exception.code, 1009)

    async def test_bad_frames(self):
        for frame in (build_frame(0x2, b"x", rsv=1), build_frame(0x3, b"x"),
                      build_frame(0x9, b"x", fin=False), build_frame(0x9, b"x" * 126)):
            with self.assertRaises(ws.WSError):
                await ws.read_frame(reader_with(frame))

    async def test_recv_ping_pong_text_binary(self):
        data = (build_frame(0x9, b"hi") + build_frame(0xA, b"") + build_frame(0x1, "héllo".encode())
                + build_frame(0x2, b"\x01\x02"))
        w = FakeWriter()
        conn = ws.WebSocket(reader_with(data), w)
        self.assertEqual(await conn.recv(), (ws.OP_TEXT, "héllo"))
        self.assertEqual(w.buf, b"\x8a\x02hi")  # auto pong
        self.assertEqual(await conn.recv(), (ws.OP_BIN, b"\x01\x02"))
        self.assertIsNone(await conn.recv())  # EOF

    async def test_fragmentation_reassembled(self):
        data = (build_frame(0x1, b"he", fin=False) + build_frame(0x9, b"p")
                + build_frame(0x0, b"l", fin=False) + build_frame(0x0, b"lo"))
        conn = ws.WebSocket(reader_with(data), FakeWriter())
        self.assertEqual(await conn.recv(), (ws.OP_TEXT, "hello"))

    async def test_fragmentation_errors(self):
        for data in (build_frame(0x0, b"x"),  # stray continuation
                     build_frame(0x1, b"a", fin=False) + build_frame(0x2, b"b")):
            w = FakeWriter()
            conn = ws.WebSocket(reader_with(data), w)
            self.assertIsNone(await conn.recv())
            self.assertEqual(w.buf[2:4], b"\x03\xea")  # 1002

    async def test_oversize_fragmented_message(self):
        half = b"a" * 40000
        data = build_frame(0x2, half, fin=False) + build_frame(0x0, half)
        w = FakeWriter()
        conn = ws.WebSocket(reader_with(data), w)
        self.assertIsNone(await conn.recv())
        self.assertEqual(w.buf[2:4], b"\x03\xf1")  # 1009

    async def test_invalid_utf8(self):
        w = FakeWriter()
        conn = ws.WebSocket(reader_with(build_frame(0x1, b"\xff\xfe")), w)
        self.assertIsNone(await conn.recv())
        self.assertEqual(w.buf[2:4], b"\x03\xef")  # 1007

    async def test_close_echo(self):
        w = FakeWriter()
        conn = ws.WebSocket(reader_with(build_frame(0x8, b"\x03\xe8")), w)
        self.assertIsNone(await conn.recv())
        self.assertEqual(conn.close_code, 1000)
        self.assertEqual(w.buf[:4], b"\x88\x02\x03\xe8")


if __name__ == "__main__":
    unittest.main()
