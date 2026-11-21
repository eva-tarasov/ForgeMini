import unittest

from calc import median


class TestMedian(unittest.TestCase):
    def test_odd_count(self):
        self.assertEqual(median([3, 1, 2]), 2)

    def test_even_count_is_mean_of_middle_two(self):
        self.assertEqual(median([4, 1, 3, 2]), 2.5)

    def test_does_not_mutate_input(self):
        data = [3, 1, 2]
        median(data)
        self.assertEqual(data, [3, 1, 2])
