"""Country packs: a starting point, never a rule.

The product was built for one country and said so in a dozen places --
lakh and crore in the formatter, PKR in the labels, a Sindh minimum
wage in the engine. None of it wrong; all of it in the wrong place.
These pin that it is now configuration.
"""

from __future__ import annotations

import pytest

from engine.country import (DEFAULT, PACKS, Pack, as_settings, listing,
                            pack, scale_for)


def test_every_pack_is_complete():
    for code, p in PACKS.items():
        assert p.code == code
        assert len(p.currency) == 3 and p.currency.isupper()
        assert p.currency_name
        assert p.scale.steps
        assert 1 <= p.fiscal_year_starts <= 12
        assert 0 <= p.cost_of_capital_annual <= 1


def test_an_unknown_country_falls_back_rather_than_raising():
    """A bad code in a settings column must not take the screen down."""
    assert pack("ZZ").code == DEFAULT
    assert pack("").code == DEFAULT
    assert pack(None).code == DEFAULT


def test_south_asian_and_western_group_numbers_differently():
    """Not cosmetic. 1,20,00,000 and 12,000,000 are the same number and
    only one of them is legible to a given reader."""
    south = scale_for("indian")
    west = scale_for("western")
    assert south.steps[0][1] == "cr"
    assert west.steps[0][1] == "bn"
    assert south is not west


def test_the_pack_decides_the_scale_not_the_currency():
    """A business in Dubai billing in rupees still reads in thousands,
    and one in Karachi billing in dollars still reads in lakh."""
    assert pack("PK").scale.grouping == "indian"
    assert pack("AE").scale.grouping == "western"
    assert pack("IN").scale.grouping == "indian"
    assert pack("GB").scale.grouping == "western"


def test_applying_a_pack_produces_plain_settings():
    s = as_settings("AE")
    assert s["currency"] == "AED"
    assert s["scale"] == "western"
    assert s["country"] == "AE"
    # Values, not a reference to the pack. Changing a pack in the code
    # later must not move a figure on somebody's screen.
    assert all(not callable(v) for v in s.values())


def test_policy_defaults_are_the_same_everywhere():
    """Cover and excess are policy, not geography. Only the money
    facts -- currency, scale, cost of capital, cash floor -- differ."""
    covers = {as_settings(c)["target_cover_months"] for c in PACKS}
    assert covers == {3.0}


def test_the_listing_is_sorted_by_name_not_code():
    names = [c["name"] for c in listing()]
    assert names == sorted(names)
    assert len(names) == len(PACKS)


def test_nothing_in_the_engine_assumes_pakistan():
    """The formatter, not the labels: a hard-coded lakh makes the whole
    product Pakistani whatever the currency string says."""
    import pathlib

    js = pathlib.Path("web/static/app.js").read_text()
    # The scales are declared in one place and chosen at runtime.
    assert "SCALES" in js
    assert js.count("1e7") <= 1, "a crore divisor outside the scale table"
    assert "S.data && S.data.scale" in js
