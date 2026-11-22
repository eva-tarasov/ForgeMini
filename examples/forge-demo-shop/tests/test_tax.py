import unittest
from decimal import Decimal

from shopkit.tax import vat


class TestVat(unittest.TestCase):
    def test_simple_rate(self):
        self.assertEqual(vat(Decimal("100.00"), Decimal("0.20")), Decimal("20.00"))

    def test_zero_amount(self):
        self.assertEqual(vat(Decimal("0.00"), Decimal("0.20")), Decimal("0.00"))
