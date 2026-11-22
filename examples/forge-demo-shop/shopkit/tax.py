"""VAT calculation."""
from decimal import Decimal


def vat(amount: Decimal, rate: Decimal) -> Decimal:
    """VAT charged on `amount`, rounded to cents. `rate` is a fraction, e.g. Decimal("0.20") for 20 %."""
    return round(amount * rate, 2)
