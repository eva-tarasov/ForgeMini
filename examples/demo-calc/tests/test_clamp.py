import unittest

from calc import clamp


class TestClamp(unittest.TestCase):
    def test_inside_range(self):
        self.assertEqual(clamp(5, 0, 10), 5)

    def test_below_range(self):
        self.assertEqual(clamp(-5, 0, 10), 0)

    def test_above_range(self):
        self.assertEqual(clamp(50, 0, 10), 10)
