"""Hidden tests for examples/demo-calc: different inputs than the visible tests, so a hard-coded answer fails here.

Run with the patched repository on PYTHONPATH (see evalkit.runner). The agent never sees this file.
"""
import unittest

from calc import average, clamp, divide, median


class AverageOracle(unittest.TestCase):
    def test_values(self):
        self.assertAlmostEqual(average([10, 20, 40]), 70 / 3)
        self.assertEqual(average([-1, -2]), -1.5)
        self.assertEqual(average([0, 0, 1, 1]), 0.5)
        self.assertEqual(average([5]), 5)


class MedianOracle(unittest.TestCase):
    def test_values(self):
        self.assertEqual(median([5, 3, 1, 2, 4]), 3)
        self.assertEqual(median([10, 2]), 6)
        self.assertEqual(median([1, 1, 1, 1]), 1)
        self.assertEqual(median([9, -1, 3, 7]), 5)

    def test_input_is_not_mutated(self):
        data = [3, 1, 2, 9]
        median(data)
        self.assertEqual(data, [3, 1, 2, 9])


class ClampOracle(unittest.TestCase):
    def test_values(self):
        self.assertEqual(clamp(5, 0, 3), 3)
        self.assertEqual(clamp(-5, 0, 3), 0)
        self.assertEqual(clamp(1, 0, 3), 1)
        self.assertEqual(clamp(0, 0, 3), 0)
        self.assertEqual(clamp(3, 0, 3), 3)
        self.assertEqual(clamp(-7, -10, -5), -7)


class DivideContract(unittest.TestCase):
    """What must NOT be changed by an eager fix: the documented behaviour of divide()."""

    def test_division_by_zero_still_raises(self):
        with self.assertRaises(ZeroDivisionError):
            divide(1, 0)

    def test_regular_division(self):
        self.assertEqual(divide(9, 3), 3)
