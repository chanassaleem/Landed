"""Receiving as a document, not a bare ledger row.

Receiving through `record` wrote a movement and nothing else. Proven on
a clean database: order 300, receive 100 by hand.

  movement: reason grn, qty 100, source_type null, source_id null
  goods_receipts: 0 rows
  grn_lines: 0 rows
  the order still reads 300 outstanding

The third is the expensive one. Outstanding quantity is the ordered
quantity less what the receipts account for, so a receipt that files no
line never reduces it, and the Ordering screen goes on recommending
stock that is already in the warehouse.

The first two are what make the rest possible. Freight is allocated
across the lines of a shipment and joined to receipt lines, and
item_cost prefers a landed cost by joining on source_type = 'grn_line'
-- which a sourceless movement never matched, so a manual receipt could
never be valued at anything but the supplier's price.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import uuid
from datetime import date

import pytest

psycopg = pytest.importorskip("psycopg")

from engine import operations
from engine.operations import NotAllowed

DSN = os.environ.get("LANDED_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="LANDED_TEST_DSN not set")

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
SHIM = ROOT / "supabase" / "tests" / "00_supabase_shim.sql"


@contextlib.contextmanager
def as_user(con, uid):
    with con.transaction():
        con.execute("set local role authenticated")
        con.execute("select set_config('request.jwt.claims', %s, true)",
                    (json.dumps({"sub": str(uid), "role": "authenticated",
                                 "email": "recv@landed.test"}),))
        yield con


@pytest.fixture(scope="module")
def schema():
    con = psycopg.connect(DSN, autocommit=True)
    for s in ("public", "app", "auth"):
        con.execute(f"drop schema if exists {s} cascade")
    con.execute("create schema public")
    for path in [SHIM, *MIGRATIONS]:
        con.execute(path.read_text())
    yield con
    con.close()


class Importer:
    """One importer with an order for 300 reagent packs at EUR 56."""

    def __init__(self, con, label, *, ordered=300, price=56.0,
                 ccy="EUR", rate=305.0):
        self.con = con
        self.who = uuid.uuid4()
        con.execute("insert into auth.users (id, email) values (%s,%s)",
                    (self.who, f"{label}-{self.who.hex[:6]}@landed.test"))
        with as_user(con, self.who) as c:
            c.execute("insert into public.organisations (name, created_by)"
                      " values (%s,%s)", (f"{label} Co", self.who))
            self.org = c.execute(
                "select org_id from public.memberships where user_id = %s",
                (self.who,)).fetchone()[0]
            self.khi = c.execute(
                "insert into public.locations (org_id, code, name)"
                " values (%s,'KHI','Karachi') returning id",
                (self.org,)).fetchone()[0]
            self.item = c.execute(
                "insert into public.items (org_id, sku, description,"
                " lead_time_days) values (%s,'RGT-1','Reagent pack',30)"
                " returning id", (self.org,)).fetchone()[0]
            sup = c.execute(
                "insert into public.suppliers (org_id, code, name)"
                " values (%s,'ACME','Acme GmbH') returning id",
                (self.org,)).fetchone()[0]
            self.po = c.execute(
                "insert into public.purchase_orders (org_id, location_id,"
                " supplier_id, po_number, currency, fx_rate, status,"
                " ordered_at) values (%s,%s,%s,'PO-1',%s,%s,'placed',"
                " current_date) returning id",
                (self.org, self.khi, sup, ccy, rate)).fetchone()[0]
            c.execute(
                "insert into public.po_lines (org_id, po_id, item_id, qty,"
                " unit_price) values (%s,%s,%s,%s,%s)",
                (self.org, self.po, self.item, ordered, price))
            self.shipment = c.execute(
                "insert into public.shipments (org_id, reference,"
                " supplier_id, mode, status) values (%s,'SHP-1',%s,'sea',"
                " 'arrived') returning id",
                (self.org, sup)).fetchone()[0]

    @contextlib.contextmanager
    def acting(self):
        with as_user(self.con, self.who) as c:
            yield c.cursor()

    def freight(self, amount, ccy="USD", rate=280.0, basis="value"):
        with as_user(self.con, self.who) as c:
            c.execute(
                "insert into public.shipment_costs (org_id, shipment_id,"
                " kind, amount, currency, fx_rate, allocate_by)"
                " values (%s,%s,'freight',%s,%s,%s,%s)",
                (self.org, self.shipment, amount, ccy, rate, basis))

    def outstanding(self) -> float:
        with as_user(self.con, self.who) as c:
            return float(c.execute(
                "with recv as (select po_line_id, sum(qty) q"
                "   from public.grn_lines where po_line_id is not null"
                "  group by po_line_id)"
                " select sum(pl.qty - coalesce(r.q, 0)) from public.po_lines pl"
                " left join recv r on r.po_line_id = pl.id"
                " where pl.org_id = %s", (self.org,)).fetchone()[0])

    def trace(self) -> list[tuple]:
        """(grn, batch, qty, po_number, shipment_ref, currency, fx)"""
        with as_user(self.con, self.who) as c:
            return c.execute(
                "select g.grn_number, coalesce(gl.batch,''), gl.qty,"
                "       po.po_number, s.reference, gl.currency, gl.fx_rate"
                "  from public.grn_lines gl"
                "  join public.goods_receipts g on g.id = gl.grn_id"
                "  left join public.po_lines pl on pl.id = gl.po_line_id"
                "  left join public.purchase_orders po on po.id = pl.po_id"
                "  left join public.shipment_lines sl"
                "         on sl.grn_line_id = gl.id"
                "  left join public.shipments s on s.id = sl.shipment_id"
                " where gl.org_id = %s order by g.grn_number, gl.batch",
                (self.org,)).fetchall()

    def status(self) -> str:
        with as_user(self.con, self.who) as c:
            return c.execute("select status from public.purchase_orders"
                             " where id = %s", (self.po,)).fetchone()[0]


# ------------------------------------- every line traces to its order


def test_a_received_line_traces_to_its_order_and_shipment(schema):
    imp = Importer(schema, "Trace")
    with imp.acting() as cur:
        out = operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                                 shipment="SHP-1", batch="B-1",
                                 grn="GRN-900", weight=400, volume=2.5)
    assert out["recorded"] is True
    assert out["linked"] is True

    rows = imp.trace()
    assert len(rows) == 1
    grn, batch, qty, po, ship, ccy, fx = rows[0]
    assert (grn, batch, float(qty)) == ("GRN-900", "B-1", 100.0)
    assert po == "PO-1", "the line does not know which order it is against"
    assert ship == "SHP-1", "the line does not know which shipment brought it"
    # Inherited from the order, which is what makes a euro invoice a
    # rupee cost once, at a rate that is recorded rather than guessed.
    assert (ccy, float(fx)) == ("EUR", 305.0)


def test_a_movement_carries_the_line_that_caused_it(schema):
    """source_type = 'grn_line' is not bookkeeping. item_cost joins on
    it to prefer the landed cost, so a sourceless movement could only
    ever be valued at the supplier's price."""
    imp = Importer(schema, "Source")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 60, po="PO-1", grn="GRN-901")
    with as_user(schema, imp.who) as c:
        rows = c.execute(
            "select m.reason, m.source_type, gl.id is not null"
            "  from public.stock_movement m"
            "  left join public.grn_lines gl on gl.id = m.source_id"
            " where m.org_id = %s and m.reason = 'grn'",
            (imp.org,)).fetchall()
    assert rows == [("grn", "grn_line", True)], rows


