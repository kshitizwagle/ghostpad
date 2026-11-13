import unittest

from ghostpad.__main__ import parse_args


class CliTests(unittest.TestCase):
    def test_trace_flag(self):
        self.assertFalse(parse_args([]).trace)
        self.assertTrue(parse_args(["--trace"]).trace)

    def test_defaults(self):
        a = parse_args([])
        self.assertEqual((a.backend, a.port, a.token), ("auto", 47800, None))


if __name__ == "__main__":
    unittest.main()
