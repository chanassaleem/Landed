"""Module 9 tests.

Stock rows are built by hand to the shape engine.report produces, so the
sequencing logic is tested without dragging modules 1 and 3 in with it.
"""

from datetime import date, timedelta

import pytest

from engine.modules.cash import evaluate as cash_evaluate
from engine.modules.sequence import evaluate
from engine.schema import (
    Commitment,
    CommitmentKind,
    OrderVerdict,
    StockStatus,
    Thresholds,
)

AS_OF = date(2026, 11, 20)
T = Thresholds()


def row(item="RGT-1", **kw):
    base = dict(
        entity_id="DX", location_id="KHI", item_key=item, description="Pack",
        category="Reagent", status=StockStatus.REORDER.value, abc="A",
        suggested_order_qty=100.0, unit_cost=1_000.0, order_value=100_000.0,
        months_cover=2.0, on_hand=200.0, lead_time_days=60.0,
        is_clinical_critical=False, annual_consumption_value=5_000_000.0,
        mean_monthly_demand=100.0,
    )
    base.update(kw)
    base["order_value"] = base["suggested_order_qty"] * base["unit_cost"]
    return base


def budget(amount, weeks=13, buffer_=0.0):
    """A cash view with a flat, known balance, so headroom is predictable."""
    t = Thresholds(cash_buffer=buffer_, cash_horizon_weeks=weeks)
    return cash_evaluate(thresholds=t, as_of=AS_OF, opening_balance=amount,
                         horizon_weeks=weeks), t


# ------------------------------------------------------- the basics


def test_an_affordable_order_is_placed_this_week():
    cash, t = budget(1_000_000)
    r = evaluate([row()], cash, [], t, AS_OF)
    assert r.orders[0].verdict is OrderVerdict.PLACE_NOW
    assert r.orders[0].week_index == 0
    assert r.value_placed_now == 100_000


def test_an_item_within_policy_is_not_sequenced_at_all():
    cash, t = budget(1_000_000)
    r = evaluate([row(status=StockStatus.OK.value)], cash, [], t, AS_OF)
    assert r.orders == ()
    assert "nothing to sequence" in r.reason


def test_an_excess_item_is_never_ordered():
    cash, t = budget(1_000_000)
    r = evaluate([row(status=StockStatus.EXCESS.value)], cash, [], t, AS_OF)
    assert r.orders == ()


def test_a_zero_quantity_suggestion_is_skipped():
    cash, t = budget(1_000_000)
    r = evaluate([row(suggested_order_qty=0.0)], cash, [], t, AS_OF)
    assert r.orders == ()


# ------------------------------------------ transfers come off first


def test_an_order_fully_covered_by_a_transfer_is_taken_off_the_book():
    """Ordering what a van could deliver this week is the most expensive
    mistake available."""
    cash, t = budget(10_000_000)
    transfers = [{"item_key": "RGT-1", "to_entity": "DX", "to_location": "KHI",
                  "quantity": 100.0}]
    r = evaluate([row()], cash, transfers, t, AS_OF)
    o = r.orders[0]
    assert o.verdict is OrderVerdict.TRANSFER_INSTEAD
    assert o.quantity == 0
    assert o.value == 0
    assert o.covered_by_transfer == 100
    assert "import price for stock the group already owns" in o.reason


def test_a_partial_transfer_reduces_the_order_rather_than_cancelling_it():
    cash, t = budget(10_000_000)
    transfers = [{"item_key": "RGT-1", "to_entity": "DX", "to_location": "KHI",
                  "quantity": 60.0}]
    r = evaluate([row()], cash, transfers, t, AS_OF)
    o = r.orders[0]
    assert o.verdict is OrderVerdict.PLACE_NOW
    assert o.quantity == 40
    assert o.value == 40_000
    assert "taken off this order" in o.reason


def test_a_transfer_to_another_branch_does_not_reduce_this_one():
    cash, t = budget(10_000_000)
    transfers = [{"item_key": "RGT-1", "to_entity": "DX", "to_location": "LHR",
                  "quantity": 100.0}]
    r = evaluate([row(location_id="KHI")], cash, transfers, t, AS_OF)
    assert r.orders[0].quantity == 100


def test_a_transfer_larger_than_the_need_does_not_go_negative():
    cash, t = budget(10_000_000)
    transfers = [{"item_key": "RGT-1", "to_entity": "DX", "to_location": "KHI",
                  "quantity": 500.0}]
    r = evaluate([row()], cash, transfers, t, AS_OF)
    assert r.orders[0].quantity == 0
    assert r.orders[0].covered_by_transfer == 100


