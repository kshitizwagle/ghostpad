"""Run: python3 -m unittest discover -s bench"""
import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bench  # noqa: E402


class StatsTests(unittest.TestCase):
    def test_summarize_and_jitter(self):
        s = bench.summarize([float(i) for i in range(100, 0, -1)])
        self.assertEqual((s["n"], s["min"], s["median"], s["p95"], s["p99"], s["max"]), (100, 1, 51, 96, 100, 100))
        self.assertIsNone(bench.summarize([])["median"])
        self.assertEqual(bench.jitter([1, 3, 2, 6]), 7 / 3)
        self.assertIsNone(bench.jitter([1]))

    def test_mask_roundtrip(self):
        m = b"\x01\x02\x03\x04"
        data = bytes(range(11))
        self.assertEqual(bench._mask(bench._mask(data, m), m), data)
        self.assertEqual(bench._mask(data, m), bytes(b ^ m[i % 4] for i, b in enumerate(data)))

    def test_spawn_smoke(self):
        args = bench.parse_args(["--spawn", "--duration", "0.6", "--rate", "100", "--ping-interval-ms", "50"])
        r = asyncio.run(bench.amain(args))
        self.assertGreater(r["pings"]["received"], 5)
        self.assertEqual(r["pings"]["lost"], 0)
        self.assertGreaterEqual(r["daemon_stats"]["recv_apply"]["count"], 50)
        self.assertEqual(r["daemon_stats"]["stale_drops"], 0)
        json.dumps(r)


if __name__ == "__main__":
    unittest.main()
