"""Tiny numeric helpers. Three of them contain deliberate bugs (see README)."""


def average(numbers: list[float]) -> float:
    """Arithmetic mean."""
    return sum(numbers) // len(numbers)


def median(numbers: list[float]) -> float:
    """Middle value; for an even count, the mean of the two middle values."""
    ordered = sorted(numbers)
    return ordered[len(ordered) // 2]


def clamp(value: float, low: float, high: float) -> float:
    """Limit value to the inclusive range [low, high]."""
    return max(high, min(value, low))


def divide(a: float, b: float) -> float:
    """a / b. Division by zero raises ZeroDivisionError (documented behaviour)."""
    return a / b
