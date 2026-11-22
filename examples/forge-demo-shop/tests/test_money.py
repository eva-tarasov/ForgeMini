import unittest
from decimal import Decimal

from shopkit.money import money


class TestMoney(unittest.TestCase):
    def test_rounds_half_up(self):
        self.assertEqual(money("2.675"), Decimal("2.68"))

    def test_keeps_two_places(self):
        self.assertEqual(money(5), Decimal("5.00"))
