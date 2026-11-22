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


# The two notations this formatter writes, named as an organisation's
# settings name them and as engine.country reports Scale.grouping. One
# vocabulary for the same two choices: a third is how the settings
# screen and the engine end up disagreeing about which country
# somebody is in.
SCALES = ("western", "indian")


def money(amount: float, currency: str = "PKR",
          scale: str = "western") -> str:
    """A figure as it belongs in a sentence.

    `scale` is "western" (K/M) or "indian" (k/L/cr). It matters more
    than it looks: a Pakistani reader sees 66.0L in the column and
    PKR 6.6M in the sentence beside it and has to stop and work out
    whether they are the same number. They are, and nobody should
    have to check.

    The default stays western so that a caller which has not been
    given an organisation's settings keeps the behaviour it had,
    rather than silently switching notation on a business in Dubai.
    """
    a = abs(amount)
    sign = "-" if amount < 0 else ""
    if scale == "indian":
        if a >= 1_00_00_000:
            return f"{sign}{currency} {_round1(a / 1_00_00_000)}cr"
        if a >= 1_00_000:
            return f"{sign}{currency} {_round1(a / 1_00_000)}L"
        if a >= 1_000:
            return f"{sign}{currency} {int(_round0(a / 1_000)):,}k"
        return f"{sign}{currency} {int(_round0(a)):,}"
    if a >= 1_000_000:
        return f"{sign}{currency} {_round1(a / 1_000_000)}M"
    if a >= 1_000:
        return f"{sign}{currency} {int(_round0(a / 1_000)):,}K"
    return f"{sign}{currency} {int(_round0(a)):,}"


def qty(amount: float) -> str:
    return f"{int(_round0(amount)):,}"
