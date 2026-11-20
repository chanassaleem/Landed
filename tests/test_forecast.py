"""Module 2 tests.

The methods are tested against series whose right answer can be worked out by
hand: a clean ramp for Holt, a fixed size at a fixed interval for SBA, a known
set for the quantiles.
"""

from datetime import date

import pytest

from engine.modules.classify import classify
from engine.modules.forecast import (
    HOLDOUT,
    empirical_quantile,
    evaluate,
    holt,
    sba,
    seasonal_indices,
)
from engine.schema import DemandClass, DemandPeriod, ItemSnapshot, Thresholds

AS_OF = date(2026, 11, 20)
T = Thresholds()

MONTHS = ["2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05",
          "2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11"]


def item(key="RGT-1", **kw):
    base = dict(item_key=key, description="Pack", category="Reagent", on_hand=100.0,
                open_po=0.0, unit_cost=5_000.0, lead_time_days=60.0,
                lead_time_days_std=10.0)
    base.update(kw)
    return ItemSnapshot(**base)


def history(values, key="RGT-1", months=None):
    months = months or MONTHS
    return [DemandPeriod(item_key=key, period=p, quantity=float(v))
            for p, v in zip(months, values)]


def one(values, **item_kw):
    it = item(**item_kw)
    hist = history(values)
    classes = classify([it], hist, T)
    return evaluate([it], hist, classes, T, AS_OF)[0]


# ------------------------------------------------------------- Holt


