"""Data sanity tests.

A checker that reports nothing on clean data and has never been seen to fire
is worthless, so every check here is proven against data broken on purpose.
The clean sample pack must stay silent, and each specific corruption must
raise its own specific finding.
"""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from engine import parse, samples, sanity
from engine.sanity import BLOCKS, NOTES, WARNS
from engine.schema import (
    DemandPeriod,
    ItemSnapshot,
    MachinePlacement,
    ReceivableInvoice,
    ServiceJob,
)

AS_OF = samples.AS_OF


@pytest.fixture(scope="module")
def pack():
    return {k: parse.parse(k, samples.sample_csv(k))[0] for k in samples.ORDER}


def codes(pack, **override):
    args = dict(
        items=pack["inventory"], demand=pack["demand"],
        invoices=pack["receivables"], jobs=pack["service"],
        placements=pack["machines"], commitments=pack["commitments"],
        as_of=AS_OF,
    )
    args.update(override)
    return {f.code for f in sanity.check(**args)}


def finding(pack, code, **override):
    args = dict(
        items=pack["inventory"], demand=pack["demand"],
        invoices=pack["receivables"], jobs=pack["service"],
        placements=pack["machines"], commitments=pack["commitments"],
        as_of=AS_OF,
    )
    args.update(override)
    return next(f for f in sanity.check(**args) if f.code == code)


def item(src, **kw):
    fields = dict(
        item_key=src.item_key, description=src.description, category=src.category,
        on_hand=src.on_hand, open_po=src.open_po, unit_cost=src.unit_cost,
        lead_time_days=src.lead_time_days,
        lead_time_days_std=src.lead_time_days_std,
        is_clinical_critical=src.is_clinical_critical,
        expiry_date=src.expiry_date, entity_id=src.entity_id,
        location_id=src.location_id,
    )
    fields.update(kw)
    return ItemSnapshot(**fields)


# ------------------------------------------------------------ the clean case


def test_the_sample_pack_raises_nothing(pack):
    """It is built to be internally consistent, so a finding here means a
    check has become over-eager rather than that the pack is wrong."""
    assert sanity.check(**{
        "items": pack["inventory"], "demand": pack["demand"],
        "invoices": pack["receivables"], "jobs": pack["service"],
        "placements": pack["machines"], "commitments": pack["commitments"],
        "as_of": AS_OF,
    }) == ()


def test_nothing_known_wrong_is_not_the_same_as_right(pack):
    s = sanity.summary(sanity.check(items=pack["inventory"], as_of=AS_OF))
    assert "nothing_known_wrong" in s
    assert "is_valid" not in s and "clean" not in s


def test_an_empty_input_raises_nothing():
    assert sanity.check(as_of=AS_OF) == ()


# ------------------------------------------------------ referential integrity


def test_demand_for_an_item_that_is_not_in_stock(pack):
    broken = [*pack["demand"],
              DemandPeriod("GHOST-99", "2026-10", 50, "DX", "KHI")]
    f = finding(pack, "demand_orphan", demand=broken)
    assert f.severity is BLOCKS or f.severity == BLOCKS
    assert "GHOST-99" in f.detail
    assert "ignored entirely" in f.effect


def test_demand_at_a_branch_with_no_stock(pack):
    broken = [*pack["demand"],
              DemandPeriod("HEM-RGT-01", "2026-10", 50, "DX", "PEW")]
    assert "demand_orphan_location" in codes(pack, demand=broken)


def test_stock_for_an_item_with_no_demand_history(pack):
    broken = [*pack["inventory"],
              item(pack["inventory"][0], item_key="NEW-1", expiry_date=None)]
    f = finding(pack, "stock_no_demand", items=broken)
    assert f.severity == WARNS
    assert "join failure" in f.effect


def test_a_service_job_against_a_machine_that_does_not_exist(pack):
    broken = [*pack["service"],
              ServiceJob("J-X", "NO-SUCH-MACHINE", date(2026, 10, 1),
                         parts_cost=250_000, parts_charged=0.0)]
    f = finding(pack, "service_orphan", jobs=broken)
    assert f.severity == BLOCKS
    assert "attached to nothing" in f.effect


