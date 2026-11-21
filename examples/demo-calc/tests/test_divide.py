import unittest

from calc import divide


class TestDivide(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(divide(9, 3), 3)

    def test_division_by_zero_raises(self):
        # Documented, relied upon by callers.
        with self.assertRaises(ZeroDivisionError):
            divide(1, 0)
