"""Golden tests for demand classification.

Expected values are hand-computed from the formulas, not copied from the
code's own output. A test that asserts whatever the code happens to return
proves nothing.
"""

import math

import pytest

from engine.modules.classify import adi, classify, classify_one, cv_squared, key_for
from engine.schema import AbcClass, DemandClass, DemandPeriod, ItemSnapshot


def test_adi_counts_periods_per_nonzero_period():
    # 12 periods, 4 with demand -> 12 / 4 = 3.0
    q = [10, 0, 0, 8, 0, 0, 12, 0, 0, 9, 0, 0]
    assert adi(q) == pytest.approx(3.0)


def test_adi_is_one_when_every_period_has_demand():
    assert adi([5, 7, 6, 8]) == pytest.approx(1.0)


def test_adi_is_none_with_no_demand_at_all():
    assert adi([0, 0, 0]) is None


def test_cv_squared_excludes_zeros():
    # non-zero sizes are 10 and 20: mean 15, sample sd 7.0710678,
    # cv = 0.4714045, cv^2 = 0.2222222
    q = [10, 0, 0, 20]
    assert cv_squared(q) == pytest.approx(2 / 9, rel=1e-6)


def test_cv_squared_is_zero_for_constant_demand():
    assert cv_squared([7, 7, 7, 7]) == pytest.approx(0.0)


@pytest.mark.parametrize(
    "quantities, expected",
    [
        # every period has demand, low variability -> smooth
        ([50, 52, 48, 51, 49, 50, 53, 47], DemandClass.SMOOTH),
        # every period has demand, high variability -> erratic
        ([10, 90, 15, 120, 8, 100, 12, 85], DemandClass.ERRATIC),
        # regular gaps, consistent size -> intermittent
        ([10, 0, 10, 0, 10, 0, 10, 0, 10, 0], DemandClass.INTERMITTENT),
        # rare and wildly variable -> lumpy
        ([0, 0, 2, 0, 0, 0, 40, 0, 0, 0, 1, 0], DemandClass.LUMPY),
    ],
)
def test_classification_quadrants(quantities, expected):
    assert classify_one(quantities) == expected


def test_boundaries_match_the_published_cutoffs():
    # ADI cutoff 1.32, CV2 cutoff 0.49
    # 13 periods / 10 nonzero = ADI 1.30, just under the cutoff
    q = [5] * 10 + [0] * 3
    assert adi(q) == pytest.approx(13 / 10)
    assert adi(q) < 1.32
    assert classify_one(q) == DemandClass.SMOOTH

    # 14 periods / 10 nonzero = ADI 1.40, just over
    q2 = [5] * 10 + [0] * 4
    assert adi(q2) == pytest.approx(1.4)
    assert classify_one(q2) == DemandClass.INTERMITTENT


def test_short_history_is_not_classified():
    assert classify_one([5, 6, 7]) == DemandClass.NO_HISTORY


def test_no_history_item_is_returned_not_dropped():
    items = [ItemSnapshot("A", "Item A", "cat", 10, 0, 100, 30)]
    result = classify(items, history=[])
    assert key_for("A", "default") in result
    assert result[key_for("A", "default")].demand_class == DemandClass.NO_HISTORY
    assert result[key_for("A", "default")].forecast_method == "manual"


def test_abc_cuts_on_cumulative_consumption_value():
    # Annual values: A 1,200,000  B 240,000  C 60,000. Total 1,500,000.
    # Cumulative shares: 0.80, 0.96, 1.00
    # With cutoffs 0.80 and 0.95 that is A, B, C.
    items = [
        ItemSnapshot("BIG", "Big", "c", 0, 0, 1000, 30),
        ItemSnapshot("MID", "Mid", "c", 0, 0, 1000, 30),
        ItemSnapshot("SML", "Small", "c", 0, 0, 1000, 30),
    ]
    history = (
        [DemandPeriod("BIG", f"2026-{m:02d}", 100) for m in range(1, 13)]
        + [DemandPeriod("MID", f"2026-{m:02d}", 20) for m in range(1, 13)]
        + [DemandPeriod("SML", f"2026-{m:02d}", 5) for m in range(1, 13)]
    )
    out = classify(items, history)

    assert out[key_for("BIG", "default")].annual_consumption_value == pytest.approx(1_200_000)
    assert out[key_for("MID", "default")].annual_consumption_value == pytest.approx(240_000)
    assert out[key_for("SML", "default")].annual_consumption_value == pytest.approx(60_000)
    assert out[key_for("BIG", "default")].abc_class == AbcClass.A
    assert out[key_for("MID", "default")].abc_class == AbcClass.B
    assert out[key_for("SML", "default")].abc_class == AbcClass.C


def test_mean_and_std_are_computed_over_all_periods_including_zeros():
    items = [ItemSnapshot("X", "X", "c", 0, 0, 10, 30)]
    history = [
        DemandPeriod("X", "2026-01", 10),
        DemandPeriod("X", "2026-02", 0),
        DemandPeriod("X", "2026-03", 20),
        DemandPeriod("X", "2026-04", 0),
        DemandPeriod("X", "2026-05", 10),
        DemandPeriod("X", "2026-06", 0),
    ]
    out = classify(items, history)[key_for("X", "default")]
    assert out.mean_monthly_demand == pytest.approx(40 / 6)
    assert out.std_monthly_demand == pytest.approx(
        math.sqrt(sum((q - 40 / 6) ** 2 for q in [10, 0, 20, 0, 10, 0]) / 5)
    )


# --------------------------------------------------- the grain: item x branch
#
# These lock down the fix for a defect that produced clean, plausible, wrong
# numbers: classification keyed by item alone pooled every branch's history
# into one series. That did two wrong things at once. It handed each branch
# the average branch's mean demand, and it read the level difference BETWEEN
# branches as variability WITHIN one, so safety stock was inflated at every
# branch. It also collapsed one result per (item, branch) into one per item.


