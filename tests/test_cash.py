"""Module 8 tests.

Amounts and dates are chosen so each week's arithmetic can be checked by hand.
"""

from datetime import date, timedelta

import pytest

from engine.modules.cash import (
    customer_lateness,
    evaluate,
    expand_commitments,
)
from engine.schema import (
    Commitment,
    CommitmentKind,
    MachinePlacement,
    ReceivableInvoice,
    ServiceJob,
    Thresholds,
)

AS_OF = date(2026, 11, 20)
T = Thresholds()


def inv(cust, due_offset, amount, **kw):
    due = AS_OF + timedelta(days=due_offset)
    return ReceivableInvoice(
        invoice_no=kw.pop("no", f"I-{cust}-{due_offset}"),
        customer_key=cust,
        customer_name=cust,
        invoice_date=due - timedelta(days=30),
        due_date=due,
        amount=float(amount),
        **kw,
    )


def commit(label, day_offset, amount, **kw):
    return Commitment(
        label=label,
        kind=kw.pop("kind", CommitmentKind.OTHER),
        due_date=AS_OF + timedelta(days=day_offset),
        amount=float(amount),
        **kw,
    )


# --------------------------------------------------------- week mapping


def test_week_one_contains_the_as_of_date():
    r = evaluate(commitments=[commit("x", 0, 100)], thresholds=T, as_of=AS_OF,
                 opening_balance=1_000)
    assert r.weeks[0].starts == AS_OF
    assert r.weeks[0].payments_commitments == 100


@pytest.mark.parametrize("offset,week", [(0, 0), (6, 0), (7, 1), (13, 1), (14, 2)])
def test_payments_land_in_seven_day_blocks_from_as_of(offset, week):
    r = evaluate(commitments=[commit("x", offset, 500)], thresholds=T,
                 as_of=AS_OF, opening_balance=0)
    assert r.weeks[week].payments_commitments == 500


def test_the_horizon_is_thirteen_weeks_by_default_and_configurable():
    assert len(evaluate(thresholds=T, as_of=AS_OF).weeks) == 13
    assert len(evaluate(thresholds=T, as_of=AS_OF, horizon_weeks=4).weeks) == 4


def test_a_payment_beyond_the_horizon_is_left_out():
    r = evaluate(commitments=[commit("far", 400, 9_000)], thresholds=T,
                 as_of=AS_OF, opening_balance=0)
    assert sum(w.payments_commitments for w in r.weeks) == 0


# -------------------------------------------------------- recurrence


def test_a_monthly_commitment_repeats_across_the_horizon():
    rows = expand_commitments(
        [commit("payroll", 1, 1_000, is_recurring=True, recurs_every_days=30)],
        AS_OF, 13,
    )
    # 13 weeks is 91 days, so a 30 day cycle lands three or four times.
    assert 3 <= len(rows) <= 4


def test_a_one_off_commitment_happens_once():
    rows = expand_commitments([commit("lc", 20, 5_000)], AS_OF, 13)
    assert len(rows) == 1


def test_a_recurring_commitment_with_a_past_date_is_wound_forward():
    """A standing payment does not stop existing because its stated date has
    gone by."""
    rows = expand_commitments(
        [commit("rent", -45, 700, is_recurring=True, recurs_every_days=30)],
        AS_OF, 13,
    )
    assert rows
    assert all(index >= 0 for index, _, _ in rows)


def test_recurrence_cannot_loop_forever():
    rows = expand_commitments(
        [commit("daily", 0, 1, is_recurring=True, recurs_every_days=1)],
        AS_OF, 13,
    )
    assert len(rows) <= 200


# -------------------------------------------- receipts follow behaviour


def test_lateness_is_measured_per_customer_and_value_weighted():
    rows = [inv("SLOW", -200, 1_000_000), inv("SLOW", -100, 3_000_000)]
    late = customer_lateness(rows, AS_OF)
    expected = (1_000_000 * 200 + 3_000_000 * 100) / 4_000_000
    assert late["SLOW"] == pytest.approx(expected)


def test_a_customer_with_nothing_overdue_is_expected_on_the_due_date():
    """Due date plus zero lateness."""
    r = evaluate(invoices=[inv("PROMPT", 21, 5_000)], thresholds=T,
                 as_of=AS_OF, opening_balance=0)
    assert r.weeks[3].receipts_receivables == 5_000


