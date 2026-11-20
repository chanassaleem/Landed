"""Pipeline, tenders and what they cost to win.

The point of this module is not the pipeline total. It is the question a
CRM cannot answer: if this is won, can it be supplied and can it be funded.
"""

from datetime import date

import pytest

from engine.modules.marketing import (
    DEFAULT_ODDS, MIN_FOR_MEASURED_ODDS, Opportunity, Spend, evaluate,
)

AS_OF = date(2026, 11, 20)


def opp(oid, stage, value, **kw):
    return Opportunity(
        oid, kw.pop("customer_key", "C1"), kw.pop("customer_name", "A customer"),
        "DX", "KHI", stage, value, **kw)


def test_only_open_deals_are_in_the_pipeline():
    r = evaluate([opp("A", "quoted", 1_000_000),
                  opp("B", "won", 2_000_000),
                  opp("C", "lost", 3_000_000)], as_of=AS_OF)
    assert r.open_count == 1
    assert r.open_value == 1_000_000
    assert r.won_value == 2_000_000 and r.lost_value == 3_000_000


def test_a_later_stage_is_weighted_more_heavily_than_an_earlier_one():
    r = evaluate([opp("A", "lead", 1_000_000),
                  opp("B", "tender_submitted", 1_000_000)], as_of=AS_OF)
    by_id = {row.opportunity_id: row for row in r.rows}
    assert by_id["B"].weighted > by_id["A"].weighted


def test_odds_are_assumptions_until_enough_has_closed_to_measure_them():
    few = evaluate([opp("A", "quoted", 1_000_000),
                    opp("W", "won", 100_000)], as_of=AS_OF)
    assert few.rows[0].odds == DEFAULT_ODDS["quoted"]
    assert not few.rows[0].odds_measured
    assert "assumed rate" in few.rows[0].reason

    many = [opp(f"W{i}", "won", 100_000) for i in range(6)]
    many += [opp(f"L{i}", "lost", 100_000) for i in range(4)]
    many.append(opp("A", "quoted", 1_000_000))
    r = evaluate(many, as_of=AS_OF)
    assert len(many) - 1 >= MIN_FOR_MEASURED_ODDS
    assert r.rows[0].odds_measured
    assert "assumed rate" not in r.rows[0].reason


def test_a_win_rate_is_not_claimed_from_three_deals():
    r = evaluate([opp("W", "won", 1), opp("L", "lost", 1)], as_of=AS_OF)
    assert not r.win_rate_measured
    assert "too few" in r.reason


def test_weighted_pipeline_is_never_presented_as_revenue():
    r = evaluate([opp("A", "quoted", 1_000_000)], as_of=AS_OF)
    assert "never be added to it" in r.caveat


# ---------------------------------------------- the question a CRM cannot ask

def test_a_deal_that_cannot_be_supplied_in_time_is_flagged():
    """Closing in 30 days, 75-day lead time, 12 units on hand against 400."""
    r = evaluate(
        [opp("A", "tender_submitted", 8_000_000,
             expected_close=date(2026, 12, 20), item_key="HEM-RGT-01",
             quantity=400)],
        stock_rows=[{"item_key": "HEM-RGT-01", "entity_id": "DX",
                     "location_id": "KHI", "on_hand": 12,
                     "lead_time_days": 75}],
        as_of=AS_OF)
    row = r.rows[0]
    assert row.supply == "cannot supply in time"
    assert len(r.at_risk_supply) == 1
    assert "75 day lead time" in row.reason
    assert "cannot be supplied in time" in r.reason


def test_a_deal_with_the_stock_on_the_shelf_is_not_flagged():
    r = evaluate(
        [opp("A", "quoted", 500_000, expected_close=date(2026, 12, 20),
             item_key="X", quantity=10)],
        stock_rows=[{"item_key": "X", "entity_id": "DX", "location_id": "KHI",
                     "on_hand": 50, "lead_time_days": 75}],
        as_of=AS_OF)
    assert r.rows[0].supply == "in stock"
    assert not r.at_risk_supply


