"""Module 6 tests. Expected values computed from the formulas.

Labour rate in the default threshold set is PKR 2,500/hour, so every labour
figure below is (hours * 2500) worked out by hand.
"""

from datetime import date

import pytest

from engine.modules.service import evaluate
from engine.schema import ServiceJob, Thresholds

AS_OF = date(2026, 11, 20)
T = Thresholds()
RATE = T.labour_rate_per_hour  # 2500.0


def job(no, machine, **kw):
    return ServiceJob(
        job_no=no,
        machine_key=machine,
        opened_date=kw.pop("opened", date(2026, 10, 1)),
        **kw,
    )


def test_unbilled_labour_is_priced_at_the_labour_rate():
    # 10 worked, 6 billed -> 4 unbilled -> 4 * 2500 = 10,000
    r = evaluate([job("J1", "M1", labour_hours=10, labour_hours_billed=6)], T, AS_OF)
    assert r.labour_hours_unbilled == 4
    assert r.labour_value_lost == pytest.approx(4 * RATE)


def test_uncharged_parts_are_counted_at_cost():
    r = evaluate(
        [job("J1", "M1", parts_cost=50_000, parts_charged=20_000)], T, AS_OF
    )
    assert r.parts_uncharged == 30_000
    assert r.total_value_lost == pytest.approx(30_000)


def test_warranty_work_is_counted_but_never_called_wastage():
    """Warranty was priced in at the point of supply. Counting it as wastage
    would double count it and point the fix at the wrong place."""
    r = evaluate(
        [
            job(
                "W1", "M1", labour_hours=8, labour_hours_billed=0,
                parts_cost=90_000, parts_charged=0, is_warranty=True,
            )
        ],
        T,
        AS_OF,
    )
    assert r.jobs == 1
    assert r.labour_hours == 8          # still visible in the workload
    assert r.labour_hours_unbilled == 0  # but not wastage
    assert r.total_value_lost == 0
    assert r.recovery_rate is None       # no billable hours at all


def test_billing_more_than_worked_does_not_create_negative_wastage():
    """Over-billing is a different problem and must not offset real wastage."""
    r = evaluate(
        [
            job("J1", "M1", labour_hours=4, labour_hours_billed=10),
            job("J2", "M1", labour_hours=10, labour_hours_billed=4),
        ],
        T,
        AS_OF,
    )
    # Only J2's 6 unbilled hours count; J1 contributes zero, not minus six.
    assert r.labour_hours_unbilled == 6
    assert r.labour_value_lost == pytest.approx(6 * RATE)


def test_recovery_rate_is_billed_over_billable_hours():
    r = evaluate(
        [
            job("J1", "M1", labour_hours=10, labour_hours_billed=6),
            job("J2", "M1", labour_hours=4, labour_hours_billed=4),
        ],
        T,
        AS_OF,
    )
    # (6 + 4) / (10 + 4) = 10/14
    assert r.recovery_rate == pytest.approx(round(10 / 14, 4))


def test_repeat_visit_rate_counts_all_jobs_including_warranty():
    r = evaluate(
        [
            job("J1", "M1", is_repeat_visit=True),
            job("J2", "M1", is_repeat_visit=False),
            job("J3", "M1", is_repeat_visit=True, is_warranty=True),
        ],
        T,
        AS_OF,
    )
    assert r.repeat_visit_rate == pytest.approx(round(2 / 3, 4))


def test_mean_days_to_close_ignores_open_jobs():
    r = evaluate(
        [
            job("J1", "M1", opened=date(2026, 10, 1), closed_date=date(2026, 10, 5)),
            job("J2", "M1", opened=date(2026, 10, 8), closed_date=date(2026, 10, 9)),
            job("J3", "M1", opened=date(2026, 11, 1), closed_date=None),
        ],
        T,
        AS_OF,
    )
    assert r.mean_days_to_close == pytest.approx(2.5)  # (4 + 1) / 2
    assert r.jobs_open == 1


def test_per_machine_rollup_ranks_the_worst_offender_first():
    r = evaluate(
        [
            job("J1", "GOOD", labour_hours=5, labour_hours_billed=5),
            job("J2", "BAD", labour_hours=20, labour_hours_billed=0),
        ],
        T,
        AS_OF,
    )
    assert [m["machine_key"] for m in r.by_machine] == ["BAD", "GOOD"]
    assert r.by_machine[0]["value_lost"] == pytest.approx(20 * RATE)
    assert r.by_machine[1]["value_lost"] == 0


def test_downtime_days_are_summed_across_all_jobs():
    r = evaluate(
        [
            job("J1", "M1", downtime_days=4),
            job("J2", "M2", downtime_days=12, is_warranty=True),
        ],
        T,
        AS_OF,
    )
    assert r.downtime_days == 16


def test_high_repeat_rate_is_called_out_in_the_reason():
    r = evaluate(
        [
            job("J1", "M1", labour_hours=10, labour_hours_billed=0, is_repeat_visit=True),
            job("J2", "M1", labour_hours=10, labour_hours_billed=0, is_repeat_visit=True),
            job("J3", "M1", labour_hours=10, labour_hours_billed=0),
        ],
        T,
        AS_OF,
    )
    assert "repeat visits" in r.reason


def test_a_clean_book_says_so_rather_than_inventing_a_problem():
    r = evaluate(
        [job("J1", "M1", labour_hours=5, labour_hours_billed=5, parts_cost=10, parts_charged=10)],
        T,
        AS_OF,
    )
    assert r.total_value_lost == 0
    assert "recovered" in r.reason


def test_no_jobs_is_safe():
    r = evaluate([], T, AS_OF)
    assert r.jobs == 0
    assert r.recovery_rate is None
    assert r.repeat_visit_rate is None
    assert r.mean_days_to_close is None
    assert r.by_machine == ()


def test_labour_rate_is_taken_from_thresholds_not_hard_coded():
    custom = Thresholds(labour_rate_per_hour=4_000.0)
    r = evaluate([job("J1", "M1", labour_hours=10, labour_hours_billed=0)], custom, AS_OF)
    assert r.labour_value_lost == pytest.approx(40_000)
