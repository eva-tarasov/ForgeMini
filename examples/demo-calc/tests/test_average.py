import unittest

from calc import average


class TestAverage(unittest.TestCase):
    def test_integers_with_fractional_mean(self):
        self.assertEqual(average([1, 2]), 1.5)

    def test_exact_mean(self):
        self.assertEqual(average([2, 4, 6]), 4)

    def test_single_value(self):
        self.assertEqual(average([7]), 7)