def _months(key, loc, qty):
    return [
        DemandPeriod(key, f"2026-{m:02d}", qty, "E1", loc) for m in range(1, 13)
    ]


def _at(key, loc, **kw):
    base = dict(
        item_key=key, description=key, category="c", on_hand=0.0, open_po=0.0,
        unit_cost=100.0, lead_time_days=30.0, entity_id="E1", location_id=loc,
    )
    base.update(kw)
    return ItemSnapshot(**base)


def test_each_branch_gets_its_own_entry_not_one_shared_per_item():
    items = [_at("R1", "KHI"), _at("R1", "LHR"), _at("R1", "ISB")]
    history = _months("R1", "KHI", 100) + _months("R1", "LHR", 10) \
        + _months("R1", "ISB", 1)

    out = classify(items, history)

    assert len(out) == 3
    assert set(out) == {
        key_for("R1", "KHI"), key_for("R1", "LHR"), key_for("R1", "ISB")
    }


def test_a_branch_mean_is_its_own_demand_not_the_average_branch():
    items = [_at("R1", "KHI"), _at("R1", "LHR"), _at("R1", "ISB")]
    history = _months("R1", "KHI", 100) + _months("R1", "LHR", 10) \
        + _months("R1", "ISB", 1)

    out = classify(items, history)

    assert out[key_for("R1", "KHI")].mean_monthly_demand == pytest.approx(100)
    assert out[key_for("R1", "LHR")].mean_monthly_demand == pytest.approx(10)
    assert out[key_for("R1", "ISB")].mean_monthly_demand == pytest.approx(1)
    # Pooled, every branch would have read 37.0 -- the average branch.
    pooled = (100 + 10 + 1) / 3
    for loc in ("KHI", "LHR", "ISB"):
        assert out[key_for("R1", loc)].mean_monthly_demand != pytest.approx(pooled)


def test_a_level_difference_between_branches_is_not_variability_within_one():
    """Each branch is perfectly flat, so every standard deviation is nil.

    Pooled into one series the three levels look like huge variation, and
    safety stock would be charged for demand risk that does not exist.
    """
    items = [_at("R1", "KHI"), _at("R1", "LHR"), _at("R1", "ISB")]
    history = _months("R1", "KHI", 100) + _months("R1", "LHR", 10) \
        + _months("R1", "ISB", 1)

    out = classify(items, history)

    for loc in ("KHI", "LHR", "ISB"):
        assert out[key_for("R1", loc)].std_monthly_demand == pytest.approx(0.0)


def test_the_same_item_can_classify_differently_at_two_branches():
    """The docstring promise of module 4, now actually true."""
    steady = _months("R1", "KHI", 50)
    # Spikes far apart (high ADI) AND of very different sizes (high CV2 on
    # the non-zero sizes, which is what CV2 is measured over). Equal-sized
    # spikes would be merely intermittent, not lumpy.
    lumpy = [
        DemandPeriod("R1", f"2026-{m:02d}", q, "E1", "LHR")
        for m, q in enumerate([0, 0, 500, 0, 0, 0, 0, 20, 0, 0, 0, 0], start=1)
    ]
    out = classify([_at("R1", "KHI"), _at("R1", "LHR")], steady + lumpy)

    assert out[key_for("R1", "KHI")].demand_class == DemandClass.SMOOTH
    assert out[key_for("R1", "LHR")].demand_class == DemandClass.LUMPY


def test_abc_is_ranked_within_a_branch_not_across_the_company():
    """The class drives THIS branch's service target, so it is this
    branch's ranking that matters.

    The same line is an A where it is one of the big movers and a C where
    it is not. Ranked company-wide it would be an A at both, and the quiet
    branch would carry A-grade safety stock for demand it does not have.
    """
    items = [
        _at("R1", "KHI"), _at("SML", "KHI"),
        _at("R1", "LHR"), _at("BULK1", "LHR"), _at("BULK2", "LHR"),
    ]
    history = (
        # KHI: R1 is nearly all of this branch's value.
        _months("R1", "KHI", 100) + _months("SML", "KHI", 1)
        # LHR: R1 barely moves, behind two much larger lines.
        + _months("R1", "LHR", 1)
        + _months("BULK1", "LHR", 100) + _months("BULK2", "LHR", 100)
    )
    out = classify(items, history)

    assert out[key_for("R1", "KHI")].abc_class == AbcClass.A
    assert out[key_for("R1", "LHR")].abc_class == AbcClass.C


def test_consumption_value_is_the_branch_own_not_the_company_total():
    items = [_at("R1", "KHI"), _at("R1", "LHR")]
    history = _months("R1", "KHI", 100) + _months("R1", "LHR", 10)
    out = classify(items, history)

    # 100 x 12 x 100 and 10 x 12 x 100, not 110 x 12 x 100 twice over.
    assert out[key_for("R1", "KHI")].annual_consumption_value \
        == pytest.approx(120_000)
    assert out[key_for("R1", "LHR")].annual_consumption_value \
        == pytest.approx(12_000)


def test_a_branch_with_no_history_is_kept_not_dropped():
    items = [_at("R1", "KHI"), _at("R1", "LHR")]
    out = classify(items, _months("R1", "KHI", 50))

    assert key_for("R1", "LHR") in out
    assert out[key_for("R1", "LHR")].demand_class == DemandClass.NO_HISTORY
    assert out[key_for("R1", "LHR")].periods_observed == 0
    # And the branch that does have history is unaffected by the one that does not.
    assert out[key_for("R1", "KHI")].mean_monthly_demand == pytest.approx(50)
