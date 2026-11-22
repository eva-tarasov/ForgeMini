import unittest
from decimal import Decimal

from shopkit.discounts import Coupon, apply_coupons, discount_amount


class TestDiscounts(unittest.TestCase):
    def test_percent_coupon(self):
        self.assertEqual(discount_amount(Coupon("P10", "percent", Decimal("10")), Decimal("80.00")), Decimal("8.00"))

    def test_fixed_coupon_is_capped_at_subtotal(self):
        self.assertEqual(discount_amount(Coupon("F50", "fixed", Decimal("50")), Decimal("20.00")), Decimal("20.00"))

    def test_single_coupon(self):
        self.assertEqual(apply_coupons([Coupon("P10", "percent", Decimal("10"))], Decimal("100.00")), Decimal("10.00"))

    def test_no_coupons(self):
        self.assertEqual(apply_coupons([], Decimal("100.00")), Decimal("0.00"))

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            discount_amount(Coupon("X", "bogus", Decimal("1")), Decimal("10.00"))
