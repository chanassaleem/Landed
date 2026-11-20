"""Golden tests for inventory policy.

Expected values are computed independently from the formulas in the
architecture document, so the tests fail if the implementation drifts.
"""

import math
from datetime import date
from statistics import NormalDist

import pytest

from engine.modules.classify import classify, key_for
from engine.modules.inventory import evaluate, safety_stock, z_for
from engine.schema import (
    AbcClass,
    ClassificationResult,
    DemandClass,
    DemandPeriod,
    ItemSnapshot,
    ServiceMetric,
    StockStatus,
    Thresholds,
)


def _cls(
    key="X",
    mean=90.0,
    std=0.0,
    dclass=DemandClass.SMOOTH,
    abc=AbcClass.A,
):
    return ClassificationResult(
        item_key=key,
        periods_observed=12,
        nonzero_periods=12,
        adi=1.0,
        cv_squared=0.0,
        demand_class=dclass,
        mean_monthly_demand=mean,
        std_monthly_demand=std,
        annual_consumption_value=mean * 12 * 100,
        abc_class=abc,
        forecast_method="holt",
    )


def test_z_matches_the_normal_distribution():
    assert z_for(0.95) == pytest.approx(1.644853, rel=1e-5)
    assert z_for(0.98) == pytest.approx(2.053749, rel=1e-5)
    assert z_for(0.99) == pytest.approx(2.326348, rel=1e-5)


def test_safety_stock_formula():
    # SS = z * sqrt(L * sd^2 + d^2 * sL^2)
    d, sd, L, sL, z = 3.0, 0.5, 60.0, 14.0, 1.96
    expected = z * math.sqrt(60 * 0.25 + 9 * 196)
    assert safety_stock(d, sd, L, sL, z) == pytest.approx(expected)


def test_lead_time_variability_dominates_for_an_importer():
    """A 75 day lead time slipping three weeks matters more than demand noise."""
    d, sd = 3.0, 0.5
    no_lead_variance = safety_stock(d, sd, 75, 0, 1.96)
    with_lead_variance = safety_stock(d, sd, 75, 21, 1.96)
    assert with_lead_variance > no_lead_variance * 10


def test_zero_variability_gives_zero_safety_stock_and_rop_is_lead_demand():
    item = ItemSnapshot("X", "X", "c", on_hand=1000, open_po=0, unit_cost=10,
                        lead_time_days=30, lead_time_days_std=0)
    classes = {key_for("X", "default"): _cls(mean=90.0, std=0.0)}
    r = evaluate([item], classes, Thresholds())[0]

    assert r.safety_stock == pytest.approx(0.0)
    # daily demand 3.0 over a 30 day lead time
    assert r.reorder_point == pytest.approx(90.0)
    # order_up_to = ROP + 3 months cover
    assert r.order_up_to == pytest.approx(90.0 + 3 * 90.0)


def test_clinical_critical_floor_raises_the_target_but_never_lowers_it():
    th = Thresholds()
    base = ItemSnapshot("X", "X", "c", 100, 0, 10, 30, 5)
    crit = ItemSnapshot("X", "X", "c", 100, 0, 10, 30, 5, is_clinical_critical=True)
    classes = {key_for("X", "default"): _cls(mean=90, std=20, dclass=DemandClass.LUMPY, abc=AbcClass.C)}

    r_base = evaluate([base], classes, th)[0]
    r_crit = evaluate([crit], classes, th)[0]

    assert r_base.service_target == pytest.approx(0.85)   # C / lumpy
    assert r_crit.service_target == pytest.approx(0.99)   # floored upward
    assert r_crit.safety_stock > r_base.safety_stock


def test_service_metric_follows_demand_class():
    item = ItemSnapshot("X", "X", "c", 100, 0, 10, 30, 2)
    smooth = evaluate([item], {key_for("X", "default"): _cls(dclass=DemandClass.SMOOTH)}, Thresholds())[0]
    lumpy = evaluate([item], {key_for("X", "default"): _cls(dclass=DemandClass.LUMPY)}, Thresholds())[0]
    assert smooth.service_metric == ServiceMetric.FILL_RATE
    assert lumpy.service_metric == ServiceMetric.CYCLE_SERVICE_LEVEL


def test_status_critical_requires_below_rop_and_under_one_month_cover():
    classes = {key_for("X", "default"): _cls(mean=90.0, std=0.0)}
    th = Thresholds()

    # Below ROP but holds 1.5 months -> REORDER, not CRITICAL
    reorder = ItemSnapshot("X", "X", "c", on_hand=135, open_po=0, unit_cost=10,
                           lead_time_days=60, lead_time_days_std=0)
    assert evaluate([reorder], classes, th)[0].status == StockStatus.REORDER

    # Below ROP and under one month of cover -> CRITICAL
    critical = ItemSnapshot("X", "X", "c", on_hand=45, open_po=0, unit_cost=10,
                            lead_time_days=60, lead_time_days_std=0)
    assert evaluate([critical], classes, th)[0].status == StockStatus.CRITICAL


