"""Module 2: forecasting, by demand class.

Module 1 decides the method before any forecast is produced, because the wrong
mathematics on intermittent demand is not slightly wrong, it is systematically
wrong. Most of a diagnostics book is reagents and spares, which means most of
it is intermittent, and ordinary exponential smoothing on intermittent demand
drifts toward the mean of a series that is mostly zeros.

What each class gets, and why:

  smooth          Holt's linear trend. Level and trend, no seasonality.
  intermittent    Syntetos-Boylan approximation (SBA), which is Croston with
                  the bias correction. Croston is itself biased upward by
                  roughly alpha/2; SBA multiplies by (1 - alpha/2) to remove it.
  lumpy, erratic  Empirical quantiles of the observed history. When size and
                  interval are both volatile, a point forecast is close to
                  meaningless and what inventory policy actually needs is the
                  upper quantile, so that is what is produced.
  no history      Nothing. Returned as manual, not as zero.

Seasonality is deliberately not fitted on short history. Holt-Winters
estimates one index per period and needs at least two full cycles to do it:
24 monthly observations for 12 monthly indices. A distributor who has just
started exporting from their ERP has twelve, and a seasonal pattern fitted to
twelve points is a fit to noise that will read as insight. Where the history is
long enough the item is upgraded and `seasonality_fitted` says so.

Accuracy is measured on a holdout, never on the data the model was fitted to.
Where there is too little history to hold anything back, the accuracy figures
come back as None rather than as a flattering number computed in-sample.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date
from statistics import NormalDist
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from engine.fmt import money, qty
from engine.modules.classify import key_for
from engine.schema import (
    ClassificationResult,
    DemandClass,
    DemandPeriod,
    ForecastResult,
    ItemSnapshot,
    Thresholds,
)

# Periods held back to measure accuracy, when there are enough to spare.
HOLDOUT = 3
MIN_FOR_HOLDOUT = 9


def _series(periods: Sequence[DemandPeriod]) -> List[float]:
    """Demand in period order. Zero months are kept: they are information."""
    return [p.quantity for p in sorted(periods, key=lambda p: p.period)]


# --------------------------------------------------------------- methods


def holt(
    series: Sequence[float], alpha: float, beta: float
) -> Tuple[float, float]:
    """Holt's linear trend. Returns (level, trend) after the last point."""
    if not series:
        return 0.0, 0.0
    if len(series) == 1:
        return float(series[0]), 0.0

    level = float(series[0])
    trend = float(series[1] - series[0])
    for value in series[1:]:
        prev = level
        level = alpha * value + (1 - alpha) * (level + trend)
        trend = beta * (level - prev) + (1 - beta) * trend
    return level, trend


def sba(series: Sequence[float], alpha: float) -> float:
    """Syntetos-Boylan approximation: Croston, debiased.

    Croston smooths the non-zero demand size and the interval between
    non-zero periods separately, then divides. That ratio is biased upward by
    about alpha/2, which is why SBA scales it down by (1 - alpha/2). Using
    plain Croston here would over-order every intermittent spare in the book.
    """
    nonzero = [(i, v) for i, v in enumerate(series) if v > 0]
    if not nonzero:
        return 0.0
    if len(nonzero) == 1:
        # One observation gives a size but no interval to estimate.
        return nonzero[0][1] / max(1, len(series))

    size = float(nonzero[0][1])
    gaps = [nonzero[i][0] - nonzero[i - 1][0] for i in range(1, len(nonzero))]
    interval = float(gaps[0])

    for k in range(1, len(nonzero)):
        size = alpha * nonzero[k][1] + (1 - alpha) * size
        interval = alpha * (nonzero[k][0] - nonzero[k - 1][0]) + (1 - alpha) * interval

    if interval <= 0:
        return size
    return (size / interval) * (1 - alpha / 2.0)


def empirical_quantile(series: Sequence[float], q: float) -> float:
    """Quantile of the observed history, interpolated.

    Used where size and interval are both volatile. No distribution is assumed,
    because assuming normality on lumpy demand is how a 95% service target
    turns into a 70% one in practice.
    """
    values = sorted(float(v) for v in series)
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    pos = q * (len(values) - 1)
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return values[int(pos)]
    return values[low] + (values[high] - values[low]) * (pos - low)


