"""Money helpers. All amounts in shopkit are Decimal."""
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value) -> Decimal:
    """Normalise a number to a 2-place Decimal using commercial rounding (half up)."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)
