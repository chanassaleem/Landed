"""Parser tests. Nothing is silently dropped, and nothing is silently guessed."""

import pytest

from engine.parse import missing_columns, parse

INV_HEADER = (
    "entity_id,location_id,item_key,description,category,on_hand,open_po,"
    "unit_cost,lead_time_days,lead_time_days_std,is_clinical_critical,expiry_date"
)


def inv(*rows: str):
    return parse("inventory", INV_HEADER + "\n" + "\n".join(rows) + "\n")


def test_a_good_row_parses():
    recs, bad = inv("DX,KHI,A1,Pack,Reagent,100,0,500,60,10,yes,2027-01-01")
    assert bad == []
    assert recs[0].item_key == "A1"
    assert recs[0].is_clinical_critical is True


def test_a_rejected_row_keeps_its_line_number():
    """The line number is what makes a rejection actionable in Excel."""
    recs, bad = inv(
        "DX,KHI,A1,Pack,Reagent,100,0,500,60,10,no,",
        "DX,KHI,,Pack,Reagent,100,0,500,60,10,no,",
    )
    assert len(recs) == 1
    assert bad[0][0] == 3  # line 1 is the header
    assert "item_key" in bad[0][1]


def test_a_bad_row_does_not_stop_the_good_ones():
    """95% usable must produce 95% of an answer, not an error page."""
    recs, bad = inv(
        "DX,KHI,A1,Pack,Reagent,100,0,500,60,10,no,",
        "DX,KHI,A2,Pack,Reagent,oops,0,500,60,10,no,",
        "DX,KHI,A3,Pack,Reagent,300,0,500,60,10,no,",
    )
    assert [r.item_key for r in recs] == ["A1", "A3"]
    assert len(bad) == 1


def test_negative_quantities_are_rejected_not_clamped():
    _, bad = inv("DX,KHI,A1,Pack,Reagent,-5,0,500,60,10,no,")
    assert "negative" in bad[0][1]


def test_thousands_separators_survive_an_excel_round_trip():
    recs, bad = inv('DX,KHI,A1,Pack,Reagent,"1,500",0,"12,000",60,10,no,')
    assert bad == []
    assert recs[0].on_hand == 1500
    assert recs[0].unit_cost == 12000


def test_blank_trailing_lines_are_ignored_not_rejected():
    recs, bad = inv("DX,KHI,A1,Pack,Reagent,100,0,500,60,10,no,", ",,,,,,,,,,,", "")
    assert len(recs) == 1
    assert bad == []


def test_missing_entity_and_location_default_rather_than_fail():
    """A single-branch distributor should not have to invent codes."""
    recs, bad = parse(
        "inventory",
        "item_key,on_hand,unit_cost,lead_time_days\nA1,100,500,60\n",
    )
    assert bad == []
    assert recs[0].entity_id == "default"
    assert recs[0].location_id == "default"


def test_missing_required_columns_are_reported_up_front():
    missing = missing_columns("inventory", "item_key,on_hand\nA1,5\n")
    assert "unit_cost" in missing
    assert "lead_time_days" in missing


def test_an_empty_file_is_reported_not_crashed():
    recs, bad = parse("inventory", "")
    assert recs == []
    assert len(bad) == 1


def test_an_ambiguous_date_is_rejected_rather_than_guessed():
    """A silently wrong date is worse than a rejected row."""
    _, bad = parse(
        "receivables",
        "invoice_no,customer_key,invoice_date,due_date,amount\n"
        "I1,C1,Jan 5 2026,2026-03-01,1000\n",
    )
    assert "not a date" in bad[0][1]


def test_common_local_date_formats_are_accepted():
    recs, bad = parse(
        "receivables",
        "invoice_no,customer_key,invoice_date,due_date,amount\n"
        "I1,C1,05/01/2026,2026-03-01,1000\n",
    )
    assert bad == []
    assert recs[0].invoice_date.day == 5


def test_a_margin_given_as_a_percentage_is_refused_not_converted():
    """35 almost certainly means 35%, but acting on that guess would change
    every payback figure in the module."""
    _, bad = parse(
        "machines",
        "machine_key,customer_key,placed_date,capital_cost,contract_months,"
        "reagent_revenue_to_date,reagent_margin_pct\n"
        "M1,C1,2025-01-01,1000000,48,500000,35\n",
    )
    assert "decimal" in bad[0][1]


def test_overpayment_is_rejected_as_a_data_fault():
    _, bad = parse(
        "receivables",
        "invoice_no,customer_key,invoice_date,due_date,amount,amount_paid\n"
        "I1,C1,2026-01-01,2026-03-01,1000,5000\n",
    )
    assert "exceeds" in bad[0][1]


def test_a_job_closed_before_it_opened_is_rejected():
    _, bad = parse(
        "service",
        "job_no,machine_key,opened_date,closed_date,labour_hours,labour_hours_billed\n"
        "J1,M1,2026-05-10,2026-05-01,4,4\n",
    )
    assert "before" in bad[0][1]


def test_a_bad_period_format_is_rejected():
    _, bad = parse("demand", "item_key,period,quantity\nA1,October 2026,50\n")
    assert "YYYY-MM" in bad[0][1]


def test_zero_demand_is_kept_because_it_is_information():
    recs, bad = parse("demand", "item_key,period,quantity\nA1,2026-10,0\n")
    assert bad == []
    assert recs[0].quantity == 0