def seasonal_indices(series: Sequence[float], period: int = 12) -> List[float]:
    """Average ratio to the overall mean, per position in the cycle."""
    mean = statistics.fmean(series) if series else 0.0
    if mean <= 0:
        return [1.0] * period
    buckets: Dict[int, List[float]] = defaultdict(list)
    for i, value in enumerate(series):
        buckets[i % period].append(value / mean)
    return [
        statistics.fmean(buckets[i]) if buckets.get(i) else 1.0
        for i in range(period)
    ]


# -------------------------------------------------------------- accuracy


def _accuracy(
    series: Sequence[float], fit, horizon: int
) -> Tuple[Optional[float], Optional[float]]:
    """MAPE and signed bias on a holdout the model never saw.

    Measured out of sample on purpose. In-sample accuracy on a smoothed series
    flatters every method and would make the worst performer look fine.
    """
    if len(series) < MIN_FOR_HOLDOUT:
        return None, None

    train = series[:-HOLDOUT]
    test = series[-HOLDOUT:]
    predictions = fit(train, HOLDOUT)

    errors = []
    pct = []
    for actual, predicted in zip(test, predictions):
        errors.append(predicted - actual)
        if actual > 0:
            pct.append(abs(predicted - actual) / actual)

    mape = statistics.fmean(pct) if pct else None
    bias = statistics.fmean(errors) if errors else None
    return (
        round(mape, 4) if mape is not None else None,
        round(bias, 2) if bias is not None else None,
    )


# ------------------------------------------------------------------ main


