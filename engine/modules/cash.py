"""Module 8: the 13-week cash view.

The loop this business runs is financed, not funded. Cash leaves at the letter
of credit, roughly in full, and comes back when a public hospital pays, 120 to
270 days later. Everything else is detail. So the question a distributor's
owner actually has is not what the margin is, it is whether there is money in
the account in week six.

Three things here are deliberate.

Receipts are timed by how customers actually pay, not by their due dates. A
public hospital's 90 day terms are a formality; the ageing shows it settling at
150 days and more. Module 5 already measures each customer's value-weighted
lateness, so an invoice is expected at its due date plus that customer's own
observed lateness. Using due dates would produce a cash curve that is wrong in
a consistent and flattering direction.

Nothing is discounted for doubt by default. It would be easy to apply a
recovery curve by age bucket, and every such curve would be invented. The
engine assumes open invoices are eventually collected, says so in the caveat,
reports the balance sitting past 180 days separately so the owner can see what
that assumption rests on, and exposes a settable recovery rate for anyone who
wants to model less.

New purchase orders are NOT in this view. Modules 8 and 9 would otherwise be
circular: the cash view would need the orders, and the order sequencing would
need the cash. This module computes cash from commitments that already exist,
which yields headroom per week. Module 9 then packs orders into that headroom.
So `payments_orders` is always zero here, and module 9 is what fills it.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from engine.fmt import money
from engine.schema import (
    CashResult,
    CashWeek,
    Commitment,
    MachinePlacement,
    ReceivableInvoice,
    ServiceJob,
    Thresholds,
)

DAYS_PER_MONTH = 30.0


def _weeks(as_of: date, count: int) -> List[Tuple[date, date]]:
    """Week 0 contains as_of. Seven day blocks from as_of, not calendar weeks,
    because a calendar week would make week 0 a partial week of arbitrary
    length and the first number anyone reads would be the odd one."""
    return [
        (as_of + timedelta(days=7 * i), as_of + timedelta(days=7 * i + 6))
        for i in range(count)
    ]


def _week_of(when: date, as_of: date, count: int) -> Optional[int]:
    delta = (when - as_of).days
    if delta < 0:
        return 0  # already due; expected in the current week
    index = delta // 7
    return index if index < count else None


def customer_lateness(
    invoices: Sequence[ReceivableInvoice], as_of: date
) -> Dict[str, float]:
    """Value-weighted days late, per customer, from their own history.

    The same measure module 5 reports. A customer with no overdue history gets
    zero, which means they are expected to pay on their due date.
    """
    weighted: Dict[str, float] = defaultdict(float)
    value: Dict[str, float] = defaultdict(float)

    for inv in invoices:
        if inv.is_intercompany:
            continue
        open_amount = inv.amount - inv.amount_paid
        if open_amount <= 0:
            continue
        days_late = (as_of - inv.due_date).days
        if days_late > 0:
            weighted[inv.customer_key] += open_amount * days_late
            value[inv.customer_key] += open_amount

    return {
        key: weighted[key] / value[key]
        for key in weighted
        if value[key] > 0
    }


def expand_commitments(
    commitments: Sequence[Commitment], as_of: date, weeks: int
) -> List[Tuple[int, Commitment, float]]:
    """One entry per payment per week it lands in, recurrences included."""
    horizon_end = as_of + timedelta(days=7 * weeks)
    out: List[Tuple[int, Commitment, float]] = []

    for c in commitments:
        when = c.due_date
        # A recurring payment whose stated date has passed still recurs, so
        # wind it forward rather than dropping it.
        if c.is_recurring and c.recurs_every_days > 0:
            while when < as_of:
                when = when + timedelta(days=c.recurs_every_days)

        guard = 0
        while when < horizon_end and guard < 200:
            index = _week_of(when, as_of, weeks)
            if index is not None:
                out.append((index, c, c.amount))
            if not (c.is_recurring and c.recurs_every_days > 0):
                break
            when = when + timedelta(days=c.recurs_every_days)
            guard += 1

    return out


def _service_weekly_run_rate(
    jobs: Sequence[ServiceJob], as_of: date
) -> float:
    """Parts spend per week, from the observed window.

    A run rate, not a forecast. Stated as such, because projecting service
    cost properly needs a failure model this module does not have.
    """
    paid = [j for j in jobs if j.parts_cost > 0]
    if not paid:
        return 0.0
    first = min(j.opened_date for j in paid)
    span_weeks = max(1.0, (as_of - first).days / 7.0)
    return sum(j.parts_cost for j in paid) / span_weeks


def evaluate(
    invoices: Iterable[ReceivableInvoice] = (),
    commitments: Iterable[Commitment] = (),
    placements: Iterable[MachinePlacement] = (),
    jobs: Iterable[ServiceJob] = (),
    thresholds: Thresholds | None = None,
    as_of: Optional[date] = None,
    opening_balance: Optional[float] = None,
    horizon_weeks: Optional[int] = None,
    collection_rate: float = 1.0,
) -> CashResult:
    """Build the weekly view from flows that are already committed."""
    thresholds = thresholds or Thresholds()
    as_of = as_of or date.today()
    count = horizon_weeks or thresholds.cash_horizon_weeks

    invoices = list(invoices)
    commitments = list(commitments)
    placements = list(placements)
    jobs = list(jobs)

    spans = _weeks(as_of, count)
    receipts_ar = [0.0] * count
    receipts_reagent = [0.0] * count
    pay_commit = [0.0] * count
    pay_service = [0.0] * count
    lines: List[List[str]] = [[] for _ in range(count)]

    # ------------------------------------------------- receipts: invoices

    lateness = customer_lateness(invoices, as_of)
    past_180 = 0.0
    beyond_horizon = 0.0

    for inv in invoices:
        if inv.is_intercompany:
            continue  # nets out in the group view
        open_amount = inv.amount - inv.amount_paid
        if open_amount <= 0:
            continue

        days_late = (as_of - inv.due_date).days
        if days_late > 180:
            past_180 += open_amount

        late_by = lateness.get(inv.customer_key, 0.0)
        expected = inv.due_date + timedelta(days=round(late_by))

        if expected < as_of:
            # Later than this customer's own average, which by construction is
            # about half the open book. Spread across the catch-up window
            # rather than banked in week one.
            spread = max(1, min(thresholds.overdue_catchup_weeks, count))
            share = (open_amount * collection_rate) / spread
            for k in range(spread):
                receipts_ar[k] += share
            continue

        index = _week_of(expected, as_of, count)
        if index is None:
            beyond_horizon += open_amount
            continue
        receipts_ar[index] += open_amount * collection_rate

    # ------------------------------------------- receipts: reagent pull

    weekly_reagent = 0.0
    for m in placements:
        monthly = m.actual_monthly_tests * m.revenue_per_test
        weekly_reagent += monthly * 12.0 / 52.0
    if weekly_reagent > 0:
        for i in range(count):
            receipts_reagent[i] = weekly_reagent

    # ------------------------------------------------------- payments

    for index, c, amount in expand_commitments(commitments, as_of, count):
        pay_commit[index] += amount
        lines[index].append(f"{c.label}: {money(amount)}")

    weekly_service = _service_weekly_run_rate(jobs, as_of)
    if weekly_service > 0:
        for i in range(count):
            pay_service[i] = weekly_service

    # -------------------------------------------------------- the walk

    known = opening_balance is not None
    balance = float(opening_balance or 0.0)

    weeks: List[CashWeek] = []
    lowest = balance
    lowest_index = 0
    negatives = 0

    for i, (starts, ends) in enumerate(spans):
        opening = balance
        inflow = receipts_ar[i] + receipts_reagent[i]
        outflow = pay_commit[i] + pay_service[i]
        closing = opening + inflow - outflow
        balance = closing

        # Against the buffer, not against zero. A balance that technically
        # stays positive but eats the float the business runs on is not a
        # week that worked.
        under = known and closing < thresholds.cash_buffer
        if under:
            negatives += 1
        if closing < lowest:
            lowest = closing
            lowest_index = i

        week_lines = list(lines[i])
        if receipts_ar[i] > 0:
            week_lines.insert(0, f"Collections expected: {money(receipts_ar[i])}")
        if receipts_reagent[i] > 0:
            week_lines.insert(1, f"Reagent pull: {money(receipts_reagent[i])}")
        if pay_service[i] > 0:
            week_lines.append(f"Service parts run rate: {money(pay_service[i])}")

        weeks.append(
            CashWeek(
                index=i,
                starts=starts,
                ends=ends,
                opening=round(opening, 2),
                receipts_receivables=round(receipts_ar[i], 2),
                receipts_reagent=round(receipts_reagent[i], 2),
                payments_commitments=round(pay_commit[i], 2),
                payments_orders=0.0,     # module 9 fills this
                payments_service=round(pay_service[i], 2),
                closing=round(closing, 2),
                is_negative=bool(under),
                lines=tuple(week_lines),
            )
        )

    total_in = sum(receipts_ar) + sum(receipts_reagent)
    total_out = sum(pay_commit) + sum(pay_service)

    # Headroom: the most that could be committed to new orders without any
    # week dropping through the buffer. Taken from the worst week, because a
    # single bad week is what bounces a payment.
    if known:
        headroom = max(0.0, lowest - thresholds.cash_buffer)
    else:
        headroom = 0.0

    # ---------------------------------------------------------- wording

    caveats: List[str] = []
    if not known:
        caveats.append(
            "No opening bank balance was given, so these are changes in cash "
            "rather than balances. The shape of the weeks is right and the "
            "level is unknown."
        )
    if collection_rate >= 1.0:
        caveats.append(
            "Every open invoice is assumed to be collected eventually. No "
            "recovery discount is applied, because any such curve would be "
            "invented rather than measured."
        )
        if past_180 > 0:
            caveats.append(
                f"That assumption carries {money(past_180)} already past 180 "
                f"days. Set a collection rate to model less than full recovery."
            )
    if beyond_horizon > 0:
        caveats.append(
            f"{money(beyond_horizon)} is expected after this {count} week "
            f"horizon, on current payment behaviour, so it is not in these "
            f"figures."
        )
    caveats.append(
        f"Invoices already later than their customer's own average are spread "
        f"evenly over the next {min(thresholds.overdue_catchup_weeks, count)} "
        f"weeks rather than expected at once, which is an assumption about "
        f"catch-up and not a measurement."
    )
    caveats.append(
        "New purchase orders are not included. This is the cash that exists "
        "before ordering anything, which is what order sequencing is then "
        "measured against."
    )
    if not commitments:
        caveats.append(
            "No committed payments were supplied. Bank repayments, letters of "
            "credit, payroll and rent go out whatever the stock position says, "
            "so without commitments.csv this view is optimistic."
        )

    if not known:
        reason = (
            f"Cash moves by {money(total_in - total_out)} across "
            f"{count} weeks: {money(total_in)} in, {money(total_out)} out. "
            f"The worst point is week {lowest_index + 1}."
        )
    elif negatives:
        reason = (
            f"Cash drops below the {money(thresholds.cash_buffer)} floor in "
            f"{negatives} of {count} weeks, worst in week {lowest_index + 1} at "
            f"{money(lowest)}. There is no headroom for new orders until that "
            f"is fixed."
        )
    else:
        reason = (
            f"Cash stays above the {money(thresholds.cash_buffer)} floor all "
            f"{count} weeks, bottoming at {money(lowest)} in week "
            f"{lowest_index + 1}. That leaves {money(headroom)} for new orders."
        )

    return CashResult(
        as_of=as_of,
        opening_balance=round(float(opening_balance or 0.0), 2),
        weeks=tuple(weeks),
        lowest_balance=round(lowest, 2),
        lowest_week_index=lowest_index,
        weeks_negative=negatives,
        total_receipts=round(total_in, 2),
        total_payments=round(total_out, 2),
        closing_balance=round(balance, 2),
        headroom_for_orders=round(headroom, 2),
        reason=reason,
        caveat=" ".join(caveats),
    )
