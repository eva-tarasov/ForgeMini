import unittest
from decimal import Decimal

from shopkit.shipping import FLAT_RATE, shipping_cost


class TestShipping(unittest.TestCase):
    def test_small_order_pays_flat_rate(self):
        self.assertEqual(shipping_cost(Decimal("19.90")), FLAT_RATE)

    def test_large_order_ships_free(self):
        self.assertEqual(shipping_cost(Decimal("120.00")), Decimal("0.00"))
