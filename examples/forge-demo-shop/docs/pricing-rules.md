# Pricing rules

All amounts are `Decimal`, prices are **VAT-exclusive**. Order of calculation in `checkout()`:

1. **Subtotal** = sum of `unit_price * qty`.
2. **Discount** (see below) is subtracted from the subtotal; the result never goes below zero.
3. **Shipping** is decided on the amount *after* discounts.
4. **VAT** is charged on (discounted subtotal + shipping).
5. **Total** = discounted subtotal + shipping + VAT.

## Coupons
Only the **single best** coupon applies - coupons do **not** stack. "Best" means the largest discount in money for this
subtotal. Percent coupons: `subtotal * value / 100`. Fixed coupons: `value`, but never more than the subtotal.

## Shipping
Flat rate 4.99. Orders of **50.00 or more** (after discounts) ship free.

## Rounding
Money is rounded to cents with **commercial rounding (half up)**: 0.125 -> 0.13, 2.675 -> 2.68. This applies to every
computed amount (discounts and VAT). Banker's rounding (half to even) is not used anywhere.
