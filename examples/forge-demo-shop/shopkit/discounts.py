"""Coupons and discounts."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from shopkit.money import ZERO, money


@dataclass(frozen=True)
class Coupon:
    code: str
    kind: str          # "percent" or "fixed"
    value: Decimal


def discount_amount(coupon: Coupon, subtotal: Decimal) -> Decimal:
    """Discount in money that one coupon gives on `subtotal` (never more than the subtotal)."""
    if coupon.kind == "percent":
        return money(subtotal * coupon.value / 100)
    if coupon.kind == "fixed":
        return min(money(coupon.value), subtotal)
    raise ValueError(f"unknown coupon kind: {coupon.kind!r}")


def apply_coupons(coupons: Iterable[Coupon], subtotal: Decimal) -> Decimal:
    """Total discount for an order."""
    total = ZERO
    for coupon in coupons:
        total += discount_amount(coupon, subtotal)
    return min(total, subtotal)
