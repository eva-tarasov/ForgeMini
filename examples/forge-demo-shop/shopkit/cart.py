"""Cart and checkout."""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from shopkit.discounts import Coupon, apply_coupons
from shopkit.money import ZERO, money
from shopkit.shipping import shipping_cost
from shopkit.tax import vat

DEFAULT_VAT_RATE = Decimal("0.20")


@dataclass
class Cart:
    items: list = field(default_factory=list)   # (sku, unit_price, qty)

    def add(self, sku: str, unit_price: Decimal, qty: int = 1) -> None:
        if qty < 1:
            raise ValueError("qty must be at least 1")
        self.items.append((sku, Decimal(unit_price), qty))

    def subtotal(self) -> Decimal:
        return money(sum((price * qty for _, price, qty in self.items), ZERO))


@dataclass(frozen=True)
class Receipt:
    subtotal: Decimal
    discount: Decimal
    shipping: Decimal
    vat: Decimal
    total: Decimal


def checkout(cart: Cart, coupons: Iterable[Coupon] = (), vat_rate: Decimal = DEFAULT_VAT_RATE) -> Receipt:
    subtotal = cart.subtotal()
    discount = apply_coupons(list(coupons), subtotal)
    discounted = subtotal - discount
    shipping = shipping_cost(discounted)
    tax = vat(discounted + shipping, vat_rate)
    return Receipt(subtotal, discount, shipping, tax, discounted + shipping + tax)