def test_receiving_the_old_way_still_files_a_receipt(schema):
    """Callers that pass a quantity and a cost to `record` get a
    traceable receipt without changing how they call."""
    imp = Importer(schema, "Legacy")
    with imp.acting() as cur:
        operations.record(cur, "RGT-1", "KHI", "grn", 25, unit_cost=17080)
    with as_user(schema, imp.who) as c:
        lines = c.execute("select count(*) from public.grn_lines"
                          " where org_id = %s", (imp.org,)).fetchone()[0]
        docs = c.execute("select count(*) from public.goods_receipts"
                         " where org_id = %s", (imp.org,)).fetchone()[0]
    assert (lines, docs) == (1, 1)


# ------------------------------------- partial delivery


def test_a_partial_receipt_leaves_the_rest_outstanding(schema):
    """The expensive one. Outstanding is ordered less received, so a
    receipt that filed no line never reduced it -- and Ordering went on
    recommending stock that was already in the warehouse."""
    imp = Importer(schema, "Partial")
    assert imp.outstanding() == 300.0

    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900")
    assert imp.outstanding() == 200.0
    assert imp.status() == "placed", "a partial receipt closed the order"

    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 200, po="PO-1",
                           shipment="SHP-1", batch="B-2", grn="GRN-902")
    assert imp.outstanding() == 0.0


def test_two_lots_of_one_item_on_one_arrival_are_two_lines(schema):
    imp = Importer(schema, "Lots")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           grn="GRN-900", batch="B-1",
                           expiry=date(2027, 12, 31))
        operations.receive(cur, "RGT-1", "KHI", 40, po="PO-1",
                           grn="GRN-900", batch="B-2",
                           expiry=date(2028, 6, 30))
    rows = imp.trace()
    assert [(r[1], float(r[2])) for r in rows] == [("B-1", 100.0),
                                                   ("B-2", 40.0)]
    assert imp.outstanding() == 160.0