def test_stock_at_a_sister_branch_is_a_transfer_not_a_shelf():
    """Saying "in stock" when it is three hundred miles away is how a deal
    is promised on a date nobody can meet."""
    r = evaluate(
        [opp("A", "quoted", 500_000, expected_close=date(2026, 12, 20),
             item_key="X", quantity=40)],
        stock_rows=[
            {"item_key": "X", "entity_id": "DX", "location_id": "KHI",
             "on_hand": 5, "lead_time_days": 75},
            {"item_key": "X", "entity_id": "DX", "location_id": "LHR",
             "on_hand": 80, "lead_time_days": 75},
        ],
        as_of=AS_OF)
    assert r.rows[0].supply == "in stock at another branch"
    assert "transfer, not an import" in r.rows[0].reason


def test_another_company_s_stock_is_not_available_to_this_one():
    """It is a different legal entity. Counting it told a Karachi tender it
    was covered by stock that would need an inter-company sale first."""
    r = evaluate(
        [opp("A", "tender_submitted", 500_000,
             expected_close=date(2026, 12, 20), item_key="X", quantity=40)],
        stock_rows=[{"item_key": "X", "entity_id": "MS", "location_id": "ISB",
                     "on_hand": 500, "lead_time_days": 75}],
        as_of=AS_OF)
    assert r.rows[0].supply == "cannot supply in time"


def test_a_deal_with_time_to_order_is_not_flagged():
    r = evaluate(
        [opp("A", "quoted", 500_000, expected_close=date(2027, 6, 1),
             item_key="X", quantity=99)],
        stock_rows=[{"item_key": "X", "entity_id": "DX",
                     "location_id": "KHI", "on_hand": 1,
                     "lead_time_days": 75}],
        as_of=AS_OF)
    assert r.rows[0].supply == "needs ordering"
    assert not r.at_risk_supply


def test_an_item_missing_from_the_stock_file_is_said_rather_than_assumed():
    r = evaluate([opp("A", "quoted", 1, item_key="GHOST")],
                 stock_rows=[], as_of=AS_OF)
    assert r.rows[0].supply == "item not in stock data"


def test_the_pipeline_is_checked_against_the_cash_the_business_has():
    r = evaluate([opp("A", "tender_submitted", 10_000_000)],
                 cash_headroom=100_000, as_of=AS_OF)
    assert r.unfundable > 0
    rich = evaluate([opp("A", "tender_submitted", 10_000_000)],
                    cash_headroom=50_000_000, as_of=AS_OF)
    assert rich.unfundable == 0


# ------------------------------------------------------------ what it costs

def test_spend_is_priced_against_wins():
    ops = [opp(f"W{i}", "won", 1_000_000) for i in range(4)]
    r = evaluate(ops, spend=[Spend("DX", "Conference", "conference",
                                   date(2026, 9, 1), 800_000)], as_of=AS_OF)
    assert r.spend == 800_000
    assert r.cost_per_win == pytest.approx(200_000)
    assert r.spend_per_rupee_won == pytest.approx(0.2)


def test_cost_per_win_is_not_invented_when_nothing_has_been_won():
    r = evaluate([opp("A", "quoted", 1_000_000)],
                 spend=[Spend("DX", "X", "digital", date(2026, 9, 1), 50_000)],
                 as_of=AS_OF)
    assert r.cost_per_win is None


def test_deals_are_grouped_by_where_they_came_from():
    r = evaluate([opp("A", "won", 1_000_000, source="tender"),
                  opp("B", "lost", 500_000, source="tender"),
                  opp("C", "quoted", 300_000, source="referral")],
                 as_of=AS_OF)
    assert r.by_source["tender"]["count"] == 2
    assert r.by_source["tender"]["won"] == 1
    assert r.by_source["referral"]["count"] == 1


def test_an_opportunity_sitting_open_for_months_is_questioned():
    r = evaluate([opp("A", "lead", 1_000_000, opened=date(2026, 1, 1))],
                 as_of=AS_OF)
    assert "long enough to question" in r.rows[0].reason


def test_the_biggest_weighted_deal_comes_first():
    r = evaluate([opp("small", "quoted", 100_000),
                  opp("big", "quoted", 9_000_000)], as_of=AS_OF)
    assert r.rows[0].opportunity_id == "big"


def test_nothing_open_says_so():
    assert "Nothing open" in evaluate([], as_of=AS_OF).reason