def test_receipts_are_timed_by_behaviour_not_by_the_due_date():
    """A public hospital's 90 day terms are a formality. Using due dates would
    make the cash curve wrong in a consistently flattering direction."""
    rows = [
        inv("SLOW", -60, 1_000_000),          # establishes 60 days of lateness
        inv("SLOW", 7, 2_000_000, no="FUTURE"),  # due next week
    ]
    r = evaluate(invoices=rows, thresholds=T, as_of=AS_OF, opening_balance=0)
    # The future invoice is due in week 1 but this customer runs 60 days late,
    # so it must NOT appear in week 1.
    assert r.weeks[1].receipts_receivables == 0
    assert sum(w.receipts_receivables for w in r.weeks) > 0


def test_an_invoice_later_than_its_own_pattern_is_spread_not_banked():
    """Roughly half the open book is later than its customer's average. All of
    it landing in week one would flatter the first week and starve the rest."""
    rows = [inv("A", -400, 4_000_000), inv("A", -10, 4_000_000)]
    r = evaluate(invoices=rows, thresholds=T, as_of=AS_OF, opening_balance=0)
    first_four = [w.receipts_receivables for w in r.weeks[:4]]
    assert all(v > 0 for v in first_four)
    assert max(first_four) < 4_000_000


def test_the_catchup_window_is_configurable():
    rows = [inv("A", -400, 4_000_000), inv("A", -10, 4_000_000)]
    tight = Thresholds(overdue_catchup_weeks=1)
    r = evaluate(invoices=rows, thresholds=tight, as_of=AS_OF, opening_balance=0)
    assert r.weeks[1].receipts_receivables == 0


def test_intercompany_invoices_are_excluded_from_cash():
    rows = [inv("E2", 7, 9_000_000, is_intercompany=True)]
    r = evaluate(invoices=rows, thresholds=T, as_of=AS_OF, opening_balance=0)
    assert sum(w.receipts_receivables for w in r.weeks) == 0


def test_a_settled_invoice_brings_in_nothing():
    rows = [inv("A", 7, 5_000, amount_paid=5_000)]
    r = evaluate(invoices=rows, thresholds=T, as_of=AS_OF, opening_balance=0)
    assert sum(w.receipts_receivables for w in r.weeks) == 0


def test_a_collection_rate_below_one_reduces_the_receipts():
    rows = [inv("A", 7, 1_000_000)]
    full = evaluate(invoices=rows, thresholds=T, as_of=AS_OF, opening_balance=0)
    half = evaluate(invoices=rows, thresholds=T, as_of=AS_OF, opening_balance=0,
                    collection_rate=0.5)
    assert sum(w.receipts_receivables for w in half.weeks) == pytest.approx(
        sum(w.receipts_receivables for w in full.weeks) / 2
    )


# ------------------------------------------------------ reagent pull


def test_reagent_pull_comes_in_every_week():
    m = MachinePlacement(
        machine_key="M1", model="A", customer_key="C", customer_name="C",
        placed_date=date(2025, 1, 1), capital_cost=1_000_000, contract_months=48,
        actual_monthly_tests=1_000, revenue_per_test=400,
    )
    r = evaluate(placements=[m], thresholds=T, as_of=AS_OF, opening_balance=0)
    weekly = 1_000 * 400 * 12 / 52
    assert r.weeks[0].receipts_reagent == pytest.approx(weekly, abs=1)
    assert all(w.receipts_reagent == pytest.approx(weekly, abs=1) for w in r.weeks)


# ------------------------------------------------------- the balance


def test_the_walk_carries_closing_into_the_next_opening():
    r = evaluate(commitments=[commit("a", 0, 1_000), commit("b", 7, 2_000)],
                 thresholds=T, as_of=AS_OF, opening_balance=10_000)
    assert r.weeks[0].opening == 10_000
    assert r.weeks[0].closing == 9_000
    assert r.weeks[1].opening == 9_000
    assert r.weeks[1].closing == 7_000


def test_a_week_is_flagged_against_the_buffer_not_against_zero():
    """A balance that technically stays positive but eats the float the
    business runs on is not a week that worked."""
    t = Thresholds(cash_buffer=5_000)
    r = evaluate(commitments=[commit("big", 0, 7_000)], thresholds=t,
                 as_of=AS_OF, opening_balance=10_000)
    assert r.weeks[0].closing == 3_000
    assert r.weeks[0].is_negative is True
    # Nothing comes in afterwards, so the balance stays under the floor for
    # the rest of the horizon. Every one of those weeks is a week that does
    # not work, and counting only the first would understate the problem.
    assert r.weeks_negative == 13


