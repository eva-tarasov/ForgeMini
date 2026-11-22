"""Hidden tests for examples/forge-demo-shop, written from docs/pricing-rules.md (not from the visible tests).

BaselineBehaviour pins things that work today: a "fix" that breaks them is a regression, not a fix.
"""
import unittest
from decimal import Decimal as D

from shopkit.cart import Cart, checkout
from shopkit.discounts import Coupon, apply_coupons, discount_amount
from shopkit.money import money
from shopkit.shipping import shipping_cost
from shopkit.tax import vat


class ShippingOracle(unittest.TestCase):
    def test_threshold_is_inclusive(self):
        self.assertEqual(shipping_cost(D("50.00")), D("0.00"))
        self.assertEqual(shipping_cost(D("50.01")), D("0.00"))
        self.assertEqual(shipping_cost(D("49.99")), D("4.99"))
        self.assertEqual(shipping_cost(D("100.00")), D("0.00"))


class VatOracle(unittest.TestCase):
    def test_half_cents_round_up(self):
        self.assertEqual(vat(D("1.25"), D("0.10")), D("0.13"))     # 0.125
        self.assertEqual(vat(D("0.90"), D("0.05")), D("0.05"))     # 0.045
        self.assertEqual(vat(D("3.65"), D("0.10")), D("0.37"))     # 0.365

    def test_result_has_two_places(self):
        self.assertEqual(str(vat(D("10.00"), D("0.20"))), "2.00")


class CouponOracle(unittest.TestCase):
    def test_only_the_best_coupon_applies(self):
        ten_percent, five_off = Coupon("A", "percent", D("10")), Coupon("B", "fixed", D("5"))
        self.assertEqual(apply_coupons([ten_percent, five_off], D("100.00")), D("10.00"))
        self.assertEqual(apply_coupons([five_off, ten_percent], D("100.00")), D("10.00"))
        self.assertEqual(apply_coupons([Coupon("C", "fixed", D("20")), ten_percent], D("100.00")), D("20.00"))

    def test_no_coupons_no_discount(self):
        self.assertEqual(apply_coupons([], D("80.00")), D("0.00"))

    def test_discount_never_exceeds_subtotal(self):
        self.assertEqual(apply_coupons([Coupon("X", "fixed", D("500"))], D("30.00")), D("30.00"))


class CheckoutOracle(unittest.TestCase):
    def test_exactly_fifty_no_coupons(self):
        cart = Cart()
        cart.add("a", D("25.00"), 2)
        r = checkout(cart)
        self.assertEqual((r.subtotal, r.discount, r.shipping, r.vat, r.total),
                         (D("50.00"), D("0.00"), D("0.00"), D("10.00"), D("60.00")))

    def test_two_coupons_use_the_best_one_and_shipping_follows_the_discounted_amount(self):
        cart = Cart()
        cart.add("a", D("60.00"))
        r = checkout(cart, [Coupon("A", "percent", D("10")), Coupon("B", "fixed", D("5"))])
        self.assertEqual((r.discount, r.shipping), (D("6.00"), D("0.00")))      # 54.00 >= 50.00 -> free
        self.assertEqual(r.total, D("64.80"))


class BaselineBehaviour(unittest.TestCase):
    def test_money_is_commercial_rounding(self):
        self.assertEqual(money("0.125"), D("0.13"))
        self.assertEqual(money("2.675"), D("2.68"))

    def test_flat_shipping_below_threshold(self):
        self.assertEqual(shipping_cost(D("10.00")), D("4.99"))

    def test_vat_plain_cases(self):
        self.assertEqual(vat(D("100.00"), D("0.20")), D("20.00"))
        self.assertEqual(vat(D("0.00"), D("0.20")), D("0.00"))

    def test_single_coupons(self):
        self.assertEqual(discount_amount(Coupon("P", "percent", D("15")), D("40.00")), D("6.00"))
        self.assertEqual(discount_amount(Coupon("F", "fixed", D("7.50")), D("40.00")), D("7.50"))
        self.assertEqual(apply_coupons([Coupon("P", "percent", D("15"))], D("40.00")), D("6.00"))

    def test_cart_subtotal(self):
        cart = Cart()
        cart.add("a", D("19.99"), 3)
        self.assertEqual(cart.subtotal(), D("59.97"))
