"""Regression tests for reported issues. Each class reproduces one bug report; they fail until the bug is fixed."""
import unittest
from decimal import Decimal

from shopkit.cart import Cart, checkout
from shopkit.discounts import Coupon, apply_coupons
from shopkit.shipping import shipping_cost
from shopkit.tax import vat


class Issue1FreeShippingAtThreshold(unittest.TestCase):
    """#1: an order of exactly 50.00 is charged shipping, although the rules say 'ships free from 50.00'."""

    def test_exactly_at_threshold_ships_free(self):
        self.assertEqual(shipping_cost(Decimal("50.00")), Decimal("0.00"))

    def test_checkout_of_exactly_fifty_has_no_shipping(self):
        cart = Cart()
        cart.add("kettle", Decimal("25.00"), qty=2)
        self.assertEqual(checkout(cart).shipping, Decimal("0.00"))


class Issue2VatRounding(unittest.TestCase):
    """#2: VAT is off by one cent on half-cent amounts (banker's rounding instead of commercial)."""

    def test_half_cent_rounds_up(self):
        self.assertEqual(vat(Decimal("2.50"), Decimal("0.05")), Decimal("0.13"))      # 0.125 -> 0.13

    def test_other_half_cent(self):
        self.assertEqual(vat(Decimal("0.30"), Decimal("0.15")), Decimal("0.05"))      # 0.045 -> 0.05


class Issue3CouponsDoNotStack(unittest.TestCase):
    """#3: two coupons are added together, but only the best one is supposed to apply."""

    def test_only_best_coupon_applies(self):
        coupons = [Coupon("P10", "percent", Decimal("10")), Coupon("F15", "fixed", Decimal("15"))]
        self.assertEqual(apply_coupons(coupons, Decimal("100.00")), Decimal("15.00"))

    def test_best_is_chosen_by_money_not_by_kind(self):
        coupons = [Coupon("P20", "percent", Decimal("20")), Coupon("F5", "fixed", Decimal("5"))]
        self.assertEqual(apply_coupons(coupons, Decimal("100.00")), Decimal("20.00"))