def evaluate(
    items: Iterable[ItemSnapshot],
    demand: Iterable[DemandPeriod],
    classes: Dict[tuple, ClassificationResult],
    thresholds: Thresholds | None = None,
    as_of: Optional[date] = None,
    horizon: Optional[int] = None,
) -> Tuple[ForecastResult, ...]:
    """Forecast every item, by the method its class already chose."""
    thresholds = thresholds or Thresholds()
    as_of = as_of or date.today()
    horizon = horizon or thresholds.forecast_horizon_months

    # Per (item, branch): a branch forecasts its own demand, for the same
    # reason it classifies its own demand.
    by_key: Dict[tuple, List[DemandPeriod]] = defaultdict(list)
    for period in demand:
        by_key[key_for(period.item_key, getattr(period, "location_id", ""))].append(
            period
        )

    out: List[ForecastResult] = []

    for item in items:
        key = key_for(item.item_key, getattr(item, "location_id", ""))
        cls = classes.get(key)
        if cls is None:
            continue

        series = _series(by_key.get(key, []))
        dclass = cls.demand_class
        method = cls.forecast_method
        n = len(series)

        level = 0.0
        trend = 0.0
        seasonal = False
        # A forecast can have more than one limitation, and the reader needs
        # all of them. Collected rather than assigned, so one does not
        # silently replace another.
        caveats: List[str] = []

        # ------------------------------------------------- no history
        if dclass is DemandClass.NO_HISTORY or not series:
            out.append(
                ForecastResult(
                    item_key=item.item_key,
                    entity_id=item.entity_id,
                    location_id=item.location_id,
                    demand_class=dclass,
                    method="manual",
                    periods_observed=n,
                    mean_monthly_demand=0.0,
                    forecast=tuple([0.0] * horizon),
                    lower=tuple([0.0] * horizon),
                    upper=tuple([0.0] * horizon),
                    level=0.0,
                    trend=0.0,
                    seasonality_fitted=False,
                    mape=None,
                    bias=None,
                    reason="No demand history, so nothing is forecast. "
                           "This item needs a manual estimate.",
                    caveat="A forecast of zero would read as a prediction. "
                           "It is an absence of data.",
                )
            )
            continue

        # The service target decides the interval, because the interval is what
        # inventory policy actually consumes.
        target = thresholds.service_targets.get(
            (cls.abc_class.value, dclass.value), 0.90
        )
        if item.is_clinical_critical:
            target = max(target, thresholds.clinical_critical_floor)
        z = NormalDist().inv_cdf(min(max(target, 0.5), 0.9999))

        # ------------------------------------------------- smooth: Holt
        if dclass is DemandClass.SMOOTH:
            level, trend = holt(series, thresholds.holt_alpha, thresholds.holt_beta)

            use_seasonality = n >= thresholds.min_periods_for_seasonality
            indices = seasonal_indices(series) if use_seasonality else None
            seasonal = bool(use_seasonality)
            if seasonal:
                method = "holt_winters"

            points = []
            for h in range(1, horizon + 1):
                value = level + trend * h
                if indices:
                    value *= indices[(n + h - 1) % 12]
                points.append(max(0.0, value))

            residuals = [
                series[i] - (series[i - 1] if i else series[0])
                for i in range(1, n)
            ]
            sigma = statistics.pstdev(residuals) if len(residuals) > 1 else 0.0

            def fit_holt(train, h, _t=thresholds):
                lv, tr = holt(train, _t.holt_alpha, _t.holt_beta)
                return [max(0.0, lv + tr * (i + 1)) for i in range(h)]

            mape, bias = _accuracy(series, fit_holt, horizon)

            if not use_seasonality:
                caveats.append(
                    f"No seasonality is fitted. That needs "
                    f"{thresholds.min_periods_for_seasonality} months of history "
                    f"and there are {n}. A seasonal pattern fitted to {n} points "
                    f"would be a fit to noise."
                )
            reason = (
                f"Smooth demand, so level and trend are smoothed directly. "
                f"Running at {qty(level)} a month"
                + (f" and {'rising' if trend > 0 else 'falling'} by "
                   f"{qty(abs(trend))} a month." if abs(trend) > 0.5 else " and flat.")
            )

        # ------------------------------------------ intermittent: SBA
        elif dclass is DemandClass.INTERMITTENT:
            rate = sba(series, thresholds.croston_alpha)
            level = rate
            points = [max(0.0, rate)] * horizon

            nonzero = [v for v in series if v > 0]
            sigma = statistics.pstdev(nonzero) if len(nonzero) > 1 else 0.0

            def fit_sba(train, h, _t=thresholds):
                return [max(0.0, sba(train, _t.croston_alpha))] * h

            mape, bias = _accuracy(series, fit_sba, horizon)

            zero_months = len([v for v in series if v == 0])
            reason = (
                f"Intermittent demand: {zero_months} of {n} months had none. "
                f"Croston with the Syntetos-Boylan correction gives a rate of "
                f"{rate:,.2f} a month, which is a long-run rate and not a "
                f"prediction for any single month."
            )
            caveats.append(
                "The monthly figure will be wrong in almost every individual "
                "month, by design. It is right on average, which is what a "
                "reorder point needs."
            )

        # -------------------------------- lumpy, erratic: quantiles
        else:
            points = [empirical_quantile(series, 0.5)] * horizon
            level = points[0]
            sigma = statistics.pstdev(series) if n > 1 else 0.0

            def fit_q(train, h):
                return [empirical_quantile(train, 0.5)] * h

            mape, bias = _accuracy(series, fit_q, horizon)

            upper_q = empirical_quantile(series, target)
            reason = (
                f"{dclass.value.capitalize()} demand, so no point forecast is "
                f"meaningful. The median month is {qty(points[0])} and the "
                f"{target:.0%} month is {qty(upper_q)}; policy should be built "
                f"on the second figure."
            )
            caveats.append(
                "Quantiles come from the observed months only, so the forecast "
                "cannot exceed what has already been seen. A first large order "
                "will not be anticipated."
            )

        # Intervals widen with the square root of the horizon, which is the
        # right shape for accumulating uncertainty under independence.
        lower, upper = [], []
        for h, value in enumerate(points, start=1):
            spread = z * sigma * math.sqrt(h)
            lower.append(max(0.0, value - spread))
            upper.append(value + spread)

        if mape is None:
            caveats.append(
                f"Accuracy is not measured: {n} months is too few to hold any "
                f"back without making the fit worse than the test."
            )

        out.append(
            ForecastResult(
                item_key=item.item_key,
                entity_id=item.entity_id,
                location_id=item.location_id,
                demand_class=dclass,
                method=method,
                periods_observed=n,
                mean_monthly_demand=round(cls.mean_monthly_demand, 2),
                forecast=tuple(round(v, 2) for v in points),
                lower=tuple(round(v, 2) for v in lower),
                upper=tuple(round(v, 2) for v in upper),
                level=round(level, 2),
                trend=round(trend, 2),
                seasonality_fitted=seasonal,
                mape=mape,
                bias=bias,
                reason=reason,
                caveat=" ".join(caveats),
            )
        )

    # Worst accuracy first: those are the items whose policy is least trustworthy.
    out.sort(key=lambda r: (r.mape is None, -(r.mape or 0)))
    return tuple(out)