def test_a_placement_at_a_customer_with_no_invoices(pack):
    broken = [*pack["machines"],
              MachinePlacement("MX-NEW", "A", "NOBODY", "Nobody",
                               date(2024, 1, 1), 1_000_000, 48)]
    assert "placement_orphan_customer" in codes(pack, placements=broken)


# ------------------------------------------------------------- master data


def test_one_customer_under_two_codes_is_the_expensive_one(pack):
    """Group exposure groups on the code, so a split debtor reads as several
    manageable balances instead of one large one."""
    broken = [*pack["receivables"],
              ReceivableInvoice("I-DUP", "PIMS-2", "Public Hospital A PIMS",
                                date(2026, 1, 1), date(2026, 4, 1),
                                9_000_000, 0.0, True, False, "DX", "KHI")]
    f = finding(pack, "customer_possible_duplicate", invoices=broken)
    assert f.severity == BLOCKS
    assert "PIMS" in f.detail
    assert "split across codes" in f.effect


def test_company_noise_words_do_not_create_false_matches(pack):
    """Two genuinely different hospitals must not merge just because both are
    called hospital."""
    broken = [*pack["receivables"],
              ReceivableInvoice("I-A", "ZEBRA", "Zebra Hospital (Pvt) Ltd",
                                date(2026, 1, 1), date(2026, 4, 1),
                                1_000_000, 0.0, False, False, "DX", "KHI")]
    assert "customer_possible_duplicate" not in codes(pack, invoices=broken)


def test_one_code_with_several_names_is_only_cosmetic(pack):
    broken = [*pack["receivables"],
              ReceivableInvoice("I-NM", "PIMS", "PUBLIC HOSP A",
                                date(2026, 1, 1), date(2026, 4, 1),
                                1_000, 0.0, True, False, "DX", "KHI")]
    f = finding(pack, "customer_name_variants", invoices=broken)
    assert f.severity == NOTES
    assert "totals are right" in f.effect


def test_one_item_described_differently_at_two_branches(pack):
    broken = [item(x, description="Haem reagent pk")
              if x.item_key == "HEM-RGT-01" and x.location_id == "LHR" else x
              for x in pack["inventory"]]
    f = finding(pack, "item_description_variants", items=broken)
    assert f.severity == NOTES


# ----------------------------------------------------------- scale and units


def test_a_cost_off_by_a_thousand_at_one_branch(pack):
    """The commonest real-world corruption: one branch exports in thousands."""
    broken = [item(x, unit_cost=5.0)
              if x.item_key == "HEM-RGT-01" and x.location_id == "LHR" else x
              for x in pack["inventory"]]
    f = finding(pack, "unit_cost_scale", items=broken)
    assert f.severity == BLOCKS
    assert "exporting in thousands" in f.effect
    assert "never guesses a unit" in f.fix


def test_a_consistent_price_difference_between_branches_is_not_flagged(pack):
    """Branches do pay different landed costs. Only a scale-sized gap counts."""
    broken = [item(x, unit_cost=x.unit_cost * 1.4)
              if x.location_id == "LHR" else x for x in pack["inventory"]]
    assert "unit_cost_scale" not in codes(pack, items=broken)


def test_a_zero_cost_makes_an_item_weightless(pack):
    broken = [item(x, unit_cost=0.0) if x.item_key == "PFT-FLT-07" else x
              for x in pack["inventory"]]
    f = finding(pack, "unit_cost_zero", items=broken)
    assert "weightless" in f.effect


# --------------------------------------------------------- policy inputs


def test_a_zero_lead_time_orders_far_too_late(pack):
    broken = [item(x, lead_time_days=0.0) if x.item_key == "PFT-FLT-07" else x
              for x in pack["inventory"]]
    f = finding(pack, "lead_time_zero", items=broken)
    assert "far too late" in f.effect


def test_lead_time_variability_of_zero_everywhere_is_a_default_not_a_figure(pack):
    """Safety stock comes from the variability of demand and of lead time. Set
    to zero it assumes every shipment arrives exactly on day 75, which for an
    importer is the one thing that never happens."""
    broken = [item(x, lead_time_days_std=0.0) for x in pack["inventory"]]
    f = finding(pack, "lead_time_std_all_zero", items=broken)
    assert f.severity == WARNS
    assert "understated" in f.effect