def test_a_dip_that_recovers_only_flags_the_weeks_it_covers():
    t = Thresholds(cash_buffer=5_000)
    r = evaluate(
        commitments=[commit("out", 0, 7_000)],
        invoices=[inv("A", 7, 20_000)],
        thresholds=t, as_of=AS_OF, opening_balance=10_000,
    )
    assert r.weeks[0].is_negative is True
    assert r.weeks[1].is_negative is False
    assert r.weeks_negative == 1


def test_headroom_comes_from_the_worst_week_not_the_average():
    """One bad week is what bounces a payment."""
    t = Thresholds(cash_buffer=1_000)
    r = evaluate(commitments=[commit("dip", 14, 6_000)], thresholds=t,
                 as_of=AS_OF, opening_balance=10_000)
    assert r.lowest_balance == 4_000
    assert r.headroom_for_orders == 3_000


def test_the_lowest_week_is_identified():
    r = evaluate(commitments=[commit("dip", 21, 5_000)], thresholds=T,
                 as_of=AS_OF, opening_balance=20_000)
    assert r.lowest_week_index == 3


def test_new_orders_are_never_included_here():
    """Modules 8 and 9 would otherwise be circular. This module produces the
    headroom; module 9 fills payments_orders."""
    r = evaluate(thresholds=T, as_of=AS_OF, opening_balance=10_000)
    assert all(w.payments_orders == 0.0 for w in r.weeks)
    assert "not included" in r.caveat


# ------------------------------------------------------- honesty


def test_without_an_opening_balance_it_reports_movement_not_balances():
    r = evaluate(commitments=[commit("a", 0, 1_000)], thresholds=T, as_of=AS_OF)
    assert "changes in cash rather than balances" in r.caveat
    assert r.weeks_negative == 0        # cannot judge a level that is unknown
    assert r.headroom_for_orders == 0.0


def test_full_collection_is_stated_as_an_assumption():
    """It would be easy to apply a recovery curve by age, and every such curve
    would be invented."""
    r = evaluate(invoices=[inv("A", -200, 1_000_000)], thresholds=T,
                 as_of=AS_OF, opening_balance=0)
    assert "assumed to be collected" in r.caveat
    assert "past 180 days" in r.caveat


def test_missing_commitments_are_called_out_as_optimistic():
    r = evaluate(invoices=[inv("A", 7, 1_000)], thresholds=T, as_of=AS_OF,
                 opening_balance=0)
    assert "commitments.csv" in r.caveat


def test_money_expected_after_the_horizon_is_reported_not_dropped():
    r = evaluate(invoices=[inv("A", 300, 8_000_000)], thresholds=T,
                 as_of=AS_OF, opening_balance=0)
    assert "after this 13 week horizon" in r.caveat


def test_the_catchup_assumption_is_declared():
    r = evaluate(invoices=[inv("A", -400, 1_000)], thresholds=T, as_of=AS_OF,
                 opening_balance=0)
    assert "catch-up and not a measurement" in r.caveat


def test_each_week_explains_itself():
    r = evaluate(commitments=[commit("Bank facility", 0, 6_500_000,
                                     kind=CommitmentKind.LOAN_REPAYMENT)],
                 invoices=[inv("A", 0, 1_000_000)],
                 thresholds=T, as_of=AS_OF, opening_balance=10_000_000)
    assert any("Bank facility" in line for line in r.weeks[0].lines)
    assert any("Collections expected" in line for line in r.weeks[0].lines)


def test_an_empty_input_is_safe():
    r = evaluate(thresholds=T, as_of=AS_OF)
    assert len(r.weeks) == 13
    assert r.total_receipts == 0
    assert r.total_payments == 0


def test_service_parts_are_a_run_rate_from_the_observed_window():
    jobs = [
        ServiceJob(job_no="J1", machine_key="M1",
                   opened_date=AS_OF - timedelta(days=70), parts_cost=700_000),
        ServiceJob(job_no="J2", machine_key="M1",
                   opened_date=AS_OF - timedelta(days=35), parts_cost=300_000),
    ]
    r = evaluate(jobs=jobs, thresholds=T, as_of=AS_OF, opening_balance=0)
    # 1,000,000 over 10 weeks is 100,000 a week.
    assert r.weeks[0].payments_service == pytest.approx(100_000, rel=0.01)