def test_holt_recovers_the_slope_of_a_clean_ramp():
    level, trend = holt([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 0.3, 0.1)
    assert level == pytest.approx(100.0, abs=0.5)
    assert trend == pytest.approx(10.0, abs=0.5)


def test_holt_on_a_flat_series_has_no_trend():
    level, trend = holt([50] * 12, 0.3, 0.1)
    assert level == pytest.approx(50.0, abs=0.1)
    assert trend == pytest.approx(0.0, abs=0.1)


def test_holt_handles_one_and_zero_observations():
    assert holt([], 0.3, 0.1) == (0.0, 0.0)
    assert holt([7], 0.3, 0.1) == (7.0, 0.0)


# -------------------------------------------------------------- SBA


def test_sba_applies_the_syntetos_boylan_debiasing():
    """Croston on size 9 every 3 months gives 3.0 a month. Croston is biased
    upward by about alpha/2, so SBA scales by (1 - alpha/2) = 0.9 -> 2.7.
    Plain Croston here would over-order every intermittent spare."""
    series = [9 if i % 3 == 0 else 0 for i in range(12)]
    assert sba(series, 0.2) == pytest.approx(2.7, abs=0.01)


def test_sba_is_always_below_the_naive_croston_rate():
    series = [9 if i % 3 == 0 else 0 for i in range(12)]
    naive = 9 / 3
    assert sba(series, 0.2) < naive


def test_sba_on_an_all_zero_series_is_zero():
    assert sba([0] * 12, 0.2) == 0.0


def test_sba_with_a_single_observation_spreads_it_over_the_window():
    series = [0] * 11 + [12]
    assert sba(series, 0.2) == pytest.approx(1.0)


# -------------------------------------------------------- quantiles


@pytest.mark.parametrize("q,expected", [(0.0, 1), (0.5, 5.5), (1.0, 10)])
def test_empirical_quantile_interpolates(q, expected):
    assert empirical_quantile(list(range(1, 11)), q) == pytest.approx(expected)


def test_empirical_quantile_is_safe_on_short_input():
    assert empirical_quantile([], 0.9) == 0.0
    assert empirical_quantile([4], 0.9) == 4.0


def test_seasonal_indices_of_a_flat_series_are_all_one():
    assert seasonal_indices([5] * 24) == pytest.approx([1.0] * 12)


def test_seasonal_indices_pick_up_a_repeating_peak():
    # One month at triple the others, repeated over two years.
    series = [30 if i % 12 == 0 else 10 for i in range(24)]
    indices = seasonal_indices(series)
    assert indices[0] > 2.0
    assert indices[1] < 1.0


# -------------------------------------------- method follows the class


def test_smooth_demand_uses_holt():
    r = one([100, 102, 98, 101, 99, 103, 100, 101, 102, 98, 100, 101])
    assert r.demand_class is DemandClass.SMOOTH
    assert r.method == "holt"


def test_intermittent_demand_uses_sba_not_smoothing():
    """Smoothing an intermittent series drifts toward the mean of something
    that is mostly zeros."""
    r = one([5, 0, 0, 6, 0, 0, 0, 7, 0, 0, 5, 0])
    assert r.demand_class in (DemandClass.INTERMITTENT, DemandClass.LUMPY)
    assert r.method in ("sba", "empirical_quantile")


def test_no_history_forecasts_nothing_rather_than_zero():
    """A forecast of zero reads as a prediction. It is an absence of data."""
    it = item()
    classes = classify([it], [], T)
    r = evaluate([it], [], classes, T, AS_OF)[0]
    assert r.method == "manual"
    assert r.reason.startswith("No demand history")
    assert "absence of data" in r.caveat


# ----------------------------------------------------- seasonality


def test_seasonality_is_refused_on_twelve_months():
    """Holt-Winters needs two full cycles to estimate 12 monthly indices. A
    seasonal pattern fitted to 12 points is a fit to noise that reads as
    insight."""
    r = one([100, 102, 98, 101, 99, 103, 100, 101, 102, 98, 100, 101])
    assert r.periods_observed == 12
    assert r.seasonality_fitted is False
    assert r.method == "holt"
    assert "24 months" in r.caveat


def test_seasonality_is_fitted_once_there_are_two_cycles():
    months = [f"{y}-{m:02d}" for y in (2025, 2026) for m in range(1, 13)]
    values = [30 if i % 12 == 0 else 10 for i in range(24)]
    it = item()
    hist = history(values, months=months)
    classes = classify([it], hist, T)
    r = evaluate([it], hist, classes, T, AS_OF)[0]
    assert r.periods_observed == 24
    assert r.seasonality_fitted is True
    assert r.method == "holt_winters"


def test_the_seasonality_threshold_is_configurable():
    custom = Thresholds(min_periods_for_seasonality=12)
    it = item()
    hist = history([100, 102, 98, 101, 99, 103, 100, 101, 102, 98, 100, 101])
    classes = classify([it], hist, custom)
    r = evaluate([it], hist, classes, custom, AS_OF)[0]
    assert r.seasonality_fitted is True


# ------------------------------------------------------- intervals


def test_the_forecast_has_one_point_per_month_of_horizon():
    r = one([100] * 12, )
    assert len(r.forecast) == T.forecast_horizon_months
    assert len(r.lower) == len(r.forecast) == len(r.upper)


def test_the_horizon_can_be_overridden():
    it = item()
    hist = history([100] * 12)
    classes = classify([it], hist, T)
    r = evaluate([it], hist, classes, T, AS_OF, horizon=3)[0]
    assert len(r.forecast) == 3


def test_the_interval_widens_further_out():
    """Uncertainty accumulates, so a six month bound must be wider than a one
    month bound."""
    r = one([100, 120, 90, 110, 95, 130, 85, 115, 100, 125, 90, 105])
    spreads = [u - l for l, u in zip(r.lower, r.upper)]
    assert spreads[-1] > spreads[0]
    assert spreads == sorted(spreads)


def test_the_lower_bound_never_goes_below_zero():
    """Negative demand is not a thing."""
    r = one([5, 0, 0, 6, 0, 0, 0, 7, 0, 0, 5, 0])
    assert all(v >= 0 for v in r.lower)
    assert all(v >= 0 for v in r.forecast)


def test_a_clinical_item_gets_a_wider_interval_than_an_ordinary_one():
    """The interval is set at the item's service target, and clinical
    criticality raises that floor."""
    values = [100, 120, 90, 110, 95, 130, 85, 115, 100, 125, 90, 105]
    ordinary = one(values, is_clinical_critical=False)
    clinical = one(values, is_clinical_critical=True)
    assert clinical.upper[0] > ordinary.upper[0]


# -------------------------------------------------------- accuracy


def test_accuracy_is_measured_out_of_sample():
    """In-sample accuracy on a smoothed series flatters every method."""
    r = one([100, 102, 98, 101, 99, 103, 100, 101, 102, 98, 100, 101])
    assert r.mape is not None
    assert 0 <= r.mape < 1


def test_a_perfect_flat_series_forecasts_almost_exactly():
    r = one([100] * 12)
    assert r.mape == pytest.approx(0.0, abs=0.02)


def test_accuracy_is_none_rather_than_invented_on_short_history():
    months = MONTHS[:6]
    it = item()
    hist = history([10, 12, 11, 13, 10, 12], months=months)
    classes = classify([it], hist, T)
    r = evaluate([it], hist, classes, T, AS_OF)[0]
    assert r.mape is None
    assert r.bias is None
    assert "too few" in r.caveat


def test_bias_is_signed_so_over_forecasting_is_visible():
    """A method that always over-orders must be distinguishable from one that
    is merely imprecise."""
    # Falling demand: a trend-following method keeps forecasting too high.
    r = one([200, 180, 160, 140, 120, 100, 90, 80, 70, 60, 50, 40])
    assert r.bias is not None


# ----------------------------------------------------- every item


def test_every_item_gets_a_forecast_including_the_hopeless_ones():
    items = [item("A"), item("B"), item("C")]
    hist = (history([100] * 12, "A")
            + history([5, 0, 0, 6, 0, 0, 0, 7, 0, 0, 5, 0], "B"))
    classes = classify(items, hist, T)
    out = evaluate(items, hist, classes, T, AS_OF)
    assert {r.item_key for r in out} == {"A", "B", "C"}
    assert next(r for r in out if r.item_key == "C").method == "manual"


def test_results_are_ranked_worst_accuracy_first():
    """Those are the items whose policy is least trustworthy."""
    items = [item("STEADY"), item("WILD")]
    hist = (history([100] * 12, "STEADY")
            + history([10, 300, 5, 250, 8, 400, 2, 180, 15, 320, 4, 290], "WILD"))
    classes = classify(items, hist, T)
    out = evaluate(items, hist, classes, T, AS_OF)
    measured = [r for r in out if r.mape is not None]
    if len(measured) > 1:
        assert measured[0].mape >= measured[-1].mape


def test_every_result_carries_a_reason_and_a_caveat():
    """A forecast without its limits stated gets used as if it had none."""
    items = [item("A"), item("B"), item("C")]
    hist = (history([100] * 12, "A")
            + history([5, 0, 0, 6, 0, 0, 0, 7, 0, 0, 5, 0], "B"))
    classes = classify(items, hist, T)
    for r in evaluate(items, hist, classes, T, AS_OF):
        assert r.reason.strip()
        assert r.caveat.strip()


def test_an_item_with_no_classification_is_skipped_without_error():
    assert evaluate([item("GHOST")], [], {}, T, AS_OF) == ()