def test_open_po_counts_toward_position_and_can_clear_a_reorder():
    classes = {key_for("X", "default"): _cls(mean=90.0, std=0.0)}
    # ROP is 90. On hand 50 alone is below it; with 200 on order it is not.
    bare = ItemSnapshot("X", "X", "c", 50, 0, 10, 30, 0)
    covered = ItemSnapshot("X", "X", "c", 50, 200, 10, 30, 0)
    assert evaluate([bare], classes, Thresholds())[0].status == StockStatus.CRITICAL
    assert evaluate([covered], classes, Thresholds())[0].status != StockStatus.CRITICAL


def test_excess_value_is_the_surplus_beyond_the_threshold():
    classes = {key_for("X", "default"): _cls(mean=100.0, std=0.0)}
    th = Thresholds(excess_cover_months=6.0)
    # 1000 on hand against 100 a month = 10 months cover, 4 months surplus
    item = ItemSnapshot("X", "X", "c", on_hand=1000, open_po=0, unit_cost=50,
                        lead_time_days=30, lead_time_days_std=0)
    r = evaluate([item], classes, th)[0]
    assert r.status == StockStatus.EXCESS
    assert r.months_cover == pytest.approx(10.0)
    assert r.excess_value == pytest.approx(4 * 100 * 50)
    assert r.suggested_order_qty == 0


def test_expiry_risk_beats_a_healthy_cover_figure():
    classes = {key_for("X", "default"): _cls(mean=100.0, std=0.0)}
    th = Thresholds()
    # 400 units is 4 months cover, inside policy, but it expires in 30 days
    # and only about 100 will be consumed by then.
    item = ItemSnapshot("X", "X", "c", on_hand=400, open_po=0, unit_cost=50,
                        lead_time_days=30, lead_time_days_std=0,
                        expiry_date=date(2026, 11, 15))
    r = evaluate([item], classes, th, as_of=date(2026, 10, 16))[0]
    assert r.status == StockStatus.EXPIRY_RISK
    assert r.days_to_expiry == 30
    assert r.excess_value == pytest.approx(300 * 50, rel=0.02)


def test_dead_item_with_stock_is_flagged_not_ignored():
    classes = {key_for("X", "default"): _cls(mean=0.0, std=0.0, dclass=DemandClass.NO_HISTORY)}
    item = ItemSnapshot("X", "X", "c", on_hand=500, open_po=0, unit_cost=10,
                        lead_time_days=30, lead_time_days_std=0)
    r = evaluate([item], classes, Thresholds())[0]
    assert r.status == StockStatus.EXCESS
    assert r.months_cover is None
    assert "No demand" in r.reason


def test_results_are_ranked_with_critical_first():
    classes = {
        key_for("CRIT", "default"): _cls("CRIT", mean=90.0),
        key_for("FINE", "default"): _cls("FINE", mean=90.0),
    }
    items = [
        ItemSnapshot("FINE", "Fine", "c", 300, 0, 10, 30, 0),
        ItemSnapshot("CRIT", "Crit", "c", 20, 0, 10, 30, 0),
    ]
    out = evaluate(items, classes, Thresholds())
    assert out[0].item_key == "CRIT"


def test_suggested_order_closes_the_gap_to_order_up_to():
    classes = {key_for("X", "default"): _cls(mean=90.0, std=0.0)}
    item = ItemSnapshot("X", "X", "c", on_hand=45, open_po=0, unit_cost=10,
                        lead_time_days=30, lead_time_days_std=0)
    r = evaluate([item], classes, Thresholds())[0]
    assert r.suggested_order_qty == pytest.approx(math.ceil(r.order_up_to - r.position))
    assert r.position + r.suggested_order_qty >= r.order_up_to


def test_thresholds_are_honoured_and_nothing_is_hard_coded():
    classes = {key_for("X", "default"): _cls(mean=100.0, std=0.0)}
    item = ItemSnapshot("X", "X", "c", on_hand=800, open_po=0, unit_cost=10,
                        lead_time_days=30, lead_time_days_std=0)
    loose = evaluate([item], classes, Thresholds(excess_cover_months=12.0))[0]
    tight = evaluate([item], classes, Thresholds(excess_cover_months=4.0))[0]
    assert loose.status != StockStatus.EXCESS
    assert tight.status == StockStatus.EXCESS


def test_end_to_end_on_the_sample_files():
    from engine.io import load_history, load_items

    items = load_items("tests/data/inventory.csv")
    history = load_history("tests/data/demand_history.csv")
    classes = classify(items, history)
    results = evaluate(items, classes, Thresholds(), as_of=date(2026, 10, 16))

    assert len(results) == len(items) == 10
    # Every item classified, nothing silently dropped
    assert {r.item_key for r in results} == {i.item_key for i in items}
    # Spares should not come out as smooth demand
    spares = {r.item_key: r.demand_class for r in results if r.item_key.startswith("SPR")}
    assert all(c != DemandClass.SMOOTH for c in spares.values()), spares
    # Clinical critical items carry the raised target
    crit = [r for r in results if r.item_key in ("HEM-RGT-01", "TEG-CUP-03", "BGA-CRT-05")]
    assert all(r.service_target >= 0.99 for r in crit)