# ----------------------------------------------------- the ranking


def test_clinical_items_outrank_everything_else():
    """No amount of ordinary consumption value may outrank a reagent a
    hospital needs, which is why clinical is a tier and not a multiplier."""
    cash, t = budget(100_000)  # only one of the two fits
    rows = [
        row("ORDINARY", is_clinical_critical=False,
            annual_consumption_value=500_000_000.0, months_cover=0.1),
        row("CLINICAL", is_clinical_critical=True,
            annual_consumption_value=1_000.0, months_cover=5.0),
    ]
    r = evaluate(rows, cash, [], t, AS_OF)
    by_key = {o.item_key: o for o in r.orders}
    assert by_key["CLINICAL"].verdict is OrderVerdict.PLACE_NOW
    assert by_key["ORDINARY"].verdict is not OrderVerdict.PLACE_NOW


def test_a_nearer_stockout_outranks_a_distant_one():
    cash, t = budget(100_000)
    rows = [row("SOON", months_cover=0.2), row("LATER", months_cover=6.0)]
    r = evaluate(rows, cash, [], t, AS_OF)
    by_key = {o.item_key: o for o in r.orders}
    assert by_key["SOON"].priority_score > by_key["LATER"].priority_score
    assert by_key["SOON"].verdict is OrderVerdict.PLACE_NOW


def test_ranking_is_per_rupee_because_cash_is_the_constraint():
    """Two rupees of protection for one rupee committed beats three for two."""
    cash, t = budget(1_000_000)
    cheap = row("CHEAP", suggested_order_qty=10.0, unit_cost=1_000.0,
                annual_consumption_value=1_000_000.0, months_cover=1.0)
    dear = row("DEAR", suggested_order_qty=1_000.0, unit_cost=1_000.0,
               annual_consumption_value=1_200_000.0, months_cover=1.0)
    r = evaluate([dear, cheap], cash, [], t, AS_OF)
    assert r.orders[0].item_key == "CHEAP"


def test_a_higher_abc_class_outranks_a_lower_one_all_else_equal():
    cash, t = budget(100_000)
    rows = [row("CLASS-C", abc="C"), row("CLASS-A", abc="A")]
    r = evaluate(rows, cash, [], t, AS_OF)
    assert r.orders[0].item_key == "CLASS-A"


def test_an_item_with_no_demand_history_ranks_last_not_first():
    """A missing figure is not an emergency."""
    cash, t = budget(100_000)
    rows = [row("KNOWN", months_cover=3.0), row("UNKNOWN", months_cover=None)]
    r = evaluate(rows, cash, [], t, AS_OF)
    assert r.orders[-1].item_key == "UNKNOWN"
    assert r.orders[-1].days_of_cover_left is None


# --------------------------------------------------- the packing


def test_what_does_not_fit_this_week_is_scheduled_not_refused():
    # Flat 150,000 of headroom: the first order fits, the second does not.
    cash, t = budget(150_000)
    rows = [row("A", months_cover=1.0), row("B", months_cover=2.0)]
    r = evaluate(rows, cash, [], t, AS_OF)
    verdicts = {o.item_key: o.verdict for o in r.orders}
    assert verdicts["A"] is OrderVerdict.PLACE_NOW
    assert verdicts["B"] in (OrderVerdict.SCHEDULED, OrderVerdict.UNFUNDED)


def test_committing_cash_removes_it_from_every_later_week_too():
    """The money is gone from the week it is spent onward, so a second order
    cannot be funded out of the same rupees next week."""
    cash, t = budget(100_000)
    rows = [row("A"), row("B")]
    r = evaluate(rows, cash, [], t, AS_OF)
    placed = [o for o in r.orders if o.verdict in
              (OrderVerdict.PLACE_NOW, OrderVerdict.SCHEDULED)]
    assert len(placed) == 1
    assert r.value_unfunded == 100_000


def test_an_order_too_large_for_any_week_is_refused_explicitly():
    cash, t = budget(50_000)
    r = evaluate([row(suggested_order_qty=1_000.0)], cash, [], t, AS_OF)
    o = r.orders[0]
    assert o.verdict is OrderVerdict.UNFUNDED
    assert o.week_index is None
    assert "short by" in o.reason


