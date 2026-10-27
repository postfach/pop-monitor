"""Picking plays spread over a list:  python3 tools/test_spread.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import receiver as rx  # noqa: E402


class Spread(unittest.TestCase):
    def test_counts(self):
        ps = list(range(10))
        self.assertEqual(rx._spread(ps, 3), [0, 4, 9])
        self.assertEqual(rx._spread(ps, 1), [5])          # filling the last place of a sample (was a crash)
        self.assertEqual(rx._spread(ps, 0), [])
        self.assertEqual(rx._spread(ps, -2), [])
        self.assertEqual(rx._spread(ps[:2], 5), [0, 1])
        self.assertEqual(rx._spread([], 1), [])


if __name__ == "__main__":
    unittest.main()
