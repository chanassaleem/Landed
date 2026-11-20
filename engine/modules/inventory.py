"""Module 3: inventory policy.

Replaces the months-of-cover rule from the earlier prototype. Safety stock
comes from the target service level for the item's segment and from the
variability of BOTH demand and lead time:

    SS  = z * sqrt( L * sigma_d^2 + d^2 * sigma_L^2 )
    ROP = d * L + SS

where d and sigma_d are DAILY and L and sigma_L are in days.

Lead-time variability dominates for an importer. A 75-day lead time that
slips three weeks is the normal case, not the exception, so sigma_L must be
measured from actual receipt dates rather than assumed to be zero.

Months of cover is still reported, because it is what the staff already
think in. It no longer drives any decision.
"""

from __future__ import annotations

import math
from datetime import date
from statistics import NormalDist
from typing import Dict, Iterable, List, Optional

from engine.schema import (
    ClassificationResult,
    DemandClass,
    InventoryResult,
    ItemSnapshot,
    ServiceMetric,
    StockStatus,
    Thresholds,
)

from engine.modules.classify import key_for

DAYS_PER_MONTH = 30.0

# Intermittent and lumpy demand is measured on cycle service level;
# steady demand under continuous review is measured on fill rate.
_METRIC_BY_CLASS = {
    DemandClass.SMOOTH: ServiceMetric.FILL_RATE,
    DemandClass.ERRATIC: ServiceMetric.FILL_RATE,
    DemandClass.INTERMITTENT: ServiceMetric.CYCLE_SERVICE_LEVEL,
    DemandClass.LUMPY: ServiceMetric.CYCLE_SERVICE_LEVEL,
    DemandClass.NO_HISTORY: ServiceMetric.CYCLE_SERVICE_LEVEL,
}


def service_target(
    cls: ClassificationResult, item: ItemSnapshot, thresholds: Thresholds
) -> float:
    """Target for this item's segment, floored by clinical criticality."""
    key = (cls.abc_class.value, cls.demand_class.value)
    target = thresholds.service_targets.get(key, 0.90)
    if item.is_clinical_critical:
        target = max(target, thresholds.clinical_critical_floor)
    return target


def z_for(service_level: float) -> float:
    """Inverse normal. Uses the standard library, so no scipy dependency."""
    service_level = min(max(service_level, 0.5), 0.9999)
    return NormalDist().inv_cdf(service_level)


def safety_stock(
    daily_demand: float,
    daily_demand_std: float,
    lead_days: float,
    lead_days_std: float,
    z: float,
) -> float:
    variance = lead_days * (daily_demand_std ** 2) + (daily_demand ** 2) * (
        lead_days_std ** 2
    )
    return z * math.sqrt(max(variance, 0.0))


def _monthly_std_to_daily(monthly_std: float) -> float:
    """Scale monthly variability to daily.

    Assumes independence between days, so sigma_daily = sigma_monthly / sqrt(30).
    This is an approximation and is documented as one. When daily history is
    available, measure sigma_daily directly instead.
    """
    return monthly_std / math.sqrt(DAYS_PER_MONTH)


def evaluate(
    items: Iterable[ItemSnapshot],
    classes: Dict[tuple, ClassificationResult],
    thresholds: Thresholds | None = None,
    as_of: Optional[date] = None,
) -> List[InventoryResult]:
    thresholds = thresholds or Thresholds()
    as_of = as_of or date.today()
    out: List[InventoryResult] = []

    for item in items:
        cls = classes[key_for(item.item_key, getattr(item, "location_id", ""))]

        monthly = cls.mean_monthly_demand
        daily = monthly / DAYS_PER_MONTH
        daily_std = _monthly_std_to_daily(cls.std_monthly_demand)

        target = service_target(cls, item, thresholds)
        z = z_for(target)

        ss = safety_stock(
            daily, daily_std, item.lead_time_days, item.lead_time_days_std, z
        )
        rop = daily * item.lead_time_days + ss
        order_up_to = rop + thresholds.target_cover_months * monthly

        position = item.on_hand + item.open_po
        cover = (item.on_hand / monthly) if monthly > 0 else None

        days_to_expiry = (
            (item.expiry_date - as_of).days if item.expiry_date else None
        )

        # Expiry beats cover: stock that will not be consumed before it
        # expires is a write-off, whatever its cover figure says.
        expiry_risk = (
            days_to_expiry is not None
            and monthly > 0
            and days_to_expiry < (item.on_hand / monthly) * DAYS_PER_MONTH
        )

        if monthly <= 0:
            status = StockStatus.EXCESS if item.on_hand > 0 else StockStatus.OK
            reason = "No demand recorded in the history window"
        elif expiry_risk:
            status = StockStatus.EXPIRY_RISK
            reason = (
                f"{item.on_hand:,.0f} units expire in {days_to_expiry} days, "
                f"but only about {daily * days_to_expiry:,.0f} will be used by then"
            )
        elif position <= rop and cover is not None and cover < 1:
            status = StockStatus.CRITICAL
            reason = (
                f"Position {position:,.0f} is below the reorder point "
                f"{rop:,.0f} with under one month of cover"
            )
        elif position <= rop:
            status = StockStatus.REORDER
            reason = f"Position {position:,.0f} is at or below the reorder point {rop:,.0f}"
        elif cover is not None and cover > thresholds.excess_cover_months:
            status = StockStatus.EXCESS
            reason = (
                f"{cover:.1f} months of cover against a "
                f"{thresholds.excess_cover_months:.0f} month limit"
            )
        else:
            status = StockStatus.OK
            reason = "Within policy"

        suggested = 0.0
        if status in (StockStatus.CRITICAL, StockStatus.REORDER):
            suggested = max(0.0, math.ceil(order_up_to - position))

        excess_value = 0.0
        if status == StockStatus.EXCESS and cover is not None:
            surplus_months = max(0.0, cover - thresholds.excess_cover_months)
            excess_value = surplus_months * monthly * item.unit_cost
        elif status == StockStatus.EXPIRY_RISK and monthly > 0:
            at_risk_units = max(0.0, item.on_hand - daily * (days_to_expiry or 0))
            excess_value = at_risk_units * item.unit_cost

        out.append(
            InventoryResult(
                item_key=item.item_key,
                description=item.description,
                on_hand=item.on_hand,
                open_po=item.open_po,
                position=position,
                demand_class=cls.demand_class,
                abc_class=cls.abc_class,
                service_target=target,
                service_metric=_METRIC_BY_CLASS[cls.demand_class],
                safety_stock=ss,
                reorder_point=rop,
                order_up_to=order_up_to,
                suggested_order_qty=suggested,
                months_cover=cover,
                status=status,
                stock_value=item.on_hand * item.unit_cost,
                excess_value=excess_value,
                days_to_expiry=days_to_expiry,
                reason=reason,
            )
        )

    # Rank by cash at stake so the caller never has to decide ordering.
    priority = {
        StockStatus.CRITICAL: 0,
        StockStatus.EXPIRY_RISK: 1,
        StockStatus.REORDER: 2,
        StockStatus.EXCESS: 3,
        StockStatus.OK: 4,
    }
    out.sort(key=lambda r: (priority[r.status], -max(r.excess_value, r.stock_value)))
    return out
