# shopkit

Tiny order-pricing library: cart totals, coupons, shipping and VAT. Pure standard library, money is `Decimal`.

```python
from decimal import Decimal
from shopkit.cart import Cart, checkout
from shopkit.discounts import Coupon

cart = Cart()
cart.add("mug", Decimal("12.50"), qty=4)
receipt = checkout(cart, coupons=[Coupon("WELCOME10", "percent", Decimal("10"))])
print(receipt.total)
```

Pricing rules are written down in [docs/pricing-rules.md](docs/pricing-rules.md) - they are the source of truth
when code and a bug report disagree.

## Tests
```
python -m unittest
```
