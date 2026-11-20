"""Module 5 tests. Expected values are computed from the formulas by hand.

Day counts are stated explicitly in the test rather than derived, so a wrong
date arithmetic change fails here rather than silently shifting a bucket.
"""

from datetime import date

import pytest

from engine.modules.receivables import bucket_for, evaluate
from engine.schema import AgeBucket, ReceivableInvoice, Thresholds

AS_OF = date(2026, 11, 20)
T = Thresholds()


def inv(no, cust, due, amount, **kw):
    return ReceivableInvoice(
        invoice_no=no,
        customer_key=cust,
        customer_name=kw.pop("name", cust),
        invoice_date=kw.pop("issued", date(2026, 1, 1)),
        due_date=due,
        amount=amount,
        **kw,
    )


# ------------------------------------------------------------- bucketing


@pytest.mark.parametrize(
    "days,expected",
    [
        (-30, AgeBucket.CURRENT),
        (0, AgeBucket.CURRENT),
        (1, AgeBucket.D1_30),
        (30, AgeBucket.D1_30),
        (31, AgeBucket.D31_60),
        (60, AgeBucket.D31_60),
        (61, AgeBucket.D61_90),
        (90, AgeBucket.D61_90),
        (91, AgeBucket.D91_180),
        (180, AgeBucket.D91_180),
        (181, AgeBucket.D180_PLUS),
        (900, AgeBucket.D180_PLUS),
    ],
)
def test_bucket_boundaries_are_inclusive_at_the_top(days, expected):
    assert bucket_for(days) == expected


# ------------------------------------------------------------- totals


def test_settled_invoices_are_excluded_from_the_book():
    rows = [
        inv("A", "C1", date(2026, 1, 1), 1_000_000, amount_paid=1_000_000),
        inv("B", "C1", date(2026, 1, 1), 500_000),
    ]
    r = evaluate(rows, T, AS_OF)
    assert r.outstanding == 500_000


def test_part_payment_leaves_only_the_open_amount():
    rows = [inv("A", "C1", date(2026, 1, 1), 1_000_000, amount_paid=400_000)]
    r = evaluate(rows, T, AS_OF)
    assert r.outstanding == 600_000
    assert r.overdue == 600_000


def test_intercompany_is_excluded_and_reported_separately():
    """Consolidating an inter-company invoice double counts it."""
    rows = [
        inv("EXT", "C1", date(2026, 10, 1), 1_000_000),
        inv("IC", "E2", date(2026, 10, 1), 750_000, is_intercompany=True),
    ]
    r = evaluate(rows, T, AS_OF)
    assert r.outstanding == 1_000_000
    assert r.intercompany_excluded == 750_000
    assert [c.customer_key for c in r.customers] == ["C1"]


def test_not_yet_due_is_outstanding_but_not_overdue():
    rows = [inv("A", "C1", date(2026, 12, 25), 300_000)]
    r = evaluate(rows, T, AS_OF)
    assert r.outstanding == 300_000
    assert r.overdue == 0
    assert r.buckets[AgeBucket.CURRENT.value] == 300_000


# ------------------------------------------------- group customer exposure


def test_group_exposure_sums_one_customer_across_entities():
    """The headline insight: no single entity's books show this total."""
    rows = [
        inv("A", "H1", date(2026, 3, 1), 1_000_000, entity_id="E1"),
        inv("B", "H1", date(2026, 8, 1), 2_000_000, entity_id="E2"),
        inv("C", "H1", date(2026, 9, 1), 500_000, entity_id="E3"),
    ]
    r = evaluate(rows, T, AS_OF)
    h1 = r.customers[0]
    assert h1.outstanding == 3_500_000
    assert h1.entities == ("E1", "E2", "E3")
    assert h1.invoice_count == 3
    # 2026-03-01 to 2026-11-20 is 264 days.
    assert h1.worst_days_overdue == 264


