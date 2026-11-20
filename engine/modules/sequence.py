"""Module 9: fundable order sequencing.

The question the whole engine exists to answer. Module 3 says policy wants
PKR 33.6M of purchase orders. Module 8 says the next thirteen weeks can fund
PKR 2.2M without dropping through the cash floor. Nobody needs to be told
there is a gap. They need to be told which orders to place.

Every ERP in this market produces the first list and none produces the second,
which is why purchasing in practice runs on whoever shouted most recently.

How the ranking works, and why this shape.

The score is the cost of NOT ordering, per rupee committed. Cost of not
ordering is built from what a stockout actually costs here: a clinically
critical reagent stopping a hospital's testing is not commensurate with a
spare lamp, and an item three days from running out is not commensurate with
one three months out. Dividing by the order value matters because cash is the
constraint: two rupees of protection for one rupee committed beats three for
two.

    urgency      = 1 / (days of cover remaining + 1)
    criticality  = clinical floor if flagged, else the ABC weight
    score        = urgency * criticality * annual consumption value
    per rupee    = score / order value

Transfers are netted off first. Module 4 may already cover part or all of a
shortfall by moving stock that is sitting above policy somewhere else, and
ordering what a van could deliver this week is the most expensive mistake
available. An order fully covered by a transfer is removed from the book
entirely, not merely deprioritised.

Then the ranked orders are packed into weeks against the headroom module 8
produced. Cash leaves at the letter of credit, which in this market is opened
at close to full value days after the order, not on arrival, so the money is
charged to the week the order is placed.

This is a greedy heuristic, not an optimum. A knapsack solver would place a
marginally better-packed set, and would also be unexplainable to the person
signing the cheque. Every line here carries the reason it was placed,
scheduled or refused, and that is worth more than the last few percent of
packing efficiency. Where it matters, the refused list is explicit about what
could not be funded rather than quietly dropping it.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from engine.fmt import money, qty
from engine.schema import (
    AbcClass,
    CashResult,
    OrderVerdict,
    SequencedOrder,
    SequencingResult,
    StockStatus,
    Thresholds,
    TransferSuggestion,
)

# Relative weight of a stockout by ABC class. A class A item is where the
# consumption value is, so a stockout there costs more than in class C.
ABC_WEIGHT = {AbcClass.A: 3.0, AbcClass.B: 2.0, AbcClass.C: 1.0}

# A clinically critical item outranks its ABC class, never the other way.
CLINICAL_WEIGHT = 6.0

DAYS_PER_MONTH = 30.0


def _days_of_cover(row: dict) -> Optional[float]:
    months = row.get("months_cover")
    if months is None:
        return None
    return months * DAYS_PER_MONTH


def _score(row: dict, clinical: bool, abc: AbcClass) -> Tuple[float, float]:
    """Returns (score, score per rupee committed)."""
    days = _days_of_cover(row)
    # An item with no demand history has no urgency to compute; it ranks last
    # rather than first, because a missing figure is not an emergency.
    urgency = 1.0 / (days + 1.0) if days is not None else 0.0
    weight = CLINICAL_WEIGHT if clinical else ABC_WEIGHT.get(abc, 1.0)

    # Annual consumption value scales the score so a stockout on a line that
    # turns over PKR 20M a year outranks one that turns over PKR 200K. Taken
    # from module 1 rather than recomputed here.
    annual_value = row.get("annual_consumption_value") or 0.0

    score = urgency * weight * max(annual_value, 1.0)
    value = row.get("order_value") or 0.0
    return score, (score / value if value > 0 else score)


def evaluate(
    stock_rows: Sequence[dict],
    cash: Optional[CashResult] = None,
    transfers: Sequence[dict] = (),
    thresholds: Thresholds | None = None,
    as_of: Optional[date] = None,
) -> SequencingResult:
    """Decide which suggested orders to place, when, and which cannot be.

    `stock_rows` are module 3's rows as engine.report shapes them, so this
    module never re-derives a reorder point. `cash` is module 8's view, which
    is computed without new orders precisely so it can be used here.
    """
    thresholds = thresholds or Thresholds()
    as_of = as_of or date.today()

    # ------------------------------------------- net off the transfers

    covered: Dict[Tuple[str, str, str], float] = defaultdict(float)
    for t in transfers:
        key = (t["item_key"], t["to_entity"], t["to_location"])
        covered[key] += t["quantity"]

    # ---------------------------------------------------- build the book

    candidates: List[dict] = []
    for row in stock_rows:
        if row["status"] not in (StockStatus.CRITICAL.value, StockStatus.REORDER.value):
            continue
        needed = row.get("suggested_order_qty") or 0.0
        if needed <= 0:
            continue

        key = (row["item_key"], row["entity_id"], row["location_id"])
        from_transfer = min(covered.get(key, 0.0), needed)
        remaining = needed - from_transfer
        unit_cost = row.get("unit_cost") or 0.0

        clinical = bool(row.get("is_clinical_critical", False))
        abc = AbcClass(row.get("abc", "C"))
        score, per_rupee = _score(row, clinical, abc)
        days = _days_of_cover(row)

        candidates.append(
            {
                "row": row,
                "key": key,
                "needed": needed,
                "from_transfer": from_transfer,
                "quantity": remaining,
                "value": remaining * unit_cost,
                "clinical": clinical,
                "abc": abc,
                "score": score,
                "per_rupee": per_rupee,
                "days": days,
            }
        )

    # Clinical first, then value protected per rupee committed. Clinical is a
    # separate tier rather than a large multiplier, so no amount of ordinary
    # consumption value can outrank a reagent a hospital needs.
    candidates.sort(key=lambda c: (not c["clinical"], -c["per_rupee"], -c["score"]))

    # ------------------------------------------------- weekly headroom

    weeks = len(cash.weeks) if cash else thresholds.cash_horizon_weeks
    if cash and cash.opening_balance:
        # Spend down to the floor, week by week, never below it. Each week's
        # own closing balance caps what that week can carry.
        headroom = [
            max(0.0, w.closing - thresholds.cash_buffer) for w in cash.weeks
        ]
        # An order placed in week k also has to be affordable in every later
        # week, because the money is gone from then on. So the usable headroom
        # in week k is the minimum from k onward.
        running = 0.0
        usable: List[float] = [0.0] * weeks
        for i in range(weeks - 1, -1, -1):
            running = headroom[i] if i == weeks - 1 else min(headroom[i], running)
            usable[i] = running
        headroom = usable
        budget_known = True
    else:
        headroom = [0.0] * weeks
        budget_known = False

    remaining_budget = list(headroom)

    # ------------------------------------------------------- the pack

    orders: List[SequencedOrder] = []

    for c in candidates:
        row = c["row"]
        lead = row.get("lead_time_days") or 0.0
        days = c["days"]

        # Last date an order can be placed and still land before the shelf is
        # empty. Negative means it is already too late to avoid a gap.
        place_by = (
            as_of + timedelta(days=max(0.0, (days or 0.0) - lead))
            if days is not None
            else None
        )

        # Fully covered by a transfer: off the book entirely.
        if c["quantity"] <= 0:
            orders.append(
                _order(c, OrderVerdict.TRANSFER_INSTEAD, None, place_by, as_of, lead,
                       f"Covered by moving {qty(c['from_transfer'])} units from "
                       f"another branch. Ordering this would pay an import price "
                       f"for stock the group already owns.")
            )
            continue

        if not budget_known:
            orders.append(
                _order(c, OrderVerdict.UNFUNDED, None, place_by, as_of, lead,
                       "No opening bank balance was given, so there is no budget "
                       "to sequence against. Enter the balance to get a schedule.")
            )
            continue

        # Earliest week whose remaining budget can carry it.
        placed_in: Optional[int] = None
        for week in range(weeks):
            if remaining_budget[week] >= c["value"]:
                placed_in = week
                break

        if placed_in is None:
            shortfall = c["value"] - max(remaining_budget) if remaining_budget else c["value"]
            note = (
                f"{money(c['value'])} cannot be funded in the next {weeks} weeks; "
                f"the best week is short by {money(max(0.0, shortfall))}."
            )
            if c["clinical"]:
                note += (
                    " This is clinically critical, so the choice is financing, "
                    "a part shipment, or telling the hospital."
                )
            orders.append(
                _order(c, OrderVerdict.UNFUNDED, None, place_by, as_of, lead, note)
            )
            continue

        # Commit it: the cash is gone from this week onward.
        for week in range(placed_in, weeks):
            remaining_budget[week] -= c["value"]

        verdict = OrderVerdict.PLACE_NOW if placed_in == 0 else OrderVerdict.SCHEDULED
        if placed_in == 0:
            note = (
                f"{qty(c['quantity'])} units at {money(c['value'])}, funded this "
                f"week."
            )
            if days is not None:
                note += (
                    f" {days:,.0f} days of cover left against a "
                    f"{lead:,.0f} day lead time"
                    + (", so it is already late." if days < lead else ".")
                )
        else:
            note = (
                f"Funded from week {placed_in + 1}, not before: placing it now "
                f"would take cash below the {money(thresholds.cash_buffer)} floor."
            )
            if days is not None and days < lead + placed_in * 7:
                note += " It will run out before this order lands."

        if c["from_transfer"] > 0:
            note += (
                f" {qty(c['from_transfer'])} of the shortfall is already covered "
                f"by a transfer and has been taken off this order."
            )

        orders.append(
            _order(c, verdict, placed_in, place_by, as_of, lead, note)
        )

    # ---------------------------------------------------------- totals

    requested = sum(c["needed"] * (c["row"].get("unit_cost") or 0.0)
                    for c in candidates)
    by_verdict = defaultdict(float)
    for o in orders:
        by_verdict[o.verdict] += o.value

    transfer_value = sum(
        c["from_transfer"] * (c["row"].get("unit_cost") or 0.0) for c in candidates
    )
    clinical_unfunded = sum(
        o.value for o in orders
        if o.verdict is OrderVerdict.UNFUNDED and o.is_clinical_critical
    )

    placed = by_verdict[OrderVerdict.PLACE_NOW]
    scheduled = by_verdict[OrderVerdict.SCHEDULED]
    unfunded = by_verdict[OrderVerdict.UNFUNDED]

    if not candidates:
        reason = "Nothing is below its reorder point, so there is nothing to sequence."
    elif not budget_known:
        reason = (
            f"{money(requested)} of orders are due across {len(candidates)} lines. "
            f"Without an opening bank balance they cannot be sequenced, only listed."
        )
    else:
        reason = (
            f"Of {money(requested)} that policy asks for, {money(placed)} can be "
            f"placed this week and {money(scheduled)} fits in later weeks. "
            f"{money(unfunded)} does not fit in {weeks} weeks at all."
        )
        if transfer_value > 0:
            reason += (
                f" A further {money(transfer_value)} comes off the book because a "
                f"transfer already covers it."
            )
        if clinical_unfunded > 0:
            reason += (
                f" {money(clinical_unfunded)} of what cannot be funded is "
                f"clinically critical."
            )

    caveats = [
        "This is a ranked, greedy schedule rather than a mathematical optimum. "
        "A solver would pack slightly more in; it could not tell you why it "
        "chose what it chose.",
        "Cash is charged to the week the order is placed, because a letter of "
        "credit in this market is opened at close to full value within days of "
        "the order rather than on arrival.",
    ]
    if not budget_known:
        caveats.append(
            "No opening balance was supplied, so every line is reported as "
            "unfunded. That is missing input, not a finding."
        )

    return SequencingResult(
        as_of=as_of,
        horizon_weeks=weeks,
        orders=tuple(orders),
        value_requested=round(requested, 2),
        value_placed_now=round(placed, 2),
        value_scheduled=round(scheduled, 2),
        value_unfunded=round(unfunded, 2),
        value_covered_by_transfer=round(transfer_value, 2),
        clinical_unfunded=round(clinical_unfunded, 2),
        reason=reason,
        caveat=" ".join(caveats),
    )


def _order(c, verdict, week, place_by, as_of, lead, note) -> SequencedOrder:
    row = c["row"]
    arrives = None
    if week is not None:
        arrives = as_of + timedelta(days=7 * week + lead)
    return SequencedOrder(
        item_key=row["item_key"],
        description=row.get("description", ""),
        entity_id=row["entity_id"],
        location_id=row["location_id"],
        quantity=round(c["quantity"], 2),
        value=round(c["value"], 2),
        verdict=verdict,
        week_index=week,
        place_by=place_by,
        arrives=arrives,
        days_of_cover_left=round(c["days"], 1) if c["days"] is not None else None,
        is_clinical_critical=c["clinical"],
        abc_class=c["abc"],
        priority_score=round(c["score"], 4),
        stockout_cost_per_rupee=round(c["per_rupee"], 6),
        covered_by_transfer=round(c["from_transfer"], 2),
        reason=note,
    )
