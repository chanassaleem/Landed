"""What the stock actually cost, which is what the product is for.

The engine was computing the landed cost and then valuing the stock
at the supplier's invoice price, in the supplier's currency. A hundred
units at EUR 50 bought at 310 to the euro, with PKR 620,000 of freight
and duty on top, cost PKR 21,700 a unit and appeared on screen as
PKR 50 -- stock worth PKR 2,170,000 shown as PKR 5,000, wrong by a
factor of 434.

Everything downstream rests on this number: stock value, the money
standing still, the value of what policy says to order, annual
consumption value and therefore the ABC class, and the margin on a
sale. It is also the claim the product is sold on -- that landed cost
adds a fifth to a half on top of FOB and a business pricing off the
supplier invoice is wrong by that much on every line.

The worked example below is the one that found it, and the numbers
are round on purpose: anybody can check them by hand.

    source scripts/dev_postgres.sh
    python3 -m pytest tests/test_valuation.py
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("LANDED_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="LANDED_TEST_DSN not set")

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
SHIM = ROOT / "supabase" / "tests" / "00_supabase_shim.sql"

# One shipment, chosen so every figure can be checked in your head.
UNITS = 100
EUR_EACH = 50            # what the supplier invoiced
RATE = 310               # rupees to the euro on the day it was received
FREIGHT = 220_000        # rupees
DUTY = 400_000           # rupees

GOODS = UNITS * EUR_EACH * RATE          # 1,550,000
COSTS = FREIGHT + DUTY                   # 620,000
AT_RECEIPT = EUR_EACH * RATE             # 15,500 a unit
LANDED = (GOODS + COSTS) / UNITS         # 21,700 a unit


@contextlib.contextmanager
def as_user(con, uid):
    with con.transaction():
        con.execute("set local role authenticated")
        con.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (json.dumps({"sub": str(uid), "role": "authenticated"}),))
        yield con


@pytest.fixture(scope="module")
def db():
    con = psycopg.connect(DSN, autocommit=True)
    for schema in ("public", "app", "auth"):
        con.execute(f"drop schema if exists {schema} cascade")
    con.execute("create schema public")
    for path in [SHIM, *MIGRATIONS]:
        con.execute(path.read_text())
    yield con
    con.close()


def a_shipment(con, *, rate=RATE, costs=True, with_po=True):
    """One euro purchase, received, optionally with its costs landed."""
    owner = uuid.uuid4()
    con.execute("insert into auth.users (id, email) values (%s, %s)",
                (owner, f"{owner.hex[:8]}@valuation.test"))
    with as_user(con, owner) as c:
        org = c.execute(
            "insert into public.organisations (name, created_by)"
            " values ('Valuation Co', %s) returning id",
            (owner,)).fetchone()[0]
        loc = c.execute(
            "insert into public.locations (org_id, code, name)"
            " values (%s,'KHI','Karachi') returning id",
            (org,)).fetchone()[0]
        sup = c.execute(
            "insert into public.suppliers (org_id, code, name, currency)"
            " values (%s,'ROCHE','Roche','EUR') returning id",
            (org,)).fetchone()[0]
        item = c.execute(
            "insert into public.items (org_id, sku, description)"
            " values (%s,'RGT-1','Reagent') returning id",
            (org,)).fetchone()[0]

        po_line = None
        if with_po:
            po = c.execute(
                "insert into public.purchase_orders (org_id, location_id,"
                " supplier_id, po_number, currency, fx_rate, status)"
                " values (%s,%s,%s,'PO-1','EUR',%s,'placed') returning id",
                (org, loc, sup, rate)).fetchone()[0]
            po_line = c.execute(
                "insert into public.po_lines (org_id, po_id, item_id, qty,"
                " unit_price) values (%s,%s,%s,%s,%s) returning id",
                (org, po, item, UNITS, EUR_EACH)).fetchone()[0]
            grn = c.execute(
                "insert into public.goods_receipts (org_id, location_id,"
                " po_id, grn_number) values (%s,%s,%s,'GRN-1') returning id",
                (org, loc, po)).fetchone()[0]
        else:
            grn = c.execute(
                "insert into public.goods_receipts (org_id, location_id,"
                " grn_number) values (%s,%s,'GRN-1') returning id",
                (org, loc)).fetchone()[0]

        line = c.execute(
            "insert into public.grn_lines (org_id, grn_id, po_line_id,"
            " item_id, qty, unit_price) values (%s,%s,%s,%s,%s,%s)"
            " returning id",
            (org, grn, po_line, item, UNITS, EUR_EACH)).fetchone()[0]

        if costs:
            ship = c.execute(
                "insert into public.shipments (org_id, reference,"
                " supplier_id, status) values (%s,'SHP-1',%s,'arrived')"
                " returning id", (org, sup)).fetchone()[0]
            c.execute(
                "insert into public.shipment_lines (shipment_id,"
                " grn_line_id, org_id, weight_kg, volume_cbm)"
                " values (%s,%s,%s,100,2)", (ship, line, org))
            for kind, amount in (("freight", FREIGHT), ("duty", DUTY)):
                c.execute(
                    "insert into public.shipment_costs (org_id,"
                    " shipment_id, kind, amount, allocate_by)"
                    " values (%s,%s,%s,%s,'value')",
                    (org, ship, kind, amount))
    return owner, org, item, line


def cost_of(con, owner, item):
    with as_user(con, owner) as c:
        row = c.execute(
            "select avg_unit_cost from public.item_cost where item_id = %s",
            (item,)).fetchone()
    return float(row[0]) if row else None


# -------------------------------------------- the invoice, in our money


def test_a_euro_invoice_is_not_a_rupee_cost(db):
    """The goods-receipt trigger posted `unit_price` with no rate, so
    EUR 50 went into a rupee ledger as 50."""
    owner, _org, item, _line = a_shipment(db, costs=False)
    with as_user(db, owner) as c:
        got = c.execute(
            "select unit_cost from public.stock_movement"
            " where item_id = %s", (item,)).fetchone()[0]
    assert float(got) == pytest.approx(AT_RECEIPT), (
        f"the ledger holds {got}, not {AT_RECEIPT}: the rate was not "
        f"applied")


def test_a_receipt_inherits_the_rate_from_its_order(db):
    """A receipt against an order is in that order's currency by
    definition. Asking somebody to retype the rate is asking them to
    get it wrong."""
    owner, _org, _item, line = a_shipment(db, costs=False)
    with as_user(db, owner) as c:
        got = c.execute(
            "select currency, fx_rate from public.grn_lines where id = %s",
            (line,)).fetchone()
    assert got[0] == "EUR"
    assert float(got[1]) == pytest.approx(RATE)


def test_a_receipt_with_no_order_stays_in_base_currency(db):
    """Nothing to inherit from, so nothing is assumed. A rate invented
    for a direct receipt would be worse than none."""
    owner, _org, _item, line = a_shipment(
        db, costs=False, with_po=False)
    with as_user(db, owner) as c:
        got = c.execute(
            "select fx_rate from public.grn_lines where id = %s",
            (line,)).fetchone()[0]
    assert float(got) == 1.0


# ------------------------------------- and what landed on top of it


def test_before_the_clearing_invoice_the_cost_is_the_invoice(db):
    """Honest rather than optimistic. The clearing agent's invoice
    arrives weeks after the goods, and until it does the only figure
    anybody has is what the supplier charged."""
    owner, _org, item, _line = a_shipment(db, costs=False)
    assert cost_of(db, owner, item) == pytest.approx(AT_RECEIPT)


def test_once_the_costs_land_the_valuation_follows(db):
    """The whole point. Landed cost adds a fifth to a half on top of
    FOB in this trade, and a business valuing stock at the invoice
    price is wrong by that much on every line."""
    owner, _org, item, _line = a_shipment(db)
    assert cost_of(db, owner, item) == pytest.approx(LANDED)


def test_the_allocation_is_not_summing_two_currencies(db):
    """`shipment_line_basis.fob_value` was quantity times unit price
    with no rate, while the costs allocated against it were converted.
    One sum, two currencies -- and the error hid inside a figure that
    still looked like money."""
    owner, _org, item, _line = a_shipment(db)
    with as_user(db, owner) as c:
        fob, allocated, landed = c.execute(
            "select fob_value, allocated_cost, landed_unit_cost"
            "  from public.landed_cost where item_id = %s",
            (item,)).fetchone()
    assert float(fob) == pytest.approx(GOODS)
    assert float(allocated) == pytest.approx(COSTS)
    assert float(landed) == pytest.approx(LANDED)


def test_the_stock_on_screen_is_worth_what_it_cost(db):
    """The number the whole product is built on.

    Before this it read PKR 5,000 against a true PKR 2,170,000.
    """
    owner, _org, item, _line = a_shipment(db)
    with as_user(db, owner) as c:
        value = c.execute(
            "select soh.on_hand * ic.avg_unit_cost"
            "  from public.stock_on_hand soh"
            "  join public.item_cost ic on ic.item_id = soh.item_id"
            " where soh.item_id = %s", (item,)).fetchone()[0]
    assert float(value) == pytest.approx(UNITS * LANDED)


def test_the_engine_sees_the_landed_cost_too(db):
    """Through engine/db/snapshot.py, which is the only route every
    module takes. A view that is right and unread is not a fix."""
    from datetime import date

    from engine.db import snapshot

    owner, _org, _item, _line = a_shipment(db)
    with as_user(db, owner) as c:
        snap = snapshot.read(c, date.today())
    held = [i for i in snap.items if i.on_hand > 0]
    assert held, "the snapshot found no stock"
    assert held[0].unit_cost == pytest.approx(LANDED)


def test_opening_stock_keeps_the_cost_it_was_given(db):
    """Only receipts have shipment costs to allocate. An opening
    balance or a count adjustment carries the cost somebody entered,
    and the valuation must not reach for a landed figure that does not
    exist for it."""
    owner, org, item, _line = a_shipment(db, costs=False)
    with as_user(db, owner) as c:
        loc = c.execute(
            "select id from public.locations limit 1").fetchone()[0]
        other = c.execute(
            "insert into public.items (org_id, sku, description)"
            " values (%s,'OPN-1','Opening only') returning id",
            (org,)).fetchone()[0]
        c.execute(
            "insert into public.stock_movement (org_id, location_id,"
            " item_id, qty, unit_cost, reason)"
            " values (%s,%s,%s,40,250,'opening')", (org, loc, other))
    assert cost_of(db, owner, other) == pytest.approx(250.0)
