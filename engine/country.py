"""Country packs: everything that is true of a place rather than of a
business.

This exists because the first version of Landed was built for one
country and said so in a dozen places -- lakh and crore in the
formatter, PKR in the labels, a Sindh minimum wage in the engine. None
of that is wrong; all of it was in the wrong place. A business in
Dubai or Nairobi or Dhaka has the same inventory problem and a
different set of facts about money.

A pack is a starting point, not a rule. Everything in one is written
into the organisation's own settings when it is applied, and every
value stays editable afterwards -- the pack saves somebody twenty
minutes of typing, it does not decide anything for them.

Number scale is the part people miss. Half the world groups digits in
thousands and half in lakh and crore, and a figure shown the wrong way
is not slightly awkward, it is unreadable: 1,20,00,000 and 12,000,000
are the same number and only one of them is legible to a given
reader.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Scale:
    """How large numbers are grouped and named."""

    #  [(divisor, suffix, decimal places)], largest first.
    steps: tuple[tuple[float, str, int], ...]
    #  Digit grouping for the raw form: 3 is 1,234,567; "indian" is
    #  12,34,567.
    grouping: str = "western"


WESTERN = Scale(
    steps=((1e9, "bn", 2), (1e6, "m", 2), (1e3, "k", 0)),
    grouping="western",
)

SOUTH_ASIAN = Scale(
    steps=((1e7, "cr", 2), (1e5, "L", 1), (1e3, "k", 0)),
    grouping="indian",
)


@dataclass(frozen=True)
class Pack:
    """A country's starting point.

    Nothing here is enforced. Applying a pack writes these values into
    the organisation's settings, where each one can then be changed.
    """

    code: str
    name: str
    currency: str
    currency_name: str
    scale: Scale
    date_format: str = "YYYY-MM-DD"
    fiscal_year_starts: int = 1          # month number
    # A reasonable starting cost of money for a business borrowing in
    # this currency. Wrong for any particular business, and the point
    # of a default is that it is edited.
    cost_of_capital_annual: float = 0.12
    # What a line is expected to cover, and the point past which money
    # is counted as not working. Both are policy, not geography, so
    # they are the same everywhere until somebody changes them.
    target_cover_months: float = 3.0
    excess_cover_months: float = 6.0
    # Cash floor, in the local currency, scaled to what a small
    # importer there would hold.
    cash_buffer: float = 0.0
    notes: str = ""


PACKS: dict[str, Pack] = {
    "PK": Pack("PK", "Pakistan", "PKR", "Pakistani rupee", SOUTH_ASIAN,
               cost_of_capital_annual=0.22, cash_buffer=2_000_000,
               fiscal_year_starts=7,
               notes="Fiscal year starts in July. Lending rates are high, "
                     "so money sitting in stock costs more than it does "
                     "in most places."),
    "IN": Pack("IN", "India", "INR", "Indian rupee", SOUTH_ASIAN,
               cost_of_capital_annual=0.11, cash_buffer=1_000_000,
               fiscal_year_starts=4,
               notes="Fiscal year starts in April."),
    "BD": Pack("BD", "Bangladesh", "BDT", "Bangladeshi taka", SOUTH_ASIAN,
               cost_of_capital_annual=0.13, cash_buffer=1_500_000,
               fiscal_year_starts=7),
    "LK": Pack("LK", "Sri Lanka", "LKR", "Sri Lankan rupee", SOUTH_ASIAN,
               cost_of_capital_annual=0.14, cash_buffer=3_000_000),
    "AE": Pack("AE", "United Arab Emirates", "AED", "UAE dirham", WESTERN,
               cost_of_capital_annual=0.07, cash_buffer=100_000),
    "SA": Pack("SA", "Saudi Arabia", "SAR", "Saudi riyal", WESTERN,
               cost_of_capital_annual=0.07, cash_buffer=100_000),
    "KE": Pack("KE", "Kenya", "KES", "Kenyan shilling", WESTERN,
               cost_of_capital_annual=0.16, cash_buffer=3_000_000),
    "NG": Pack("NG", "Nigeria", "NGN", "Nigerian naira", WESTERN,
               cost_of_capital_annual=0.25, cash_buffer=20_000_000),
    "ZA": Pack("ZA", "South Africa", "ZAR", "South African rand", WESTERN,
               cost_of_capital_annual=0.12, cash_buffer=200_000,
               fiscal_year_starts=3),
    "GB": Pack("GB", "United Kingdom", "GBP", "Pound sterling", WESTERN,
               cost_of_capital_annual=0.08, cash_buffer=25_000,
               fiscal_year_starts=4),
    "US": Pack("US", "United States", "USD", "US dollar", WESTERN,
               cost_of_capital_annual=0.09, cash_buffer=25_000),
    "AU": Pack("AU", "Australia", "AUD", "Australian dollar", WESTERN,
               cost_of_capital_annual=0.08, cash_buffer=40_000,
               fiscal_year_starts=7),
    "MY": Pack("MY", "Malaysia", "MYR", "Malaysian ringgit", WESTERN,
               cost_of_capital_annual=0.07, cash_buffer=100_000),
    "ID": Pack("ID", "Indonesia", "IDR", "Indonesian rupiah", WESTERN,
               cost_of_capital_annual=0.11, cash_buffer=300_000_000),
    "EG": Pack("EG", "Egypt", "EGP", "Egyptian pound", WESTERN,
               cost_of_capital_annual=0.24, cash_buffer=1_000_000,
               fiscal_year_starts=7),
}

DEFAULT = "PK"


def pack(code: str) -> Pack:
    return PACKS.get((code or "").upper(), PACKS[DEFAULT])


def as_settings(code: str) -> dict:
    """A pack flattened into the settings an organisation stores.

    Written in rather than referenced, so that changing a pack here
    never silently changes a figure on somebody's screen. They took a
    starting point; they did not subscribe to one.
    """
    p = pack(code)
    return {
        "country": p.code,
        "currency": p.currency,
        "scale": p.scale.grouping,
        "date_format": p.date_format,
        "fiscal_year_starts": p.fiscal_year_starts,
        "cost_of_capital_annual": p.cost_of_capital_annual,
        "target_cover_months": p.target_cover_months,
        "excess_cover_months": p.excess_cover_months,
        "cash_buffer": p.cash_buffer,
    }


def scale_for(grouping: str) -> Scale:
    return SOUTH_ASIAN if (grouping or "").lower() == "indian" else WESTERN


def listing() -> list[dict]:
    """Every pack, for a dropdown. Sorted by name, not by code."""
    return [
        {"code": p.code, "name": p.name, "currency": p.currency,
         "currency_name": p.currency_name, "scale": p.scale.grouping,
         "notes": p.notes}
        for p in sorted(PACKS.values(), key=lambda x: x.name)
    ]
