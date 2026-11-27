"""Module 7 tests. Expected values computed from the formulas.

Elapsed months use 30-day months, so the placement dates below are chosen to
give round numbers: 600 days is exactly 20 months.
"""

from datetime import date, timedelta

import pytest

from engine.modules.machines import evaluate
from engine.schema import MachinePlacement, PaybackStatus, Thresholds

AS_OF = date(2026, 11, 20)
T = Thresholds()


def placed_days_ago(days: int) -> date:
    return AS_OF - timedelta(days=days)


def machine(key="MX-1", **kw):
    base = dict(
        machine_key=key,
        model="Analyser",
        customer_key="H1",
        customer_name="Hospital One",
        placed_date=placed_days_ago(600),  # exactly 20 months
        capital_cost=3_000_000.0,
        contract_months=60,
        # These placements are built by hand with the revenue stated,
        # so the figure is given rather than missing. The distinction
        # matters only for a zero -- nothing sold and nothing recorded
        # look identical in the number and mean opposite things -- so
        # it is set here and a test that wants the missing case says
        # so by passing False.
        reagent_revenue_measured=True,
    )
    base.update(kw)
    return MachinePlacement(**base)


def one(thresholds=None, **kw):
    return evaluate([machine(**kw)], thresholds or T, AS_OF)[0]


def test_elapsed_months_use_thirty_day_months():
    r = one()
    assert r.months_elapsed == pytest.approx(20.0)
    assert r.months_remaining_on_contract == pytest.approx(40.0)


def test_net_recovery_is_margin_less_service_cost():
    # 4,000,000 * 0.35 = 1,400,000 margin, less 200,000 service = 1,200,000
    r = one(
        reagent_revenue_to_date=4_000_000,
        reagent_margin_pct=0.35,
        service_cost_to_date=200_000,
    )
    assert r.margin_recovered == pytest.approx(1_400_000)
    assert r.net_recovered == pytest.approx(1_200_000)
    assert r.recovery_pct == pytest.approx(0.4)  # 1.2M of 3M


def test_payback_months_from_the_current_run_rate():
    # net 1,200,000 over 20 months = 60,000/month. 3,000,000 / 60,000 = 50 months.
    r = one(
        reagent_revenue_to_date=4_000_000,
        reagent_margin_pct=0.35,
        service_cost_to_date=200_000,
    )
    assert r.monthly_margin_rate == pytest.approx(60_000)
    assert r.payback_months == pytest.approx(50.0)
    # 50 months is inside the 60 month contract but past the 24 month target.
    assert r.status is PaybackStatus.ON_TRACK


def test_fast_payback_is_ahead_of_target():
    # net = 6,000,000 * 0.35 = 2,100,000 over 20 months = 105,000/month
    # 3,000,000 / 105,000 = 28.6 months... still past 24, so push revenue up.
    # 9,000,000 * 0.35 = 3,150,000 / 20 = 157,500/month -> 19.0 months.
    r = one(reagent_revenue_to_date=9_000_000, reagent_margin_pct=0.35)
    assert r.payback_months == pytest.approx(19.0, abs=0.1)
    assert r.status is PaybackStatus.AHEAD


def test_payback_beyond_the_contract_is_unrecoverable():
    """The failure mode the accounts never show: capital spent against a
    volume forecast that did not materialise."""
    # net = 1,000,000 * 0.30 = 300,000 over 20 months = 15,000/month
    # 5,000,000 / 15,000 = 333 months against a 48 month contract.
    r = one(
        capital_cost=5_000_000,
        contract_months=48,
        reagent_revenue_to_date=1_000_000,
        reagent_margin_pct=0.30,
    )
    assert r.payback_months > r.contract_months
    assert r.status is PaybackStatus.UNRECOVERABLE
    assert "never be recovered" in r.reason


def test_no_reagent_pull_at_all_is_stalled():
    """Measured, and the measurement is zero: the customer really has
    stopped buying. That is a finding worth acting on."""
    r = one(reagent_revenue_to_date=0, service_cost_to_date=50_000)
    assert r.monthly_margin_rate is not None
    assert r.payback_months is None
    assert r.status is PaybackStatus.STALLED
    assert r.recovery_measured is True


def test_nothing_recorded_is_not_the_same_finding_as_nothing_sold():
    """Both are a zero and they mean opposite things.

    STALLED sends somebody to a hospital to retrieve an analyser.
    Doing that because nobody linked the reagent to the machine --
    while the customer buys it every month -- is the most expensive
    mistake this module could make."""
    r = one(reagent_revenue_to_date=0, service_cost_to_date=50_000,
            reagent_revenue_measured=False)
    assert r.status is PaybackStatus.UNMEASURED
    assert r.recovery_measured is False
    assert "no reagent sale has been recorded" in r.reason.lower()
    assert "retrieve" not in r.reason.lower()


