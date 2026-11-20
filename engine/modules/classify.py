"""Module 1: demand classification.

Runs before any forecasting, because the class decides which forecast
method is valid. Reagents and spare parts are intermittent demand, so
ordinary exponential smoothing is the wrong mathematics for most of the book.

Classification follows the ADI / CV-squared scheme:

                 CV2 < 0.49      CV2 >= 0.49
    ADI < 1.32   smooth          erratic
    ADI >= 1.32  intermittent    lumpy

ADI is the average interval between non-zero demand periods.
CV2 is the squared coefficient of variation of non-zero demand sizes.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Dict, Iterable, List

from engine.schema import (
    AbcClass,
    ClassificationResult,
    DemandClass,
    DemandPeriod,
    ItemSnapshot,
    Thresholds,
)

ADI_CUTOFF = 1.32
CV2_CUTOFF = 0.49

# Minimum history before a class is asserted rather than guessed.
MIN_PERIODS = 6

# Which method module 2 will use for each class.
#
# Smooth demand is labelled "holt" rather than "holt_winters" deliberately.
# Holt-Winters estimates a seasonal index per period, which needs at least two
# full cycles of history: 24 monthly observations to fit 12 monthly indices.
# A distributor who has just started exporting from their ERP has twelve
# points, and fitting seasonality to that produces a seasonal pattern which is
# really a fit to noise. Module 2 upgrades an item to holt_winters only when
# the history is long enough, and says so per item.
FORECAST_METHOD = {
    DemandClass.SMOOTH: "holt",
    DemandClass.ERRATIC: "empirical_quantile",
    DemandClass.INTERMITTENT: "sba",
    DemandClass.LUMPY: "empirical_quantile",
    DemandClass.NO_HISTORY: "manual",
}


def adi(quantities: List[float]) -> float | None:
    """Average inter-demand interval: periods observed per non-zero period."""
    nonzero = sum(1 for q in quantities if q > 0)
    if nonzero == 0:
        return None
    return len(quantities) / nonzero


def cv_squared(quantities: List[float]) -> float | None:
    """Squared coefficient of variation of the NON-ZERO demand sizes.

    Zeros are excluded deliberately: ADI already carries the intermittency,
    and including zeros would double-count it in CV2.
    """
    sizes = [q for q in quantities if q > 0]
    if len(sizes) < 2:
        return None
    mean = statistics.fmean(sizes)
    if mean == 0:
        return None
    sd = statistics.stdev(sizes)
    return (sd / mean) ** 2


def classify_one(quantities: List[float]) -> DemandClass:
    if len([q for q in quantities if q > 0]) == 0 or len(quantities) < MIN_PERIODS:
        return DemandClass.NO_HISTORY

    a = adi(quantities)
    c = cv_squared(quantities)
    if a is None:
        return DemandClass.NO_HISTORY
    if c is None:
        # A single non-zero period. Intermittency is known, variability is not.
        return DemandClass.INTERMITTENT if a >= ADI_CUTOFF else DemandClass.SMOOTH

    if a < ADI_CUTOFF:
        return DemandClass.SMOOTH if c < CV2_CUTOFF else DemandClass.ERRATIC
    return DemandClass.INTERMITTENT if c < CV2_CUTOFF else DemandClass.LUMPY


def _abc_classes(
    annual_value: Dict[str, float], thresholds: Thresholds
) -> Dict[str, AbcClass]:
    """Rank items by annual consumption value, cut at the configured shares."""
    total = sum(annual_value.values())
    if total <= 0:
        return {k: AbcClass.C for k in annual_value}

    ordered = sorted(annual_value.items(), key=lambda kv: kv[1], reverse=True)
    out: Dict[str, AbcClass] = {}
    running = 0.0
    for key, value in ordered:
        # Classify on the cumulative share BEFORE this item, so the item that
        # crosses a boundary is counted in the higher class. This is the usual
        # ABC convention: the SKU that takes you past 80% is still an A.
        share_before = running / total
        if share_before < thresholds.abc_a_cutoff:
            out[key] = AbcClass.A
        elif share_before < thresholds.abc_b_cutoff:
            out[key] = AbcClass.B
        else:
            out[key] = AbcClass.C
        running += value
    return out


def key_for(item_key: str, location_id: str) -> tuple:
    """The key every classification dict is keyed by.

    One function so that callers cannot disagree about the grain. A blank
    location is a legitimate key: a single-branch business, or a caller that
    does not track branches, still gets exactly one entry per item.
    """
    return (item_key, location_id or "")


def classify(
    items: Iterable[ItemSnapshot],
    history: Iterable[DemandPeriod],
    thresholds: Thresholds | None = None,
) -> Dict[tuple, ClassificationResult]:
    """Classify every item snapshot, per item AND branch.

    Items with no history are returned as NO_HISTORY rather than omitted,
    so that nothing silently disappears from downstream modules.

    The grain is (item, branch), not item. Each branch holds its own stock
    and reorders for itself, so pooling the branches would do two wrong
    things at once: hand every branch the average branch's mean demand, and
    read the level difference BETWEEN branches as variability WITHIN one,
    inflating safety stock everywhere. ABC is the exception and stays
    company-wide per item below, because an item's importance to the business
    does not change between branches.
    """
    thresholds = thresholds or Thresholds()
    items = list(items)

    by_key: Dict[tuple, List[float]] = defaultdict(list)
    for row in history:
        by_key[key_for(row.item_key, getattr(row, "location_id", ""))].append(
            row.quantity
        )

    stats: Dict[tuple, tuple] = {}
    value_by_key: Dict[tuple, float] = {}
    # Keys grouped by branch, because ABC is ranked within a branch.
    keys_by_branch: Dict[str, List[tuple]] = defaultdict(list)

    for item in items:
        key = key_for(item.item_key, getattr(item, "location_id", ""))
        qty = by_key.get(key, [])
        mean = statistics.fmean(qty) if qty else 0.0
        sd = statistics.stdev(qty) if len(qty) >= 2 else 0.0
        stats[key] = (qty, mean, sd)
        value_by_key[key] = mean * 12 * item.unit_cost
        keys_by_branch[key[1]].append(key)

    # ABC is ranked WITHIN each branch, not across the company. The class
    # drives that branch's service target, and so its safety stock and its
    # money: the question it answers is "which of the lines THIS branch
    # carries deserve the tightest service", and a branch's top lines are
    # its own. Ranked company-wide, a line that is big nationally but slow
    # here would be held to a 98% target at a branch that sells one a month.
    abc: Dict[tuple, AbcClass] = {}
    for branch_keys in keys_by_branch.values():
        abc.update(
            _abc_classes({k: value_by_key[k] for k in branch_keys}, thresholds)
        )

    results: Dict[tuple, ClassificationResult] = {}
    for item in items:
        key = key_for(item.item_key, getattr(item, "location_id", ""))
        qty, mean, sd = stats[key]
        dclass = classify_one(qty)
        results[key] = ClassificationResult(
            item_key=item.item_key,
            location_id=key[1],
            periods_observed=len(qty),
            nonzero_periods=sum(1 for q in qty if q > 0),
            adi=adi(qty),
            cv_squared=cv_squared(qty),
            demand_class=dclass,
            mean_monthly_demand=mean,
            std_monthly_demand=sd,
            annual_consumption_value=value_by_key[key],
            abc_class=abc[key],
            forecast_method=FORECAST_METHOD[dclass],
        )
    return results
