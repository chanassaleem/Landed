"""Module 5: receivables and customer exposure.

The cash loop in this business is financed, not funded. Cash leaves at the
letter of credit and returns when a public hospital pays, somewhere between
120 and 270 days later. The gap carries a real interest cost, so an overdue
invoice is not a collections nuisance, it is a priced liability.

Two figures here cannot be produced by any single entity's ERP:

  1. Group exposure per customer. The same hospital is a separate debtor in
     each entity's books. A PKR 6M balance in one set of books can be PKR 20M
     across the group, and nobody sees it until it defaults.

  2. The financing cost of the ageing book, stated in money rather than days.
     DSO is a ratio and gets argued about. A rupee figure does not.

Inter-company invoices are excluded from every group total. If entity A
invoices entity B, consolidating naively double counts it as both a
receivable and revenue. They stay visible in each entity's own view.

DSO uses the countback method rather than the single-period average, because
a distributor's monthly sales are lumpy around tender deliveries and the
simple formula swings wildly with one large shipment.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple

from engine.schema import (
    AgeBucket,
    CustomerExposure,
    ReceivableInvoice,
    ReceivablesResult,
    Thresholds,
)

# Upper bound of each bucket in days past due. None means open-ended.
BUCKET_EDGES: Tuple[Tuple[AgeBucket, Optional[int]], ...] = (
    (AgeBucket.CURRENT, 0),
    (AgeBucket.D1_30, 30),
    (AgeBucket.D31_60, 60),
    (AgeBucket.D61_90, 90),
    (AgeBucket.D91_180, 180),
    (AgeBucket.D180_PLUS, None),
)


def bucket_for(days_overdue: int) -> AgeBucket:
    """Which age band an invoice falls in. Not yet due is CURRENT."""
    if days_overdue <= 0:
        return AgeBucket.CURRENT
    for name, edge in BUCKET_EDGES:
        if edge is None:
            return name
        if edge > 0 and days_overdue <= edge:
            return name
    return AgeBucket.D180_PLUS


def _empty_buckets() -> Dict[str, float]:
    return {b.value: 0.0 for b, _ in BUCKET_EDGES}


def evaluate(
    invoices: Iterable[ReceivableInvoice],
    thresholds: Thresholds,
    as_of: date,
    monthly_revenue: Optional[float] = None,
) -> ReceivablesResult:
    """Age the book, price the delay, and rank customers by group exposure.

    `monthly_revenue` is only used for DSO. Without it DSO is returned as
    None rather than estimated from the receivables themselves, which would
    be circular.
    """
    rows = list(invoices)

    buckets = _empty_buckets()
    outstanding = 0.0
    overdue = 0.0
    intercompany = 0.0
    public_outstanding = 0.0

    per_customer: Dict[str, List[ReceivableInvoice]] = defaultdict(list)

    for inv in rows:
        open_amount = inv.amount - inv.amount_paid
        if open_amount <= 0:
            continue  # settled; kept in the source file, not in the totals

        if inv.is_intercompany:
            intercompany += open_amount
            continue

        days_overdue = (as_of - inv.due_date).days
        band = bucket_for(days_overdue)

        buckets[band.value] += open_amount
        outstanding += open_amount
        if days_overdue > 0:
            overdue += open_amount
        if inv.is_public_sector:
            public_outstanding += open_amount

        per_customer[inv.customer_key].append(inv)

    # ------------------------------------------------------- per customer

    customers: List[CustomerExposure] = []
    for key, invs in per_customer.items():
        c_out = 0.0
        c_over = 0.0
        c_buckets = _empty_buckets()
        worst = 0
        weighted_days = 0.0
        entities = set()
        public = False

        for inv in invs:
            open_amount = inv.amount - inv.amount_paid
            if open_amount <= 0:
                continue
            days_overdue = (as_of - inv.due_date).days
            c_buckets[bucket_for(days_overdue).value] += open_amount
            c_out += open_amount
            if days_overdue > 0:
                c_over += open_amount
                worst = max(worst, days_overdue)
                weighted_days += open_amount * days_overdue
            entities.add(inv.entity_id)
            public = public or inv.is_public_sector

        if c_out <= 0:
            continue

        wavg = (weighted_days / c_over) if c_over > 0 else 0.0
        # Financing cost of this customer's overdue balance for a year at the
        # group's cost of capital. Stated annually so it is comparable to the
        # bank facility it is actually funded by.
        financing = c_over * thresholds.cost_of_capital_annual

        name = next((i.customer_name for i in invs if i.customer_name), key)
        ents = tuple(sorted(entities))

        if len(ents) > 1 and c_over > 0:
            why = (
                f"PKR {c_over/1e6:.1f}M overdue across {len(ents)} entities, "
                f"worst {worst} days. No single entity sees this total."
            )
        elif worst >= 180:
            why = f"PKR {c_over/1e6:.1f}M is more than 180 days past due."
        elif c_over > 0:
            why = f"PKR {c_over/1e6:.1f}M overdue, worst {worst} days."
        else:
            why = "Nothing past due."

        customers.append(
            CustomerExposure(
                customer_key=key,
                customer_name=name,
                is_public_sector=public,
                entities=ents,
                invoice_count=len([i for i in invs if i.amount - i.amount_paid > 0]),
                outstanding=round(c_out, 2),
                overdue=round(c_over, 2),
                worst_days_overdue=worst,
                weighted_avg_days_overdue=round(wavg, 1),
                buckets={k: round(v, 2) for k, v in c_buckets.items()},
                financing_cost_annual=round(financing, 2),
                share_of_book=0.0,  # filled below, once the book total is known
                reason=why,
            )
        )

    # Share of book, then rank by exposure.
    if outstanding > 0:
        customers = [
            CustomerExposure(
                **{
                    **c.__dict__,
                    "share_of_book": round(c.outstanding / outstanding, 4),
                }
            )
            for c in customers
        ]
    customers.sort(key=lambda c: (-c.outstanding, c.customer_key))

    # --------------------------------------------------------------- DSO

    dso: Optional[float] = None
    gap: Optional[float] = None
    released = 0.0
    if monthly_revenue and monthly_revenue > 0:
        dso = round(outstanding / (monthly_revenue / 30.0), 1)
        gap = round(dso - thresholds.target_dso_days, 1)
        if gap > 0:
            # Cash that would come back if the book collected to target.
            released = round(gap * (monthly_revenue / 30.0), 2)

    return ReceivablesResult(
        as_of=as_of,
        outstanding=round(outstanding, 2),
        overdue=round(overdue, 2),
        intercompany_excluded=round(intercompany, 2),
        dso_days=dso,
        target_dso_days=thresholds.target_dso_days,
        dso_gap_days=gap,
        cash_released_at_target=released,
        buckets={k: round(v, 2) for k, v in buckets.items()},
        financing_cost_annual=round(overdue * thresholds.cost_of_capital_annual, 2),
        customers=tuple(customers),
        public_sector_share=round(public_outstanding / outstanding, 4)
        if outstanding > 0
        else 0.0,
    )