def test_an_unmeasured_placement_ranks_below_a_stalling_one():
    """It deserves attention -- capital whose position nobody knows --
    but it is not evidence of trouble, and ranking it first would
    bury the placements that are actually failing."""
    out = evaluate([
        machine(key="UNKNOWN", reagent_revenue_to_date=0,
                reagent_revenue_measured=False),
        machine(key="STALLING", reagent_revenue_to_date=0,
                service_cost_to_date=50_000),
    ], T, AS_OF)
    assert [r.machine_key for r in out] == ["STALLING", "UNKNOWN"]


def test_service_cost_exceeding_margin_is_stalled_not_negative_payback():
    r = one(reagent_revenue_to_date=1_000_000, reagent_margin_pct=0.35,
            service_cost_to_date=900_000)
    assert r.net_recovered < 0
    assert r.payback_months is None
    assert r.status is PaybackStatus.STALLED


def test_utilisation_against_the_placement_forecast():
    r = one(expected_monthly_tests=1_000, actual_monthly_tests=250,
            reagent_revenue_to_date=9_000_000)
    assert r.utilisation == pytest.approx(0.25)
    assert "25% of the forecast volume" in r.reason


def test_utilisation_is_none_when_no_forecast_was_recorded():
    r = one(expected_monthly_tests=0, actual_monthly_tests=100)
    assert r.utilisation is None


def test_downtime_is_priced_as_lost_reagent_margin():
    """The engineer's time is the small number. The lost pull is the big one."""
    # daily tests = 900/30 = 30. per-test margin = 400 * 0.35 = 140.
    # 10 days * 30 * 140 = 42,000
    r = one(
        actual_monthly_tests=900,
        revenue_per_test=400,
        reagent_margin_pct=0.35,
        downtime_days_ytd=10,
        reagent_revenue_to_date=4_000_000,
    )
    assert r.revenue_lost_to_downtime == pytest.approx(42_000)
    # Lower-case k: the default Thresholds is the Pakistani one, and
    # the reason now reads in the notation the reader of this product
    # actually uses. It used to say 42K here and 42k in the column
    # beside it.
    assert "42k" in r.reason


def test_a_reason_reads_in_the_notation_the_reader_uses():
    """The same amount, two businesses, two notations.

    This is the bug the scale exists to prevent: a Pakistani screen
    showing 66.0L in a column and PKR 6.6M in the sentence under it,
    which a reader has to stop and reconcile.
    """
    from dataclasses import replace

    shared = dict(actual_monthly_tests=900, revenue_per_test=400,
                  reagent_margin_pct=0.35, downtime_days_ytd=10,
                  reagent_revenue_to_date=4_000_000)
    here = one(**shared)
    assert "PKR 42k" in here.reason

    abroad = one(thresholds=replace(
        Thresholds(), currency="AED", number_scale="western"), **shared)
    assert "AED 42K" in abroad.reason
    assert "PKR" not in abroad.reason


def test_no_downtime_costs_nothing_and_is_not_mentioned():
    r = one(downtime_days_ytd=0, actual_monthly_tests=900, revenue_per_test=400,
            reagent_revenue_to_date=4_000_000)
    assert r.revenue_lost_to_downtime == 0
    assert "down this year" not in r.reason


def test_payback_target_comes_from_thresholds():
    custom = Thresholds(payback_target_months=60)
    m = machine(reagent_revenue_to_date=4_000_000, reagent_margin_pct=0.35,
                service_cost_to_date=200_000)
    r = evaluate([m], custom, AS_OF)[0]
    # 50 months payback now sits inside a 60 month target.
    assert r.status is PaybackStatus.AHEAD


def test_ranking_puts_unrecovered_capital_first():
    good = machine("GOOD", reagent_revenue_to_date=9_000_000)
    bad = machine("BAD", capital_cost=5_000_000, contract_months=48,
                  reagent_revenue_to_date=1_000_000, reagent_margin_pct=0.30)
    stalled = machine("STALLED", reagent_revenue_to_date=0)
    order = [r.machine_key for r in evaluate([good, stalled, bad], T, AS_OF)]
    assert order[0] == "BAD"       # unrecoverable
    assert order[1] == "STALLED"
    assert order[2] == "GOOD"      # ahead of target


def test_placed_today_does_not_divide_by_zero():
    r = one(placed_date=AS_OF)
    assert r.months_elapsed == 0
    assert r.monthly_margin_rate is None
    assert r.status is PaybackStatus.RAMPING


def test_a_new_placement_is_ramping_not_a_write_off():
    """A payback rate extrapolated from a few weeks of pull is noise. Without
    an observation window every new machine reads as unrecoverable on day
    thirty, which would train people to ignore the status."""
    r = one(placed_date=placed_days_ago(75), reagent_revenue_to_date=180_000,
            reagent_margin_pct=0.38, service_cost_to_date=40_000)
    assert r.months_elapsed == pytest.approx(2.5)
    assert r.status is PaybackStatus.RAMPING
    assert "Too early to judge" in r.reason


