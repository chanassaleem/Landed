"""Module 6: service and workshop wastage.

Engineering and workshop wastage is invisible in every ERP in this market
because no ERP is told what a job *should* have cost. It records what was
billed. The gap between labour consumed and labour billed, and between parts
consumed and parts charged, is simply absent from the accounts.

That gap is real money. An engineer's hour has a rate whether or not anybody
invoices it, and a part fitted free was still bought with a letter of credit.

Three figures matter, and the third is the one that changes behaviour:

  1. Labour value lost  = (hours worked - hours billed) * labour rate
  2. Parts value lost   = parts cost - parts charged
  3. Repeat-visit rate  = the share of jobs that are a second visit for the
     same fault. A high rate means the first visit did not fix it, which is
     the most expensive failure mode in a field service operation.

Warranty jobs are counted but never treated as wastage. Work under warranty
is a cost of sale that was priced in at the point of supply; calling it
wastage would double count it and would point the fix at the wrong place.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from statistics import mean
from typing import Dict, Iterable, List, Optional

from engine.fmt import money
from engine.schema import ServiceJob, ServiceResult, Thresholds


def _machine_rollup(
    jobs: List[ServiceJob], rate: float
) -> tuple:
    """Per-machine wastage, worst first. Identifies the bad actors."""
    by_machine: Dict[str, List[ServiceJob]] = defaultdict(list)
    for j in jobs:
        by_machine[j.machine_key].append(j)

    rows = []
    for key, js in by_machine.items():
        billable = [j for j in js if not j.is_warranty]
        unbilled_hours = sum(
            max(0.0, j.labour_hours - j.labour_hours_billed) for j in billable
        )
        uncharged_parts = sum(
            max(0.0, j.parts_cost - j.parts_charged) for j in billable
        )
        lost = unbilled_hours * rate + uncharged_parts
        repeats = len([j for j in js if j.is_repeat_visit])
        rows.append(
            {
                "machine_key": key,
                "jobs": len(js),
                "warranty_jobs": len(js) - len(billable),
                "repeat_visits": repeats,
                "labour_hours": round(sum(j.labour_hours for j in js), 2),
                "labour_hours_unbilled": round(unbilled_hours, 2),
                "parts_uncharged": round(uncharged_parts, 2),
                "value_lost": round(lost, 2),
                "downtime_days": round(sum(j.downtime_days for j in js), 1),
            }
        )

    rows.sort(key=lambda r: (-r["value_lost"], r["machine_key"]))
    return tuple(rows)


def evaluate(
    jobs: Iterable[ServiceJob],
    thresholds: Thresholds | None = None,
    as_of: Optional[date] = None,
) -> ServiceResult:
    thresholds = thresholds or Thresholds()
    as_of = as_of or date.today()
    rows = list(jobs)
    rate = thresholds.labour_rate_per_hour

    billable = [j for j in rows if not j.is_warranty]

    labour_hours = sum(j.labour_hours for j in rows)
    unbilled_hours = sum(
        max(0.0, j.labour_hours - j.labour_hours_billed) for j in billable
    )
    parts_cost = sum(j.parts_cost for j in rows)
    uncharged_parts = sum(max(0.0, j.parts_cost - j.parts_charged) for j in billable)

    labour_lost = unbilled_hours * rate
    total_lost = labour_lost + uncharged_parts

    billed_hours = sum(j.labour_hours_billed for j in billable)
    billable_hours = sum(j.labour_hours for j in billable)
    recovery = (billed_hours / billable_hours) if billable_hours > 0 else None

    repeat_rate = (
        len([j for j in rows if j.is_repeat_visit]) / len(rows) if rows else None
    )

    closed = [j for j in rows if j.closed_date is not None]
    mean_close = (
        mean([(j.closed_date - j.opened_date).days for j in closed])
        if closed
        else None
    )

    open_jobs = len([j for j in rows if j.closed_date is None])

    if not rows:
        why = "No service jobs in the window."
    elif total_lost <= 0:
        why = "Every billable hour and part was recovered."
    else:
        parts = []
        if unbilled_hours > 0:
            parts.append(f"{unbilled_hours:,.0f} engineer hours were never billed")
        if uncharged_parts > 0:
            parts.append(f"{money(uncharged_parts, thresholds.currency, thresholds.number_scale)} "
                         f"of parts were fitted free")
        why = (
            " and ".join(parts)
            + f", costing {money(total_lost, thresholds.currency, thresholds.number_scale)} "
            + f"across {len(billable)} billable jobs."
        )
        if repeat_rate and repeat_rate > 0.2:
            why += (
                f" {repeat_rate:.0%} of jobs were repeat visits, so the first "
                f"visit is not fixing the fault."
            )

    return ServiceResult(
        as_of=as_of,
        jobs=len(rows),
        jobs_open=open_jobs,
        labour_hours=round(labour_hours, 2),
        labour_hours_unbilled=round(unbilled_hours, 2),
        labour_value_lost=round(labour_lost, 2),
        parts_cost=round(parts_cost, 2),
        parts_uncharged=round(uncharged_parts, 2),
        parts_value_lost=round(uncharged_parts, 2),
        total_value_lost=round(total_lost, 2),
        recovery_rate=round(recovery, 4) if recovery is not None else None,
        repeat_visit_rate=round(repeat_rate, 4) if repeat_rate is not None else None,
        mean_days_to_close=round(mean_close, 1) if mean_close is not None else None,
        downtime_days=round(sum(j.downtime_days for j in rows), 1),
        by_machine=_machine_rollup(rows, rate),
        reason=why,
    )