def test_one_item_with_zero_variability_is_not_the_same_finding(pack):
    broken = [item(x, lead_time_days_std=0.0) if x.item_key == "PFT-FLT-07" else x
              for x in pack["inventory"]]
    assert "lead_time_std_all_zero" not in codes(pack, items=broken)


# ------------------------------------------------------------------- dates


def test_stock_that_has_already_expired_is_still_counted_as_cover(pack):
    broken = [item(x, expiry_date=AS_OF - timedelta(days=30))
              if x.item_key == "BGA-CRT-05" else x for x in pack["inventory"]]
    f = finding(pack, "expiry_in_the_past", items=broken)
    assert f.severity == BLOCKS
    assert "write-off" in f.effect


def test_a_placement_dated_after_the_as_of_date(pack):
    broken = [MachinePlacement(
        x.machine_key, x.model, x.customer_key, x.customer_name,
        AS_OF + timedelta(days=30), x.capital_cost, x.contract_months,
    ) if x.machine_key == "AN-5100-01" else x for x in pack["machines"]]
    assert "placed_in_the_future" in codes(pack, placements=broken)


def test_an_invoice_due_before_it_was_raised(pack):
    broken = [*pack["receivables"],
              ReceivableInvoice("I-BACK", "PIMS", "Public Hospital A (PIMS)",
                                date(2026, 5, 1), date(2026, 2, 1),
                                500_000, 0.0, True, False, "DX", "KHI")]
    assert "due_before_invoice" in codes(pack, invoices=broken)


def test_demand_history_that_stops_months_before_the_as_of_date(pack):
    """Nothing is arithmetically wrong. It is all answering a question about a
    different month."""
    broken = [d for d in pack["demand"] if d.period <= "2026-06"]
    f = finding(pack, "demand_stale", demand=broken)
    assert "different month" in f.effect


def test_a_month_missing_from_the_middle_of_a_series(pack):
    """An absent month is not the same as a zero month, and it changes the
    demand class, which changes the forecast method."""
    broken = [d for d in pack["demand"]
              if not (d.item_key == "HEM-RGT-01" and d.location_id == "KHI"
                      and d.period == "2026-05")]
    f = finding(pack, "demand_gaps", demand=broken)
    assert "demand class" in f.effect


def test_a_short_series_at_the_edges_is_not_called_a_gap(pack):
    """An item that only started selling in June has a short series, not a
    gap, and must not be reported as one."""
    broken = [d for d in pack["demand"]
              if not (d.item_key == "HEM-RGT-01" and d.location_id == "KHI"
                      and d.period < "2026-06")]
    assert "demand_gaps" not in codes(pack, demand=broken)


# -------------------------------------------------------------- duplicates


@pytest.mark.parametrize("dataset,code", [
    ("inventory", "inventory_duplicate_rows"),
    ("demand", "demand_duplicate_rows"),
    ("receivables", "invoice_duplicate"),
    ("service", "job_duplicate"),
    ("machines", "machine_duplicate"),
])
def test_a_repeated_row_is_caught_in_every_dataset(pack, dataset, code):
    key = {"inventory": "items", "demand": "demand", "receivables": "invoices",
           "service": "jobs", "machines": "placements"}[dataset]
    broken = [*pack[dataset], pack[dataset][0]]
    assert code in codes(pack, **{key: broken})


def test_a_duplicated_month_pushes_items_toward_smooth(pack):
    f = finding(pack, "demand_duplicate_rows",
                demand=[*pack["demand"], pack["demand"][0]])
    assert "wrong forecast method" in f.effect


# --------------------------------------------------------------- coverage


def test_missing_commitments_overstate_the_order_headroom(pack):
    f = finding(pack, "no_commitments", commitments=[])
    assert "headroom is overstated" in f.effect


def test_no_demand_at_all_blocks_everything(pack):
    f = finding(pack, "no_demand", demand=[])
    assert f.severity == BLOCKS