def test_the_window_closes_and_a_real_verdict_follows():
    r = one(placed_date=placed_days_ago(300), reagent_revenue_to_date=180_000,
            reagent_margin_pct=0.38, service_cost_to_date=40_000)
    assert r.months_elapsed == pytest.approx(10.0)
    assert r.status is PaybackStatus.UNRECOVERABLE


def test_the_observation_window_is_configurable():
    custom = Thresholds(placement_min_months_to_judge=12)
    m = machine(placed_date=placed_days_ago(300), reagent_revenue_to_date=180_000)
    assert evaluate([m], custom, AS_OF)[0].status is PaybackStatus.RAMPING


def test_no_placements_is_safe():
    assert evaluate([], T, AS_OF) == ()


# -------------------------------- a fleet figure over what can be seen

def test_the_fleet_percentage_ignores_capital_it_cannot_measure():
    """Four machines performing to plan and one with no reagent
    linked read as 62% recovered when the true answer is "78% across
    the four we can see, and one we cannot". The unmeasured placement
    contributed a zero to the top of the fraction while its capital
    stayed at the bottom."""
    from web.modules import _equipment

    class F:
        thresholds = T
        machines = [
            {"capital_cost": 1_000_000.0, "net_recovered": 780_000.0,
             "recovery_measured": True, "revenue_lost_to_downtime": 0.0}
            for _ in range(4)
        ] + [
            {"capital_cost": 1_000_000.0, "net_recovered": 0.0,
             "recovery_measured": False, "revenue_lost_to_downtime": 0.0}
        ]

    t = _equipment(F())["totals"]
    assert t["recovery_pct"] == pytest.approx(0.78)
    assert t["capital_deployed"] == 5_000_000
    assert t["unmeasured_placements"] == 1
    assert t["unmeasured_capital"] == 1_000_000
    assert t["measured_placements"] == 4


def test_a_fleet_with_nothing_linked_reports_no_percentage_at_all():
    """Not zero. Zero is a finding; this is an empty measurement."""
    from web.modules import _equipment

    class F:
        thresholds = T
        machines = [{"capital_cost": 2_000_000.0, "net_recovered": 0.0,
                     "recovery_measured": False,
                     "revenue_lost_to_downtime": 0.0}]

    t = _equipment(F())["totals"]
    assert t["recovery_pct"] is None
    assert t["unmeasured_capital"] == 2_000_000


def test_the_unmeasured_note_reads_as_english_in_every_case():
    """It is assembled from counts, which is how "1 of 1 placements
    has no reagent sale linked to it, so the percentage above is
    across the rest" got shipped -- ungrammatical, and false, because
    there is no rest."""
    from web.modules import _unmeasured_note

    mk = lambda measured: {"capital_cost": 1_000_000.0,
                           "recovery_measured": measured}
    cases = {
        "one, unmeasured": [mk(False)],
        "three, none measured": [mk(False)] * 3,
        "one of five": [mk(True)] * 4 + [mk(False)],
        "two of five": [mk(True)] * 3 + [mk(False)] * 2,
    }
    for name, rows in cases.items():
        got = _unmeasured_note(rows, 1_000_000.0, T)
        assert got, name
        # The failure that shipped: a whole-fleet case claiming the
        # percentage covers everyone else.
        if all(not r["recovery_measured"] for r in rows):
            assert "the rest" not in got, name
            assert "across the other" not in got, name
        # The failure that nearly shipped: the sentence saying the
        # opposite of what it means.
        assert ("has no reagent sale" in got
                or "have no reagent sale" in got
                or "None of the" in got), f"{name}: {got}"
        for wrong in ("placements has a reagent sale linked to them",
                      "1 of 1 placements"):
            assert wrong not in got, f"{name}: {got}"


def test_a_measured_fleet_gets_no_note_at_all():
    from web.modules import _unmeasured_note
    rows = [{"capital_cost": 1_000_000.0, "recovery_measured": True}]
    assert _unmeasured_note(rows, 0.0, T) is None


def test_an_unmeasured_placement_shows_no_figure_rather_than_a_negative():
    """With no reagent revenue, net recovered is zero minus the
    service spend -- so the screen read "recovered −55k" against a
    machine whose customer may well be buying every month. That is
    not an approximation of the truth; it is an artefact of having
    no revenue data at all."""
    from web.modules import _equipment

    class F:
        thresholds = T
        machines = [{
            "capital_cost": 6_500_000.0, "net_recovered": -55_000.0,
            "recovery_pct": -0.009, "payback_months": None,
            "monthly_margin_rate": -2_750.0,
            "margin_recovered": 0.0,
            "recovery_measured": False,
            "revenue_lost_to_downtime": 0.0,
        }]

    out = _equipment(F())
    row = out["rows"][0]
    for field in ("net_recovered", "recovery_pct", "margin_recovered"):
        assert row[field] is None, f"{field} still shows a figure"
    assert out["totals"]["capital_recovered"] is None, (
        "the fleet tile shows a confident zero for a fleet with "
        "nothing measured")
    # The capital is still real and still reported.
    assert out["totals"]["capital_deployed"] == 6_500_000
