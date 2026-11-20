"""Money formatting for the plain-language reason strings.

Lives here rather than in each module so a figure reads the same wherever it
appears. Scale is chosen from the magnitude: "PKR 0.03M" is harder to read
than "PKR 30,000", and "PKR 33,579,400" is harder than "PKR 33.6M".

Rounding is half away from zero, done on the exact binary value, which is what
ECMAScript's toFixed does. Python's own formatting rounds half to even, so
PKR 1,250,000 would print as 1.2M here and 1.3M in the browser, and the same
amount would appear with two different values in a tile and in the sentence
underneath it. The web shell mirrors this function; tests/test_frontend.py
checks the two agree on the boundary cases.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def _round1(value: float) -> str:
    """One decimal place, ties away from zero, matching JS toFixed(1)."""
    return str(Decimal(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _round0(value: float) -> str:
    return str(Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money(amount: float, currency: str = "PKR") -> str:
    a = abs(amount)
    sign = "-" if amount < 0 else ""
    if a >= 1_000_000:
        return f"{sign}{currency} {_round1(a / 1_000_000)}M"
    if a >= 1_000:
        return f"{sign}{currency} {int(_round0(a / 1_000)):,}K"
    return f"{sign}{currency} {int(_round0(a)):,}"


def qty(amount: float) -> str:
    return f"{int(_round0(amount)):,}"
