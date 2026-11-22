import unittest
from decimal import Decimal

from shopkit.cart import Cart, checkout


class TestCart(unittest.TestCase):
    def test_subtotal(self):
        cart = Cart()
        cart.add("mug", Decimal("12.50"), qty=2)
        cart.add("tea", Decimal("3.20"))
        self.assertEqual(cart.subtotal(), Decimal("28.20"))

    def test_qty_must_be_positive(self):
        with self.assertRaises(ValueError):
            Cart().add("mug", Decimal("1.00"), qty=0)

    def test_small_order_checkout(self):
        cart = Cart()
        cart.add("tea", Decimal("10.00"))
        r = checkout(cart)
        self.assertEqual((r.subtotal, r.discount, r.shipping), (Decimal("10.00"), Decimal("0.00"), Decimal("4.99")))
        self.assertEqual(r.vat, Decimal("3.00"))          # 20 % of 14.99 = 2.998 -> 3.00
        self.assertEqual(r.total, Decimal("17.99"))