def test_weighted_average_days_overdue_is_value_weighted():
    """A large recent invoice must not be averaged flat against a small old one."""
    rows = [
        # 264 days overdue, 1M
        inv("A", "H1", date(2026, 3, 1), 1_000_000),
        # 111 days overdue, 3M
        inv("B", "H1", date(2026, 8, 1), 3_000_000),
    ]
    r = evaluate(rows, T, AS_OF)
    expected = (1_000_000 * 264 + 3_000_000 * 111) / 4_000_000
    assert r.customers[0].weighted_avg_days_overdue == pytest.approx(
        round(expected, 1)
    )


def test_customers_are_ranked_by_exposure_and_share_sums_to_one():
    rows = [
        inv("A", "SMALL", date(2026, 10, 1), 1_000_000),
        inv("B", "BIG", date(2026, 10, 1), 9_000_000),
    ]
    r = evaluate(rows, T, AS_OF)
    assert [c.customer_key for c in r.customers] == ["BIG", "SMALL"]
    assert sum(c.share_of_book for c in r.customers) == pytest.approx(1.0)
    assert r.customers[0].share_of_book == pytest.approx(0.9)


# ----------------------------------------------------- financing and DSO


def test_financing_cost_prices_the_overdue_book_at_cost_of_capital():
    rows = [inv("A", "C1", date(2026, 1, 1), 10_000_000)]
    r = evaluate(rows, T, AS_OF)
    # 10M overdue at 22% = 2.2M a year.
    assert r.financing_cost_annual == pytest.approx(2_200_000)


def test_dso_is_none_without_revenue_rather_than_estimated():
    """Deriving DSO from receivables alone would be circular."""
    r = evaluate([inv("A", "C1", date(2026, 1, 1), 1_000_000)], T, AS_OF)
    assert r.dso_days is None
    assert r.dso_gap_days is None
    assert r.cash_released_at_target == 0


def test_dso_and_cash_released_at_target():
    rows = [inv("A", "C1", date(2026, 1, 1), 30_000_000)]
    # Daily revenue = 6,000,000 / 30 = 200,000.
    # DSO = 30,000,000 / 200,000 = 150 days. Target 60, so gap is 90 days.
    # Cash released = 90 * 200,000 = 18,000,000.
    r = evaluate(rows, T, AS_OF, monthly_revenue=6_000_000)
    assert r.dso_days == pytest.approx(150.0)
    assert r.dso_gap_days == pytest.approx(90.0)
    assert r.cash_released_at_target == pytest.approx(18_000_000)


def test_collecting_better_than_target_releases_nothing():
    rows = [inv("A", "C1", date(2026, 12, 1), 3_000_000)]
    r = evaluate(rows, T, AS_OF, monthly_revenue=6_000_000)
    assert r.dso_gap_days < 0
    assert r.cash_released_at_target == 0


def test_public_sector_share_of_the_book():
    rows = [
        inv("A", "GOV", date(2026, 5, 1), 7_500_000, is_public_sector=True),
        inv("B", "PRIV", date(2026, 5, 1), 2_500_000),
    ]
    r = evaluate(rows, T, AS_OF)
    assert r.public_sector_share == pytest.approx(0.75)


def test_empty_book_is_safe():
    r = evaluate([], T, AS_OF)
    assert r.outstanding == 0
    assert r.customers == ()
    assert r.public_sector_share == 0.0


def test_buckets_sum_to_outstanding():
    rows = [
        inv("A", "C1", date(2026, 12, 1), 100_000),
        inv("B", "C1", date(2026, 11, 1), 200_000),
        inv("C", "C2", date(2026, 9, 1), 300_000),
        inv("D", "C2", date(2026, 1, 1), 400_000),
    ]
    r = evaluate(rows, T, AS_OF)
    assert sum(r.buckets.values()) == pytest.approx(r.outstanding)


def test_multi_entity_reason_names_the_blind_spot():
    rows = [
        inv("A", "H1", date(2026, 3, 1), 1_000_000, entity_id="E1"),
        inv("B", "H1", date(2026, 3, 1), 1_000_000, entity_id="E2"),
    ]
    r = evaluate(rows, T, AS_OF)
    assert "2 entities" in r.customers[0].reason
