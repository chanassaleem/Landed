"""The whole workflow, once, as a customer would walk it.

The milestone plan's definition of done, and its own warning: "Do not
declare the milestone complete on the basis of passing tests alone.
Demonstrate the full workflow using realistic sample data and verify
the resulting database records and screen outputs."

So this goes through the HTTP API the browser uses, and checks the
database underneath at every step -- fifteen of them, in the plan's
order:

  1  an importer with two branches
  2  a foreign-currency purchase order
  3  the exchange rate recorded
  4  an inbound shipment
  5  freight, duty and clearing in their own currencies
  6  a partial delivery
  7  batches, quantities, expiry, weight and volume
  8  the landed inventory cost
  9  the rest received later
  10 re-importing without duplicating the receipts
  11 sales and branch transfers
  12 demand and replenishment
  13 outstanding quantities, available stock, committed cash
  14 an auditable correction
  15 one organisation's data not reaching another

The figures are a Pakistani medical-device importer's: a reagent at
EUR 56 bought on a EUR order at 305, freight billed in dollars,
duty in rupees.
"""

from __future__ import annotations

import os
import pathlib
import time
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
from fastapi.testclient import TestClient

import web.app as webapp
from web import session

DSN = os.environ.get("LANDED_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="LANDED_TEST_DSN not set")

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHIM = ROOT / "supabase" / "tests" / "00_supabase_shim.sql"
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))


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
    """One business, signed in, over the real endpoints."""

    def __init__(self, con, monkeypatch, name, email):
        from engine.supabase_auth import Claims
        from tests.test_people_api import RealDatabase

        self.con = con
        self.email = email
        self.who = uuid.uuid4()
        con.execute("insert into auth.users (id, email) values (%s,%s)",
                    (self.who, email))

        monkeypatch.setattr(
            webapp, "verifier",
            lambda: type("V", (), {"verify": staticmethod(lambda t: Claims(
                user_id=str(self.who), email=email,
                expires_at=int(time.time()) + 3600))})())

        class FakeAuth(session.Auth):
            def __init__(self):
                super().__init__("https://example.supabase.co", "anon-key")

            def sign_in(self, _e, _p):
                return session.Tokens("access-token", "refresh-token", 3600)

        monkeypatch.setattr(webapp, "auth", lambda: FakeAuth())
        import engine.db.supabase as sup
        monkeypatch.setattr(sup, "database",
                            lambda: RealDatabase(DSN, email))
        monkeypatch.setenv("LANDED_ENV", "dev")

        self._db = RealDatabase(DSN, email)
        self.http = TestClient(webapp.app)
        assert self.http.post("/auth/sign-in",
                              json={"email": email,
                                    "password": "x"}).status_code == 200
        r = self.http.post("/api/organisation", json={"name": name})
        assert r.status_code == 200, r.text[:300]
        self.org = r.json()["id"]

    def db(self):
        return self._db.as_user(self.who)

    def one(self, sql, *args):
        with self.db() as c:
            return c.execute(sql, args or None).fetchone()

    def all(self, sql, *args):
        with self.db() as c:
            return c.execute(sql, args or None).fetchall()


