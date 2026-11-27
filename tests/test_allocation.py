"""Charges that reach no item, and the figures that rescue them.

Every cost on a shipment is divided across its lines on its own basis.
The division is a share -- this line's weight over the shipment's
weight -- so when the shipment's weight is nothing, the share is a
division by zero. The database refuses it, the allocation comes out
null, and the bill sits in the shipment total with not one unit price
moved. An importer pricing off that stock is short by the whole freight
bill and has nothing on any screen to tell them.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from engine import operations
from engine.operations import NotAllowed

from tests.test_receiving import (  # noqa: F401  (fixtures)
    DSN, Importer, as_user, pytestmark, schema)


def charges(con, who, org) -> dict:
    """{(shipment, kind): (billed, allocated, unallocated, blocked_by)}"""
    with as_user(con, who) as c:
        rows = c.execute(
            "select reference, kind, billed, allocated, unallocated,"
            "       blocked_by from public.shipment_charge_allocation"
            " where org_id = %s", (org,)).fetchall()
    return {(r[0], r[1]): (float(r[2]), float(r[3]), float(r[4]), r[5])
            for r in rows}


def test_a_charge_with_no_basis_is_named_not_just_counted(schema):
    """2,000 USD of freight divided by volume across lines with no
    volume recorded reaches nothing. The totals said how much had not
    been allocated; nothing said which charge or why."""
    imp = Importer(schema, "Stranded")
    imp.freight(2000, "USD", 280, "volume")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900",
                           weight=400)          # a weight, but no volume

    billed, allocated, unallocated, blocked = charges(
        schema, imp.who, imp.org)[("SHP-1", "freight")]
    assert billed == 560_000.0
    assert allocated == 0.0, "the share divided by zero and landed"
    assert unallocated == 560_000.0
    assert blocked == "no volume recorded"


def test_billed_allocated_and_unallocated_reconcile(schema):
    """Whatever state the data is in, the three add up. A charge either
    lands whole or does not land at all: a share divides out to exactly
    one whenever its basis is not zero."""
    imp = Importer(schema, "Reconcile")
    imp.freight(2000, "USD", 280, "volume")     # will be stranded
    imp.freight(500, "USD", 280, "value")       # will land
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900",
                           weight=400)

    rows = charges(schema, imp.who, imp.org)
    for key, (billed, allocated, unallocated, _why) in rows.items():
        assert round(allocated + unallocated, 2) == round(billed, 2), key

    with as_user(schema, imp.who) as c:
        actually = float(c.execute(
            "select coalesce(sum(allocated_cost), 0) from public.landed_cost"
            " where org_id = %s", (imp.org,)).fetchone()[0])
    claimed = sum(r[1] for r in rows.values())
    assert round(actually, 2) == round(claimed, 2), (
        "the view claims a different amount landed than landed_cost did")


def test_entering_the_missing_volume_rescues_the_charge(schema):
    imp = Importer(schema, "Rescue")
    imp.freight(2000, "USD", 280, "volume")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900",
                           weight=400)
    assert charges(schema, imp.who, imp.org)[
        ("SHP-1", "freight")][1] == 0.0

    with imp.acting() as cur:
        out = operations.set_shipment_basis(
            cur, "SHP-1", "RGT-1", batch="B-1", volume=2.5,
            why="Bill of lading gives 2.5 cbm")
    assert out["changed"] == {"volume_cbm": (None, 2.5)}

    billed, allocated, unallocated, blocked = charges(
        schema, imp.who, imp.org)[("SHP-1", "freight")]
    assert allocated == 560_000.0
    assert unallocated == 0.0
    assert blocked is None

    with as_user(schema, imp.who) as c:
        landed = float(c.execute(
            "select landed_unit_cost from public.landed_cost"
            " where org_id = %s", (imp.org,)).fetchone()[0])
    # 17,080 invoice plus 5,600 a unit of freight that was reaching
    # nothing a moment ago.
    assert round(landed, 2) == 22_680.0


def test_correcting_a_volume_moves_the_landed_cost(schema):
    """Two lines, and the split follows the figures. Correcting one
    moves money between items, which is why the change is recorded."""
    imp = Importer(schema, "Split")
    imp.freight(1000, "USD", 280, "volume")     # 280,000 in base
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900",
                           volume=1.0)
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-2", grn="GRN-900",
                           volume=1.0)

    def alloc() -> dict:
        with as_user(schema, imp.who) as c:
            return {r[0]: float(r[1]) for r in c.execute(
                "select gl.batch, lc.allocated_cost from public.landed_cost lc"
                "  join public.grn_lines gl on gl.id = lc.grn_line_id"
                " where lc.org_id = %s", (imp.org,)).fetchall()}

    assert alloc() == {"B-1": 140_000.0, "B-2": 140_000.0}

    with imp.acting() as cur:
        operations.set_shipment_basis(cur, "SHP-1", "RGT-1", batch="B-1",
                                      volume=3.0,
                                      why="Measured on arrival: 3 cbm")

    after = alloc()
    assert after == {"B-1": 210_000.0, "B-2": 70_000.0}
    # Still all of it, just divided differently.
    assert round(sum(after.values()), 2) == 280_000.0


def test_the_change_is_written_down_with_its_reason(schema):
    """The allocation is a view over the current figures, so the
    division that produced last month's landed cost is gone the moment
    a figure changes. The audit row is the only thing left that can
    explain it."""
    imp = Importer(schema, "Audited")
    imp.freight(1000, "USD", 280, "volume")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900",
                           volume=1.0)
    with imp.acting() as cur:
        operations.set_shipment_basis(cur, "SHP-1", "RGT-1", batch="B-1",
                                      volume=3.0, weight=640,
                                      why="Measured on arrival")

    with as_user(schema, imp.who) as c:
        rows = c.execute(
            "select field, was, became, why, changed_by is not null"
            "  from public.shipment_line_changes where org_id = %s"
            " order by field", (imp.org,)).fetchall()
    assert [(r[0], r[1], float(r[2])) for r in rows] == [
        ("volume_cbm", 1.0, 3.0),
        ("weight_kg", None, 640.0),
    ]
    assert all(r[3] == "Measured on arrival" for r in rows)
    assert all(r[4] for r in rows), "the change does not say who made it"


def test_a_change_needs_a_reason(schema):
    imp = Importer(schema, "NoWhy")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900")
    with pytest.raises(NotAllowed) as e:
        with imp.acting() as cur:
            operations.set_shipment_basis(cur, "SHP-1", "RGT-1",
                                          batch="B-1", volume=2.5)
    assert "why" in str(e.value).lower()


def test_a_stated_zero_weight_is_a_figure(schema):
    """A line that genuinely weighs nothing still has to be stated:
    that is what tells the allocation to give it no share, rather than
    leaving the whole shipment with no basis at all."""
    imp = Importer(schema, "Zero")
    imp.freight(1000, "USD", 280, "weight")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900")
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-2", grn="GRN-900")
    with imp.acting() as cur:
        operations.set_shipment_basis(cur, "SHP-1", "RGT-1", batch="B-1",
                                      weight=640, why="Weighbridge ticket")
        operations.set_shipment_basis(cur, "SHP-1", "RGT-1", batch="B-2",
                                      weight=0, why="Samples, carried free")

    billed, allocated, unallocated, blocked = charges(
        schema, imp.who, imp.org)[("SHP-1", "freight")]
    assert (allocated, unallocated, blocked) == (280_000.0, 0.0, None)
    with as_user(schema, imp.who) as c:
        got = {r[0]: float(r[1]) for r in c.execute(
            "select gl.batch, lc.allocated_cost from public.landed_cost lc"
            "  join public.grn_lines gl on gl.id = lc.grn_line_id"
            " where lc.org_id = %s", (imp.org,)).fetchall()}
    assert got == {"B-1": 280_000.0, "B-2": 0.0}


def test_a_shipment_still_at_sea_is_not_a_fault(schema):
    """Flagging the normal state of a shipment as though somebody
    forgot something trains people to ignore the flags."""
    imp = Importer(schema, "AtSea")
    with as_user(schema, imp.who) as c:
        c.execute("update public.shipments set status = 'in_transit'"
                  " where id = %s", (imp.shipment,))
    imp.freight(2000, "USD", 280, "value")

    key = ("SHP-1", "freight")
    assert charges(schema, imp.who, imp.org)[key][3] == "awaiting goods"

    from web import modules
    with as_user(schema, imp.who) as c:
        class F:
            conn = c
        got = modules._freight(F())
    problem = [p for p in got["problems"] if p["shipment"] == "SHP-1"][0]
    assert problem["blocked_by"] == "awaiting goods"
    assert problem["expected"] is True
    assert got["needs_attention"] == 0


def test_a_blocked_charge_says_what_to_go_and_do(schema):
    imp = Importer(schema, "Actionable")
    imp.freight(2000, "USD", 280, "weight")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900")

    from web import modules
    with as_user(schema, imp.who) as c:
        class F:
            conn = c
        got = modules._freight(F())
    problem = [p for p in got["problems"] if p["shipment"] == "SHP-1"][0]
    assert problem["blocked_by"] == "no weight recorded"
    assert "weight" in problem["action"]
    assert problem["billed"] == 560_000.0
    assert problem["expected"] is False
    assert got["needs_attention"] == 1


def test_weighing_a_line_that_never_arrived_is_refused(schema):
    imp = Importer(schema, "Ghost")
    with pytest.raises(NotAllowed) as e:
        with imp.acting() as cur:
            operations.set_shipment_basis(cur, "SHP-1", "RGT-1",
                                          volume=1.0, why="x")
    assert "SHP-1" in str(e.value)


def test_a_change_to_the_same_figure_is_refused(schema):
    imp = Importer(schema, "Same2")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900",
                           volume=2.5)
    with pytest.raises(NotAllowed) as e:
        with imp.acting() as cur:
            operations.set_shipment_basis(cur, "SHP-1", "RGT-1",
                                          batch="B-1", volume=2.5,
                                          why="No change")
    assert "changes nothing" in str(e.value)


# ------------------------------------- landed in full, in the wrong place
#
# 0019 asked one question of each charge: is the basis it divides by
# zero? Necessary, and not sufficient. One shipment, two lines, freight
# by weight, and only one line weighed:
#
#   the diagnostic said:  billed 280,000  allocated 280,000
#                         unallocated 0   nothing missing
#   where it went:        A 280,000, landed 3,800 a unit
#                         B       0, landed 1,000 a unit
#
# A charge that reaches nothing at least shows up as a figure that has
# not moved. This one produces a complete-looking allocation and a
# confident unit cost, too high on the measured goods and too low on
# the rest, and feeds the ABC class, the service target and the price
# list from there.


def test_a_charge_split_across_only_some_of_the_goods_is_reported(schema):
    imp = Importer(schema, "Skewed")
    imp.freight(1000, "USD", 280, "weight")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900",
                           weight=500)
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-2", grn="GRN-900")

    with as_user(schema, imp.who) as c:
        billed, allocated, blocked, skewed, lines, missing = c.execute(
            "select billed, allocated, blocked_by, skewed_by, lines,"
            "       lines_missing_basis"
            "  from public.shipment_charge_allocation where org_id = %s",
            (imp.org,)).fetchone()
    # It all landed, which is why nothing caught it.
    assert float(allocated) == float(billed) == 280_000.0
    assert blocked is None
    assert skewed == "weight missing on 1 of 2 lines"
    assert (lines, missing) == (2, 1)


def test_the_skew_is_what_it_says_it_is(schema):
    """The whole charge on the measured line, none on the other."""
    imp = Importer(schema, "Proof")
    imp.freight(1000, "USD", 280, "weight")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900",
                           weight=500)
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-2", grn="GRN-900")
    with as_user(schema, imp.who) as c:
        got = {r[0]: float(r[1]) for r in c.execute(
            "select gl.batch, lc.allocated_cost from public.landed_cost lc"
            "  join public.grn_lines gl on gl.id = lc.grn_line_id"
            " where lc.org_id = %s", (imp.org,)).fetchall()}
    assert got == {"B-1": 280_000.0, "B-2": 0.0}


def test_entering_the_missing_weight_clears_it_and_resplits(schema):
    imp = Importer(schema, "Clears")
    imp.freight(1000, "USD", 280, "weight")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900",
                           weight=500)
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-2", grn="GRN-900")
    with imp.acting() as cur:
        operations.set_shipment_basis(cur, "SHP-1", "RGT-1", batch="B-2",
                                      weight=500, why="Weighbridge ticket")

    with as_user(schema, imp.who) as c:
        skewed, missing = c.execute(
            "select skewed_by, lines_missing_basis"
            "  from public.shipment_charge_allocation where org_id = %s",
            (imp.org,)).fetchone()
        got = {r[0]: float(r[1]) for r in c.execute(
            "select gl.batch, lc.allocated_cost from public.landed_cost lc"
            "  join public.grn_lines gl on gl.id = lc.grn_line_id"
            " where lc.org_id = %s", (imp.org,)).fetchall()}
    assert (skewed, missing) == (None, 0)
    assert got == {"B-1": 140_000.0, "B-2": 140_000.0}


def test_a_stated_zero_weight_is_not_a_missing_one(schema):
    """A line carried free really does take no share. Only a null is
    an unanswered question."""
    imp = Importer(schema, "ZeroNotNull")
    imp.freight(1000, "USD", 280, "weight")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900",
                           weight=500)
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-2", grn="GRN-900",
                           weight=0)
    with as_user(schema, imp.who) as c:
        skewed = c.execute(
            "select skewed_by from public.shipment_charge_allocation"
            " where org_id = %s", (imp.org,)).fetchone()[0]
    assert skewed is None


def test_value_and_quantity_bases_are_never_called_incomplete(schema):
    """They come from the receipt line, where both columns are not
    null. A line priced at zero contributes nothing to a value split
    and that is correct, not missing: free goods attract no duty by
    value."""
    imp = Importer(schema, "ByValue")
    imp.freight(1000, "USD", 280, "value")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900")
        operations.receive(cur, "RGT-1", "KHI", 50, unit_cost=0,
                           shipment="SHP-1", batch="B-FREE", grn="GRN-900")
    with as_user(schema, imp.who) as c:
        skewed, missing = c.execute(
            "select skewed_by, lines_missing_basis"
            "  from public.shipment_charge_allocation where org_id = %s",
            (imp.org,)).fetchone()
    assert (skewed, missing) == (None, 0)


def test_the_screen_says_which_lines_and_what_it_does_to_the_costs(schema):
    imp = Importer(schema, "Said")
    imp.freight(1000, "USD", 280, "weight")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900",
                           weight=500)
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-2", grn="GRN-900")

    from web import modules
    with as_user(schema, imp.who) as c:
        class F:
            conn = c
        got = modules._freight(F())
    p = [x for x in got["problems"] if x["shipment"] == "SHP-1"][0]
    assert p["blocked_by"] == "weight missing on 1 of 2 lines"
    assert p["expected"] is False
    assert "valued too high" in p["action"]
    assert got["skewed"] == 1
    assert got["needs_attention"] >= 1
