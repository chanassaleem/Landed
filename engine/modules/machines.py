"""Module 7: placement economics.

The record no off-the-shelf system models. Cin7 tracks stock, Xero tracks
money, a CMMS tracks the asset, an equipment-rental platform tracks a rental
fee. None of them models an instrument placed with a hospital for free, whose
capital is recovered through a margin premium on every reagent test it runs.

That is the dominant commercial structure in diagnostics in this market, and it
has a failure mode that nothing in the accounts reveals: an instrument placed
against a volume forecast that never materialised. The capital is spent, the
contract is running down, and the reagent pull is a third of the plan. Nobody
notices until the contract ends and the machine comes back unpaid for.

So the measure is not revenue. It is recovery against capital, on the clock:

    net recovered   = reagent revenue * margin% - service cost to date
    monthly rate    = net recovered / months elapsed
    payback months  = capital / monthly rate
    verdict         = does payback land inside the contract?

Downtime is priced too. A machine down for twenty days did not just cost an
engineer's time, it lost twenty days of reagent pull, and that is the larger
number. Service and placement economics are the same question seen twice.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, List, Optional, Tuple

from engine.fmt import money
from engine.schema import (
    MachinePlacement,
    MachineResult,
    PaybackStatus,
    Thresholds,
)

DAYS_PER_MONTH = 30.0


def _months_between(start: date, end: date) -> float:
    return max(0.0, (end - start).days / DAYS_PER_MONTH)


def evaluate(
    placements: Iterable[MachinePlacement],
    thresholds: Thresholds | None = None,
    as_of: Optional[date] = None,
) -> Tuple[MachineResult, ...]:
    thresholds = thresholds or Thresholds()
    as_of = as_of or date.today()
    out: List[MachineResult] = []

    for m in placements:
        elapsed = _months_between(m.placed_date, as_of)
        remaining = max(0.0, m.contract_months - elapsed)

        margin = m.reagent_revenue_to_date * m.reagent_margin_pct
        net = margin - m.service_cost_to_date
        recovery_pct = (net / m.capital_cost) if m.capital_cost > 0 else 0.0

        rate: Optional[float] = (net / elapsed) if elapsed > 0 else None
        payback: Optional[float] = None
        if rate is not None and rate > 0 and m.capital_cost > 0:
            payback = m.capital_cost / rate

        utilisation: Optional[float] = None
        if m.expected_monthly_tests > 0:
            utilisation = m.actual_monthly_tests / m.expected_monthly_tests

        # Downtime is priced at lost reagent pull, not at engineer hours.
        per_test_margin = m.revenue_per_test * m.reagent_margin_pct
        daily_tests = m.actual_monthly_tests / DAYS_PER_MONTH
        downtime_lost = m.downtime_days_ytd * daily_tests * per_test_margin

        # ------------------------------------------------------- verdict

        shortfall = 0.0

        if elapsed < thresholds.placement_min_months_to_judge:
            status = PaybackStatus.RAMPING
            why = (
                f"Placed {elapsed:.0f} months ago. Too early to judge payback: "
                f"{money(net, thresholds.currency, thresholds.number_scale)} recovered of {money(m.capital_cost, thresholds.currency, thresholds.number_scale)} so far. "
                f"Reassess after {thresholds.placement_min_months_to_judge} months."
            )
        elif rate is None or rate <= 0:
            status = PaybackStatus.STALLED
            why = (
                f"No net recovery in {elapsed:.0f} months. "
                f"{money(m.capital_cost, thresholds.currency, thresholds.number_scale)} of capital is sitting in this placement "
                f"with nothing coming back."
            )
        elif payback is not None and payback > m.contract_months:
            status = PaybackStatus.UNRECOVERABLE
            shortfall = max(0.0, m.capital_cost - net - rate * remaining)
            why = (
                f"At the current pull this pays back in {payback:.0f} months "
                f"against a {m.contract_months} month contract. "
                f"About {money(shortfall, thresholds.currency, thresholds.number_scale)} will never be recovered "
                f"unless volume rises or the contract is extended."
            )
        elif payback is not None and payback <= thresholds.payback_target_months:
            status = PaybackStatus.AHEAD
            why = (
                f"Paying back in {payback:.0f} months against a "
                f"{thresholds.payback_target_months} month target. "
                f"{recovery_pct:.0%} of capital recovered so far."
            )
        else:
            status = PaybackStatus.ON_TRACK
            why = (
                f"Paying back in {payback:.0f} months, inside the "
                f"{m.contract_months} month contract but past the "
                f"{thresholds.payback_target_months} month target."
            )

        if utilisation is not None and utilisation < 0.7:
            why += (
                f" Running at {utilisation:.0%} of the forecast volume "
                f"it was placed against."
            )
        if downtime_lost > 0 and m.downtime_days_ytd > 0:
            why += (
                f" {m.downtime_days_ytd:.0f} days down this year cost about "
                f"{money(downtime_lost, thresholds.currency, thresholds.number_scale)} of reagent margin."
            )

        out.append(
            MachineResult(
                machine_key=m.machine_key,
                model=m.model,
                customer_name=m.customer_name or m.customer_key,
                entity_id=m.entity_id,
                location_id=m.location_id,
                months_elapsed=round(elapsed, 1),
                contract_months=m.contract_months,
                capital_cost=m.capital_cost,
                margin_recovered=round(margin, 2),
                service_cost_to_date=m.service_cost_to_date,
                net_recovered=round(net, 2),
                recovery_pct=round(recovery_pct, 4),
                monthly_margin_rate=round(rate, 2) if rate is not None else None,
                payback_months=round(payback, 1) if payback is not None else None,
                months_remaining_on_contract=round(remaining, 1),
                utilisation=round(utilisation, 4)
                if utilisation is not None
                else None,
                downtime_days_ytd=m.downtime_days_ytd,
                revenue_lost_to_downtime=round(downtime_lost, 2),
                status=status,
                reason=why,
                unrecoverable_shortfall=round(shortfall, 2),
            )
        )

    # Worst commercial position first: unrecovered capital at the top.
    priority = {
        PaybackStatus.UNRECOVERABLE: 0,
        PaybackStatus.STALLED: 1,
        PaybackStatus.ON_TRACK: 2,
        PaybackStatus.RAMPING: 3,
        PaybackStatus.AHEAD: 4,
    }
    out.sort(key=lambda r: (priority[r.status], -(r.capital_cost - r.net_recovered)))
    return tuple(out)