def test_a_branch_with_rows_but_no_stock(pack):
    broken = [item(x, on_hand=0.0) if x.location_id == "ISB" else x
              for x in pack["inventory"]]
    assert "branch_empty" in codes(pack, items=broken)


# ---------------------------------------------------------------- shape


def test_findings_are_ordered_worst_first(pack):
    broken_items = [*pack["inventory"], pack["inventory"][0]]
    out = sanity.check(items=broken_items, demand=pack["demand"],
                       commitments=[], as_of=AS_OF)
    severities = [f.severity for f in out]
    rank = {BLOCKS: 0, WARNS: 1, NOTES: 2}
    assert severities == sorted(severities, key=lambda s: rank[s])


def test_every_finding_says_what_it_does_to_the_numbers(pack):
    """"This is odd" is not actionable. "Your stock value is wrong by a factor
    of a thousand" is."""
    out = sanity.check(items=[*pack["inventory"], pack["inventory"][0]],
                       demand=[], commitments=[], as_of=AS_OF)
    for f in out:
        assert f.title.strip()
        assert f.detail.strip()
        assert f.effect.strip()
        assert f.fix.strip()


def test_the_named_rows_are_capped_so_one_finding_cannot_flood_the_panel(pack):
    broken = [item(x, unit_cost=0.0) for x in pack["inventory"]]
    f = finding(pack, "unit_cost_zero", items=broken)
    assert f.count == len(pack["inventory"])
    assert len(f.rows) <= 25


def test_sanity_never_changes_a_figure(pack):
    """Silently correcting a customer's data is how a product loses the right
    to be trusted. check() takes sequences and returns findings only."""
    before = [x.unit_cost for x in pack["inventory"]]
    sanity.check(items=pack["inventory"], demand=pack["demand"], as_of=AS_OF)
    assert [x.unit_cost for x in pack["inventory"]] == before


# ------------------------- data that is present but silently incomplete
#
# The two faults found by hand this session, neither of which any
# check caught. Both share a shape: the record exists, every field
# on it looks reasonable, and a downstream figure is quietly built
# on an absence.

def test_a_placement_with_no_reagent_linked_is_a_finding(pack):
    """The whole Equipment module is blind for that machine: it
    cannot tell a customer who stopped buying from one nobody has
    linked a reagent to, and the second reads as the first."""
    placements = [
        replace(p, reagent_revenue_to_date=0.0,
                reagent_revenue_measured=False)
        for p in pack["machines"]
    ]
    assert "placement_unmeasured" in codes(pack, placements=placements)


def test_a_placement_with_sales_behind_it_is_not_a_finding(pack):
    placements = [
        replace(p, reagent_revenue_to_date=400_000.0,
                reagent_revenue_measured=True)
        for p in pack["machines"]
    ]
    assert "placement_unmeasured" not in codes(pack, placements=placements)


def test_a_stock_line_on_a_provisional_landed_cost_is_a_finding(pack):
    """Its unit cost will move once a missing weight or volume is
    entered, and everything priced off it moves with it."""
    items = [item(pack["inventory"][0], cost_provisional=True,
                  cost_provisional_of=1, cost_receipts=2)]
    items += list(pack["inventory"][1:])
    assert "cost_provisional" in codes(pack, items=items)


def test_a_settled_landed_cost_is_not_a_finding(pack):
    assert "cost_provisional" not in codes(pack)


def test_both_findings_say_what_they_do_to_the_numbers(pack):
    """A finding nobody can act on is a finding read once and never
    acted on. Every other check in this file carries an effect and a
    fix; these must too."""
    placements = [
        replace(p, reagent_revenue_to_date=0.0,
                reagent_revenue_measured=False)
        for p in pack["machines"]
    ]
    f = finding(pack, "placement_unmeasured", placements=placements)
    assert f.effect and f.fix
    assert "reagent" in f.fix.lower()

    items = [item(pack["inventory"][0], cost_provisional=True,
                  cost_provisional_of=1, cost_receipts=2)]
    items += list(pack["inventory"][1:])
    g = finding(pack, "cost_provisional", items=items)
    assert g.effect and g.fix