def test_a_clinical_refusal_says_what_the_real_choice_is():
    cash, t = budget(10_000)
    r = evaluate([row(is_clinical_critical=True, suggested_order_qty=1_000.0)],
                 cash, [], t, AS_OF)
    assert r.orders[0].verdict is OrderVerdict.UNFUNDED
    assert "financing, a part shipment, or telling the hospital" in r.orders[0].reason
    assert r.clinical_unfunded == 1_000_000


def test_the_cash_floor_is_respected():
    """Spending down to zero is not the same as spending down to the floor."""
    cash = cash_evaluate(thresholds=Thresholds(cash_buffer=80_000),
                         as_of=AS_OF, opening_balance=100_000)
    t = Thresholds(cash_buffer=80_000)
    r = evaluate([row()], cash, [], t, AS_OF)
    # Only 20,000 of headroom against a 100,000 order.
    assert r.orders[0].verdict is OrderVerdict.UNFUNDED


def test_a_later_week_is_used_when_cash_arrives_then():
    """A dip now and money later should schedule, not refuse."""
    t = Thresholds(cash_buffer=0.0)
    cash = cash_evaluate(
        commitments=[Commitment(label="dip", kind=CommitmentKind.OTHER,
                                due_date=AS_OF, amount=90_000)],
        thresholds=t, as_of=AS_OF, opening_balance=100_000,
    )
    r = evaluate([row(suggested_order_qty=10.0)], cash, [], t, AS_OF)
    assert r.orders[0].verdict is OrderVerdict.PLACE_NOW  # 10,000 still fits


# ----------------------------------------------------- the dates


def test_place_by_is_the_last_date_an_order_still_arrives_in_time():
    cash, t = budget(1_000_000)
    # 90 days of cover, 60 day lead time: 30 days to decide.
    r = evaluate([row(months_cover=3.0, lead_time_days=60.0)], cash, [], t, AS_OF)
    assert r.orders[0].place_by == AS_OF + timedelta(days=30)


def test_an_item_already_past_its_decision_date_is_said_to_be_late():
    cash, t = budget(1_000_000)
    # 30 days of cover against a 60 day lead time: already too late.
    r = evaluate([row(months_cover=1.0, lead_time_days=60.0)], cash, [], t, AS_OF)
    assert r.orders[0].place_by == AS_OF
    assert "already late" in r.orders[0].reason


def test_arrival_accounts_for_the_scheduled_week_and_the_lead_time():
    cash, t = budget(1_000_000)
    r = evaluate([row(lead_time_days=60.0)], cash, [], t, AS_OF)
    assert r.orders[0].arrives == AS_OF + timedelta(days=60)


# -------------------------------------------------------- honesty


def test_without_a_known_balance_nothing_is_scheduled_and_it_says_why():
    """Reporting every line as unfunded would read as a finding. It is
    missing input."""
    cash = cash_evaluate(thresholds=T, as_of=AS_OF)  # no opening balance
    r = evaluate([row()], cash, [], T, AS_OF)
    assert r.orders[0].verdict is OrderVerdict.UNFUNDED
    assert "no budget to sequence against" in r.orders[0].reason
    assert "missing input, not a finding" in r.caveat


def test_the_heuristic_is_declared_rather_than_dressed_up_as_optimal():
    cash, t = budget(1_000_000)
    r = evaluate([row()], cash, [], t, AS_OF)
    assert "rather than a mathematical optimum" in r.caveat


def test_the_letter_of_credit_timing_assumption_is_stated():
    cash, t = budget(1_000_000)
    r = evaluate([row()], cash, [], t, AS_OF)
    assert "letter of credit" in r.caveat


def test_the_totals_reconcile_against_what_policy_asked_for():
    cash, t = budget(250_000)
    rows = [row("A"), row("B"), row("C"), row("D")]
    transfers = [{"item_key": "D", "to_entity": "DX", "to_location": "KHI",
                  "quantity": 100.0}]
    r = evaluate(rows, cash, transfers, t, AS_OF)
    accounted = (r.value_placed_now + r.value_scheduled + r.value_unfunded
                 + r.value_covered_by_transfer)
    assert accounted == pytest.approx(r.value_requested)


def test_every_order_carries_a_reason():
    cash, t = budget(150_000)
    rows = [row("A"), row("B"), row("C", suggested_order_qty=10_000.0)]
    r = evaluate(rows, cash, [], t, AS_OF)
    assert all(o.reason.strip() for o in r.orders)


def test_no_stock_and_no_cash_is_safe():
    r = evaluate([], None, [], T, AS_OF)
    assert r.orders == ()
    assert r.value_requested == 0