def test_the_same_lot_twice_is_one_line(schema):
    """Identity is the receipt, the item and the batch -- the three the
    importer already uses."""
    imp = Importer(schema, "Repeat")
    with imp.acting() as cur:
        first = operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                                   grn="GRN-900", batch="B-1")
        again = operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                                   grn="GRN-900", batch="B-1")
    assert first["recorded"] is True
    assert again["recorded"] is False
    assert len(imp.trace()) == 1
    assert imp.outstanding() == 200.0, "the repeat was counted twice"


def test_a_retry_of_the_same_request_receives_once(schema):
    """No GRN number given, so it is derived from the idempotency key:
    a retry lands on the same number and the identity recognises it."""
    imp = Importer(schema, "Retry")
    key = f"attempt-{uuid.uuid4()}"
    with imp.acting() as cur:
        a = operations.receive(cur, "RGT-1", "KHI", 30, unit_cost=17080,
                               client_ref=key)
        b = operations.receive(cur, "RGT-1", "KHI", 30, unit_cost=17080,
                               client_ref=key)
    assert a["grn"] == b["grn"]
    assert (a["recorded"], b["recorded"]) == (True, False)
    assert len(imp.trace()) == 1


# ------------------------------------- manual against shipment-linked


def test_a_manual_arrival_is_distinguishable_from_a_shipment(schema):
    """Without a flag to keep in step. A receipt is shipment-linked
    exactly when a shipment line joins to it, and a join cannot
    disagree with itself."""
    imp = Importer(schema, "Mixed")
    with imp.acting() as cur:
        linked = operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                                    shipment="SHP-1", grn="GRN-900",
                                    batch="B-1")
        manual = operations.receive(cur, "RGT-1", "KHI", 20, unit_cost=17080,
                                    grn="GRN-901", batch="B-9")
    assert linked["linked"] is True
    assert manual["linked"] is False
    by_grn = {r[0]: r[4] for r in imp.trace()}
    assert by_grn["GRN-900"] == "SHP-1"
    assert by_grn["GRN-901"] is None


def test_a_receipt_against_an_order_that_does_not_exist_is_refused(schema):
    imp = Importer(schema, "NoOrder")
    with pytest.raises(NotAllowed) as e:
        with imp.acting() as cur:
            operations.receive(cur, "RGT-1", "KHI", 10, po="PO-NOPE")
    assert "PO-NOPE" in str(e.value)


def test_a_receipt_of_an_item_not_on_the_order_is_refused(schema):
    """Receiving something nobody ordered is a receipt without an
    order, not a receipt against this one."""
    imp = Importer(schema, "WrongItem")
    with as_user(schema, imp.who) as c:
        c.execute("insert into public.items (org_id, sku, description,"
                  " lead_time_days) values (%s,'OTHER-1','Other',30)",
                  (imp.org,))
    with pytest.raises(NotAllowed) as e:
        with imp.acting() as cur:
            operations.receive(cur, "OTHER-1", "KHI", 10, po="PO-1")
    assert "PO-1" in str(e.value) and "OTHER-1" in str(e.value)


def test_a_receipt_against_a_shipment_that_does_not_exist_is_refused(schema):
    imp = Importer(schema, "NoShip")
    with pytest.raises(NotAllowed) as e:
        with imp.acting() as cur:
            operations.receive(cur, "RGT-1", "KHI", 10, po="PO-1",
                               shipment="SHP-NOPE")
    assert "SHP-NOPE" in str(e.value)


# ------------------------------------- a cost, in a stated currency


def test_a_manual_receipt_cannot_create_stock_without_a_cost(schema):
    imp = Importer(schema, "NoCost")
    with pytest.raises(NotAllowed) as e:
        with imp.acting() as cur:
            operations.receive(cur, "RGT-1", "KHI", 10)
    assert "cost" in str(e.value).lower()
    assert imp.trace() == []


def test_a_receipt_against_an_order_takes_the_order_price(schema):
    imp = Importer(schema, "Inherit")
    with imp.acting() as cur:
        out = operations.receive(cur, "RGT-1", "KHI", 10, po="PO-1",
                                 grn="GRN-900")
    assert out["unit_cost"] == 56.0


