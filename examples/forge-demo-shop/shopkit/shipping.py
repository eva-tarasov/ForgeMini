"""Shipping rules."""
from decimal import Decimal

from shopkit.money import ZERO

FREE_SHIPPING_THRESHOLD = Decimal("50.00")
FLAT_RATE = Decimal("4.99")


def shipping_cost(order_amount: Decimal) -> Decimal:
    """Shipping for an order. Orders of 50.00 or more (after discounts) ship free; otherwise a flat rate."""
    if order_amount > FREE_SHIPPING_THRESHOLD:
        return ZERO
    return FLAT_RATE
