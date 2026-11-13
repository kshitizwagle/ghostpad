import struct
import unittest

from ghostpad import protocol as p


class ProtocolTests(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(p.STATE_SIZE, 20)
        self.assertEqual(p.PING_SIZE, 13)
        self.assertEqual(p.PONG_SIZE, 17)
        self.assertEqual(p.PAD_GONE_SIZE, 2)
        self.assertEqual(p.RUMBLE_SIZE, 4)

    def test_state_roundtrip_and_layout(self):
        s = p.PadState(0xF3FF, 255, 7, -32767, 32767, -1, 1)
        data = p.encode_state(3, 65535, s, 0xDEADBEEF)
        self.assertEqual(len(data), 20)
        self.assertEqual(data[0], 1)
        self.assertEqual(data[1], 3)
        self.assertEqual(struct.unpack_from("<H", data, 2)[0], 65535)
        m = p.decode_binary(data)
        self.assertEqual(m, p.StateFrame(3, 65535, s, 0xDEADBEEF))

    def test_state_little_endian_bytes(self):
        data = p.encode_state(0, 0x0102, p.PadState(0x1000, 0, 0, 0x0100, 0, 0, 0), 0)
        self.assertEqual(data[2:4], b"\x02\x01")
        self.assertEqual(data[4:6], b"\x00\x10")
        self.assertEqual(data[8:10], b"\x00\x01")

    def test_ping_pong_roundtrip(self):
        m = p.decode_binary(p.encode_ping(42, 1234.5678))
        self.assertEqual(m, p.Ping(42, 1234.5678))
        self.assertEqual(p.decode_pong(p.encode_pong(42, 1234.5678, 77)), (42, 1234.5678, 77))

    def test_pad_gone_and_rumble(self):
        self.assertEqual(p.decode_binary(p.encode_pad_gone(2)), p.PadGone(2))
        self.assertEqual(p.decode_rumble(p.encode_rumble(1, 200, 100)), (1, 200, 100))

    def test_malformed(self):
        good = p.encode_state(0, 1, p.NEUTRAL)
        for bad in (b"", b"\x01", good[:-1], good + b"\x00", b"\x7f" + good[1:],
                    p.encode_state(4, 1, p.NEUTRAL), p.encode_state(255, 1, p.NEUTRAL),
                    p.encode_ping(1, 1.0)[:-1], p.encode_pad_gone(4), b"\x04",
                    p.encode_pong(1, 1.0, 1),  # server->client type, not accepted from clients
                    ):
            self.assertIsNone(p.decode_binary(bad), bad)

    def test_seq_newer(self):
        self.assertTrue(p.seq_newer(1, 0))
        self.assertFalse(p.seq_newer(0, 0))
        self.assertFalse(p.seq_newer(0, 1))
        self.assertTrue(p.seq_newer(0, 65535))  # wrap
        self.assertTrue(p.seq_newer(5, 65530))
        self.assertFalse(p.seq_newer(65530, 5))
        self.assertTrue(p.seq_newer(32768 + 0, 1))  # diff 32767
        self.assertFalse(p.seq_newer(32769, 1))  # diff 32768
        self.assertTrue(p.seq_newer(32767, 0))
        self.assertFalse(p.seq_newer(32768, 0))

    def test_json(self):
        h = p.parse_hello(p.encode_hello("web"))
        self.assertEqual(h["client"], "web")
        self.assertTrue(p.hello_version_ok(h))
        self.assertFalse(p.hello_version_ok({"type": "hello", "version": 2}))
        self.assertFalse(p.hello_version_ok({"type": "hello", "version": True}))
        self.assertFalse(p.hello_version_ok({"type": "hello"}))
        for bad in ("nope", "[]", '{"type":"x"}', "5"):
            self.assertIsNone(p.parse_hello(bad))
        self.assertEqual(p.parse_client_log(p.encode_client_log("hi there")), "hi there")
        self.assertEqual(p.parse_client_log(p.encode_client_log("x" * 600)), "x" * p.CLIENT_LOG_MAX)
        self.assertEqual(p.parse_client_log(p.encode_client_log("a\nb\x1b")), "a\\nb\\x1b")
        self.assertEqual(p.parse_client_log(p.encode_client_log("ünï")), "ünï")
        for bad in ("nope", p.encode_hello("x"), '{"type":"log"}', '{"type":"log","msg":5}', "[]"):
            self.assertIsNone(p.parse_client_log(bad))
        import json
        self.assertEqual(json.loads(p.encode_welcome("mock")),
                         {"type": "welcome", "version": 1, "backend": "mock", "max_pads": 4})


if __name__ == "__main__":
    unittest.main()