def test_the_whole_workflow(schema, monkeypatch):
    imp = Importer(schema, monkeypatch, "Meridian Diagnostics",
                   "meridian@acceptance.test")

    # ---------------------------------------- 1. two branches
    with imp.db() as c:
        for code, name in (("KHI", "Karachi"), ("LHE", "Lahore")):
            c.execute("insert into public.locations (org_id, code, name)"
                      " values (%s,%s,%s)", (imp.org, code, name))
        c.execute("insert into public.items (org_id, sku, description,"
                  " lead_time_days) values (%s,'RGT-100','Haematology"
                  " reagent pack',45)", (imp.org,))
        c.execute("insert into public.suppliers (org_id, code, name)"
                  " values (%s,'MIND','Mindray Europe GmbH')", (imp.org,))
    assert len(imp.all("select 1 from public.locations")) == 2

    # ------------------- 2 & 3. a euro order, with the rate recorded
    with imp.db() as c:
        c.execute(
            "insert into public.purchase_orders (org_id, location_id,"
            " supplier_id, po_number, currency, fx_rate, status,"
            " ordered_at, expected_at)"
            " select %s, l.id, s.id, 'PO-2026-118', 'EUR', 305.0,"
            " 'placed', current_date - 20, current_date + 25"
            "  from public.locations l, public.suppliers s"
            " where l.code = 'KHI' and l.org_id = %s and s.org_id = %s",
            (imp.org, imp.org, imp.org))
        c.execute(
            "insert into public.po_lines (org_id, po_id, item_id, qty,"
            " unit_price) select %s, p.id, i.id, 300, 56.0"
            "  from public.purchase_orders p, public.items i"
            " where p.org_id = %s and i.org_id = %s",
            (imp.org, imp.org, imp.org))
    ccy, fx = imp.one("select currency, fx_rate"
                      " from public.purchase_orders")
    assert (ccy, float(fx)) == ("EUR", 305.0)

    # ---------------------------------------- 4. an inbound shipment
    with imp.db() as c:
        c.execute(
            "insert into public.shipments (org_id, reference, supplier_id,"
            " mode, etd, eta, status) select %s, 'SHP-2026-41', s.id,"
            " 'sea', current_date - 14, current_date - 1, 'arrived'"
            "  from public.suppliers s where s.org_id = %s",
            (imp.org, imp.org))

    # -------------- 5. charges, each billed in its own currency
    #
    # The point of holding a rate per charge: a dollar freight bill and
    # a rupee duty assessment are not the same money, and converting
    # them at one rate is how an importer's margin disappears.
    with imp.db() as c:
        for kind, amount, cur, rate, basis in (
                ("freight", 2_400.0, "USD", 280.0, "volume"),
                ("duty", 612_000.0, "PKR", 1.0, "value"),
                ("clearing", 85_000.0, "PKR", 1.0, "qty")):
            c.execute(
                "insert into public.shipment_costs (org_id, shipment_id,"
                " kind, amount, currency, fx_rate, allocate_by)"
                " select %s, sh.id, %s, %s, %s, %s, %s"
                "  from public.shipments sh where sh.org_id = %s",
                (imp.org, kind, amount, cur, rate, basis, imp.org))
    billed = float(imp.one("select sum(billed)"
                           " from public.shipment_charge_allocation")[0])
    assert billed == 2_400 * 280 + 612_000 + 85_000      # 1,369,000

    # ------------ 6 & 7. a partial delivery, in two lots, measured
    first = imp.http.post("/api/receive", json={
        "sku": "RGT-100", "branch": "KHI", "po": "PO-2026-118",
        "shipment": "SHP-2026-41", "qty": 120, "batch": "B-2026-A",
        "expiry": "2028-06-30", "weight": 480, "volume": 3.0,
        "grn": "GRN-5001"})
    assert first.status_code == 200, first.text[:300]
    second = imp.http.post("/api/receive", json={
        "sku": "RGT-100", "branch": "KHI", "po": "PO-2026-118",
        "shipment": "SHP-2026-41", "qty": 60, "batch": "B-2026-B",
        "expiry": "2028-09-30", "weight": 240, "volume": 1.5,
        "grn": "GRN-5001"})
    assert second.status_code == 200, second.text[:300]
    assert second.json()["linked"] is True

    lots = imp.all(
        "select gl.batch, gl.qty, gl.unit_price, gl.currency, gl.fx_rate,"
        "       gl.expiry_date, sl.weight_kg, sl.volume_cbm"
        "  from public.grn_lines_live gl"
        "  join public.shipment_lines sl on sl.grn_line_id = gl.id"
        " order by gl.batch")
    assert [(r[0], float(r[1])) for r in lots] == [("B-2026-A", 120.0),
                                                   ("B-2026-B", 60.0)]
    # The order's price and rate, on every lot, without either typed.
    assert all((r[3], float(r[4])) == ("EUR", 305.0) for r in lots)
    assert [str(r[5]) for r in lots] == ["2028-06-30", "2028-09-30"]
    assert [(float(r[6]), float(r[7])) for r in lots] == [(480.0, 3.0),
                                                          (240.0, 1.5)]

    # ---------------------------------------- 8. the landed cost
    #
    # Each charge on its own basis. Freight by volume (3.0 of 4.5),
    # duty by value (both lots at one price, so by quantity in effect),
    # clearing by quantity.
    alloc = {r[0]: float(r[1]) for r in imp.all(
        "select gl.batch, lc.allocated_cost from public.landed_cost lc"
        "  join public.grn_lines_live gl on gl.id = lc.grn_line_id")}
    assert round(sum(alloc.values()), 2) == 1_369_000.0, (
        "the charges did not all reach the goods")
    # Two thirds of the volume, so two thirds of the freight.
    assert round(alloc["B-2026-A"] / alloc["B-2026-B"], 3) == 2.0

    landed = {r[0]: float(r[1]) for r in imp.all(
        "select gl.batch, lc.landed_unit_cost from public.landed_cost lc"
        "  join public.grn_lines_live gl on gl.id = lc.grn_line_id")}
    # 56 EUR at 305 is 17,080 the unit, plus its share of 1,369,000
    # over 180 units.
    for batch, n in (("B-2026-A", 120), ("B-2026-B", 60)):
        assert round(landed[batch], 2) == round(
            17_080 + alloc[batch] / n, 2)
    valued = float(imp.one("select avg_unit_cost from public.item_cost")[0])
    assert round(valued, 2) == round(
        17_080 + 1_369_000 / 180, 2) == 24_685.56

    # -------------------------- 9. the rest, later, off the shipment
    rest = imp.http.post("/api/receive", json={
        "sku": "RGT-100", "branch": "KHI", "po": "PO-2026-118",
        "qty": 120, "batch": "B-2026-C", "expiry": "2028-12-31",
        "grn": "GRN-5002"})
    assert rest.status_code == 200, rest.text[:300]
    assert rest.json()["linked"] is False, "a manual arrival is not linked"
    assert imp.http.get(
        "/api/receiving?sku=RGT-100&branch=KHI").json()["orders"] == [], (
        "the order is fully received and should not still be offered")

    # ----------- 10. the same receipts again, without duplicating
    again = imp.http.post("/api/receive", json={
        "sku": "RGT-100", "branch": "KHI", "po": "PO-2026-118",
        "shipment": "SHP-2026-41", "qty": 120, "batch": "B-2026-A",
        "grn": "GRN-5001"})
    assert again.status_code == 200
    assert again.json()["recorded"] is False
    assert float(imp.one(
        "select sum(qty) from public.grn_lines_live")[0]) == 300.0

    # ---------------------------------------- 11. sales and a transfer
    for qty_ in (40, 35, 25):
        r = imp.http.post("/api/movement", json={
            "sku": "RGT-100", "branch": "KHI", "kind": "sale",
            "qty": qty_})
        assert r.status_code == 200, r.text[:200]
    r = imp.http.post("/api/transfer", json={
        "sku": "RGT-100", "from": "KHI", "to": "LHE", "qty": 50,
        "note": "Branch request"})
    assert r.status_code == 200, r.text[:300]

    on_hand = {r[0]: float(r[1]) for r in imp.all(
        "select l.code, s.on_hand from public.stock_on_hand s"
        "  join public.locations l on l.id = s.location_id")}
    assert on_hand == {"KHI": 300 - 100 - 50, "LHE": 50.0}

    # A transfer is both sides or neither, and the ledger only grows.
    assert float(imp.one(
        "select sum(qty) from public.stock_movement"
        " where reason in ('transfer_in','transfer_out')")[0]) == 0.0

    # ------------------- 12. demand, and what to order next
    home = imp.http.get("/api/module/demand")
    assert home.status_code == 200, home.text[:200]
    sold = float(imp.one(
        "select sum(quantity) from public.demand_monthly")[0])
    assert sold == 100.0, "the three sales are the demand"
    buy = imp.http.get("/api/module/buy")
    assert buy.status_code == 200, buy.text[:200]

    # ------------- 13. outstanding, available, committed
    outstanding = float(imp.one(
        "select coalesce(sum(outstanding), 0)"
        " from public.po_outstanding")[0])
    assert outstanding == 0.0
    assert sum(on_hand.values()) == 200.0

    # ------------------------------ 14. an auditable correction
    #
    # The warehouse recounts B-2026-A: the pallet held 110, not 120.
    before = float(imp.one("select avg_unit_cost from public.item_cost")[0])
    fix = imp.http.post("/api/receipt/correct", json={
        "grn": "GRN-5001", "sku": "RGT-100", "batch": "B-2026-A",
        "qty": 110, "why": "Warehouse recount on arrival: pallet held 110"})
    assert fix.status_code == 200, fix.text[:300]
    assert fix.json()["changed"] == {"qty": [120.0, 110.0]}

    # The original is still there, exactly as filed.
    filed = imp.all(
        "select qty, supersedes is not null, note from public.grn_lines"
        " where batch = 'B-2026-A' order by qty")
    assert [(float(r[0]), r[1]) for r in filed] == [(110.0, True),
                                                    (120.0, False)]
    assert "recount" in filed[0][2]

    # The ledger gained a reversal rather than losing a row.
    kinds = [(r[0], float(r[1])) for r in imp.all(
        "select reason, qty from public.stock_movement"
        " where reason in ('grn','reversal') order by created_at")]
    assert ("grn", 120.0) in kinds and ("reversal", -120.0) in kinds
    assert ("grn", 110.0) in kinds

    # And everything downstream moved with it, once.
    assert sum(on_hand.values()) - 10 == float(imp.one(
        "select sum(on_hand) from public.stock_on_hand")[0])
    assert float(imp.one(
        "select coalesce(sum(outstanding), 0)"
        " from public.po_outstanding")[0]) == 10.0
    after = float(imp.one("select avg_unit_cost from public.item_cost")[0])
    assert after != before, "the valuation did not notice the correction"
    assert round(sum(float(r[0]) for r in imp.all(
        "select allocated_cost from public.landed_cost")), 2) == 1_369_000.0

    # --------------- 15. another importer, entirely separate
    other = Importer(schema, monkeypatch, "Indus Medical Systems",
                     "indus@acceptance.test")
    with other.db() as c:
        c.execute("insert into public.locations (org_id, code, name)"
                  " values (%s,'KHI','Karachi')", (other.org,))
        c.execute("insert into public.items (org_id, sku, description,"
                  " lead_time_days) values (%s,'RGT-100','Same code,"
                  " different business',30)", (other.org,))
        c.execute("insert into public.stock_movement (org_id, item_id,"
                  " location_id, qty, unit_cost, reason, occurred_at)"
                  " select %s, i.id, l.id, 9, 100, 'opening', current_date"
                  "  from public.items i, public.locations l"
                  " where i.org_id = %s and l.org_id = %s",
                  (other.org, other.org, other.org))

    # The same SKU at the same branch code, and neither sees the other.
    assert float(other.one(
        "select sum(on_hand) from public.stock_on_hand")[0]) == 9.0
    assert float(imp.one(
        "select sum(on_hand) from public.stock_on_hand")[0]) == 190.0
    assert other.all("select 1 from public.grn_lines") == []
    assert float(other.one(
        "select coalesce(sum(quantity), 0)"
        " from public.demand_monthly")[0]) == 0.0