def test_a_stated_zero_is_a_cost_and_is_kept(schema):
    """A free sample and a warranty replacement are both real, and both
    are stated. What is refused is silence."""
    imp = Importer(schema, "Free")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 5, unit_cost=0,
                           grn="GRN-FREE")
    with as_user(schema, imp.who) as c:
        price = c.execute("select unit_price from public.grn_lines"
                          " where org_id = %s", (imp.org,)).fetchone()[0]
    assert float(price) == 0.0


def test_a_foreign_price_without_a_rate_is_refused(schema):
    """A bare number was taken as rupees in silence, so a supplier's
    euro price typed into a manual receipt valued the stock at a
    three-hundredth of what it cost. This project has paid for that
    once already."""
    imp = Importer(schema, "Silent")
    with pytest.raises(NotAllowed) as e:
        with imp.acting() as cur:
            operations.receive(cur, "RGT-1", "KHI", 10, unit_cost=56,
                               currency="EUR", grn="GRN-900")
    assert "EUR" in str(e.value)
    assert imp.trace() == []


def test_a_foreign_price_with_its_rate_is_converted_once(schema):
    imp = Importer(schema, "Stated")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 10, unit_cost=56,
                           currency="EUR", fx=305, grn="GRN-900")
    with as_user(schema, imp.who) as c:
        cost = c.execute(
            "select unit_cost from public.stock_movement"
            " where org_id = %s and reason = 'grn'", (imp.org,)).fetchone()[0]
    assert float(cost) == 56 * 305


def test_a_rate_on_the_receipt_beats_the_order(schema):
    """Holding a rate per receipt rather than per order is the whole
    point: the second consignment cleared at a different rate, and that
    difference is the importer's margin."""
    imp = Importer(schema, "Moved")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 10, po="PO-1",
                           grn="GRN-900", fx=312.5)
    with as_user(schema, imp.who) as c:
        ccy, fx = c.execute(
            "select currency, fx_rate from public.grn_lines"
            " where org_id = %s", (imp.org,)).fetchone()
    assert (ccy, float(fx)) == ("EUR", 312.5)


# ------------------------------------- landed cost reconciles


def test_the_landed_cost_is_fob_plus_its_share_of_the_charges(schema):
    imp = Importer(schema, "Landed")
    imp.freight(2000, "USD", 280, "value")      # 560,000 in base
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900",
                           weight=400, volume=2.5)
        operations.receive(cur, "RGT-1", "KHI", 40, po="PO-1",
                           shipment="SHP-1", batch="B-2", grn="GRN-900",
                           weight=160, volume=1.0)

    with as_user(schema, imp.who) as c:
        rows = c.execute(
            "select gl.batch, lc.fob_value, lc.allocated_cost,"
            "       lc.landed_unit_cost from public.landed_cost lc"
            "  join public.grn_lines gl on gl.id = lc.grn_line_id"
            " where lc.org_id = %s order by gl.batch",
            (imp.org,)).fetchall()
        billed = float(c.execute(
            "select sum(amount * fx_rate) from public.shipment_costs"
            " where org_id = %s", (imp.org,)).fetchone()[0])

    fob = {r[0]: float(r[1]) for r in rows}
    alloc = {r[0]: float(r[2]) for r in rows}
    landed = {r[0]: float(r[3]) for r in rows}

    # 56 EUR at 305 is 17,080 a unit.
    assert fob == {"B-1": 1_708_000.0, "B-2": 683_200.0}
    # Billed, allocated and unallocated reconcile.
    assert round(sum(alloc.values()), 2) == round(billed, 2) == 560_000.0
    # By value, so both lots land at the same figure a unit.
    assert landed["B-1"] == landed["B-2"]
    for b in ("B-1", "B-2"):
        assert round(landed[b], 2) == round(
            (fob[b] + alloc[b]) / (100 if b == "B-1" else 40), 2)


def test_the_valuation_uses_the_landed_cost_not_the_invoice(schema):
    """item_cost prefers the landed cost by joining on source_type =
    'grn_line'. A manual receipt never matched that join, so stock that
    had carried freight was valued as though it had not."""
    imp = Importer(schema, "Valued")
    imp.freight(2000, "USD", 280, "value")
    with imp.acting() as cur:
        operations.receive(cur, "RGT-1", "KHI", 100, po="PO-1",
                           shipment="SHP-1", batch="B-1", grn="GRN-900",
                           weight=400, volume=2.5)
    with as_user(schema, imp.who) as c:
        avg = float(c.execute(
            "select avg_unit_cost from public.item_cost where org_id = %s",
            (imp.org,)).fetchone()[0])
    # 17,080 invoice plus 5,600 freight a unit.
    assert round(avg, 2) == 22_680.0
